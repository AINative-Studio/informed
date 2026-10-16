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
"""

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
    FollowResponse,
    LoadedInterest,
    LoadedProfile,
    SaveProfileRequest,
    SaveProfileResponse,
    TierWeightsResponse,
    follow_person,
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
PROFILE_TABLES = (VoterProfileRow.table_name, KEY_WRAP_TABLE)

__all__ = [
    "DEFAULT_METHODOLOGY_VERSION",
    "DEFAULT_TIER_WEIGHTS",
    "KEY_WRAP_TABLE",
    "PROFILE_TABLES",
    "AnonymousModeError",
    "FollowResponse",
    "Interest",
    "InterestProfile",
    "LoadedInterest",
    "LoadedProfile",
    "MethodologyVersion",
    "MethodologyVersionService",
    "PersonNotFoundError",
    "ProfileCipher",
    "ProfileKeyring",
    "ProfileRootKey",
    "RootKeyMissingError",
    "SaveProfileRequest",
    "SaveProfileResponse",
    "TierWeightsResponse",
    "VoterProfileRow",
    "VoterProfileService",
    "follow_person",
    "profile_service",
    "router",
    "unfollow_person",
]
