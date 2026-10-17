"""Encrypted voter-profile storage (story 14.1), tiers/dealbreaker/load (4.2-4.4).

A voter's interests are special-category political-opinion data under GDPR
(prd.md, "Privacy"). This package is the only place they are written or read,
and the only place a decrypted profile exists: everything above it works with
`InterestProfile` objects, everything below it sees ciphertext.

  * `models` — the Pydantic v2 shapes: the outer `voter_profiles` row and the
    decrypted `InterestProfile` payload, with datamodel.md's limits enforced
    (1-5 primary, up to 8 secondary, dealbreaker allowed only on primary —
    see that module's docstring; stories 4.2 and 4.3's limits were already
    built here by 14.1).
  * `crypto` — envelope encryption (per-user AES-256-GCM data keys) and the
    HMAC row ids.
  * `service` — encrypt-before-write, decrypt-after-read, version bump per
    save, and the delete that shreds the key. `load()` is story 4.4's save/
    load round trip; this story adds the HTTP route onto it.
  * `methodology` — story 4.2's tier-weight reader: `GET /v1/profiles/methodology`
    reads live from `methodology_versions` rather than a hard-coded constant.
  * `consent` — story 14.2's append-only GDPR special-category consent ledger
    and the versioned consent wording. The record of *what* a voter agreed to
    and *when*, which story 4.1's `consent: true` request flag could not be.
"""

from app.profiles.consent import (
    CONSENT_TABLE,
    CONSENT_VERSIONS,
    CURRENT_CONSENT_VERSION,
    LAWFUL_BASIS,
    ConsentLedger,
    ConsentOffer,
    ConsentRecord,
    ConsentText,
    UnknownConsentVersionError,
    consent_offer,
    consent_record_id,
)
from app.profiles.crypto import (
    KEY_WRAP_TABLE,
    ProfileCipher,
    ProfileKeyring,
    ProfileRootKey,
    RootKeyMissingError,
)
from app.profiles.methodology import (
    DEFAULT_METHODOLOGY_VERSION,
    DEFAULT_TIER_WEIGHTS,
    MethodologyVersion,
    MethodologyVersionService,
)
from app.profiles.models import Interest, InterestProfile, VoterProfileRow
from app.profiles.router import (
    ConsentRecordResponse,
    DeleteProfileResponse,
    FollowResponse,
    LoadedInterest,
    LoadedProfile,
    ProfileExportResponse,
    SaveProfileRequest,
    SaveProfileResponse,
    TierWeightsResponse,
    delete_profile,
    export_profile,
    follow_person,
    get_consent_offer,
    profile_service,
    router,
    unfollow_person,
)
from app.profiles.service import AnonymousModeError, PersonNotFoundError, VoterProfileService

#: The two tables profile storage touches, in the order they must exist. Named
#: here so a test fixture and the live verification script cannot disagree with
#: `app.bootstrap.TABLE_SCHEMAS` about which tables the feature needs — a
#: disagreement that shows up as a 404 from the real API and as a green test
#: against a fake that invented the table.
#: Story 14.2 adds the consent ledger to that list. Three tables now, still
#: named in one place so a fixture, the bootstrap schema and the live
#: verification script cannot disagree about which the feature needs.
PROFILE_TABLES = (VoterProfileRow.table_name, KEY_WRAP_TABLE, CONSENT_TABLE)

__all__ = [
    "CONSENT_TABLE",
    "CONSENT_VERSIONS",
    "CURRENT_CONSENT_VERSION",
    "DEFAULT_METHODOLOGY_VERSION",
    "DEFAULT_TIER_WEIGHTS",
    "KEY_WRAP_TABLE",
    "LAWFUL_BASIS",
    "PROFILE_TABLES",
    "AnonymousModeError",
    "ConsentLedger",
    "ConsentOffer",
    "ConsentRecord",
    "ConsentRecordResponse",
    "ConsentText",
    "DeleteProfileResponse",
    "FollowResponse",
    "Interest",
    "InterestProfile",
    "LoadedInterest",
    "LoadedProfile",
    "MethodologyVersion",
    "MethodologyVersionService",
    "PersonNotFoundError",
    "ProfileCipher",
    "ProfileExportResponse",
    "ProfileKeyring",
    "ProfileRootKey",
    "RootKeyMissingError",
    "SaveProfileRequest",
    "SaveProfileResponse",
    "TierWeightsResponse",
    "UnknownConsentVersionError",
    "VoterProfileRow",
    "VoterProfileService",
    "consent_offer",
    "consent_record_id",
    "delete_profile",
    "export_profile",
    "follow_person",
    "get_consent_offer",
    "profile_service",
    "router",
    "unfollow_person",
]
