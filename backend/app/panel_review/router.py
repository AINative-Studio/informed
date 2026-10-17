"""HTTP surface for the panel review workflow (story 3.3).

Six endpoints under `/v1/panel`, all reviewer-facing:

    GET  /v1/panel/queue                              what still owes a review
    GET  /v1/panel/proposals                          proposals awaiting a reviewer
    GET  /v1/panel/reviews/{subject_table}/{subject_id}   one row's status + history
    POST /v1/panel/reviews                            decide on live wording
    POST /v1/panel/proposals                          propose a wording change
    POST /v1/panel/proposals/{proposal_id}/decision   decide on a proposed change

Thin, like every router here: validate the body, call `PanelReviewService`, map
its domain refusals to status codes. Every rule — the version check, the
self-review ban, the neutrality guards, the gate itself — lives in the service.

Why these are not under `/v1/topics` or `/v1/stances`
=====================================================

Those prefixes are the voter-facing taxonomy and stance-card reads (story 4.1),
and a reviewer-facing write surface mounted inside them would make the two
indistinguishable to anything that routes on path — a rate limiter, a CDN rule,
an auth middleware when one arrives, prd.md's own "rate limits and bot
protection on public endpoints". `/v1/panel` is one prefix to protect once
real authentication exists, and until then it is one prefix to point at in the
open question about who may call it.

**No authentication, and that is the known gap.** `reviewer_id` is a body
field, recorded and not verified, exactly as `user_id` is in
`app.profiles.router` — see that module's docstring and this service's. The
difference in stakes is worth stating plainly: a forged `user_id` lets a caller
read or overwrite one voter's own profile, while a forged `reviewer_id` on
`POST /v1/panel/proposals/{id}/decision` lets a caller publish wording to
every voter. Nothing in this story closes that, and nothing in this story
pretends to: these endpoints are usable by whoever can reach the API, so this
prefix must not be exposed publicly before an auth story lands. It is flagged
in the PR as the first thing a reviewer should push back on, and the mitigation
available today is deployment-level (do not route `/v1/panel` from the public
edge) rather than in code.

Why the voter-facing endpoints still read the ungated path
===========================================================

`GET /v1/topics` and `GET /v1/topics/{id}/stances` are deliberately unchanged
by this story and still serve everything stored. The gate is reachable and
proven — it is the `GET /v1/panel/published/*` pair below, which returns the
same wire shapes as those two voter-facing endpoints precisely so that
switching them over later is a dependency swap rather than a rewrite. Doing
that swap now, while the only approvals in the ledger are grandfathered ones,
would be a change with no behavioural difference and a real risk: any
environment whose backfill had not run would serve an empty taxonomy and no
stance cards. See `app.panel_review.service`'s module docstring for the full
argument, and this story's PR for the open question.

**Story 12.5 revisited this question and chose not to switch it, for a
reason sharper than "the only approvals are grandfathered".** The real blocker
is operational, not editorial: nothing in this codebase orchestrates
"seed the corpus, THEN run the grandfathering backfill" as one guaranteed
step. `scripts/bootstrap.py` does not call
`scripts/backfill_panel_review.py`, and no deploy workflow does either — the
backfill is a separate, manually-run script. Today, with the ungated path
live, a freshly seeded environment serves voters correctly the moment
`seed_taxonomy.py`/`seed_stances.py` finish; switching to the gated path
would make that same environment serve an EMPTY taxonomy until a human
remembers to run a second, unenforced script, with nothing in CI or at
deploy time to catch the gap before a voter does. Story 12.5's own release
gate (`app.panel_review.gate.check_panel_gate`, registered as the nightly
`run_panel_gate_check` job once ZeroDB credentials exist — see
`scripts/check_panel_gate.py`'s module docstring) would EVENTUALLY catch a
missed backfill and fail loudly, but "caught within a day, nightly" is not
the same guarantee as "the voter-facing app was never dark in the first
place" — and the second is what an un-backfilled fresh environment would
risk today if this switch were made. Making the switch safe is therefore an
operational-hardening story of its own (teach `bootstrap.py` or a deploy
workflow to run the backfill immediately after seeding, so the two can never
drift apart), not something a CI/release-gate story should decide as a side
effect. Until that ordering is enforced somewhere, this stays unswitched.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status

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
from app.panel_review.service import (
    BACKFILL_REVIEWER_ID,
    EmptyProposalError,
    MissingReasonError,
    PanelReviewService,
    ReviewStatus,
    SelfReviewError,
    UnknownProposalError,
    UnknownSubjectError,
    UnreviewableWordingError,
    VersionMismatchError,
    WordingProposal,
)
from app.repositories.models import IssueTopic, PanelReview, ReviewSubjectTable, Stance
from app.seed_taxonomy import TAXONOMY_LOCALE, TAXONOMY_VERSION
from app.stances.models import StanceOption, StanceOptionsResponse
from app.topics.models import TopicTreeResponse
from app.zerodb import ZeroDBClient

router = APIRouter(prefix="/v1/panel", tags=["panel review"])


def zerodb_client_for_panel_review() -> ZeroDBClient:
    """The ZeroDB client for this request.

    Its own dependency function rather than a shared one, for the reason
    `app.topics.router` documents: FastAPI keys `dependency_overrides` on the
    function object, so a test overriding one router's client must not have to
    know which other routers share it.
    """
    return ZeroDBClient.from_env()


def panel_review_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_panel_review)],
) -> PanelReviewService:
    return PanelReviewService(client)


# --- reads -----------------------------------------------------------------


@router.get(
    "/queue",
    response_model=ReviewQueueResponse,
    summary="Every row awaiting a real panel review",
    description=(
        "The standing review debt. `grandfathered` counts the taxonomy and "
        "stance corpus stories 3.1 and 3.2 published before this workflow "
        "existed — served, never panel-reviewed, and deliberately reported "
        "rather than quietly marked approved. `never_reviewed` counts wording "
        "with no ledger entry at all, which is the urgent half: it is also "
        "withheld from every gated read."
    ),
)
def get_review_queue(
    service: Annotated[PanelReviewService, Depends(panel_review_service)],
) -> ReviewQueueResponse:
    entries = service.queue()
    return ReviewQueueResponse(
        awaiting_review=len(entries),
        grandfathered=sum(1 for entry in entries if entry.current_decision == "grandfathered"),
        never_reviewed=sum(1 for entry in entries if entry.current_decision is None),
        items=[
            QueueItem(
                subject_table=entry.subject_table,
                subject_id=entry.subject_id,
                subject_version=entry.subject_version,
                current_decision=entry.current_decision,
                wording=entry.wording,
            )
            for entry in entries
        ],
    )


@router.get(
    "/proposals",
    response_model=ProposalListResponse,
    summary="Wording changes proposed and not yet decided, oldest first",
    description=(
        "Oldest first, because this is a work queue and the wording that has "
        "been waiting longest is the next thing to read. A proposal never "
        "changes the live row: the wording a voter sees is whatever was last "
        "approved, until a reviewer other than the author approves the change."
    ),
)
def get_proposals(
    service: Annotated[PanelReviewService, Depends(panel_review_service)],
) -> ProposalListResponse:
    return ProposalListResponse(
        proposals=[_proposal_record(proposal) for proposal in service.proposals_awaiting_review()]
    )


@router.get(
    "/reviews/{subject_table}/{subject_id}",
    response_model=ReviewHistoryResponse,
    summary="One row's review status and its whole history",
    description=(
        "Story 3.3's third acceptance criterion. `history` is every decision "
        "ever recorded about this row — proposals, withdrawals, rejections "
        "with their reasons, the grandfathering mark and every approval — "
        "newest first, because nothing in the ledger is ever edited or "
        "deleted (prd.md: wording changes are versioned and public).\n\n"
        "An unreviewed row returns an empty history and a null status rather "
        "than a 404: 'nothing has happened to this yet' is a real answer. A "
        "row id that does not exist at all IS a 404."
    ),
    responses={404: {"description": "No such topic or stance"}},
)
def get_review_history(
    service: Annotated[PanelReviewService, Depends(panel_review_service)],
    subject_table: Annotated[ReviewSubjectTable, Path(description="issue_topics or stances")],
    subject_id: Annotated[
        str, Path(description='e.g. "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"')
    ],
) -> ReviewHistoryResponse:
    try:
        current = service.status_for(subject_table=subject_table, subject_id=subject_id)
    except UnknownSubjectError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    history = service.history_for(subject_table=subject_table, subject_id=subject_id)
    return ReviewHistoryResponse(
        subject_table=subject_table,
        subject_id=subject_id,
        subject_version=current.subject_version,
        status=current.decision,
        is_panel_approved=current.is_panel_approved,
        history=[_review_record(row) for row in history],
    )


@router.get(
    "/published/topics",
    response_model=TopicTreeResponse,
    summary="The topic tree with unreviewed wording withheld",
    description=(
        "The gated read: a topic appears only if the ledger carries an "
        "in-force approval or grandfathering for the exact taxonomy_version "
        "the row is at. A top-level topic whose own wording is unreviewed is "
        "dropped with its subtopics, because a subtopic with no rendered "
        "parent has nowhere to appear in a picker.\n\n"
        "`GET /v1/topics` is the ungated voter-facing read and is unchanged by "
        "this story; see this router's module docstring for why switching it "
        "over is a separate decision."
    ),
)
def get_published_topics(
    service: Annotated[PanelReviewService, Depends(panel_review_service)],
    locale: Annotated[str, Query(description='Taxonomy locale, e.g. "en-US".')] = TAXONOMY_LOCALE,
    taxonomy_version: Annotated[
        int, Query(ge=1, description="Published taxonomy_version to read.")
    ] = TAXONOMY_VERSION,
) -> TopicTreeResponse:
    return service.approved_topic_tree(locale=locale, taxonomy_version=taxonomy_version)


@router.get(
    "/published/topics/{topic_id}/stances",
    response_model=StanceOptionsResponse,
    summary="Only the stance cards the panel has let through, for one subtopic",
    description=(
        "The gated read for stance cards. A card appears only if the ledger "
        "carries an in-force approval or grandfathering for the exact "
        "wording_version the row is at — so a newly written card, a card "
        "whose wording a reviewer rejected, and a card whose version has been "
        "bumped past its approval are all withheld.\n\n"
        "**Not shuffled**, unlike `GET /v1/topics/{topic_id}/stances`. Story "
        "3.2's randomization is a property of the voter-facing read; a "
        "reviewer table needs a stable order, and a voter-facing caller that "
        "moves onto this path shuffles what it gets back."
    ),
)
def get_published_stances(
    service: Annotated[PanelReviewService, Depends(panel_review_service)],
    topic_id: Annotated[str, Path(description='e.g. "en-US:prescription-drug-pricing:v1"')],
) -> StanceOptionsResponse:
    cards = service.approved_stances_for_topic(topic_id)
    return StanceOptionsResponse(
        topic_id=topic_id,
        options=[
            StanceOption(id=str(card.id), label=card.label, description=card.description)
            for card in cards
        ],
    )


# --- writes ----------------------------------------------------------------


@router.post(
    "/reviews",
    response_model=SubjectStatusResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Decide on wording that is already stored",
    description=(
        "Records a panel decision about the wording a row currently carries. "
        "Writes only the ledger — a decision here never changes a word of the "
        "row. To change wording, propose it (`POST /v1/panel/proposals`) and "
        "have a different reviewer approve the proposal, which is the only "
        "path that edits a live row and the only path that bumps its version."
    ),
    responses={
        404: {"description": "No such topic or stance"},
        409: {"description": "The row is not at the version the decision names"},
        422: {"description": "A rejection with no reason, or a malformed body"},
    },
)
def post_review_decision(
    service: Annotated[PanelReviewService, Depends(panel_review_service)],
    body: Annotated[ReviewDecisionRequest, Body()],
) -> SubjectStatusResponse:
    try:
        service.record_decision(
            subject_table=body.subject_table,
            subject_id=body.subject_id,
            subject_version=body.subject_version,
            reviewer_id=body.reviewer_id,
            decision=body.decision,
            reason=body.reason,
        )
    except UnknownSubjectError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except VersionMismatchError as exc:
        # 409 rather than 422: the body is well-formed and was valid when the
        # reviewer loaded the wording. The wording moved underneath them, which
        # is a conflict with stored state, and a client should re-read and
        # re-decide rather than correct its request.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (MissingReasonError, ValueError) as exc:
        # A rejection must say why. `MissingReasonError` is the service's own
        # refusal; the bare `ValueError` is `PanelReview`'s model validator,
        # kept as a backstop in case a rule is added there and not here.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _status_response(service, body.subject_table, body.subject_id)


@router.post(
    "/proposals",
    response_model=ProposalRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Propose new wording for a topic or stance",
    description=(
        "Stores the proposed wording in the review ledger and leaves the live "
        "row untouched, so nothing unreviewed can reach a voter. Pass only "
        "the fields that change: `label` and/or `description` for a stance, "
        "`name` for a topic. The wording being replaced is read off the stored "
        "row and recorded alongside, which is what makes the history able to "
        "show what changed.\n\n"
        "Proposed wording is checked against stories 3.1's and 3.2's own "
        "neutrality guards before it is stored — a topic name may not "
        "presuppose a stance, a stance label may not name a political side — "
        "so a reviewer's time is not spent on wording a word list can reject."
    ),
    responses={
        404: {"description": "No such topic or stance"},
        422: {
            "description": (
                "The proposal changes nothing, is blank, is addressed to the "
                "wrong kind of row, or fails a neutrality guard"
            )
        },
    },
)
def post_proposal(
    service: Annotated[PanelReviewService, Depends(panel_review_service)],
    body: Annotated[ProposalRequest, Body()],
) -> ProposalRecord:
    try:
        proposal = service.propose(
            subject_table=body.subject_table,
            subject_id=body.subject_id,
            author_id=body.author_id,
            label=body.label,
            description=body.description,
            name=body.name,
        )
    except UnknownSubjectError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (EmptyProposalError, UnreviewableWordingError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _proposal_record(proposal)


@router.post(
    "/proposals/{proposal_id}/decision",
    response_model=SubjectStatusResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Approve or reject a proposed wording change",
    description=(
        "The only path in this API that edits live editorial wording. "
        "Approving applies the proposed wording to the row and bumps its "
        "`wording_version` (a stance) or `taxonomy_version` (a topic) in the "
        "same call — story 3.3's second acceptance criterion, made one "
        "operation so that a bump cannot happen without a review and a review "
        "cannot happen without a bump. **The row's id does not change**, "
        "because a voter's saved profile stores `stance_id` and re-wording a "
        "card under a new id would orphan their choice.\n\n"
        "Rejecting records the decision and its reason and leaves the live "
        "wording and version exactly as they were.\n\n"
        "A reviewer may not approve their own proposal: prd.md requires a "
        "cross-partisan review, and wording passed by its own author has had "
        "no review at all."
    ),
    responses={
        403: {"description": "The reviewer is the proposal's own author"},
        404: {"description": "No such proposal"},
        409: {"description": "The row has moved since the proposal was written"},
        422: {"description": "A rejection with no reason, or a malformed body"},
    },
)
def post_proposal_decision(
    service: Annotated[PanelReviewService, Depends(panel_review_service)],
    proposal_id: Annotated[str, Path(description="The proposal's id, as returned when created")],
    body: Annotated[ProposalDecisionRequest, Body()],
) -> SubjectStatusResponse:
    try:
        result = service.decide(
            proposal_id=proposal_id,
            reviewer_id=body.reviewer_id,
            decision=body.decision,
            reason=body.reason,
        )
    except UnknownProposalError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SelfReviewError as exc:
        # 403, not 422: the request is well-formed and the server understood it
        # perfectly. It is refused because of who is asking, which is what 403
        # means — and saying so plainly matters here, since this is the rule
        # that makes the review cross-partisan rather than ceremonial.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except VersionMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (MissingReasonError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _status_response(service, result.subject_table, result.subject_id)


# --- mapping helpers -------------------------------------------------------


def _review_record(row: PanelReview) -> ReviewRecord:
    return ReviewRecord(
        id=str(row.id),
        subject_table=row.subject_table,
        subject_id=row.subject_id,
        subject_version=row.subject_version,
        decision=row.decision,
        reviewer_id=row.reviewer_id,
        decided_at=row.decided_at,
        reason=row.reason,
        proposal_id=row.proposal_id,
        proposed_label=row.proposed_label,
        proposed_description=row.proposed_description,
        proposed_name=row.proposed_name,
        previous_label=row.previous_label,
        previous_description=row.previous_description,
        previous_name=row.previous_name,
    )


def _proposal_record(proposal: WordingProposal) -> ProposalRecord:
    return ProposalRecord(
        proposal_id=proposal.proposal_id,
        subject_table=proposal.subject_table,
        subject_id=proposal.subject_id,
        subject_version=proposal.subject_version,
        author_id=proposal.author_id,
        proposed_at=proposal.proposed_at,
        label=proposal.label,
        description=proposal.description,
        name=proposal.name,
        previous_label=proposal.previous_label,
        previous_description=proposal.previous_description,
        previous_name=proposal.previous_name,
        decided=proposal.decided,
    )


def _status_response(
    service: PanelReviewService, subject_table: ReviewSubjectTable, subject_id: str
) -> SubjectStatusResponse:
    """The row's resulting state, re-read so the response reflects storage.

    Re-read rather than composed from the request: an approved edit changes the
    row's wording and version, and a response built from what the caller sent
    would report the version it was decided *against* rather than the one it is
    now at. That difference is the whole point of the second acceptance
    criterion, so it must not be papered over in the response mapping.
    """
    current: ReviewStatus = service.status_for(subject_table=subject_table, subject_id=subject_id)
    row = service.subject(subject_table=subject_table, subject_id=subject_id)
    return SubjectStatusResponse(
        subject_table=subject_table,
        subject_id=subject_id,
        subject_version=current.subject_version,
        status=current.decision,
        is_panel_approved=current.is_panel_approved,
        is_published=current.is_publishable,
        label=row.label if isinstance(row, Stance) else None,
        description=row.description if isinstance(row, Stance) else None,
        name=row.name if isinstance(row, IssueTopic) else None,
    )


__all__ = [
    "BACKFILL_REVIEWER_ID",
    "get_proposals",
    "get_published_stances",
    "get_published_topics",
    "get_review_history",
    "get_review_queue",
    "panel_review_service",
    "post_proposal",
    "post_proposal_decision",
    "post_review_decision",
    "router",
    "zerodb_client_for_panel_review",
]
