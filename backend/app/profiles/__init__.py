"""Encrypted voter-profile storage (story 14.1).

A voter's interests are special-category political-opinion data under GDPR
(prd.md, "Privacy"). This package is the only place they are written or read,
and the only place a decrypted profile exists: everything above it works with
`InterestProfile` objects, everything below it sees ciphertext.

  * `models` — the Pydantic v2 shapes: the outer `voter_profiles` row and the
    decrypted `InterestProfile` payload, with datamodel.md's limits enforced.
  * `crypto` — envelope encryption (per-user AES-256-GCM data keys) and the
    HMAC row ids.
  * `service` — encrypt-before-write, decrypt-after-read, version bump per
    save, and the delete that shreds the key.
"""

from app.profiles.crypto import (
    KEY_WRAP_TABLE,
    ProfileCipher,
    ProfileKeyring,
    ProfileRootKey,
    RootKeyMissingError,
)
from app.profiles.models import Interest, InterestProfile, VoterProfileRow
from app.profiles.router import SaveProfileRequest, SaveProfileResponse, profile_service, router
from app.profiles.service import AnonymousModeError, VoterProfileService

#: The two tables profile storage touches, in the order they must exist. Named
#: here so a test fixture and the live verification script cannot disagree with
#: `app.bootstrap.TABLE_SCHEMAS` about which tables the feature needs — a
#: disagreement that shows up as a 404 from the real API and as a green test
#: against a fake that invented the table.
PROFILE_TABLES = (VoterProfileRow.table_name, KEY_WRAP_TABLE)

__all__ = [
    "KEY_WRAP_TABLE",
    "PROFILE_TABLES",
    "AnonymousModeError",
    "Interest",
    "InterestProfile",
    "ProfileCipher",
    "ProfileKeyring",
    "ProfileRootKey",
    "RootKeyMissingError",
    "SaveProfileRequest",
    "SaveProfileResponse",
    "VoterProfileRow",
    "VoterProfileService",
    "profile_service",
    "router",
]
