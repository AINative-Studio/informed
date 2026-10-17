"""Builds a shareable candidate summary card from live candidate data
(story 14.6).

Why a "minimal" card, and from what data
==========================================

datamodel.md's `candidate_cards` is documented as a read model rebuilt from
events (`verdict.published`, `positions.recomputed`). Neither event exists
yet — epics 6, 7 and 11, which compute positions and verdicts, have not
shipped. Rather than block this story on three unbuilt epics, or on the
events-driven rebuild machinery datamodel.md rule 4 describes, this service
builds the card **directly from the one table that already carries real
candidate data**: `candidacies` (story 2.6), joined in code to `districts`
for a human-readable seat name. That is the same "read the live table
directly, move to the read model once it exists" posture
`app.ballot.service` already takes for the same reason (see that module's
docstring on why it reads `people` directly rather than waiting for
`candidate_cards`).

`verdict_counts`, `say_do_gap` and `positions_summary` are left `None` for
every candidacy today — honestly absent, not fabricated. See
`app.summary_card.models.CandidateSummaryCard`'s docstring for why `None`
and not `0`.

Why this service cannot leak a profile field, structurally
============================================================

`card_for` takes exactly one argument: a candidacy id. There is no second
parameter for a profile, user or voter id — checked directly against the
method's signature by
`tests/test_summary_card_unit.py::test_card_for_takes_only_a_candidacy_id`,
so a later edit that adds one fails a test immediately rather than quietly
opening a leak path. The response type it returns,
`CandidateSummaryCard`, independently forbids extra fields and declares none
that resemble identity — so even a caller who reaches in by hand has no field
to write a profile value into. Two independent guarantees, not one guarded by
the other.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.repositories.base import Repository
from app.repositories.models import Candidacy, District
from app.summary_card.models import CandidateSummaryCard
from app.zerodb.client import ZeroDBClient


class CandidacyNotFoundError(Exception):
    """No `candidacies` row exists with the given id."""


class SummaryCardService:
    """Reads a `candidacies` row and shapes it into a shareable summary card."""

    def __init__(self, *, client: ZeroDBClient) -> None:
        self._candidacies: Repository[Candidacy] = Repository(client=client, model=Candidacy)
        self._districts: Repository[District] = Repository(client=client, model=District)

    def card_for(self, candidacy_id: str) -> CandidateSummaryCard:
        """The summary card for one candidacy.

        The **only** input is the candidacy id — see the module docstring for
        why that is the structural half of "no profile field can leak here".

        Raises:
            CandidacyNotFoundError: no `candidacies` row has this id. Distinct
                from the empty-ballot case in `app.ballot.service`: a card is
                requested for a specific, named id, so "not found" is an
                honest 404 rather than a legitimately empty answer.
        """
        candidacy = self._candidacies.get(candidacy_id)
        if candidacy is None:
            raise CandidacyNotFoundError(
                f"no candidacy {candidacy_id!r} is known; it may not have been ingested yet"
            )

        district_name = self._district_name(candidacy.district_id)

        return CandidateSummaryCard(
            candidacy_id=candidacy_id,
            person_name=candidacy.person_name,
            party=candidacy.party,
            district=district_name,
            photo_url=None,  # `candidacies` carries no photo; `people.photo_url` is a future join.
            verdict_counts=None,
            say_do_gap=None,
            positions_summary=None,
            updated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def _district_name(self, district_id: str) -> str:
        """A human-readable district name, falling back to the raw id.

        Falling back rather than raising: a candidacy whose district row is
        somehow missing (it should never be, since `Candidacy.district_id`
        is a declared reference checked at write time — datamodel.md rule 5)
        still has a card worth sharing, and the raw OCD-ID is a legible,
        honest answer rather than a 500 for a cosmetic field.
        """
        district = self._districts.get(district_id)
        return district.name if district is not None else district_id


__all__ = ["CandidacyNotFoundError", "SummaryCardService"]
