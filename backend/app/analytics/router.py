"""HTTP surface for story 14.7's privacy-safe analytics.

    POST /v1/analytics/events     — record one coarse usage event
    GET  /v1/analytics/aggregate  — the k-anonymous count for one slice

Both endpoints are thin wrappers over `AnalyticsService`, in the same shape
as every other router in this codebase. Neither accepts anything shaped
like a voter identity — see `AnalyticsService`'s module docstring — and
`RecordEventRequest` forbids extra fields for the same structural reason
`app.summary_card.models.CandidateSummaryCard` does: a caller cannot attach
a `user_id` even by trying, because the request type has no field for one.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.analytics.models import AggregateResult, RecordedEvent
from app.analytics.service import AnalyticsService
from app.zerodb import ZeroDBClient

router = APIRouter(prefix="/v1/analytics", tags=["analytics"])


def zerodb_client_for_analytics() -> ZeroDBClient:
    """The ZeroDB client for this request.

    A separate dependency function from every other router's own
    `zerodb_client_for_*`, for the reason each of them documents:
    `dependency_overrides` keys on the function object, so one router's test
    override must not implicate another's.
    """
    return ZeroDBClient.from_env()


def analytics_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_analytics)],
) -> AnalyticsService:
    return AnalyticsService(client=client)


class RecordEventRequest(BaseModel):
    """The body of `POST /v1/analytics/events`.

    `event_type` and an optional coarse `bucket` — nothing else. There is no
    `user_id`/`ip`/`address` field for a client to fill in, which is what
    makes "this cannot carry identity" true of the wire format itself.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(
        min_length=1, description='e.g. "onboarding_completed", "ballot_viewed"'
    )
    bucket: str | None = Field(
        default=None,
        description=(
            "A coarse, non-identifying label, e.g. a two-letter state code. "
            "Never a precise location, a device id or anything else that "
            "narrows to an individual."
        ),
    )


@router.post(
    "/events",
    response_model=RecordedEvent,
    summary="Record one coarse usage event (story 14.7)",
    description=(
        "Publishes a usage event through datamodel.md's events primitive. "
        "Carries only an event type, a timestamp assigned by ZeroDB, and an "
        "optional coarse bucket — never a user id, an IP address or an "
        "address of any kind. There is no field in this request for any of "
        "those, so a client cannot send one even by mistake."
    ),
)
def record_event(
    service: Annotated[AnalyticsService, Depends(analytics_service)],
    body: Annotated[RecordEventRequest, Body()],
) -> RecordedEvent:
    return service.record(body.event_type, bucket=body.bucket)


@router.get(
    "/aggregate",
    response_model=AggregateResult,
    summary="A k-anonymous aggregate count (story 14.7)",
    description=(
        "The count of recorded events for one (event_type, bucket) slice — "
        "but only once at least 50 events exist for that exact slice "
        "(k-anonymity). Below that threshold the response says the data is "
        "insufficient and `count` is always null, never the real small "
        "number: a rare event in a small bucket is itself re-identifying."
    ),
)
def get_aggregate(
    service: Annotated[AnalyticsService, Depends(analytics_service)],
    event_type: str,
    bucket: str | None = None,
) -> AggregateResult:
    return service.aggregate(event_type, bucket=bucket)


__all__ = [
    "RecordEventRequest",
    "analytics_service",
    "get_aggregate",
    "record_event",
    "router",
    "zerodb_client_for_analytics",
]
