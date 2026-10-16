"""Public corrections: anyone reports an error, and it gets triaged (12.4).

prd.md, "Trust, neutrality, privacy and safety": "Public correction form on
every claim, with an SLA (72 hours to triage)."

Module map:
    models.py    — the stored `corrections` row and the public wire shapes;
                   argues the status vocabulary datamodel.md leaves open and
                   the anonymous tracking-token scheme
    service.py   — submit, status-by-token, triage, and the "past SLA" query
    retention.py — the 90-day `contact_email` erasure sweep
    router.py    — `POST /v1/corrections`, `GET /v1/corrections/{token}`

The two operations that are public are the two that are mounted: submitting a
report and reading your own report's status. Triage (`set_status`) is a service
method with no HTTP surface in this story, because there is no authentication
in this codebase and an unauthenticated status write would let anyone bury an
inconvenient correction. See `service.py`'s module docstring.
"""

from app.corrections.models import (
    CLOSED_STATUSES,
    INITIAL_STATUS,
    TABLE_NAME,
    CorrectionReceipt,
    CorrectionRequest,
    CorrectionRow,
    CorrectionStatus,
)
from app.corrections.retention import (
    RETENTION_DAYS,
    SweepResult,
    sweep_expired_contact_emails,
)
from app.corrections.router import (
    correction_service,
    router,
    zerodb_client_for_corrections,
)
from app.corrections.service import (
    SLA_HOURS,
    CorrectionService,
    UnknownCorrectionError,
)

__all__ = [
    "CLOSED_STATUSES",
    "INITIAL_STATUS",
    "RETENTION_DAYS",
    "SLA_HOURS",
    "TABLE_NAME",
    "CorrectionReceipt",
    "CorrectionRequest",
    "CorrectionRow",
    "CorrectionService",
    "CorrectionStatus",
    "SweepResult",
    "UnknownCorrectionError",
    "correction_service",
    "router",
    "sweep_expired_contact_emails",
    "zerodb_client_for_corrections",
]
