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

Consent: the gate (story 4.1) and the record (story 14.2)
==========================================================

ui-prd.md 2.2: "Before any interest is saved to an account, show the GDPR
special-category consent language and the anonymous-mode alternative, side by
side, as an actual choice — not a pre-checked box. This blocks profile save."

Story 4.1 built the **gate**: `POST /v1/profiles` requires `consent: true` and
refuses anything else with a 422 naming consent — the same shape of error as a
missing address in `GET /v1/ballot`, because the caller sent something unusable
and theirs to fix. That satisfied "no save without it" and explicitly deferred
the rest, predicting that "14.2 can add a `consent_version` field to this same
request later without this story's gate changing shape". That is exactly what
happened, and the gate did not change shape:

  * `GET /v1/profiles/consent` publishes the wording a voter is being asked to
    agree to, its version, and — in the same payload, so a client cannot show
    one without the other — the anonymous-mode alternative.
  * `POST /v1/profiles` now also takes `consent_version`, the version the
    client actually displayed, and an unknown version is a 422 rather than a
    stored record pointing at wording nobody can reproduce.
  * Each save appends an auditable record to the append-only consent ledger.
    See `app.profiles.consent`'s module docstring for the table's design and
    why it holds neither a user id nor an interest.

Export and delete (story 14.4)
===============================

`GET /v1/profiles/{user_id}/export` returns the voter's own profile decrypted,
as one JSON document, with its consent history. `DELETE /v1/profiles/{user_id}`
deletes the row, shreds the data key and records the withdrawal. Both are the
"one action" prd.md promises, and both run through `VoterProfileService` —
14.1 already built the mechanism; this story gives it an address.

Decrypting for the export is not a weakening of 14.1. That story's encryption
protects the profile from whoever holds the database, which is a different
party from the person whose profile it is: an export the voter cannot read
would satisfy no data-subject right at all.

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

import time
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.profiles.consent import (
    CURRENT_CONSENT_VERSION,
    ConsentOffer,
    UnknownConsentVersionError,
    consent_offer,
)
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
    consent_version: str = Field(
        default=CURRENT_CONSENT_VERSION,
        min_length=1,
        description=(
            "The version of the consent wording this client actually showed "
            "the voter — read it from GET /v1/profiles/consent and echo it "
            "back here (story 14.2). A version this release cannot resolve is "
            "refused with a 422: a stored record naming wording nobody can "
            "reproduce would read as proof of consent while proving nothing. "
            "Defaulted to the current version so story 4.1's existing clients "
            "keep working, but a client that renders the consent screen should "
            "always send the version it rendered."
        ),
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


class ConsentRecordResponse(BaseModel):
    """One consent event as it appears in an export (story 14.2).

    Mirrors `app.profiles.consent.ConsentRecord` minus `profile_ref`: the HMAC
    is how the server finds the row, and echoing it into a document the voter
    downloads and may forward would publish a stable per-voter handle for no
    benefit to the reader — they already know whose export it is.
    """

    model_config = ConfigDict(extra="forbid")

    consent_version: str = Field(description="Which version of the wording was agreed")
    action: str = Field(description='"granted" or "withdrawn"')
    lawful_basis: str = Field(description="The GDPR basis claimed, e.g. Article 9(2)(a)")
    recorded_at: str = Field(description="When this event was recorded, ISO-8601 UTC")


class ProfileExportResponse(BaseModel):
    """The body of `GET /v1/profiles/{user_id}/export` (story 14.4).

    A self-describing document rather than a bare profile: a file in a
    downloads folder has to say what it is, which version it captured and when
    — see `VoterProfileService.export_document`'s docstring for each field and
    for why `key_ref` is deliberately absent.
    """

    model_config = ConfigDict(extra="forbid")

    exported_at: str = Field(description="When this export was produced, ISO-8601 UTC")
    version: int = Field(description="The profile version this export captured")
    updated_at: str | None = Field(description="When that version was saved")
    locale: str
    jurisdiction_ids: list[str]
    profile: dict[str, Any] = Field(
        description=(
            "The voter's own decrypted interests: interests (topic_id, "
            "stance_id, tier, intensity, dealbreaker, note), evidence_mode, "
            "followed_person_ids and the change-date history."
        )
    )
    consent: list[ConsentRecordResponse] = Field(
        description="The append-only consent history for this profile, oldest first"
    )


class DeleteProfileResponse(BaseModel):
    """What `DELETE /v1/profiles/{user_id}` returns (story 14.4).

    Named booleans rather than a bare 204, because the acceptance criterion is
    two distinct guarantees ("removes the row and shreds the key") and a voter
    tapping an irreversible button deserves a response that states both
    happened rather than an empty body they have to trust. `elapsed_seconds`
    is reported for the same reason the criterion mentions a minute: the
    number is observable rather than asserted in a release note.
    """

    model_config = ConfigDict(extra="forbid")

    deleted: bool = Field(description="The profile row is gone (true even if it was already gone)")
    key_shredded: bool = Field(
        description=(
            "The wrapped data key is destroyed, so any retained ciphertext is "
            "permanently undecryptable — see app.profiles.crypto's "
            "ProfileKeyring.shred for exactly what that claims and what it "
            "does not."
        )
    )
    consent_withdrawal_recorded: bool = Field(
        description=(
            "A 'withdrawn' event was appended to the append-only consent "
            "ledger (story 14.2). The ledger holds no interests and no user "
            "id; see app.profiles.service.VoterProfileService.delete."
        )
    )
    elapsed_seconds: float = Field(description="How long the deletion took, measured server-side")


@router.get(
    "/profiles/consent",
    response_model=ConsentOffer,
    summary="The consent language a voter is asked to agree to (story 14.2)",
    description=(
        "Publishes the GDPR special-category consent wording, its version, "
        "and the anonymous-mode alternative — all in one payload, so a client "
        "cannot render the ask without the alternative beside it (ui-prd.md "
        "2.2: 'side by side, as an actual choice'). Echo `consent_version` "
        "back on POST /v1/profiles so the stored record names the wording "
        "this voter actually saw.\n\n"
        "Needs no identity: an anonymous voter reads this to decide whether "
        "to have an account at all, and reading it writes nothing."
    ),
    responses={404: {"description": "No such consent version in this release"}},
)
def get_consent_offer(version: str | None = None) -> ConsentOffer:
    """The consent wording, current by default.

    `version` is accepted so a client can re-display the exact wording an old
    record names — the reason superseded versions are never removed from
    `CONSENT_VERSIONS`. An unknown version is a 404 rather than a silent
    fallback to the current one: quietly showing different text than was asked
    for is how a voter ends up looking at wording they never agreed to.
    """
    try:
        return consent_offer(version)
    except UnknownConsentVersionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


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
            consent_version=body.consent_version,
        )
    except UnknownConsentVersionError as exc:
        # 422, not 404: the *request* is the thing that cannot be processed.
        # The usual cause is a PWA cached across a release showing last
        # version's wording, and the message tells the client to re-read
        # GET /v1/profiles/consent and ask again.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
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


