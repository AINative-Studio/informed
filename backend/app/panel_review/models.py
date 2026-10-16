"""Wire shapes for the panel review endpoints (story 3.3).

Separate models from `app.repositories.models.PanelReview`, the stored row, for
the reason `app.profiles.router`'s response models are separate from
`InterestProfile`: a client should drift against the response type, not against
the domain model, so adding a field to the ledger row does not silently change
the API contract.

These are reviewer-facing shapes, not voter-facing ones. Nothing here carries a
voter, a profile or a score — the whole surface is editorial wording and who
judged it, which is public by design (prd.md: "wording changes are versioned
and public").
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.repositories.models import ReviewDecision, ReviewSubjectTable

#: What a reviewer may decide through the API. A narrower set than the stored
#: `ReviewDecision` vocabulary, on purpose:
#:
#:   * `proposed` is written by `POST .../proposals`, never by a decision
#:     call — a caller who could POST a `proposed` "decision" could store a
#:     proposal with no wording in it;
#:   * `grandfathered` is written by the backfill CLI and by nothing else.
#:     Exposing it over HTTP would let any caller mark unreviewed wording
#:     publishable, which is precisely the gate this story installs;
#:   * `withdrawn` has its own endpoint, because only an author may withdraw
#:     and the authorisation rule differs from a reviewer's.
#:
#: So the API's decision vocabulary is the two real judgements and nothing
#: else. The stored vocabulary stays wider because the ledger has to be able
#: to record states the API does not let a caller invent.
ApiDecision = Literal["approved", "rejected"]


class ReviewDecisionRequest(BaseModel):
    """The body of a decision about wording already live.

    `reviewer_id` is an authentication stand-in, exactly as `user_id` is in
    `app.profiles.router` — no auth layer exists anywhere in this codebase yet.
    It is required rather than optional, unlike `user_id`: an anonymous voter
    is a real and supported case, while an anonymous panel review is a
    contradiction. A decision nobody is named for cannot be a cross-partisan
    review of anything.
    """

    model_config = ConfigDict(extra="forbid")

    reviewer_id: str = Field(
        min_length=1,
        description=(
            "Who is deciding. An authentication stand-in until a real auth "
            "story lands: it is recorded, not verified, so this ledger proves "
            "what was claimed rather than who claimed it."
        ),
    )
    subject_table: ReviewSubjectTable = Field(
        description="Which reviewable table the row is in: issue_topics or stances."
    )
    subject_id: str = Field(
        min_length=1, description="The row being judged, by its deterministic id."
    )
    subject_version: int = Field(
        ge=1,
        description=(
            "The wording_version (stance) or taxonomy_version (topic) the "
            "reviewer actually read. Checked against the stored row and "
            "refused on a mismatch: an approval applies only to the wording "
            "it was given."
        ),
    )
    decision: ApiDecision = Field(description="approved or rejected.")
    reason: str | None = Field(
        default=None,
        description=(
            "Why. Required when rejecting — a rejection the author cannot act "
            "on is not a review — and optional when approving."
        ),
    )


class ProposalRequest(BaseModel):
    """The body of a proposed wording change.

    Only the fields that change need passing. The rest are read off the stored
    row and recorded as the previous wording, which is what lets the history
    show what changed without re-deriving it from a seeder constant.
    """

    model_config = ConfigDict(extra="forbid")

    author_id: str = Field(
        min_length=1, description="Who is proposing the change. An auth stand-in; see above."
    )
    subject_table: ReviewSubjectTable = Field(description="issue_topics or stances.")
    subject_id: str = Field(min_length=1, description="The row whose wording is being changed.")
    label: str | None = Field(
        default=None, description="New stance label. Stances only — a topic has a name."
    )
    description: str | None = Field(
        default=None, description="New stance description. Stances only."
    )
    name: str | None = Field(
        default=None, description="New topic name. Topics only — a stance has a label."
    )


class ProposalDecisionRequest(BaseModel):
    """The body of a decision about a proposed change."""

    model_config = ConfigDict(extra="forbid")

    reviewer_id: str = Field(
        min_length=1,
        description=(
            "Who is deciding. Refused if it matches the proposal's author: "
            "prd.md requires a cross-partisan review, and wording passed by "
            "its own author has had no review at all."
        ),
    )
    decision: ApiDecision = Field(description="approved or rejected.")
    reason: str | None = Field(default=None, description="Required when rejecting.")


class ReviewRecord(BaseModel):
    """One ledger entry as the API returns it.

    `previous_*`/`proposed_*` are returned as a pair so a client can render
    "before → after" for an approved edit without holding any state of its own
    — which is what "the review history is visible" means in practice.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    subject_table: ReviewSubjectTable
    subject_id: str
    subject_version: int
    decision: ReviewDecision
    reviewer_id: str
    decided_at: str
    reason: str | None = None
    proposal_id: str | None = None
    proposed_label: str | None = None
    proposed_description: str | None = None
    proposed_name: str | None = None
    previous_label: str | None = None
    previous_description: str | None = None
    previous_name: str | None = None


