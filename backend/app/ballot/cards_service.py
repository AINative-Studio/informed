"""Assembling `GET /v1/ballot/cards`'s response from story 7.2's read model
(story 7.1).

Two reads, both bounded to one race's candidate count (prd.md: 6-15
candidates per race), and neither an aggregation over `candidate_positions`,
`verdicts` or `member_votes`:

  1. `CandidateCardReader.cards_for_race` — story 7.2's own one-table read.
  2. `candidacies` for the same (election, district), filtered the same way,
     so `is_incumbent` can be labeled. `candidate_cards` does not carry that
     field (see `app.ballot.models.CandidateCardView`'s docstring), and
     datamodel.md's own query for `candidacies` is "By (election, district) →
     ballot view" — the identical filter this story's own endpoint takes, so
     this is the query datamodel.md already names rather than a new one
     invented for this story.

The `confidence_badge` on each card is computed from data already stored on
the card (`positions_summary[].evidence_count`), through the same pure
`app.scoring.badges.badge_for` story 6.5 built — not a read of any source
table. The live methodology's `badge_thresholds` are read once per request,
the same shape `AlignmentService.alignment_for` already uses.
"""

from __future__ import annotations

import logging

from app.ballot.cards import CandidateCardReader
from app.ballot.models import BallotCardsResponse, CandidateCardView
from app.profiles.methodology import MethodologyVersionService
from app.repositories import Repository
from app.repositories.models import Candidacy, CandidateCard
from app.scoring.badges import BADGE_THRESHOLDS_GROUP, badge_for
from app.zerodb import ZeroDBClient

logger = logging.getLogger(__name__)

#: Candidacies read per race. Matches `app.ballot.cards.RACE_PAGE_SIZE`: a
#: race has 6-15 candidates (prd.md), so this is headroom, not a budget.
RACE_PAGE_SIZE = 500


class BallotCardsService:
    """Builds one race's `BallotCardsResponse` from `candidate_cards` and
    `candidacies`.

    Holds no per-request state; safe to share across requests like every
    other `*Service` in this codebase.
    """

    def __init__(self, client: ZeroDBClient) -> None:
        self._reader = CandidateCardReader(client)
        self._candidacies: Repository[Candidacy] = Repository(client=client, model=Candidacy)
        self._methodology = MethodologyVersionService(client)

    def cards_for_race(self, *, election_id: str, district_id: str) -> BallotCardsResponse:
        """Every built card for one race, labeled with incumbency and a
        confidence badge.

        A race with no cards yet returns an empty list rather than an error —
        an honest "nothing computed here yet", the same posture
        `BallotService.ballot_for` takes for an empty delegation.
        """
        cards = self._reader.cards_for_race(election_id=election_id, district_id=district_id)
        incumbency = self._incumbency_for(election_id=election_id, district_id=district_id)
        thresholds = self._methodology.current_version().weights.get(BADGE_THRESHOLDS_GROUP, {})

        views = [
            _view_from(
                card,
                is_incumbent=incumbency.get(card.candidacy_id, False),
                thresholds=thresholds,
            )
            for card in cards
        ]
        return BallotCardsResponse(election_id=election_id, district_id=district_id, cards=views)

    def _incumbency_for(self, *, election_id: str, district_id: str) -> dict[str, bool]:
        """`candidacy_id` -> `is_incumbent` for this race.

        Filtered locally as well as server-side, like every other read in
        this codebase since story 1.4: a candidacy from the wrong race
        leaking into this map would mislabel a candidate's incumbency on a
        ballot that is not theirs.
        """
        rows = self._candidacies.query(
            filter={"election_id": election_id, "district_id": district_id},
            limit=RACE_PAGE_SIZE,
        )
        matched = [
            row for row in rows if row.election_id == election_id and row.district_id == district_id
        ]
        if len(rows) >= RACE_PAGE_SIZE:
            logger.error(
                "candidacies returned a full page of %d row(s) for (%s, %s); an "
                "incumbency flag may be missing from this ballot",
                len(rows),
                election_id,
                district_id,
            )
        return {row.id: row.is_incumbent for row in matched if row.id}


def _view_from(
    card: CandidateCard, *, is_incumbent: bool, thresholds: dict[str, float]
) -> CandidateCardView:
    """One stored `CandidateCard` plus this request's incumbency and badge."""
    evidence_count = sum(entry.evidence_count for entry in card.positions_summary or [])
    return CandidateCardView(
        candidacy_id=card.candidacy_id,
        person_id=card.person_id,
        person_name=card.person_name,
        party=card.party,
        district=card.district,
        photo_url=card.photo_url,
        is_incumbent=is_incumbent,
        verdict_counts=card.verdict_counts,
        say_do_gap=card.say_do_gap,
        positions_summary=card.positions_summary,
        confidence_badge=badge_for(evidence_count, thresholds),
        methodology_version=card.methodology_version,
        updated_at=card.updated_at,
    )


__all__ = ["RACE_PAGE_SIZE", "BallotCardsService"]