@router.get(
    "/profiles/{user_id}/export",
    response_model=ProfileExportResponse,
    summary="Export everything stored for one voter, as JSON (story 14.4)",
    description=(
        "prd.md: 'One-tap export and permanent deletion.' Returns the voter's "
        "own decrypted interests, the stored row's metadata, and the "
        "append-only consent history, as one JSON document — the audit trail "
        "travels with the data rather than needing a second request.\n\n"
        "Decrypted on purpose: story 14.1's encryption protects the profile "
        "from whoever holds the database, which is not the same party as the "
        "person whose profile it is. An export the voter cannot read would "
        "satisfy no data-subject right at all.\n\n"
        "A voter with no stored profile gets a 404 — unlike GET "
        "/v1/profiles/{user_id}, which answers `null` with a 200. The two "
        "differ deliberately: a load asks 'is there a profile to edit', where "
        "'no' is an ordinary answer, while an export asks for a document, and "
        "an empty document is not one."
    ),
    responses={404: {"description": "No profile is stored for this voter"}},
)
def export_profile(
    service: Annotated[VoterProfileService, Depends(profile_service)],
    user_id: str,
) -> ProfileExportResponse:
    document = service.export_document(user_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "there is no stored profile to export. If you use Informed in "
                "anonymous mode your profile lives only in this browser and "
                "was never sent to us, so there is nothing here to export."
            ),
        )
    return ProfileExportResponse.model_validate(document)


@router.delete(
    "/profiles/{user_id}",
    response_model=DeleteProfileResponse,
    summary="Permanently delete one voter's profile and shred its key (14.4)",
    description=(
        "prd.md: 'One-tap export and permanent deletion.' datamodel.md: "
        "'Deleting a profile deletes the row and shreds the data key.' Both "
        "happen here, plus a 'withdrawn' event appended to the append-only "
        "consent ledger (story 14.2) — see "
        "app.profiles.service.VoterProfileService.delete for why the ledger "
        "survives the deletion and why that is still the data-minimising "
        "answer.\n\n"
        "Irreversible: the data key is 32 random bytes that were never "
        "derivable from anything, so once its wrap is gone no retained "
        "ciphertext — not a backup, not a snapshot — can be decrypted again, "
        "by anyone, including Informed's operators holding the root key. A "
        "client MUST confirm with the voter before calling this.\n\n"
        "Idempotent, and a 200 for a voter who never had a profile: 'it is "
        "gone' is the true answer in both cases, and a 404 would leave a "
        "voter who tapped delete twice wondering whether their data is still "
        "there."
    ),
)
def delete_profile(
    service: Annotated[VoterProfileService, Depends(profile_service)],
    user_id: str,
) -> DeleteProfileResponse:
    """Delete, then verify the deletion rather than reporting it on faith.

    `service.delete` is idempotent and raises nothing on a missing row, so a
    bare "deleted: true" would be true of a call that did nothing at all. The
    two reads after it are what make the response's claims checkable: the
    profile is actually unloadable, and the key wrap is actually gone. That is
    three ZeroDB calls for the delete and two for the verification, which is
    why the measured elapsed time is reported — story 14.4's "completes in
    under 1 minute" is then observable per call rather than assumed.
    """
    started = time.monotonic()
    service.delete(user_id)
    elapsed = time.monotonic() - started
    return DeleteProfileResponse(
        deleted=service.version_of(user_id) == 0,
        key_shredded=not service.has_data_key(user_id),
        consent_withdrawal_recorded=any(
            record.action == "withdrawn" for record in service.consent_history(user_id)
        ),
        elapsed_seconds=round(elapsed, 3),
    )


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
    "ConsentRecordResponse",
    "DeleteProfileResponse",
    "FollowResponse",
    "LoadedInterest",
    "LoadedProfile",
    "ProfileExportResponse",
    "SaveProfileRequest",
    "SaveProfileResponse",
    "TierWeightsResponse",
    "delete_profile",
    "export_profile",
    "follow_person",
    "get_consent_offer",
    "get_tier_weights",
    "load_profile",
    "methodology_service",
    "profile_service",
    "router",
    "save_profile",
    "unfollow_person",
    "zerodb_client_for_profiles",
]
