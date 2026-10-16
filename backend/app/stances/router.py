"""HTTP surface for `GET /v1/topics/{topic_id}/stances` (story 4.1).

Sits under `/v1/topics` rather than `/v1/stances`, because a stance card only
ever makes sense in the context of the one subtopic it belongs to (story 3.2:
"no stored stance references a top-level topic") — there is no scenario where
a client wants a flat, cross-topic page of stance cards. Thin, as every router
in this codebase is: validate the path parameter, call `StanceService`, map
its answer onto the wire shape in `app.stances.models`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.stances.models import StanceOption, StanceOptionsResponse
from app.stances.service import StanceService
from app.zerodb import ZeroDBClient

router = APIRouter(prefix="/v1", tags=["stances"])


def zerodb_client_for_stances() -> ZeroDBClient:
    """The ZeroDB client for this request. See `app.topics.router`'s sibling
    function for why this is not shared with the ballot router's.
    """
    return ZeroDBClient.from_env()


def stance_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_stances)],
) -> StanceService:
    return StanceService(client)


@router.get(
    "/topics/{topic_id}/stances",
    response_model=StanceOptionsResponse,
    summary="Balanced stance cards for one subtopic, freshly randomized",
    description=(
        "Every stance card for one subtopic, in a fresh random order on "
        "every call (story 3.2). None is pre-selected, and none is marked "
        "as a default or recommendation — ui-prd.md 2.2 requires the "
        "client to hold this order stable for the rest of one onboarding "
        "session rather than re-fetching or re-sorting it.\n\n"
        "An unknown or top-level `topic_id` returns an empty `options` "
        "list rather than a 404: `StanceService` makes no claim about "
        "whether an id is a real subtopic, only about what cards exist "
        "for it, and both answers are 'none, as far as this read is "
        "concerned'."
    ),
)
def get_stances_for_topic(
    service: Annotated[StanceService, Depends(stance_service)],
    topic_id: Annotated[
        str,
        Path(description='A subtopic id, e.g. "en-US:prescription-drug-pricing:v1"'),
    ],
) -> StanceOptionsResponse:
    cards = service.for_topic(topic_id)
    return StanceOptionsResponse(
        topic_id=topic_id,
        options=[
            StanceOption(id=str(card.id), label=card.label, description=card.description)
            for card in cards
        ],
    )


__all__ = ["get_stances_for_topic", "router", "stance_service", "zerodb_client_for_stances"]
