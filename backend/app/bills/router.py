"""HTTP surface for `GET /v1/bills/{bill_id}/text` (story 5.4).

Not one of prd.md's Core API table's six endpoints by name, but required by
the one it sits under: `GET /v1/candidates/{id}/promise-record` returns
"per-promise verdicts with citations", and a citation to a recorded vote is
only verifiable if the voter can open the bill text that vote was on. Story
5.4's acceptance criterion names the mechanism explicitly — "the UI opens it
via a presigned URL" — so the URL has to come from somewhere the browser can
call.

Thin, following `app.summary_card.router`'s shape: validate the path, call
`BillTextService`, map its one exception to a status code.

`bill_id` is a path parameter containing colons (`us-house:119:hr-3424`),
which is legal in a URL path segment (RFC 3986 `pchar` includes `:`) and needs
no encoding by the client. Confirmed against Starlette's own routing by this
story's BDD scenarios, which call the real path.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.bills.models import BillTextResponse
from app.bills.service import BillTextService, BillTextUnavailableError
from app.zerodb import ZeroDBClient

router = APIRouter(prefix="/v1", tags=["bills"])


def zerodb_client_for_bills() -> ZeroDBClient:
    """The ZeroDB client for this request.

    A separate dependency function from every other router's, for the reason
    `app.topics.router.zerodb_client_for_topics` documents:
    `dependency_overrides` keys on the function object, so a test overriding
    this router's client must not have to know which other router shares the
    same construction.
    """
    return ZeroDBClient.from_env()


def bill_text_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_bills)],
) -> BillTextService:
    return BillTextService(client=client)


@router.get(
    "/bills/{bill_id}/text",
    response_model=BillTextResponse,
    summary="A short-lived URL for the archived text of one bill",
    description=(
        "The exact bill text Informed archived, so a voter can read what a "
        "vote was actually on and verify it themselves (story 5.4).\n\n"
        "**The URL is short-lived** and the response says for how long. A "
        "citation link that outlives the page view is a link that can be "
        "shared past its access check, so clients should request this "
        "endpoint when the voter asks to open the text rather than caching "
        "the URL.\n\n"
        "**Verify it yourself.** `sha256` is the hash of the archived bytes "
        "as measured at capture: download what `url` serves, hash it, and "
        "compare. `source_url` names the www.congress.gov document the bytes "
        "came from, so the primary source can be opened directly and "
        "compared against the archive.\n\n"
        "**Which version this is.** `version_type` and `version_date` name "
        "the published text version archived. A bill commonly has several "
        "(introduced, reported, engrossed, enrolled); the archived one is the "
        "newest the source publishes, which is **not necessarily the version "
        "a particular roll call voted on** — read `version_type` before "
        "treating it as the text of a specific vote."
    ),
    responses={
        404: {
            "description": (
                "No archived text is available for that bill — it is not "
                "ingested, its text is not published yet, or it has not been "
                "archived. Deliberately the same answer in every case."
            )
        }
    },
)
def get_bill_text(
    service: Annotated[BillTextService, Depends(bill_text_service)],
    bill_id: str,
) -> BillTextResponse:
    try:
        return service.text_for(bill_id)
    except BillTextUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no archived text is available for that bill",
        ) from exc


__all__ = ["bill_text_service", "get_bill_text", "router", "zerodb_client_for_bills"]
