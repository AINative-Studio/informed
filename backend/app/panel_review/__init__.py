"""The cross-partisan advisory panel's wording review workflow (story 3.3).

prd.md: "Stance cards and issue taxonomy reviewed by a cross-partisan advisory
panel before each release; wording changes are versioned and public."

Module map:
    service.py — the ledger, the gate and the propose/approve edit path
    models.py  — the reviewer-facing wire shapes
    router.py  — the six `/v1/panel` endpoints

See `service.py`'s module docstring for the design: why review state is its
own append-only table rather than a field on `issue_topics`/`stances`, why the
existing 80 topics and 201 stances are marked `grandfathered` rather than
`approved`, and why the gated reads are a separate path from the voter-facing
ones.
"""

from app.panel_review.models import (
    ProposalDecisionRequest,
    ProposalListResponse,
    ProposalRecord,
    ProposalRequest,
    QueueItem,
    ReviewDecisionRequest,
    ReviewHistoryResponse,
    ReviewQueueResponse,
    ReviewRecord,
    SubjectStatusResponse,
)
from app.panel_review.router import panel_review_service, router
from app.panel_review.service import (
    BACKFILL_REVIEWER_ID,
    PUBLISHABLE_DECISIONS,
    EmptyProposalError,
    GrandfatherReport,
    PanelReviewError,
    PanelReviewService,
    QueueEntry,
    ReviewStatus,
    SelfReviewError,
    UnknownProposalError,
    UnknownSubjectError,
    UnreviewableWordingError,
    VersionMismatchError,
    WordingProposal,
    review_id,
)

__all__ = [
    "BACKFILL_REVIEWER_ID",
    "PUBLISHABLE_DECISIONS",
    "EmptyProposalError",
    "GrandfatherReport",
    "PanelReviewError",
    "PanelReviewService",
    "ProposalDecisionRequest",
    "ProposalListResponse",
    "ProposalRecord",
    "ProposalRequest",
    "QueueEntry",
    "QueueItem",
    "ReviewDecisionRequest",
    "ReviewHistoryResponse",
    "ReviewQueueResponse",
    "ReviewRecord",
    "ReviewStatus",
    "SelfReviewError",
    "SubjectStatusResponse",
    "UnknownProposalError",
    "UnknownSubjectError",
    "UnreviewableWordingError",
    "VersionMismatchError",
    "WordingProposal",
    "panel_review_service",
    "review_id",
    "router",
]
