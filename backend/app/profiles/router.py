"""HTTP surface for `POST /v1/profiles` (story 4.1, building on 14.1).

Story 14.1 built `VoterProfileService` — validate, encrypt, upsert, version
bump — and a full step-definition and live-verification suite for it, but
nothing before this story called it from HTTP. This router is intentionally
thin, in the same shape as `app.ballot.router`: validate the request body,
call the service, map its exceptions to status codes. All of the actual rules
(tier limits, dealbreaker-on-primary-only, encryption, versioning) already
live in `app.profiles.models` and `app.profiles.service`; this file adds none
of its own.

Why the request carries `user_id` rather than reading an auth header
=====================================================================

No authentication layer exists yet anywhere in this codebase (iteration 1's
walking skeleton has none, and adding one is not this story's scope). Every
caller of `VoterProfileService.save` — the unit tests, the BDD steps, the live
verification script — already identifies the voter with a plain string id, so
this endpoint accepts the same shape explicitly in the body rather than
inventing a session concept this story does not own. The field is clearly
named and clearly temporary: a later story that adds real authentication
replaces "trust the caller's `user_id`" with "read it off a verified session",
and this endpoint's `VoterProfileService` call does not change at all — only
where `user_id` comes from does.

Why `consent` is a required field rather than a free-standing endpoint
========================================================================

ui-prd.md 2.2: "Before any interest is saved to an account, show the GDPR
special-category consent language and the anonymous-mode alternative, side by
side, as an actual choice — not a pre-checked box. This blocks profile save."
Story 14.2 ("Consent at onboarding") owns the actual consent screen, its copy,
and any record of *when* and *to what wording* a voter agreed — none of that
exists yet, and none of it is invented here. What this story owes is the one
thing that would otherwise let a client bypass the gate entirely: the save
endpoint itself must refuse a write that does not carry an explicit, true
`consent` flag. A request missing it, or carrying `consent: false`, is a 422
naming consent — the same shape of error as a missing or blank address in
`GET /v1/ballot` — because in both cases the caller sent something unusable
and theirs to fix, not a server failure.

This is deliberately a conservative design for 14.2 to build on, not a
pre-emption of it: 14.2 can add a `consent_version` or `consented_at` field to
this same request later without this story's gate changing shape, because the
gate is "no consent, no write" and that rule does not get weaker as it gets
more detailed.

Anonymous mode: refused here, intentionally
=============================================

datamodel.md: "In anonymous mode no row is written: the same payload lives in
the browser, and `/v1/alignment` accepts it inline." A request with no
`user_id` reaches `VoterProfileService.save(None, ...)`, which raises
`AnonymousModeError` by design (see that module's docstring) rather than
silently no-op'ing. This router turns that into a 422 that says so in plain
language, so a client that mistakenly POSTs an anonymous profile here gets a
loud, actionable error rather than a response that looks like success for a
row that was never written. The *working* anonymous path is client-side only
(browser storage) and never reaches this endpoint at all — see
`saveAnonymousProfile` in `frontend/src/api/profiles.ts`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.profiles.crypto import ProfileKeyring, ProfileRootKey, RootKeyMissingError
from app.profiles.methodology import MethodologyVersionService
from app.profiles.service import AnonymousModeError, PersonNotFoundError, VoterProfileService
from app.zerodb import ZeroDBClient

router = APIRouter(prefix="/v1", tags=["profiles"])


def zerodb_client_for_profiles() -> ZeroDBClient:
    """The ZeroDB client for this request. See `app.topics.router`'s sibling
    function for why this is not shared with the ballot router's dependency.
    """
    return ZeroDBClient.from_env()


def methodology_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_profiles)],
) -> MethodologyVersionService:
    """The tier-weight reader (story 4.2), wired to the same client as the
    profile service. See `app.profiles.methodology`'s module docstring for
    why the weights live there rather than as a constant in this router.
    """
    return MethodologyVersionService(client)


def profile_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_profiles)],
) -> VoterProfileService:
    """The encrypting profile service, wired to the live root key.

    Raises on an unset `$ZERODB_PROFILE_ROOT_KEY` via `ProfileRootKey.from_env`
    (story 14.1) — a deployment missing it must fail loudly at first use, not
    silently fall back to an unencrypted or constant key. FastAPI surfaces that
    as a 500, which is correct here: a missing root key is this deployment's
    misconfiguration, never the voter's mistake.
    """
    return VoterProfileService(
        client=client, keyring=ProfileKeyring(client=client, root_key=ProfileRootKey.from_env())
    )


class SaveProfileRequest(BaseModel):
    """The body of `POST /v1/profiles`.

    `profile` is passed through to `VoterProfileService.validate`/`save`
    unparsed (`dict[str, Any]`, not `InterestProfile`): the service is the one
    place that model is validated, and parsing it twice would mean two Pydantic
    models that could silently drift. Any shape this field needs to carry
    (tier limits, dealbreaker-only-on-primary, duplicate-topic rejection) is
    already enforced by `InterestProfile`'s own validators — see that model's
    docstring — and this request type adds nothing but the envelope around it.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = Field(
        default=None,
        description=(
            "The voter's id. Required to actually save: omitting it (or "
            "sending null) is the anonymous-mode shape, which this endpoint "
            "refuses — datamodel.md's anonymous mode never writes a row, so "
            "a client in that mode must not call this endpoint at all and "
            "should keep the profile in browser storage instead."
        ),
    )
    consent: bool = Field(
        description=(
            "Must be true. ui-prd.md 2.2's consent gate: no interest profile "
            "is saved to an account without an explicit, affirmative consent "
            "signal. False or omitted is refused with a 422, never silently "
            "treated as consent withheld — the save simply does not happen."
        )
    )
    profile: dict[str, Any] = Field(
        description=(
            "The InterestProfile payload: interests (topic_id, stance_id, "
            "tier, intensity, dealbreaker, optional note), evidence_mode, "
            "followed_person_ids. Validated by app.profiles.models."
            "InterestProfile — 1-5 primary interests, up to 8 secondary, "
            "unlimited nice-to-have, dealbreaker allowed only on primary."
        )
    )
    locale: str = Field(
        default="en-US", min_length=2, description="Taxonomy locale for this profile"
    )
    jurisdiction_ids: list[str] = Field(
        default_factory=list, description="Jurisdictions this voter's ballot spans"
    )


