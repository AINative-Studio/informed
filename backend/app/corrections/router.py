"""HTTP surface for public corrections (story 12.4).

    POST /v1/corrections          — anyone reports an error on any claim
    GET  /v1/corrections/{token}  — the reporter checks their own status

prd.md's Core API table lists `POST /v1/corrections | Public correction
submission`; the status lookup is what makes backlog.md 12.4's "status is
visible to the reporter" true for a reporter with no account.

## Public means public, and this one is a WRITE

`/v1/methodology` (story 12.2) was the first endpoint here with no credential
of any kind, and it is a read of published reference data. This is the first
uncredentialed **write**, which is a different risk, so four choices are
deliberate:

  * **Both request and response models forbid extra fields.** A submission
    cannot smuggle `status`, `resolved_at` or `tracking_token_hash`, and a
    field added to the stored row by a later story cannot appear on either
    response.
  * **The receipt echoes neither the body nor the email the reporter sent.**
    The reporter has them; this response ends up in logs and screenshots.
  * **The status lookup returns status only** — no body, no email, no target,
    no id. See `CorrectionStatus` for the full argument: the token is a bearer
    secret in a URL, so what it unlocks is held to the minimum that satisfies
    the acceptance criterion.
  * **Triage is NOT exposed here.** `CorrectionService.set_status` exists and
    is tested, but there is no `PATCH /v1/corrections/{id}` in this story,
    because there is no authentication anywhere in this codebase and an
    unauthenticated status write would let anyone mark an inconvenient
    correction `rejected`. See the service's module docstring. The ops path
    today is `scripts/triage_correction.py`.

Rate limiting is prd.md's "Integrity and abuse" item and belongs to the story
that adds it across the public surface. It is worth saying what that means for
*this* endpoint specifically, since it is the one that writes: until that story
lands, nothing in code stops a script from filling this table. The body cap
(`MAX_BODY_LENGTH`) bounds the damage per request and the row holds nothing
identifying to limit on by design, so the mitigation is necessarily at the edge
— flagged in this story's PR as the first thing to put behind a limiter.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status

from app.corrections.models import (
    CorrectionReceipt,
    CorrectionRequest,
    CorrectionStatus,
)
from app.corrections.service import CorrectionService, UnknownCorrectionError
from app.repositories.errors import RowValidationError
from app.zerodb import ZeroDBClient

router = APIRouter(prefix="/v1/corrections", tags=["corrections"])


def zerodb_client_for_corrections() -> ZeroDBClient:
    """The ZeroDB client for this request.

    Its own dependency rather than a shared one, for the reason
    `app.topics.router`'s sibling gives: FastAPI keys `dependency_overrides` on
    the function object, so a test overriding one router's client must not have
    to know which other routers share it.
    """
    return ZeroDBClient.from_env()


def correction_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_corrections)],
) -> CorrectionService:
    return CorrectionService(client)


@router.post(
    "",
    response_model=CorrectionReceipt,
    status_code=status.HTTP_201_CREATED,
    summary="Report an error on any claim",
    description=(
        "Public and unauthenticated, by design: the people most likely to "
        "spot an error about a candidate are the least likely to hold an "
        "account, and a correction form behind a login is a correction form "
        "nobody uses.\n\n"
        "`contact_email` is optional. Leaving it out costs nothing — the "
        "`status_path` in the response works either way — and an address that "
        "is given is erased 90 days after the report is closed.\n\n"
        "**Keep the `tracking_token`.** It is shown once and never again: only "
        "a hash of it is stored, so it cannot be looked up or re-sent, and it "
        "is the only way to check this report's status without an account.\n\n"
        "A report about a row that turns out not to exist is accepted rather "
        "than refused — that is useful signal about what readers are seeing, "
        "not a client error. What is refused is a `target_table` that is not "
        "part of the published schema."
    ),
    responses={
        422: {
            "description": (
                "The target table is not in the schema, the body is empty, or "
                "the contact email is malformed"
            )
        }
    },
)
def post_correction(
    service: Annotated[CorrectionService, Depends(correction_service)],
    body: Annotated[CorrectionRequest, Body()],
) -> CorrectionReceipt:
    try:
        return service.submit(
            target_table=body.target_table,
            target_id=body.target_id,
            body=body.body,
            contact_email=body.contact_email,
        )
    except RowValidationError as exc:
        # A backstop, not the main path. `CorrectionRequest` already enforces
        # the blank-body and email-shape rules at the boundary, so a reporter's
        # typo is a 422 naming the field. This catches a rule that is added to
        # `CorrectionRow` and not to the request model: without it, the same
        # input would reach the repository and surface as a 500.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ValueError as exc:
        # An unknown target table, a blank body or a malformed email. All three
        # are the caller's to fix and all three are already described in this
        # endpoint's own documentation, so the message is returned as-is: it
        # names only what the caller sent, never a table list, a project id or
        # anything about the store behind it.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get(
    "/{tracking_token}",
    response_model=CorrectionStatus,
    summary="Check a correction's status with the tracking token",
    description=(
        'Backlog story 12.4\'s "status is visible to the reporter", for a '
        "reporter who has no account and may have given no email.\n\n"
        "Returns the **status only** — never the report's text, never the "
        "contact email, never the target or the row id. The token is a bearer "
        "secret in a URL, so what it unlocks is held to the minimum that "
        "answers 'what is happening with my report'.\n\n"
        "`past_sla` is shown to the reporter rather than only to us: an SLA "
        "the person waiting cannot check is a promise to ourselves."
    ),
    responses={404: {"description": "No correction matches that tracking token"}},
)
def get_correction_status(
    service: Annotated[CorrectionService, Depends(correction_service)],
    tracking_token: Annotated[
        str,
        Path(
            min_length=16,
            max_length=128,
            description="The token returned when the correction was submitted.",
        ),
    ],
) -> CorrectionStatus:
    try:
        return service.status_for_token(tracking_token)
    except UnknownCorrectionError as exc:
        # One answer for every miss — wrong token, correction deleted, or the
        # caller trying the row id instead (which does NOT open this lookup).
        # Distinguishing them would turn this endpoint into an oracle for
        # which ids exist.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No correction matches that tracking token.",
        ) from exc


__all__ = [
    "correction_service",
    "get_correction_status",
    "post_correction",
    "router",
    "zerodb_client_for_corrections",
]
