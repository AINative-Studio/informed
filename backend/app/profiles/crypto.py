"""Envelope encryption and HMAC row ids for voter profiles (story 14.1).

===============================================================================
THE KEY-MANAGEMENT DECISION — READ THIS FIRST
===============================================================================

datamodel.md's open questions include: *"Key management for `voter_profiles`:
AINative KMS, or a cloud KMS?"* **That question is still open.** No AINative KMS
integration exists, and provisioning an AWS/GCP KMS key is infrastructure work
outside this story — the same boundary story 1.6 drew around ZeroDB project
provisioning, and story 1.2 around MCP-vs-REST.

So this module implements **a local envelope-encryption scheme that is a
stand-in for a real KMS**, shaped so the swap changes neither the data model nor
any caller:

    root KEK  (32 bytes, from $ZERODB_PROFILE_ROOT_KEY)
      │
      ├─ HKDF(info="informed/profiles/id-hmac/v1") ─────► id HMAC key
      │                                                   (vp:{hmac(user_id)})
      │
      └─ HKDF(info="informed/profiles/wrap/v1", salt=per-user) ─► per-user KEK
                                                                     │
      per-user DEK (32 random bytes, AES-256-GCM)  ◄── unwrapped by ─┘
                      │
                      └─► encrypts the profile payload

**This is a real envelope scheme, not a toy.** The data key is 32 random bytes
per voter, generated once and never derived from anything reproducible; what is
stored is that key *wrapped* under a per-user KEK. The property that matters for
`delete` is exactly what a KMS gives you: because the DEK is random rather than
derived, destroying the wrap destroys the key, and the ciphertext becomes
unrecoverable *even to someone holding the root key and a database dump*.

A simpler scheme — DEK = HKDF(root, user_id) — was rejected for that reason
alone. It round-trips identically, it is one fewer row, and it **cannot
implement delete**: anyone with the root key can re-derive the key for any user
id forever, so "the key is shredded" would be a false claim about a ciphertext
that a backup still holds. datamodel.md's retention rule is "key shredded on
delete", and that is only true of a key that was never derivable.

### FOLLOW-UP, TRACKED (not resolved by this story)

Replace the root key with a real KMS. What that costs:

  * `ProfileRootKey` becomes a KMS client; `ProfileKeyring.wrap_key_for` calls
    `Encrypt`/`Decrypt` (or `GenerateDataKey`) instead of HKDF + local AES.
  * `KEY_REF` changes from `local-envelope:…` to `kms:informed/users/v1` —
    which is why every row records its own `key_ref`: a mixed table can be
    migrated row by row, and a row wrapped under the old scheme is
    identifiable rather than guessed at.
  * Nothing in `models`, `service`, the API or the stored row shape changes.

What this stand-in does **not** give you, and a KMS would: hardware-backed key
custody, audited key use, automatic rotation, and a root key that application
memory never holds. Until then the root key is an ordinary environment secret,
so an attacker who reads the process environment *and* the key-wrap table can
decrypt profiles. That is a real and acknowledged weaker boundary than the
acceptance criterion's "via KMS" implies — it is the one part of 14.1 that the
infrastructure, not this code, has to finish.

===============================================================================
HMAC ROW IDS
===============================================================================

`vp:{hmac_sha256(id_key, user_id)}` — datamodel.md's documented pattern, so
that a database dump cannot be joined back to accounts. Deterministic (the same
voter always reaches the same row, with no lookup table to leak) and one-way
(the digest is not reversible, and without the id key an attacker cannot even
test a guessed user id against it, which a bare SHA-256 would permit).

===============================================================================
WHAT NEVER GETS LOGGED
===============================================================================

Nothing in this module logs. Not at debug, not on an error path. There is no
logger and no f-string carrying a key, a digest, a user id or a payload — the
acceptance criterion is "plaintext never logged", and the way to be sure of that
is to have nothing to audit. `ProfileRootKey.__repr__` is overridden for the
same reason: a settings object in a traceback must not take the key with it.
`tests/test_profiles_crypto_unit.py` captures logs across a full key lifecycle
and asserts the key material is absent, so a future `logger.debug` here fails
CI rather than leaking quietly.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
from hashlib import sha256
from typing import Any, ClassVar

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict

from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBNotFoundError

#: Environment variable holding the root KEK, base64 or hex, 32 bytes decoded.
#: A placeholder for a real KMS — see the module docstring. Documented in
#: `.env.example` and `docs/environments.md`.
ROOT_KEY_ENV_VAR = "ZERODB_PROFILE_ROOT_KEY"

#: AES-256 means a 256-bit key. Not configurable: the acceptance criterion
#: names AES-256-GCM, and a shorter key would satisfy the code and not the
#: criterion.
DATA_KEY_BYTES = 32

#: 96 bits, the only nonce length AES-GCM is specified for. A fresh one per
#: encryption, from `os.urandom`: a nonce reused under one key leaks the XOR of
#: the two plaintexts, which for two versions of one voter's profile would be
#: close to total disclosure.
NONCE_BYTES = 12

#: Per-user HKDF salt length. Random per user, stored beside the wrap.
WRAP_SALT_BYTES = 16

#: What a stored row's `key_ref` says about how it was wrapped. The prefix is
#: the scheme, so a migration to a real KMS can tell a locally-wrapped row from
#: a KMS-wrapped one instead of assuming. datamodel.md's example reads
#: `kms:informed/users/v1`, which is what this becomes once a KMS is wired in.
KEY_REF = "local-envelope:informed/profiles/v1"

#: HKDF `info` strings. Domain separation: the id HMAC key and a wrap KEK are
#: derived from the same root and must be unrelated, so that exposing one does
#: not hand over the other.
_INFO_ID_HMAC = b"informed/profiles/id-hmac/v1"
_INFO_WRAP = b"informed/profiles/wrap/v1"

#: AAD for a wrapped data key: binds the wrap to the row it belongs to, so a
#: wrap row cannot be copied onto another voter's key row and still unwrap.
_WRAP_AAD_PREFIX = b"informed/profiles/wrap/"

#: The table holding wrapped per-user data keys. Separate from
#: `voter_profiles` on purpose: delete removes the key row, and a key stored
#: inside the profile row would be destroyed by the same write that is supposed
#: to be revocable independently — plus datamodel.md keeps `voter_profiles` to
#: exactly the seven documented fields.
KEY_WRAP_TABLE = "voter_profile_keys"


class RootKeyMissingError(RuntimeError):
    """`$ZERODB_PROFILE_ROOT_KEY` is unset or unusable.

    Raised rather than falling back to a generated or constant key. A default
    key is not encryption: every deployment would share it, and a profile
    "encrypted" under it is readable by anyone with the source. Refusing to
    start is the correct failure for a store of special-category data.
    """


class ProfileRootKey:
    """The root KEK every per-user key is derived or wrapped under.

    A stand-in for a KMS root key — see the module docstring. Deliberately a
    plain class rather than a Pydantic model: the material must never be
    serialized, and a model would happily `model_dump()` it.
    """

    __slots__ = ("_material",)

    def __init__(self, material: bytes) -> None:
        if len(material) != DATA_KEY_BYTES:
            raise ValueError(
                f"the profile root key must be exactly {DATA_KEY_BYTES} bytes "
                f"(AES-256); got {len(material)}"
            )
        self._material = material

    @classmethod
    def from_env(cls, env_var: str = ROOT_KEY_ENV_VAR) -> ProfileRootKey:
        """Read the root key from the environment, base64 or hex.

        Both encodings are accepted because both are what a secret manager
        hands you, and guessing wrong is a 32-byte key that decodes to the
        wrong 32 bytes — which would encrypt successfully and decrypt nothing.

        Raises:
            RootKeyMissingError: unset, empty, or not a 32-byte value.
        """
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            raise RootKeyMissingError(
                f"${env_var} is not set. Voter profiles are special-category "
                "political-opinion data and are never stored unencrypted, so there is "
                "no default and no fallback. Generate one with "
                '`python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"` '
                "and see docs/environments.md."
            )
        material = _decode_key(raw)
        if material is None:
            raise RootKeyMissingError(
                f"${env_var} is set but is not a 32-byte key in base64 or hex. "
                "Its value is not echoed here on purpose."
            )
        return cls(material)

    @property
    def material(self) -> bytes:
        """The raw root key. Only `ProfileKeyring` should reach for this."""
        return self._material

    def derive(self, *, info: bytes, salt: bytes | None = None, length: int = 32) -> bytes:
        """One subkey from the root, by purpose.

        HKDF-SHA256 rather than a bare hash: it is the standard construction
        for exactly this (one high-entropy secret, many purpose-separated
        subkeys), and `info` is what keeps the id-HMAC key and a wrap KEK
        unrelated.
        """
        return HKDF(algorithm=SHA256(), length=length, salt=salt, info=info).derive(self._material)

    def __repr__(self) -> str:
        """Never the key. A settings object lands in tracebacks and log lines."""
        return "ProfileRootKey(<redacted>)"

    __str__ = __repr__


class WrappedDataKeyRow(BaseModel):
    """One voter's wrapped data key, as stored.

    Carries no user id and no plaintext key: the row id is the same HMAC the
    profile row uses, and `wrapped_key` is the DEK sealed under a per-user KEK.
    A database dump of this table yields neither who the voters are nor any key.

    Deleting this row is what "shreds the key" means (`ProfileKeyring.shred`).
    """

    model_config = ConfigDict(extra="forbid")

    table_name: ClassVar[str] = KEY_WRAP_TABLE

    id: str
    #: base64(nonce || AES-GCM ciphertext || tag) of the 32-byte data key.
    wrapped_key: str
    #: base64 of the per-user HKDF salt. Public by design: a salt is not a
    #: secret, it is what stops one derivation from serving every user.
    salt: str
    key_ref: str = KEY_REF
    created_at: str | None = None

    def row_data(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ProfileCipher:
    """AES-256-GCM over one voter's profile payload, under one data key.

    The row id goes in as additional authenticated data, so a payload cannot be
    lifted onto a different voter's row and still decrypt — a protection that
    costs nothing and closes a real attack on a store where the operator can
    edit rows.
    """

    __slots__ = ("_aesgcm",)

    def __init__(self, data_key: bytes) -> None:
        if len(data_key) != DATA_KEY_BYTES:
            raise ValueError(f"a profile data key must be {DATA_KEY_BYTES} bytes (AES-256)")
        self._aesgcm = AESGCM(data_key)

    def encrypt(self, payload: dict[str, Any], *, aad: str) -> str:
        """`base64(nonce || ciphertext || tag)` for one profile payload.

        The nonce travels with the ciphertext rather than being stored in its
        own column: one opaque string is one thing to get right, and a nonce in
        a separate field is a field some future migration can drop.

        `sort_keys` + no whitespace so the same profile serializes identically
        every time — which keeps the ciphertext length a function of the
        content and not of dict ordering.
        """
        plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._aesgcm.encrypt(nonce, plaintext, aad.encode("utf-8"))
        return base64.b64encode(nonce + sealed).decode("ascii")

    def decrypt(self, token: str, *, aad: str) -> dict[str, Any]:
        """The payload back, or `InvalidTag` if anything was altered.

        No exception here is caught and softened. A tampered or wrong-key
        payload must raise: returning an empty profile would silently re-score
        a voter against interests they never set.
        """
        raw = base64.b64decode(token)
        nonce, sealed = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
        plaintext = self._aesgcm.decrypt(nonce, sealed, aad.encode("utf-8"))
        decoded: dict[str, Any] = json.loads(plaintext.decode("utf-8"))
        return decoded


class ProfileKeyring:
    """Per-user data keys and HMAC row ids, over one ZeroDB project.

    The KMS-shaped seam. Every method here is one a real KMS would provide, so
    the migration described in the module docstring replaces this class's
    internals and nothing above it:

        row_id(user_id)          deterministic, un-reversible row locator
        ensure_data_key(user_id) GenerateDataKey — create once, then unwrap
        data_key(user_id)        Decrypt — unwrap the stored DEK
        shred(user_id)           ScheduleKeyDeletion — destroy the wrap

    Holds no per-request state and is safe to share, like `ZeroDBClient`.
    """

    def __init__(self, *, client: ZeroDBClient, root_key: ProfileRootKey) -> None:
        self._client = client
        self._root = root_key
        # Derived once: the id HMAC key is the same for every row, and
        # re-running HKDF per request buys nothing.
        self._id_key = root_key.derive(info=_INFO_ID_HMAC, length=32)

    @property
    def key_ref(self) -> str:
        """What to record on a row wrapped by this keyring."""
        return KEY_REF

    @property
    def id_key(self) -> bytes:
        """The HMAC key behind every row id. Exposed for the test that asserts
        it is not the same key that wraps data."""
        return self._id_key

    # --- row ids ----------------------------------------------------------

    def row_id(self, user_id: str) -> str:
        """`vp:{hmac_sha256(id_key, user_id)}` — datamodel.md's pattern.

        Keyed, not a bare hash: user ids come from a small, guessable space
        (emails, sequential ids), so an unkeyed digest is reversible by
        dictionary attack in minutes. With the key held outside the database,
        a dump cannot be tested against a guess at all.

        Raises:
            ValueError: an empty user id, which would give every caller the
                same row.
        """
        if not user_id:
            raise ValueError("a profile row id needs a non-empty user id")
        digest = hmac.new(self._id_key, user_id.encode("utf-8"), sha256).hexdigest()
        return f"vp:{digest}"

    def wrap_key_for(self, user_id: str, *, salt: bytes | None = None) -> bytes:
        """The KEK that wraps one voter's data key.

        Derived from the root key, this voter's row id and a per-user salt, so
        one voter's wrap cannot be unwrapped with another's KEK even if the
        salts were somehow equal.
        """
        if salt is None:
            salt = self._salt_for(user_id)
        return self._root.derive(
            info=_INFO_WRAP + b"/" + self.row_id(user_id).encode("ascii"),
            salt=salt,
            length=DATA_KEY_BYTES,
        )

    # --- data keys --------------------------------------------------------

    def ensure_data_key(self, user_id: str) -> bytes:
        """This voter's data key, created and stored if there is none yet.

        The key is **32 random bytes**, not a derivation. That is the whole
        basis of `shred`: see the module docstring. Created once per voter and
        reused across saves, so re-encrypting a profile never invalidates an
        in-flight read.

        Not atomic — two concurrent first saves could both generate a key and
        the second would win, orphaning the first ciphertext. ZeroDB offers no
        conditional write (datamodel.md's open questions), the window is one
        voter's very first save, and the loser's row is rewritten by that same
        save, so the outcome is a consistent profile under the surviving key
        rather than a readable one under neither.
        """
        existing = self.stored_wrap(user_id)
        if existing is not None:
            return self._unwrap(user_id, existing)

        data_key = os.urandom(DATA_KEY_BYTES)
        salt = os.urandom(WRAP_SALT_BYTES)
        kek = self.wrap_key_for(user_id, salt=salt)
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(kek).encrypt(nonce, data_key, self._wrap_aad(user_id))
        row = WrappedDataKeyRow(
            id=self.row_id(user_id),
            wrapped_key=base64.b64encode(nonce + sealed).decode("ascii"),
            salt=base64.b64encode(salt).decode("ascii"),
        )
        self._client.upsert_row(KEY_WRAP_TABLE, row.id, row.row_data())
        return data_key

    def data_key(self, user_id: str) -> bytes:
        """This voter's data key, or `LookupError` when it has been shredded.

        `LookupError` rather than None, deliberately: after a delete there is
        no key, and a caller that treated that as "no interests" would hand a
        voter an empty profile instead of telling them their data is gone.
        """
        wrap = self.stored_wrap(user_id)
        if wrap is None:
            raise LookupError("no data key is stored for this profile")
        return self._unwrap(user_id, wrap)

    def stored_wrap(self, user_id: str) -> WrappedDataKeyRow | None:
        """The stored wrap row, or None. Reads nothing into a log line."""
        row = self._client.get_row(KEY_WRAP_TABLE, self.row_id(user_id))
        if row is None:
            return None
        return WrappedDataKeyRow.model_validate(row.row_data)

    def shred(self, user_id: str) -> None:
        """Destroy this voter's data key: delete the wrap row.

        **What "shredded" means here, exactly.** The data key existed in two
        places: as 32 random bytes inside this process while a request was
        being served, and as the wrapped copy in `voter_profile_keys`. The
        in-process copy is garbage after the request. Deleting the wrap removes
        the only persistent copy, and because the key was *random rather than
        derived*, nothing — not the root key, not a backup of
        `voter_profiles`, not this code — can reconstruct it. Any ciphertext
        that survives in a snapshot is then permanently undecryptable.

        **What it does not claim.** Not a cryptographic erase of the bytes on
        ZeroDB's disks: whether a deleted row's pages are overwritten is
        ZeroDB's business, not Informed's, which is exactly why the scheme is
        built so that recovering the row would yield a wrapped key an attacker
        still cannot unwrap without the root key — and why the root key
        belongs in a KMS (the tracked follow-up above).

        Idempotent: a missing row is success, because prd.md's one-tap delete
        must survive a retry.
        """
        try:
            self._client.delete_row(KEY_WRAP_TABLE, self.row_id(user_id))
        except ZeroDBNotFoundError:
            return

    # --- internals --------------------------------------------------------

    def _unwrap(self, user_id: str, wrap: WrappedDataKeyRow) -> bytes:
        salt = base64.b64decode(wrap.salt)
        kek = self.wrap_key_for(user_id, salt=salt)
        raw = base64.b64decode(wrap.wrapped_key)
        nonce, sealed = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
        return AESGCM(kek).decrypt(nonce, sealed, self._wrap_aad(user_id))

    def _salt_for(self, user_id: str) -> bytes:
        """The stored salt for an existing wrap, for `wrap_key_for` without one.

        Falls back to a deterministic salt only when no wrap exists yet, so
        `wrap_key_for` is callable (and testable) before the first save without
        ever reusing a random salt it did not store.
        """
        wrap = self.stored_wrap(user_id)
        if wrap is not None:
            return base64.b64decode(wrap.salt)
        return sha256(self.row_id(user_id).encode("ascii")).digest()[:WRAP_SALT_BYTES]

    def _wrap_aad(self, user_id: str) -> bytes:
        return _WRAP_AAD_PREFIX + self.row_id(user_id).encode("ascii")

    def __repr__(self) -> str:
        return "ProfileKeyring(<redacted>)"


def _decode_key(raw: str) -> bytes | None:
    """32 bytes from a base64 or hex string, or None.

    Tried in that order because a 64-character hex string is also valid
    base64 — and decodes to 48 bytes, the wrong length — so the length check
    is what disambiguates rather than the encoding guess.
    """
    for decode in (_try_base64, _try_hex):
        material = decode(raw)
        if material is not None and len(material) == DATA_KEY_BYTES:
            return material
    return None


def _try_base64(raw: str) -> bytes | None:
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None


def _try_hex(raw: str) -> bytes | None:
    try:
        return bytes.fromhex(raw)
    except ValueError:
        return None


__all__ = [
    "DATA_KEY_BYTES",
    "KEY_REF",
    "KEY_WRAP_TABLE",
    "NONCE_BYTES",
    "ROOT_KEY_ENV_VAR",
    "ProfileCipher",
    "ProfileKeyring",
    "ProfileRootKey",
    "RootKeyMissingError",
    "WrappedDataKeyRow",
]
