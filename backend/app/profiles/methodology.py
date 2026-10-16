"""Tier weights, read live from `methodology_versions` (story 4.2).

Why this exists
================

prd.md's Feature 1 tier table gives three weights — primary 3.0, secondary
1.5, nice_to_have 0.5 — and datamodel.md repeats, twice, that they must never
be hard-coded: "The tier sets the weight... The live values come from
`methodology_versions` and are never hard-coded." Story 14.1's `Interest`
model already obeys half of that rule (it carries no `weight` field at all —
see `app.profiles.models`'s module docstring and
`test_an_interest_carries_no_weight_field`). This module is the other half:
somewhere has to actually hold the number 3.0, and datamodel.md says that
somewhere is a `methodology_versions` row, not a Python constant buried in a
scorer.

Scope line against story 6.1
=============================

Story 6.1 ("Methodology versions", backlog.md, iteration 4) is a full
governance feature: publishing a NEW methodology, writing its changelog URL,
and re-scoring every candidate position under it (datamodel.md's
`positions.recomputed` event). None of that exists yet, and none of it is
built here. What story 4.2 actually needs — "weights load from
`methodology_versions`" as its own acceptance criterion — is narrower: at
least one current row has to exist, and the tier-editing UI has to read its
numbers from that row rather than from a constant in this codebase.

The honest, scope-appropriate choice made here: `DEFAULT_TIER_WEIGHTS` is
still a Python constant (prd.md's own published table — this is not invented
data), but it is used only to SEED the first `methodology_versions` row when a
live project has none yet, exactly as `app.seed.Seeder` seeds `us-federal` so
later stories have something to reference (see that module's docstring for
the identical shape of argument). Every *read* after that first seed goes
through `current_tier_weights()`, which always queries the live table. A
project already carrying a published `methodology_versions` row (story 6.1,
once built) is read as-is and this module's constant is never consulted
again — `test_an_existing_current_row_is_read_live_rather_than_reseeded`
pins exactly that.

Why seed rather than require an operator to do it by hand: every other story
that needs a row to exist before anything else can work (2.1's jurisdiction
seed) ships its own seed rather than documenting a manual setup step, and a
missing `methodology_versions` row would otherwise make tier-editing
silently un-weighted on a fresh environment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.zerodb.client import ZeroDBClient

#: prd.md, Feature 1 — Interest Profile's published tier table. Not invented
#: here: this is the same table CLAUDE.md and this story's own brief quote
#: verbatim. Used only as the seed value for the first-ever
#: `methodology_versions` row (see module docstring); every other read of a
#: tier weight goes through a live row.
DEFAULT_TIER_WEIGHTS: dict[str, float] = {
    "primary": 3.0,
    "secondary": 1.5,
    "nice_to_have": 0.5,
}

#: The id this module seeds. A real version id, not a placeholder — if
#: story 6.1 later publishes under this same id (unlikely; it would normally
#: bump to `2027.1` or similar) that is an ordinary re-publish, handled the
#: same as any other `is_current` move.
DEFAULT_METHODOLOGY_VERSION = "2026.1"

TABLE_NAME = "methodology_versions"


class MethodologyVersion(BaseModel):
    """One `methodology_versions` row, datamodel.md's documented fields.

    Deliberately minimal — just enough to expose the tier weights and the
    version id this story needs. Story 6.1 owns the full row shape (it may
    add `evidence_blend`, `vote_types` weights beyond `tiers`, audit-run
    cross-references, etc.); nothing here forbids extra keys inside
    `weights` because `extra` is not forbidden on that nested dict, only on
    this model's own declared fields.
    """

    model_config = ConfigDict(extra="forbid")

    table_name: ClassVar[str] = TABLE_NAME

    id: str = Field(min_length=1)
    published_at: str
    #: `{"tiers": {"primary": 3.0, ...}, ...}` — a dict rather than a typed
    #: sub-model, because story 6.1 may add sibling keys (`evidence_blend`,
    #: `vote_types`) this story has no acceptance criterion for and should
    #: not have to pre-guess the shape of.
    weights: dict[str, dict[str, float]]
    changelog_url: str
    is_current: bool = True

    @property
    def tier_weights(self) -> dict[str, float]:
        return self.weights.get("tiers", {})


class MethodologyVersionService:
    """Reads the current methodology's tier weights, seeding one if needed.

    Holds no per-request state; safe to share across requests like every
    other `*Service` in this codebase.
    """

    def __init__(self, client: ZeroDBClient) -> None:
        self._client = client

    def current_tier_weights(self) -> dict[str, float]:
        """The live tier -> weight mapping. Never a hard-coded constant from
        the caller's point of view — see module docstring.
        """
        return self.current_version().tier_weights

    def current_version(self) -> MethodologyVersion:
        """The current `methodology_versions` row, seeding the default when
        the live table holds none yet.

        Reads `is_current = True` first (the same shape every other
        append-only table in this codebase uses — see
        `Repository.current_for`); a project with exactly the seeded row
        answers from that row, and a project where story 6.1 has since
        published a newer one answers from THAT row instead, with no change
        to this method.
        """
        existing = self._current_row()
        if existing is not None:
            return existing
        return self._seed_default()

    def _current_row(self) -> MethodologyVersion | None:
        page = self._client.query_rows(TABLE_NAME, filter={"is_current": True}, limit=1)
        if not page.rows:
            return None
        return MethodologyVersion.model_validate(page.rows[0].row_data)

    def _seed_default(self) -> MethodologyVersion:
        """Write the one default row, idempotently.

        `upsert_row` (story 1.2) finds-then-writes by id, so calling this
        twice in a race leaves one row rather than two — the same guarantee
        `app.seed.Seeder` relies on for its own permanent rows.
        """
        version = MethodologyVersion(
            id=DEFAULT_METHODOLOGY_VERSION,
            published_at=_now(),
            weights={"tiers": dict(DEFAULT_TIER_WEIGHTS)},
            changelog_url=f"https://informed.example/methodology/{DEFAULT_METHODOLOGY_VERSION}",
            is_current=True,
        )
        self._client.upsert_row(TABLE_NAME, version.id, version.model_dump(mode="json"))
        return version


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "DEFAULT_METHODOLOGY_VERSION",
    "DEFAULT_TIER_WEIGHTS",
    "TABLE_NAME",
    "MethodologyVersion",
    "MethodologyVersionService",
]
