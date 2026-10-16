"""Reading stance cards back for the interest-profile screen (story 3.2, 4.1).

Module map:
    service.py — the per-request shuffle over the `stances` table (3.2)
    models.py  — the voter-facing wire shape for one stance card (4.1)
    router.py  — `GET /v1/topics/{topic_id}/stances` (4.1)
"""

from app.stances.models import StanceOption, StanceOptionsResponse
from app.stances.router import get_stances_for_topic, router, stance_service
from app.stances.service import StanceService

__all__ = [
    "StanceOption",
    "StanceOptionsResponse",
    "StanceService",
    "get_stances_for_topic",
    "router",
    "stance_service",
]
