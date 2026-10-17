"""Privacy-safe usage analytics (story 14.7).

See `app.analytics.models` for the recorded-event and aggregate-result
shapes, `app.analytics.service` for the k-anonymity gate, and
`app.analytics.tracker_audit` for the no-third-party-tracker check.
`app.analytics.router` mounts `POST /v1/analytics/events` and
`GET /v1/analytics/aggregate`.
"""

from __future__ import annotations

from app.analytics.models import AggregateResult, RecordedEvent
from app.analytics.router import router
from app.analytics.service import K_ANONYMITY_THRESHOLD, AnalyticsService

__all__ = [
    "K_ANONYMITY_THRESHOLD",
    "AggregateResult",
    "AnalyticsService",
    "RecordedEvent",
    "router",
]
