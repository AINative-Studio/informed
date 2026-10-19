"""HTTP surface for `GET /v1/candidates/{candidacy_id}/alignment` (6.4-6.7).

prd.md's Core API table, verbatim:

    GET /v1/candidates/{id}/alignment?profile_id=
        Score, confidence, dealbreakers, top reasons

Thin, in the same shape as `app.summary_card.router`: validate the path, call
`AlignmentService`, map its two exceptions to status codes.

`profile_id`, and why it is spelled that way here
==================================================

prd.md publishes the parameter as `profile_id`; every existing endpoint in this
codebase that touches a profile spells it `user_id`
(`GET /v1/profiles/{user_id}`, and see `app.profiles.router`'s docstring on why
the request carries it rather than an auth header). They name the same thing —
`ProfileKeyring.row_id` HMACs the user id into the `vp:` row id, so a voter's
"profile id" IS their user id as far as this API is concerned.

The published name wins on the wire, and the translation happens on one line in
this handler where a reader can see it. Two reasons: prd.md's API table is the
contract the frontend is built against, and `profile_id` is the better name for
a public parameter anyway — it says what the value selects rather than who it
belongs to.

Why `profile_id` is REQUIRED rather than optional
==================================================

An alignment score is a function of a voter's priorities; there is no
"anonymous" version of it that means anything. Story 14.3's anonymous mode
keeps the profile client-side and never stores it, so an anonymous voter cannot
be scored by this endpoint at all — the client scores locally, or stores a
profile first. Making the parameter required means that shows up as a 422 at
the edge rather than as a mysteriously empty score.

What this endpoint is NOT, and the contrast with story 14.6
============================================================

The summary-card endpoint takes no voter identity at all, deliberately, so
"no personalization is possible here" is readable off its signature. This
endpoint is the opposite by necessity: it exists to personalize. So the
guarantee it carries is a different one — the RESPONSE cannot carry profile
content. `app.scoring.models`' four models forbid extra fields and declare
nothing that could hold an interest's wording, tier, note or the encrypted
payload, and the tests pin that absence. Personalized in, never personal out.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.service import VoterProfileService
from app.scoring.models import CandidateAlignment
from app.scoring.service import (
    AlignmentService,
    CandidacyNotFoundError,
    ProfileNotFoundError,
)
from app.zerodb import ZeroDBClient

router = APIRouter(prefix="/v1", tags=["alignment"])


def zerodb_client_for_alignment() -> ZeroDBClient:
    """The ZeroDB client for this request.

    Its own dependency function, not shared with any other router, for the
    reason `app.topics.router.zerodb_client_for_topics` documents:
    `dependency_overrides` keys on the function object, so a test overriding
    this router's client must not have to know which other routers happen to
    share one.
    """
    return ZeroDBClient.from_env()


def alignment_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_alignment)],
) -> AlignmentService:
    """The scorer, with the profile service that can decrypt a profile.

    `VoterProfileService` is constructed here rather than injected separately
    because there is exactly one way to read a voter's profile in this codebase
    and a caller able to substitute another could read one without its keyring
    — see `app.profiles.service`'s own reasoning for the identical decision
    about its consent ledger.
    """
    profiles = VoterProfileService(
        client=client,
        keyring=ProfileKeyring(client=client, root_key=ProfileRootKey.from_env()),
    )
    return AlignmentService(client=client, profiles=profiles)


@router.get(
    # `{candidacy_id:path}`, not `{candidacy_id}`, and this is not cosmetic.
    #
    # datamodel.md's `candidacies` id is `{election_id}:{district_id}:{person_id}`
    # and a `district_id` is an OCD-ID — `ocd-division/country:us/state:ca/cd:19`
    # — which contains SLASHES. With the default converter, Starlette matches a
    # path parameter against `[^/]+`, so a real candidacy id would never reach
    # this handler: `/v1/candidates/us-2026-11-03-general:ocd-division/country:
    # us/.../alignment` 404s at the router before any code runs.
    #
    # Found while writing this story's BDD steps, which seed a candidacy through
    # the real `Candidacy` repository and so got a real OCD-ID in the id. Story
    # 14.6's summary-card endpoint has the same shape and its tests use a
    # slash-free synthetic candidacy id, so the problem is latent there too —
    # flagged in this branch's PR rather than fixed here, because changing
    # another story's route is not this story's call to make.
    #
    # The `:path` converter is greedy, which is why `/alignment` has to follow
    # it: Starlette resolves the literal suffix, so the id captures everything
    # between `/candidates/` and the final `/alignment`. A candidacy id ending
    # in a literal `/alignment` would confuse it, which no id pattern in
    # datamodel.md can produce.
    "/candidates/{candidacy_id:path}/alignment",
    response_model=CandidateAlignment,
    summary="One candidate's alignment with one voter's profile",
    description=(
        "prd.md's alignment score for one candidate against one voter's "
        "stated priorities, with the confidence it rests on, any dealbreaker "
        "warnings, and the top three reasons behind the number.\n\n"
        "**Computed per request and never stored.** No table links a voter's "
        "priorities to a candidate (datamodel.md), so there is no cache and "
        "no stored score — the same profile and the same evidence always "
        "produce the same answer, and asking twice leaves no trace.\n\n"
        "**The score is `null`, not 50, when nothing could be measured.** "
        "50 means 'measured, and found neutral'; `null` with a "
        "`confidence_badge` of `insufficient` means there is no recorded "
        "evidence about this candidate on the voter's priorities.\n\n"
        "**A dealbreaker warning never changes the score.** prd.md requires a "
        "violation to be 'shown above the score rather than averaged away', "
        "so `dealbreaker_warnings` is computed separately and the number in "
        "`score` is unaffected by it.\n\n"
        "**Every reason cites a roll call.** The response type cannot hold an "
        "uncited reason, so this is a property of the shape rather than a "
        "policy to audit.\n\n"
        "**This endpoint never recommends a candidate** and carries no "
        "ranking across candidates — a client orders its own ballot view "
        "(prd.md: 'Never a single \"vote for X\" output')."
    ),
    responses={
        404: {
            "description": (
                "No candidacy with that id is known, or the voter has no "
                "stored profile to score against."
            )
        }
    },
)
def get_alignment(
    service: Annotated[AlignmentService, Depends(alignment_service)],
    candidacy_id: str,
    profile_id: Annotated[
        str,
        Query(
            min_length=1,
            description=(
                "Whose profile to score against. Required: an alignment score "
                "is a function of a voter's priorities, so there is no "
                "meaningful anonymous answer. A voter in anonymous mode "
                "(story 14.3) holds their profile client-side and is not "
                "scoreable here."
            ),
        ),
    ],
) -> CandidateAlignment:
    try:
        # The one line where prd.md's published `profile_id` meets this
        # codebase's `user_id`. See the module docstring.
        return service.alignment_for(candidacy_id, user_id=profile_id)
    except CandidacyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ProfileNotFoundError as exc:
        # A 404, like the unknown candidacy, rather than a 422: the request is
        # well formed and names a profile that simply is not there. The detail
        # text says which of the two it was, and deliberately does not echo the
        # profile id back — an error body is the one place a profile locator
        # most easily ends up in a log.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


__all__ = [
    "alignment_service",
    "get_alignment",
    "router",
    "zerodb_client_for_alignment",
]