class ReviewHistoryResponse(BaseModel):
    """`GET /v1/panel/reviews/{subject_table}/{subject_id}`.

    `status` is what the ledger says now; `history` is everything that ever
    happened. Both, because a reviewer needs to know the current state and how
    it got there, and deriving the first from the second client-side would mean
    re-implementing `_in_force`'s version and tie-breaking rules in TypeScript.
    """

    model_config = ConfigDict(extra="forbid")

    subject_table: ReviewSubjectTable
    subject_id: str
    subject_version: int
    status: ReviewDecision | None = Field(
        default=None,
        description=(
            "The in-force decision for the version the row currently carries, "
            "or null when nothing has been decided about it. Null and "
            "'rejected' both mean the wording is not published; 'grandfathered' "
            "means it IS served and has still never been panel-reviewed."
        ),
    )
    is_panel_approved: bool = Field(
        description=(
            "Whether a person approved this wording. False for a "
            "grandfathered row — the pre-existing corpus is served without "
            "ever having been through a panel, and this field is where that "
            "is visible rather than buried in a migration note."
        )
    )
    history: list[ReviewRecord]


class QueueItem(BaseModel):
    """One row awaiting a real panel review."""

    model_config = ConfigDict(extra="forbid")

    subject_table: ReviewSubjectTable
    subject_id: str
    subject_version: int
    current_decision: ReviewDecision | None = Field(
        default=None,
        description=(
            "'grandfathered' for the pre-existing corpus, 'rejected' for "
            "wording a reviewer refused, null for wording nobody has read. "
            "The last is the most urgent: it is also invisible to voters."
        ),
    )
    wording: str = Field(description="The stance label or topic name under review.")


class ReviewQueueResponse(BaseModel):
    """`GET /v1/panel/queue` — the standing review debt, made countable."""

    model_config = ConfigDict(extra="forbid")

    awaiting_review: int = Field(
        description="How many rows no panel member has approved at their current version."
    )
    grandfathered: int = Field(
        description=(
            "How many of those are the corpus stories 3.1 and 3.2 published "
            "before this workflow existed. Reported separately because it is a "
            "known, deliberate, honest gap rather than ordinary backlog."
        )
    )
    never_reviewed: int = Field(
        description="How many have no ledger entry at all — new or edited wording."
    )
    items: list[QueueItem]


class ProposalRecord(BaseModel):
    """One proposed wording change as the API returns it."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    subject_table: ReviewSubjectTable
    subject_id: str
    subject_version: int
    author_id: str
    proposed_at: str
    label: str | None = None
    description: str | None = None
    name: str | None = None
    previous_label: str | None = None
    previous_description: str | None = None
    previous_name: str | None = None
    decided: bool = Field(description="Whether any reviewer has decided on it yet.")


class ProposalListResponse(BaseModel):
    """`GET /v1/panel/proposals` — proposals awaiting review, oldest first."""

    model_config = ConfigDict(extra="forbid")

    proposals: list[ProposalRecord]


class SubjectStatusResponse(BaseModel):
    """What a decision or proposal-decision call returns.

    The row's resulting state rather than the ledger entry just written: a
    caller who just approved an edit wants to know the wording and version the
    row now carries, which is the thing that changed.
    """

    model_config = ConfigDict(extra="forbid")

    subject_table: ReviewSubjectTable
    subject_id: str
    subject_version: int
    status: ReviewDecision | None = None
    is_panel_approved: bool
    is_published: bool = Field(
        description=(
            "Whether a gated read will now serve this row. True for approved "
            "and for grandfathered wording; false for rejected and for "
            "anything nobody has decided on."
        )
    )
    label: str | None = None
    description: str | None = None
    name: str | None = None


__all__ = [
    "ApiDecision",
    "ProposalDecisionRequest",
    "ProposalListResponse",
    "ProposalRecord",
    "ProposalRequest",
    "QueueItem",
    "ReviewDecisionRequest",
    "ReviewHistoryResponse",
    "ReviewQueueResponse",
    "ReviewRecord",
    "SubjectStatusResponse",
]
