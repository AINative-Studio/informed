"""HTTP surface for `GET /v1/ballot` (stories 1.5, 2.4).

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
  * **200 with an empty House race** — the district exists but its members have
    not been ingested. An honest empty answer, not an error; `data_state` tells
    the client that challengers are missing too.

Neither error message contains the address (prd.md, privacy first), and nothing
here logs the query string.

The one piece of state in this module (story 2.4)
=================================================

`_PEOPLE_SNAPSHOT` is a module-level `PeopleSnapshot`, deliberately, and it is
the only shared state in the ballot path. A per-request cache would cache
nothing — each request would read `people` once and throw the rows away — and
reading `people` on every request put the live p95 at 978 ms against a budget of
800 (see `app.ballot.service` for the measurements). Sharing one snapshot across
requests is what makes the second and subsequent lookups a median 456 ms.

It holds **public reference data about legislators** and no voter data of any
kind: its key is nothing at all, so there is no address, profile or request
detail in it. See `PeopleSnapshot` for why that is structural rather than
careful.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.ballot.models import BallotResponse
from app.ballot.service import BallotService, PeopleSnapshot
from app.districts.resolver import AddressResolutionError, DistrictNotFoundError
from app.zerodb import ZeroDBClient

router = APIRouter(prefix="/v1", tags=["ballot"])

#: Shared by every request in this process, which is the whole point — see the
#: module docstring. Not request-scoped: a snapshot that lived one request would
#: cost one full read per request, which is the latency this story fixes.
_PEOPLE_SNAPSHOT = PeopleSnapshot()


def zerodb_client() -> ZeroDBClient:
    """The ZeroDB client for this request, built from the environment.

    A FastAPI dependency rather than a module-level singleton so tests override
    it with the in-memory fake (`app.dependency_overrides[zerodb_client]`) and
    so story 1.6's per-environment settings have one place to land. Declared
    here and re-exported from `app.main`, which is where callers reach for it.
    """
    return ZeroDBClient.from_env()


def people_snapshot() -> PeopleSnapshot:
    """The process's `people` cache.

    A dependency rather than a direct reference so a test can give one scenario
    a cold cache without reaching into module state, and so a future worker
    process model has one place to change.
    """
    return _PEOPLE_SNAPSHOT


def ballot_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client)],
    snapshot: Annotated[PeopleSnapshot, Depends(people_snapshot)],
) -> BallotService:
    """A service wired to this request's client, the default resolver and the
    shared `people` snapshot.

    The default resolver is `CensusAddressResolver` (story 2.3), which replaced
    story 1.5's stub behind the same `AddressResolver` protocol — one changed
    default, no changed caller. Declared as a dependency rather than constructed
    inside the handler so a test can override the resolver without a second HTTP
    surface, and so prd.md's `JurisdictionAdapter` has one place to vary it per
    country later.
    """
    return BallotService(client=client, snapshot=snapshot)


@router.get(
    "/ballot",
    response_model=BallotResponse,
    summary="Districts, races and candidates for an address",
    description=(
        "Resolves an address to its congressional district and returns the "
        "races on that voter's ballot, each with its candidates and an "
        "explicit incumbent flag.\n\n"
        "**Candidate coverage.** Only sitting officeholders are listed so far, "
        "so `data_state` is `incumbents_only` and every candidate's "
        "`is_incumbent` is true. A race with one candidate is therefore **not** "
        "an uncontested race — challengers arrive with story 2.6. A client must "
        "say so rather than present a short ballot as a complete one.\n\n"
        "**Latency.** Target p95 under 800 ms, most of which is the one "
        "external call to the US Census Geocoder (measured at roughly 500 ms). "
        "Design a loading state regardless.\n\n"
        "**Privacy.** The address is used to resolve a district and then "
        "discarded: it is never stored, never logged, never cached, and never "
        "echoed in a response or an error message."
    ),
    responses={
        404: {"description": "That district resolved but Informed cannot record it"},
        422: {"description": "The address is missing, blank or unusable"},
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
    """Resolve an address to its district, races and candidates.

    prd.md's Core API calls this "districts, races and candidates for an
    address", which as of story 2.4 is what it returns — with every candidate a
    sitting officeholder until story 2.6 loads candidacies.
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


__all__ = ["ballot_service", "get_ballot", "people_snapshot", "router", "zerodb_client"]
