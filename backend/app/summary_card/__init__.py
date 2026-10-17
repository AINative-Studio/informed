"""Shareable candidate summary card (story 14.6).

See `app.summary_card.models` for the response shape and
`app.summary_card.service` for how it is built. `app.summary_card.router`
mounts `GET /v1/candidates/{candidacy_id}/summary-card`.
"""

from __future__ import annotations

from app.summary_card.models import CandidateSummaryCard
from app.summary_card.router import router
from app.summary_card.service import CandidacyNotFoundError, SummaryCardService

__all__ = [
    "CandidacyNotFoundError",
    "CandidateSummaryCard",
    "SummaryCardService",
    "router",
]
