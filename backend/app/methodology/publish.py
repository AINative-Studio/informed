"""Publishing a methodology version (story 6.1).

Why this is a third module beside two that already read this table
==================================================================

`app.profiles.methodology` (story 4.2) reads the current version's tier
weights and seeds the first row. `app.methodology.service` (story 12.2) reads
a version — current, superseded, or the whole changelog — for the public page.
Both are readers. Neither has a writer beyond 4.2's one-time seed, and that
seed is deliberately not a publication: see `publish_new_version`'s own note
on why the distinction matters.

This module is the writer. It exists separately rather than as a method on
either reader because publishing is a different operation with different
consequences: it moves `is_current`, and it emits datamodel.md's
`methodology.published` event whose documented consumer is "Full recompute +
symmetry audit". A read must never be able to trigger that by accident, and
keeping the writer in its own module with its own error type is how that stays
true by construction rather than by care.

What story 6.1 actually closed
==============================

Three gaps, all of which 4.2 and 12.2 named out loud and left open:

1. **`evidence_blend` and `vote_types` had no numbers anywhere.** The live
   `2026.1` row carried `weights.tiers` and nothing else; 12.2's public page
   correctly reported the other two documented groups in
   `weights_not_yet_published`, and `docs/methodology-changelog.md` explained
   why. This module gives them real defaults, sourced below.
2. **"Exactly one current version exists" was a property of a response, not
   an invariant of the table.** 12.2 pinned that the changelog lists exactly
   one current version; nothing stopped a torn write from leaving two. The
   invariant now lives in `app.integrity.checks` beside the identical one for
   `verdicts` and `candidate_positions` (story 16.2's check 3), and
   `publish_new_version` is the only writer that can move the flag.
3. **`methodology.published` was never emitted by anything.** The live project
   had zero of them. Without the event, datamodel.md's documented recompute
   never fires, and every stored score keeps silently citing a methodology
   that is no longer in force — the exact failure this story's acceptance
   criterion exists to prevent.

Append-only, and what that means for a table with one row per version
=====================================================================

datamodel.md's rule: "Mark the live row with `is_current`; never update
history in place." `methodology_versions` differs from `verdicts` in that its
id *is* its version key (`2026.1`), so there is no `:v{n}` suffix to append
and `Repository.append_version` does not apply. The equivalent move here is:

  1. insert the new version's row with `is_current = True`, then
  2. clear `is_current` on every previously-current row, through
     `ZeroDBClient.update_row`, which read-merge-writes.

Step 2 is a flag flip and nothing else. The superseded row's `weights`,
`published_at` and `changelog_url` are never touched — that is precisely the
history this table exists to keep, and `docs/methodology-changelog.md` promises
a reader that a verdict scored under `2026.1` can still show the weights it was
actually computed with.

New row first, for the same reason `Repository.append_version` writes in that
order: these are two requests and are not atomic. Torn after step 1 leaves two
current rows, which the integrity check reports and the next publish repairs
(it clears *every* previously-current row, not just the newest). Torn before
step 1 leaves the old version in force, which is simply "the publish did not
happen". The reverse order would risk a window with no current version at all,
and `MethodologyVersionService.current_version()` would then seed a default
row over the top of a real history.

Event last, after both writes, so a consumer that reacts by recomputing every
score cannot read a half-moved `is_current`.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from app.methodology.models import DOCUMENTED_WEIGHT_GROUPS, VERSION_ID_PATTERN
from app.profiles.methodology import (
    DEFAULT_TIER_WEIGHTS,
    TABLE_NAME,
    MethodologyVersion,
)
from app.zerodb.client import ZeroDBClient

#: datamodel.md's Events table: `methodology.published`, payload `version`,
#: consumers "Full recompute + symmetry audit".
METHODOLOGY_PUBLISHED_TOPIC = "methodology.published"

#: prd.md, Feature 3: "Evidence blend for incumbents — votes 70%,
#: sponsorships 15%, stated positions 15% by default; the voter can switch to
#: 'votes only'." Not invented here; these are prd.md's published numbers.
#:
#: Stored as fractions of 1.0 rather than as the percentages prd.md writes,
#: because a scorer multiplies by them directly: `0.7 * votes_position + ...`
#: needs no division by 100, and a weight that must be rescaled before use is
#: a weight someone will eventually forget to rescale. They sum to exactly
#: 1.0, which `test_the_evidence_blend_is_a_complete_split` pins — a blend
#: that did not sum to 1 would silently scale every incumbent's position up or
#: down relative to a challenger's, and prd.md's nonpartisan-by-construction
#: rule means the two must be on the same scale.
#:
#: `statements` rather than prd.md's prose "stated positions": it is the key a
#: scorer writes, and `candidate_positions`' evidence counts use the same
#: short noun. The 15% is the same number either way.
DEFAULT_EVIDENCE_BLEND: dict[str, float] = {
    "votes": 0.7,
    "sponsorships": 0.15,
    "statements": 0.15,
}

#: Relative weight per `vote_type`, keyed by datamodel.md's exact enum:
#: "`vote_type` takes `final_passage`, `amendment`, `cloture`,
#: `motion_to_recommit`, `procedural` or `other`".
#:
#: THIS STORY IS THE FIRST PLACE THESE ARE REAL NUMBERS. prd.md gives the
#: ordering but no figures, so the figures are a judgment call and are
#: documented as such here and in `docs/methodology-changelog.md`. What prd.md
#: actually constrains (Feature 2, "Context rules (to stay fair)"):
#:
#:   * "Procedural and cloture votes are shown but weighted lower than final
#:     passage, with a plain-language explainer."
#:   * Backlog 6.2's acceptance criterion: "Final passage outweighs procedural
#:     and bundled votes."
#:
#: The scheme, and the reasoning for each number:
#:
#:   * `final_passage` **1.0** — the anchor. A vote on final passage is the
#:     clearest evidence of a member's position on a bill, so it is the unit
#:     every other type is a fraction of. Anchoring at 1.0 rather than at some
#:     other figure keeps the scorer's arithmetic readable: a member with only
#:     final-passage votes is scored at full confidence.
#:   * `amendment` **0.7** — a substantive vote on the actual text, and often
#:     the most revealing vote on a specific commitment (prd.md's "Partial"
#:     verdict is defined by exactly this case: "supported the bill but voted
#:     for an amendment that weakened it"). Below final passage because an
#:     amendment is a vote on one provision rather than on the whole measure.
#:   * `motion_to_recommit` **0.5** — the minority's substantive last attempt
#:     to change a bill, so it carries real policy content, but it is voted
#:     close to party lines often enough that it is weaker evidence of an
#:     individual position than an amendment vote.
#:   * `cloture` **0.4** — about whether to proceed, not about the policy. A
#:     member can vote for cloture and against passage, which prd.md's own BDD
#:     scenario names as a case that must NOT read as a broken promise ("Given
#:     B voted Nay on a cloture motion but Yea on final passage of the same
#:     bill / Then the verdict is 'Kept' with a 'procedural vote' note").
#:     Weighted above bare `procedural` because a cloture vote is at least
#:     about a specific measure.
#:   * `procedural` **0.2** — rules, previous question, adjournment. Almost
#:     purely a floor-management vote. Non-zero rather than zero deliberately:
#:     prd.md says these are "shown but weighted lower", not excluded, and a
#:     zero weight would make them invisible to the scorer while still being
#:     displayed, which is the one outcome that would let the page show a vote
#:     it did not count.
#:   * `other` **0.2** — the fallback for anything the classifier could not
#:     type. Weighted as low as `procedural` because an unclassified vote is
#:     not evidence that a scorer should lean on, and the ingest's classifier
#:     (`app.ingest.house_rollcalls`) assigns `other` precisely when it is
#:     unsure.
#:
#: Every value is in `(0, 1]`, so each is a usable multiplier and none can
#: silently erase a vote. `is_bundled` is NOT in this map: datamodel.md models
#: it as a separate boolean on the roll call, so an omnibus discount composes
#: with the vote-type weight rather than needing six more entries. Its own
#: factor belongs to story 6.2, which owns the scorer that applies it.
DEFAULT_VOTE_TYPE_WEIGHTS: dict[str, float] = {
    "final_passage": 1.0,
    "amendment": 0.7,
    "motion_to_recommit": 0.5,
    "cloture": 0.4,
    "procedural": 0.2,
    "other": 0.2,
}


def default_weights() -> dict[str, dict[str, float]]:
    """All three documented weight groups, with prd.md's defaults.

    Returns fresh copies so a caller that edits one group cannot mutate the
    module constants every other caller reads.
    """
    return {
        "tiers": dict(DEFAULT_TIER_WEIGHTS),
        "evidence_blend": dict(DEFAULT_EVIDENCE_BLEND),
        "vote_types": dict(DEFAULT_VOTE_TYPE_WEIGHTS),
    }


#: How many rows one supersede scan asks for. `methodology_versions` grows by
#: roughly one row per publication, so this is many years of headroom;
#: `_clear_previous_current` warns rather than silently truncating.
PAGE_SIZE = 500

#: Nothing voter-derived is reachable from this module, so this logger cannot
#: leak political-opinion data. It carries version ids and row counts only.
logger = logging.getLogger(__name__)

_VERSION_ID = re.compile(VERSION_ID_PATTERN)


class MethodologyPublishError(RuntimeError):
    """A publication was refused before anything was written.

    Its own type rather than `ValueError` so a caller can tell "this publish
    was rejected on its inputs, nothing happened" apart from a ZeroDB failure
    midway through, which is a genuinely different situation: the first needs
    a corrected request, the second needs the integrity check run.
    """


class MethodologyPublisher:
    """Publishes a new `methodology_versions` row and announces it.

    Holds no per-request state; safe to share like every other service here.
    """

    def __init__(self, client: ZeroDBClient) -> None:
        self._client = client

    @property
    def client(self) -> ZeroDBClient:
        """The underlying client, for callers that need to read what was
        written (verification scripts and step definitions)."""
        return self._client

    def publish_new_version(
        self,
        version: str,
        *,
        weights: dict[str, dict[str, float]] | None = None,
        changelog_url: str | None = None,
        published_at: str | None = None,
    ) -> MethodologyVersion:
        """Publish `version` as the methodology in force, and emit the event.

        Not the same operation as story 4.2's seed, and the difference is the
        event. A seed happens when a fresh environment reads a table that has
        no row yet; it establishes the baseline rather than changing it, so
        there is nothing for `methodology.published`'s "full recompute"
        consumer to recompute — no score was ever computed under a previous
        version. A publication changes the methodology in force, which makes
        every stored score stale. `test_seeding_a_default_version_is_not_a_
        publication` pins that a seed emits nothing.

        Args:
            version: the new version id, matching datamodel.md's `2026.1`
                pattern.
            weights: all three documented groups. Defaults to `default_weights()`
                — prd.md's published figures; see the module constants.
            changelog_url: where this version's plain-language entry is
                published. Defaults to the anchor for this version in
                `docs/methodology-changelog.md`.
            published_at: ISO-8601 UTC. Defaults to now. Explicit only so a
                verification script can publish a dated version reproducibly.

        Returns:
            The row as written.

        Raises:
            MethodologyPublishError: the id does not match the documented
                pattern, the weights are missing a documented group, or this
                version is already the one in force. Nothing is written and no
                event is emitted in any of those cases — refusing before the
                first write is what keeps a bad request from leaving the table
                with two current rows.
        """
        if not _VERSION_ID.match(version):
            raise MethodologyPublishError(
                f"{version!r} is not a methodology version id; datamodel.md's pattern is "
                f"{{year}}.{{sequence}}, e.g. '2026.1'"
            )

        resolved_weights = default_weights() if weights is None else weights
        missing = [group for group in DOCUMENTED_WEIGHT_GROUPS if not resolved_weights.get(group)]
        if missing:
            # Refused rather than filled in: datamodel.md documents three
            # groups, and a version published without one would be a
            # methodology with a hole in it that the public page would then
            # report as "not yet published" on a version that claims to be in
            # force. Story 4.2's seed is the one legitimate partial row, and
            # it is a seed, not a publication.
            raise MethodologyPublishError(
                f"a published methodology must carry every documented weight group; "
                f"missing or empty: {', '.join(missing)}"
            )

        existing_current = self._current_rows()
        if any(row.get("id") == version for row in existing_current):
            # Idempotence here would be a lie: a second publish of the id
            # already in force would emit a second `methodology.published`
            # and trigger a second full recompute for no change. Refused so
            # the caller bumps the version instead, which is what a real
            # methodology change is (see docs/methodology-changelog.md,
            # "Changing this page").
            raise MethodologyPublishError(
                f"methodology version {version!r} is already the version in force; "
                f"a methodology change is a NEW version id, never a republish"
            )

        row = MethodologyVersion(
            id=version,
            published_at=published_at or _now(),
            weights=resolved_weights,
            changelog_url=changelog_url or _changelog_url_for(version),
            is_current=True,
        )

        # 1. The new row first — see the module docstring on why this order.
        self._client.upsert_row(TABLE_NAME, row.id, row.model_dump(mode="json"))
        # 2. Clear the flag on every previously-current row, so a table that
        #    somehow already had two current rows is repaired by this publish
        #    rather than left with one stale flag set for good — the same
        #    reasoning as `Repository.append_version`.
        self._clear_previous_current(keep=row.id)
        # 3. Announce it, after both writes, so a consumer that recomputes
        #    every score cannot read a half-moved `is_current`.
        self._client.create_event(
            topic=METHODOLOGY_PUBLISHED_TOPIC,
            # datamodel.md's payload for this event is `version` and nothing
            # else. Kept to exactly that: an event is the one place in this
            # codebase that fans out to consumers we do not control, so it
            # carries the minimum a consumer needs to read the row itself.
            payload={"version": row.id},
        )
        logger.info("published methodology version %s", row.id)
        return row

    def _current_rows(self) -> list[dict[str, object]]:
        """Every row marked current, read through the client's own paging.

        An unfiltered read plus a local filter, deliberately: live ZeroDB's
        server-side `/query` filter matches nothing (story 1.4), and this is
        the read that decides whether a flag needs clearing. A filter that
        silently matched nothing here would make every publish look like the
        first one and leave the old version marked current forever.
        """
        page = self._client.query_rows(TABLE_NAME, limit=PAGE_SIZE)
        if page.has_more or len(page.rows) >= PAGE_SIZE:
            logger.warning(
                "methodology_versions returned a full page of %d row(s); a previously "
                "current version may not have been superseded",
                len(page.rows),
            )
        return [row.row_data for row in page.rows if row.row_data.get("is_current") is True]

    def _clear_previous_current(self, *, keep: str) -> None:
        for row in self._current_rows():
            row_id = row.get("id")
            if not isinstance(row_id, str) or row_id == keep:
                continue
            # `update_row` read-merge-writes, so the superseded version's own
            # weights, published_at and changelog_url survive. A bare
            # `PUT {"is_current": false}` would replace the row wholesale and
            # destroy the history this table exists to keep (confirmed live in
            # story 1.2, and the same hazard `Repository.append_version`
            # documents).
            self._client.update_row(TABLE_NAME, row_id, {"is_current": False})
            logger.info("superseded methodology version %s", row_id)


def _changelog_url_for(version: str) -> str:
    """This version's anchor in the repository's published changelog.

    Points at `docs/methodology-changelog.md` for the reason story 12.2 chose
    it over a product domain that is not stood up: the changelog is
    version-controlled, publicly readable, and its own history is auditable.
    The anchor shape matches GitHub's heading slugs, which is why the version's
    dot is dropped rather than replaced.
    """
    slug = version.replace(".", "")
    return (
        "https://github.com/AINative-Studio/informed/blob/main/docs/"
        f"methodology-changelog.md#{slug}"
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "DEFAULT_EVIDENCE_BLEND",
    "DEFAULT_VOTE_TYPE_WEIGHTS",
    "METHODOLOGY_PUBLISHED_TOPIC",
    "MethodologyPublishError",
    "MethodologyPublisher",
    "default_weights",
]
