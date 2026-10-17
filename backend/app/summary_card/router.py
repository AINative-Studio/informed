"""HTTP surface for `GET /v1/candidates/{candidacy_id}/summary-card`
(story 14.6).

Thin, in the same shape as `app.ballot.router`: validate the path, call
`SummaryCardService`, map its one exception to a status code.

Why this endpoint takes no profile_id/user_id parameter at all
==================================================================

Story 14.6's acceptance criterion is "the card contains candidate data
only; a test asserts no profile fields leak". The strongest version of
that guarantee is architectural: this handler's signature has exactly one
input, the path parameter `candidacy_id`, and no optional query parameter,
header dependency or body for a voter's identity. That means the response
is byte-for-byte identical regardless of who calls it or what they pass —
confirmed by `tests/step_defs/test_summary_card_steps.py`'s scenario that
tries smuggling a profile through query params and a header and asserts
the response is unaffected, and by a schema check against the generated
OpenAPI document that no such parameter is even declared.

This is deliberately NOT "the endpoint accepts a profile_id and ignores
it" — accepting and ignoring still means someone reading the signature
has to trust that the ignoring is real and will stay real. Not accepting
it at all makes "no personalization is possible here" checkable by
reading the function's parameter list.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.summary_card.models import CandidateSummaryCard
from app.summary_card.service import CandidacyNotFoundError, SummaryCardService
from app.zerodb import ZeroDBClient

router = APIRouter(prefix="/v1", tags=["summary-card"])


def zerodb_client_for_summary_card() -> ZeroDBClient:
    """The ZeroDB client for this request.

    A separate dependency function from `app.ballot.router.zerodb_client`,
    for the same reason `app.topics.router.zerodb_client_for_topics`
    documents: `dependency_overrides` keys on the function object, so a test
    overriding this router's client must not have to know any other router
    shares the same construction.
    """
    return ZeroDBClient.from_env()


def summary_card_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_summary_card)],
) -> SummaryCardService:
    return SummaryCardService(client=client)


@router.get(
    "/candidates/{candidacy_id}/summary-card",
    response_model=CandidateSummaryCard,
    summary="A shareable candidate summary, candidate data only",
    description=(
        "A summary of one candidacy, built entirely from public candidate "
        "data, that a voter can share to discuss a candidate without "
        "revealing anything about their own interests (story 14.6).\n\n"
        "**Identical for every caller.** This endpoint takes no profile, "
        "user or voter identity of any kind — not as a query parameter, not "
        "as a header, not in a body. The response for a given "
        "`candidacy_id` is the same JSON no matter who asks, which is what "
        "makes 'no voter data can leak through this path' a fact about the "
        "endpoint's shape rather than a policy to audit.\n\n"
        "**Fields not yet computed are `null`, never invented.** "
        "`verdict_counts`, `say_do_gap` and `positions_summary` depend on "
        "epics 6, 7 and 11 (candidate positions, verdicts, say/do scoring), "
        "none of which are built yet — they come back `null` rather than a "
        "fabricated zero or summary."
    ),
    responses={404: {"description": "No candidacy with that id is known"}},
)
def get_summary_card(
    service: Annotated[SummaryCardService, Depends(summary_card_service)],
    candidacy_id: str,
) -> CandidateSummaryCard:
    try:
        return service.card_for(candidacy_id)
    except CandidacyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


__all__ = ["get_summary_card", "router", "summary_card_service", "zerodb_client_for_summary_card"]
