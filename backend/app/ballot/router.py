"""HTTP surface for `GET /v1/ballot` (story 1.5).

Thin on purpose: validate the query, call `BallotService`, map its two
exceptions to status codes. Everything else lives in the service, so the same
logic is reachable from an agent or a batch job without going through HTTP.

The status codes, and why each:

  * **422** — a blank address. The voter's input is unusable and theirs to fix,
    and 422 is what FastAPI already returns for a missing `?address=`, so a
    blank one and an absent one answer the same way rather than one 400 and one
    422 for the same mistake.
  * **404** — the address resolved to a district Informed cannot record. Since
    story 2.3 a `districts` row is created on first resolution, so "we have no
    row for your district" is no longer reachable; what remains is "we cannot
    write one", in practice because the chamber it references was never seeded.
    Still Informed's data gap and not the voter's error — nothing they type
    differently will help — which is why it keeps the same status.
  * **200 with an empty `officeholders`** — the district exists but its members
    have not been ingested (story 2.2). An honest empty answer, not an error.

Neither error message contains the address (prd.md, privacy first), and nothing
here logs the query string.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.ballot.models import BallotResponse
from app.ballot.service import BallotService
from app.districts.resolver import AddressResolutionError, DistrictNotFoundError
from app.zerodb import ZeroDBClient

router = APIRouter(prefix="/v1", tags=["ballot"])


def zerodb_client() -> ZeroDBClient:
    """The ZeroDB client for this request, built from the environment.

    A FastAPI dependency rather than a module-level singleton so tests override
    it with the in-memory fake (`app.dependency_overrides[zerodb_client]`) and
    so story 1.6's per-environment settings have one place to land. Declared
    here and re-exported from `app.main`, which is where callers reach for it.
    """
    return ZeroDBClient.from_env()


def ballot_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client)],
) -> BallotService:
    """A service wired to this request's client and the default resolver.

    The default resolver is `CensusAddressResolver` (story 2.3), which replaced
    story 1.5's stub behind the same `AddressResolver` protocol — one changed
    default, no changed caller. Declared as a dependency rather than constructed
    inside the handler so a test can override the resolver without a second HTTP
    surface, and so prd.md's `JurisdictionAdapter` has one place to vary it per
    country later.
    """
    return BallotService(client=client)


@router.get(
    "/ballot",
    response_model=BallotResponse,
    summary="Districts and sitting officeholders for an address",
    responses={
        404: {"description": "That district resolved but Informed cannot record it"},
        422: {"description": "The address is missing or blank"},
    },
)
def get_ballot(
    service: Annotated[BallotService, Depends(ballot_service)],
    address: Annotated[
        str,
        Query(
            min_length=1,
            max_length=500,
            description=(
                "A street address or postcode. Used to resolve a district and "
                "then discarded: it is never stored or logged."
            ),
        ),
    ],
) -> BallotResponse:
    """Resolve an address to its district and the members who represent it.

    prd.md's Core API calls this "districts, races and candidates for an
    address". Story 1.5 returns the district and the sitting officeholders;
    epic 2 adds races and candidacies to the same payload.
    """
    try:
        return service.ballot_for(address)
    except DistrictNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AddressResolutionError as exc:
        # 422, matching FastAPI's own answer to an absent `?address=`, so a
        # blank address and a missing one are not two different errors.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


__all__ = ["ballot_service", "get_ballot", "router", "zerodb_client"]
