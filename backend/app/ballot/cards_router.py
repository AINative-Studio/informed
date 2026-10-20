"""HTTP surface for `GET /v1/ballot/cards?election_id=&district_id=` (story 7.1).

Thin, in the same shape as every other router in this codebase: validate the
query, call `BallotCardsService`, return its response model. There is no
error path that needs mapping to a status code beyond FastAPI's own 422 for a
missing query parameter — an unknown race is not a 404 here, because "no
candidacy in ZeroDB has that (election, district) yet" and "every candidacy in
that race has no card yet" are indistinguishable from this endpoint's own read
and both describe an honestly empty ballot rather than an error a voter caused
(the same posture `BallotService.ballot_for` takes for an empty delegation).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.ballot.cards_service import BallotCardsService
from app.ballot.models import BallotCardsResponse
from app.zerodb import ZeroDBClient

router = APIRouter(prefix="/v1", tags=["ballot-cards"])


def zerodb_client_for_ballot_cards() -> ZeroDBClient:
    """The ZeroDB client for this request.

    Its own dependency function, not shared with `app.ballot.router`'s, for
    the reason every other router's own copy documents:
    `dependency_overrides` keys on the function object, so a test overriding
    this router's client must not have to know which other router happens to
    share one.
    """
    return ZeroDBClient.from_env()


def ballot_cards_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_ballot_cards)],
) -> BallotCardsService:
    return BallotCardsService(client)


@router.get(
    "/ballot/cards",
    response_model=BallotCardsResponse,
    summary="One race's candidates, each with their card",
    description=(
        "Every candidate with a built card for one race, labeled with "
        "incumbency and a confidence badge (story 7.1).\n\n"
        "**Never aggregates `candidate_positions`, `verdicts` or "
        "`member_votes` at request time** (datamodel.md rule 4, story 7.2). "
        "This endpoint's own work is two bounded reads sized to one race's "
        "candidate count — `candidate_cards` and `candidacies` — plus one "
        "read of the live methodology version for the confidence-badge "
        "thresholds (story 6.5), which a test counts to hold this true "
        "rather than only asserting a latency.\n\n"
        "**No personalized score.** `candidate_cards` holds no voter "
        "reference (datamodel.md), so this endpoint cannot carry one either "
        "— a per-voter alignment score is "
        "`GET /v1/candidates/{id}/alignment?profile_id=`'s job (story 6.4). "
        "`say_do_gap` is the one alignment-shaped number shown here, and it "
        "is the same number for every voter.\n\n"
        "**Ballot order, never ranked.** Candidates come back in the stable "
        "name order `candidate_cards` is queried in — never sorted by score, "
        "per prd.md's 'the app never recommends who to vote for'.\n\n"
        "**A candidacy with no card yet is absent, not a placeholder.** No "
        "`positions.recomputed` or `verdict.published` event has touched it, "
        "so there is nothing honest to show; a fabricated all-null row would "
        "claim a candidate exists in a race this endpoint has not actually "
        "checked for them."
    ),
)
def get_ballot_cards(
    service: Annotated[BallotCardsService, Depends(ballot_cards_service)],
    election_id: Annotated[
        str, Query(min_length=1, description="`elections` row id this race belongs to")
    ],
    district_id: Annotated[
        str, Query(min_length=1, description="OCD-ID of the seat these cards are for")
    ],
) -> BallotCardsResponse:
    return service.cards_for_race(election_id=election_id, district_id=district_id)


__all__ = ["ballot_cards_service", "get_ballot_cards", "router", "zerodb_client_for_ballot_cards"]