class SaveProfileResponse(BaseModel):
    """What a successful save returns.

    Deliberately the row's metadata only — version, locale, updated_at — never
    the interests the caller just sent. `VoterProfileService.save` itself
    returns `VoterProfileRow`, the encrypted row, "so a caller cannot
    accidentally round-trip plaintext through a response model" (that
    method's own docstring); this response type keeps that guarantee all the
    way to the wire rather than trusting every future caller to remember it.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(description="The profile's version after this save, starting at 1")
    locale: str = Field(description="The locale this profile was saved under")
    updated_at: str = Field(description="When this version was written, ISO-8601 UTC")


class TierWeightsResponse(BaseModel):
    """What `GET /v1/profiles/methodology` returns (story 4.2).

    The tier-editing UI reads this once to label and weight each tier's
    section rather than hard-coding "3.0" / "1.5" / "0.5" into a component —
    datamodel.md: "The live values come from `methodology_versions` and are
    never hard-coded."
    """

    model_config = ConfigDict(extra="forbid")

    methodology_version: str = Field(
        description="The methodology version id these weights came from"
    )
    weights: dict[str, float] = Field(
        description=(
            'Tier name -> weight, e.g. {"primary": 3.0, "secondary": 1.5, "nice_to_have": 0.5}'
        )
    )


class LoadedInterest(BaseModel):
    """One interest as returned by `GET /v1/profiles/{user_id}`.

    The same fields `Interest` validates on the way in (story 14.1), returned
    on the way out so a profile-editing UI can re-render exactly what was
    saved, dealbreaker and all (story 4.3: "shown in the profile summary").
    """

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    stance_id: str
    tier: str
    intensity: int
    dealbreaker: bool
    note: str | None = None


class LoadedProfile(BaseModel):
    """The body of a successful `GET /v1/profiles/{user_id}` with a profile.

    Mirrors `InterestProfile` (story 14.1) field for field, with `Interest`
    rows reshaped through `LoadedInterest` so this response type — not
    `InterestProfile` itself — is the one thing a client's shape can drift
    against, the same separation `SaveProfileRequest`'s docstring describes
    for the save side.
    """

    model_config = ConfigDict(extra="forbid")

    interests: list[LoadedInterest]
    evidence_mode: str
    followed_person_ids: list[str]


class FollowResponse(BaseModel):
    """What `POST`/`DELETE /v1/profiles/{user_id}/follow/{person_id}` return.

    Just the updated list, same shape as `LoadedProfile.followed_person_ids` —
    deliberately not the whole profile: a follow action should not round-trip
    a voter's interests back to the client that just asked to follow someone,
    mirroring why `SaveProfileResponse` withholds the interests it was handed.
    """

    model_config = ConfigDict(extra="forbid")

    followed_person_ids: list[str] = Field(
        description="Every person_id this voter currently follows, after this action"
    )


@router.get(
    "/profiles/methodology",
    response_model=TierWeightsResponse,
    summary="The current tier weights, read from methodology_versions",
    description=(
        "Story 4.2: the tier-editing UI's own numbers for primary/secondary/"
        "nice_to_have, read live from the methodology_versions table rather "
        "than hard-coded into the client. See app.profiles.methodology's "
        "module docstring for the seeding behaviour on a fresh environment."
    ),
)
def get_tier_weights(
    service: Annotated[MethodologyVersionService, Depends(methodology_service)],
) -> TierWeightsResponse:
    version = service.current_version()
    return TierWeightsResponse(methodology_version=version.id, weights=version.tier_weights)


@router.get(
    "/profiles/{user_id}",
    response_model=LoadedProfile | None,
    summary="Load a voter's saved interest profile (story 4.4)",
    description=(
        "Decrypts and returns the voter's stored profile (story 14.1's "
        "encrypted store), or `null` with a 200 when nothing has been saved "
        "yet — 'never saved' and 'not found' are the same answer to a "
        "client, not an error. Anonymous-mode profiles are never reachable "
        "here: they never leave the browser (see app.profiles.service's "
        "module docstring), so there is no user_id to load them by."
    ),
)
def load_profile(
    service: Annotated[VoterProfileService, Depends(profile_service)],
    user_id: str,
) -> LoadedProfile | None:
    profile = service.load(user_id)
    if profile is None:
        return None
    return LoadedProfile(
        interests=[
            LoadedInterest(
                topic_id=item.topic_id,
                stance_id=item.stance_id,
                tier=item.tier,
                intensity=item.intensity,
                dealbreaker=item.dealbreaker,
                note=item.note,
            )
            for item in profile.interests
        ],
        evidence_mode=profile.evidence_mode,
        followed_person_ids=profile.followed_person_ids,
    )


@router.post(
    "/profiles",
    response_model=SaveProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Save a tiered interest profile",
    description=(
        "Validates and encrypts a voter's interest profile and stores it "
        "(story 14.1). Requires `consent: true` — ui-prd.md 2.2's consent "
        "gate blocks every other path to this write. A profile with no "
        "primary interest, more than 5 primary, more than 8 secondary, or a "
        "dealbreaker outside a primary interest is refused with a 422 "
        "naming the rule (prd.md's tier table, enforced in "
        "`InterestProfile`).\n\n"
        "Anonymous mode is NOT served here: datamodel.md's anonymous mode "
        "never writes a row, so a client offering that choice keeps the "
        "profile in browser storage and never calls this endpoint."
    ),
    responses={
        422: {
            "description": (
                "Missing or false consent, an invalid profile shape, or an "
                "anonymous (no user_id) request"
            )
        },
    },
)
def save_profile(
    service: Annotated[VoterProfileService, Depends(profile_service)],
    body: Annotated[SaveProfileRequest, Body()],
) -> SaveProfileResponse:
    if not body.consent:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "consent is required before a profile can be saved. Choose "
                "the anonymous-mode alternative instead if you do not want "
                "to save an account profile."
            ),
        )
    try:
        row = service.save(
            body.user_id,
            body.profile,
            locale=body.locale,
            jurisdiction_ids=body.jurisdiction_ids,
        )
    except AnonymousModeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"the profile is invalid: {_safe_error_summary(exc)}",
        ) from exc
    except RootKeyMissingError as exc:
        # A deployment misconfiguration, not the voter's mistake — but still
        # worth naming plainly rather than a bare 500, since the operator
        # reading logs needs to know which env var is missing.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc
    return SaveProfileResponse(version=row.version, locale=row.locale, updated_at=row.updated_at)


@router.post(
    "/profiles/{user_id}/follow/{person_id}",
    response_model=FollowResponse,
    summary="Follow an incumbent (story 15.1)",
    description=(
        "Adds person_id to the voter's followed persons and saves — a "
        "read-modify-write through the same encrypted VoterProfileService "
        "path every other profile change takes (datamodel.md: 'Follows are "
        "stored inside the encrypted profile payload'), never a second, "
        "unencrypted storage path. Rejects a person_id that is not a live "
        "row in the people table with a 404, consistent with the "
        "repository layer's reference-validation rule (story 1.4). "
        "Idempotent: following someone already followed just re-saves the "
        "same list."
    ),
    responses={
        404: {"description": "person_id is not a known person"},
        422: {
            "description": "The voter has no user_id (anonymous mode), or no "
            "saved profile with at least one primary interest to attach a "
            "follow to"
        },
    },
)
def follow_person(
    service: Annotated[VoterProfileService, Depends(profile_service)],
    user_id: str,
    person_id: str,
) -> FollowResponse:
    try:
        profile = service.follow(user_id, person_id)
    except PersonNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AnonymousModeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"could not save the follow: {_safe_error_summary(exc)}",
        ) from exc
    return FollowResponse(followed_person_ids=profile.followed_person_ids)


@router.delete(
    "/profiles/{user_id}/follow/{person_id}",
    response_model=FollowResponse,
    summary="Unfollow an incumbent (story 15.1)",
    description=(
        "Removes person_id from the voter's followed persons and saves. No "
        "existence check against the people table — unfollowing someone who "
        "no longer exists, or was never followed, always succeeds, unlike "
        "following. A voter with no saved profile has nothing to unfollow: "
        "this returns an empty list rather than erroring."
    ),
    responses={
        422: {"description": "The voter has no user_id (anonymous mode)"},
    },
)
def unfollow_person(
    service: Annotated[VoterProfileService, Depends(profile_service)],
    user_id: str,
    person_id: str,
) -> FollowResponse:
    try:
        profile = service.unfollow(user_id, person_id)
    except AnonymousModeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return FollowResponse(followed_person_ids=profile.followed_person_ids)


def _safe_error_summary(exc: ValidationError) -> str:
    """A validation error's field names and counts, never its values.

    `InterestProfile`'s own validators already write messages that name a
    tier or a count rather than a topic or stance id (see that model's
    docstring: "a message quoting the voter's interests would leak exactly
    what this story encrypts"). This wrapper trusts that discipline for the
    message text, and adds nothing of its own that could leak a value: only
    the dotted field path and the problem count, the same shape
    `Repository._validated` uses for its own error messages.
    """
    fields = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
    return f"{exc.error_count()} problem(s) in {fields or 'the profile'}: {exc.errors()[0]['msg']}"


__all__ = [
    "FollowResponse",
    "LoadedInterest",
    "LoadedProfile",
    "SaveProfileRequest",
    "SaveProfileResponse",
    "TierWeightsResponse",
    "follow_person",
    "get_tier_weights",
    "load_profile",
    "methodology_service",
    "profile_service",
    "router",
    "save_profile",
    "unfollow_person",
    "zerodb_client_for_profiles",
]
