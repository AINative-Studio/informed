"""HTTP surface for `GET /v1/candidates/{id}/promises` (story 9.6).

Not one of prd.md's Core API table's six endpoints by name, same posture as
`app.bills.router`'s `/bills/{bill_id}/text`: required by the acceptance
criterion a listed endpoint cannot satisfy on its own.
`GET /v1/candidates/{id}/alignment` groups by a voter's own interests already
(story 6.7's `stance_id`), but only from `candidate_positions` -- a table a
challenger has no rows in. backlog.md 9.6 needs the same grouping over
`promises` instead, so a challenger shows up at all.

Thin, in the same shape as `app.scoring.router`: validate the path, call
`ChallengerPromisesService`, map its two exceptions to status codes. The
dependency wiring below is copied line for line from `app.scoring.router`'s,
including its own `profile_id` naming rationale -- see that module's
docstring for why the published query parameter is spelled `profile_id` while
every service method underneath it takes `user_id`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.service import VoterProfileService
from app.promises.models import ChallengerPromises
from app.promises.service import (
    CandidacyNotFoundError,
    ChallengerPromisesService,
    ProfileNotFoundError,
)
from app.zerodb import ZeroDBClient

router = APIRouter(prefix="/v1", tags=["promises"])


def zerodb_client_for_promises() -> ZeroDBClient:
    """The ZeroDB client for this request.

    Its own dependency function, not shared with any other router -- see
    `app.topics.router.zerodb_client_for_topics` for why: `dependency_overrides`
    keys on the function object, so a test overriding this router's client
    must not have to know which other routers happen to share one.
    """
    return ZeroDBClient.from_env()


def challenger_promises_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_promises)],
) -> ChallengerPromisesService:
    """The service, with the profile service that can decrypt a profile.

    Constructed here rather than injected separately, for the same reason
    `app.scoring.router.alignment_service` constructs its own
    `VoterProfileService`: there is exactly one way to read a voter's profile
    in this codebase, and a caller able to substitute another could read one
    without its keyring.
    """
    profiles = VoterProfileService(
        client=client,
        keyring=ProfileKeyring(client=client, root_key=ProfileRootKey.from_env()),
    )
    return ChallengerPromisesService(client=client, profiles=profiles)


@router.get(
    # `{candidacy_id:path}`, not `{candidacy_id}` -- see
    # `app.scoring.router.get_alignment`'s own comment on why: a real
    # candidacy id embeds an OCD-ID, which contains slashes, and the default
    # Starlette converter would 404 before this handler ever ran.
    "/candidates/{candidacy_id:path}/promises",
    response_model=ChallengerPromises,
    summary="One candidate's promises, grouped by one voter's own interests",
    description=(
        "A challenger's promises (story 9.3's extracted commitments), grouped "
        "under the voter's OWN stated interests -- the same `stance_id` key "
        "`GET /v1/candidates/{id}/alignment` groups its reasons by. Built so "
        "a candidate with no voting record is still comparable: this endpoint "
        "reads `promises` directly rather than `candidate_positions`, which a "
        "challenger has no rows in.\n\n"
        "**Nothing is scored here.** A testable promise and a non-testable "
        "one both appear, each labeled as such; there is no score, "
        "confidence badge or ranking in this response at all.\n\n"
        "**A promise outside the voter's interests is counted, not "
        "dropped.** `unmatched_count` says how many of this challenger's "
        "promises are not about one of the voter's chosen interests, so the "
        "existence of other promises is never hidden.\n\n"
        "**A prior office is linked when one exists.** `prior_offices` reads "
        "the person's own `people.terms[]` record for any office that is not "
        "this candidacy's own seat -- never fabricated when there is none.\n\n"
        "**Computed per request and never stored**, like `/alignment`: no "
        "table links a voter's priorities to a candidate."
    ),
    responses={
        404: {
            "description": (
                "No candidacy with that id is known, or the voter has no "
                "stored profile to group promises against."
            )
        }
    },
)
def get_challenger_promises(
    service: Annotated[ChallengerPromisesService, Depends(challenger_promises_service)],
    candidacy_id: str,
    profile_id: Annotated[
        str,
        Query(
            min_length=1,
            description=(
                "Whose interests to group by. Required -- see "
                "`app.scoring.router.get_alignment`'s identical rule: there is "
                "no meaningful anonymous grouping, because the grouping key IS "
                "the voter's own stated interests."
            ),
        ),
    ],
) -> ChallengerPromises:
    try:
        return service.promises_for(candidacy_id, user_id=profile_id)
    except CandidacyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ProfileNotFoundError as exc:
        # A 404, like the unknown candidacy, rather than a 422 -- see
        # `app.scoring.router.get_alignment`'s identical choice and its note
        # on never echoing the profile id back into an error body.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


__all__ = [
    "challenger_promises_service",
    "get_challenger_promises",
    "router",
    "zerodb_client_for_promises",
]
