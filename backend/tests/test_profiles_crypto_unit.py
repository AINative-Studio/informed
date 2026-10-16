"""Unit tests for story 14.1's envelope encryption and HMAC row ids.

These cover the crypto primitives directly, where the BDD feature covers the
behaviour a voter sees. Both matter: a scheme that round-trips is not
necessarily a scheme that is *correct*, and the properties below are the ones
whose failure would be silent — a reused nonce, a ciphertext that accepts a
tampered byte, an id derived from the root key alone.
"""

from __future__ import annotations

import base64
import json
import logging
import os

import pytest
from cryptography.exceptions import InvalidTag

from app.bootstrap import TABLE_SCHEMAS
from app.profiles import PROFILE_TABLES
from app.profiles.crypto import (
    DATA_KEY_BYTES,
    KEY_REF,
    NONCE_BYTES,
    ProfileCipher,
    ProfileKeyring,
    ProfileRootKey,
    RootKeyMissingError,
)
from app.zerodb.client import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

ROOT = bytes(range(32))
USER = "synthetic-user-1"


@pytest.fixture
def client() -> ZeroDBClient:
    zdb = ZeroDBClient(
        transport=FakeZeroDBTransport(), project_id="proj-test", sleep=lambda _: None
    )
    # The fake 404s on an unknown table, as live ZeroDB does.
    for table in PROFILE_TABLES:
        zdb.create_table(table, TABLE_SCHEMAS[table])
    return zdb


@pytest.fixture
def keyring(client: ZeroDBClient) -> ProfileKeyring:
    return ProfileKeyring(client=client, root_key=ProfileRootKey(ROOT))


# --- the root key ---------------------------------------------------------


def test_root_key_requires_32_bytes() -> None:
    """AES-256 means a 256-bit key. A shorter one must not be silently padded."""
    with pytest.raises(ValueError, match="32 bytes"):
        ProfileRootKey(b"too short")


def test_root_key_from_env_reads_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERODB_PROFILE_ROOT_KEY", base64.b64encode(ROOT).decode())
    assert ProfileRootKey.from_env().material == ROOT


def test_root_key_from_env_accepts_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERODB_PROFILE_ROOT_KEY", ROOT.hex())
    assert ProfileRootKey.from_env().material == ROOT


def test_root_key_from_env_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse rather than invent a key: a default key is no encryption at all."""
    monkeypatch.delenv("ZERODB_PROFILE_ROOT_KEY", raising=False)
    with pytest.raises(RootKeyMissingError):
        ProfileRootKey.from_env()


def test_root_key_never_appears_in_its_repr() -> None:
    """A settings object in a traceback must not carry the key with it."""
    root = ProfileRootKey(ROOT)
    for text in (repr(root), str(root)):
        assert ROOT.hex() not in text
        assert base64.b64encode(ROOT).decode() not in text


# --- the cipher ----------------------------------------------------------


def test_payload_round_trips() -> None:
    key = os.urandom(DATA_KEY_BYTES)
    cipher = ProfileCipher(key)
    plaintext = {"interests": [], "evidence_mode": "blend"}
    token = cipher.encrypt(plaintext, aad="vp:abc")
    assert cipher.decrypt(token, aad="vp:abc") == plaintext


def test_ciphertext_is_not_the_plaintext() -> None:
    key = os.urandom(DATA_KEY_BYTES)
    token = ProfileCipher(key).encrypt({"topic": "test-topic-alpha"}, aad="vp:abc")
    raw = base64.b64decode(token)
    assert b"test-topic-alpha" not in raw


def test_each_encryption_uses_a_fresh_nonce() -> None:
    """A reused GCM nonce under one key leaks the XOR of two plaintexts."""
    key = os.urandom(DATA_KEY_BYTES)
    cipher = ProfileCipher(key)
    nonces = {
        base64.b64decode(cipher.encrypt({"n": index}, aad="vp:abc"))[:NONCE_BYTES]
        for index in range(200)
    }
    assert len(nonces) == 200


def test_a_tampered_ciphertext_is_rejected() -> None:
    """GCM authenticates: a flipped byte must fail, not decrypt to garbage."""
    key = os.urandom(DATA_KEY_BYTES)
    cipher = ProfileCipher(key)
    raw = bytearray(base64.b64decode(cipher.encrypt({"a": 1}, aad="vp:abc")))
    raw[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        cipher.decrypt(base64.b64encode(bytes(raw)).decode(), aad="vp:abc")


def test_the_row_id_is_bound_into_the_ciphertext() -> None:
    """The row id is the AAD, so one voter's payload cannot be moved onto
    another voter's row and still decrypt — even if an operator held both keys.
    """
    key = os.urandom(DATA_KEY_BYTES)
    cipher = ProfileCipher(key)
    token = cipher.encrypt({"a": 1}, aad="vp:aaa")
    with pytest.raises(InvalidTag):
        cipher.decrypt(token, aad="vp:bbb")


def test_another_key_cannot_decrypt() -> None:
    token = ProfileCipher(os.urandom(DATA_KEY_BYTES)).encrypt({"a": 1}, aad="vp:abc")
    with pytest.raises(InvalidTag):
        ProfileCipher(os.urandom(DATA_KEY_BYTES)).decrypt(token, aad="vp:abc")


# --- HMAC row ids ---------------------------------------------------------


def test_row_id_is_deterministic(keyring: ProfileKeyring) -> None:
    assert keyring.row_id(USER) == keyring.row_id(USER)


def test_row_id_matches_the_documented_pattern(keyring: ProfileKeyring) -> None:
    """datamodel.md: `vp:{hmac(user_id)}`."""
    row_id = keyring.row_id(USER)
    digest = row_id.removeprefix("vp:")
    assert row_id.startswith("vp:")
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)


def test_row_id_differs_per_user(keyring: ProfileKeyring) -> None:
    assert keyring.row_id(USER) != keyring.row_id("synthetic-user-2")


def test_row_id_depends_on_the_root_key(client: ZeroDBClient) -> None:
    """A dump plus the id scheme is not enough: without the key, no match."""
    first = ProfileKeyring(client=client, root_key=ProfileRootKey(ROOT))
    second = ProfileKeyring(client=client, root_key=ProfileRootKey(bytes(32)))
    assert first.row_id(USER) != second.row_id(USER)


def test_row_id_does_not_contain_the_user_id(keyring: ProfileKeyring) -> None:
    assert USER not in keyring.row_id(USER)


def test_id_key_is_not_the_wrap_key(keyring: ProfileKeyring) -> None:
    """Derived for different purposes, so neither one can stand in for the
    other if one is ever exposed.
    """
    assert keyring.id_key != keyring.wrap_key_for(USER)


def test_an_empty_user_id_is_refused(keyring: ProfileKeyring) -> None:
    """An empty subject would give every anonymous caller the same row."""
    with pytest.raises(ValueError, match="user id"):
        keyring.row_id("")


# --- envelope: per-user data keys -----------------------------------------


def test_each_user_gets_a_distinct_data_key(keyring: ProfileKeyring) -> None:
    first = keyring.ensure_data_key(USER)
    second = keyring.ensure_data_key("synthetic-user-2")
    assert first != second
    assert len(first) == DATA_KEY_BYTES


def test_a_data_key_is_stable_across_reads(keyring: ProfileKeyring) -> None:
    created = keyring.ensure_data_key(USER)
    assert keyring.data_key(USER) == created
    assert keyring.ensure_data_key(USER) == created


def test_the_stored_wrap_is_not_the_data_key(keyring: ProfileKeyring) -> None:
    """The envelope's whole point: what is stored is wrapped, not the key."""
    data_key = keyring.ensure_data_key(USER)
    wrap = keyring.stored_wrap(USER)
    assert wrap is not None
    serialized = json.dumps(wrap.row_data())
    assert base64.b64encode(data_key).decode() not in serialized
    assert data_key.hex() not in serialized


def test_the_wrap_row_carries_no_user_id(keyring: ProfileKeyring) -> None:
    keyring.ensure_data_key(USER)
    wrap = keyring.stored_wrap(USER)
    assert wrap is not None
    assert USER not in json.dumps(wrap.row_data())


def test_reading_an_absent_data_key_raises(keyring: ProfileKeyring) -> None:
    """Not None: "no key" after a shred must be loud, never an empty profile."""
    with pytest.raises(LookupError):
        keyring.data_key("synthetic-user-never-saved")


def test_shredding_makes_the_data_key_unrecoverable(keyring: ProfileKeyring) -> None:
    """The acceptance criterion for delete: holding the root key AND the
    ciphertext is not enough once the wrap is gone.
    """
    data_key = keyring.ensure_data_key(USER)
    token = ProfileCipher(data_key).encrypt({"a": 1}, aad=keyring.row_id(USER))

    keyring.shred(USER)

    assert keyring.stored_wrap(USER) is None
    with pytest.raises(LookupError):
        keyring.data_key(USER)
    # And a fresh key for the same user does not open the old ciphertext.
    replacement = keyring.ensure_data_key(USER)
    assert replacement != data_key
    with pytest.raises(InvalidTag):
        ProfileCipher(replacement).decrypt(token, aad=keyring.row_id(USER))


def test_shredding_an_absent_key_is_a_no_op(keyring: ProfileKeyring) -> None:
    """Delete must be idempotent: a retried one-tap delete cannot error."""
    keyring.shred("synthetic-user-never-saved")


def test_key_ref_names_the_scheme(keyring: ProfileKeyring) -> None:
    """The row records which scheme wrapped it, so a later real-KMS migration
    can tell a locally-wrapped row from a KMS-wrapped one.
    """
    assert keyring.key_ref == KEY_REF
    assert "local" in KEY_REF


# --- edge cases the live path can actually reach --------------------------


def test_a_root_key_of_the_wrong_length_in_env_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 16-byte value is a plausible paste error and must not become a key.

    It decodes cleanly and only the length check catches it — and were that
    check a silent pad instead, everything would be encrypted under 128 bits
    while the code claimed 256.
    """
    monkeypatch.setenv("ZERODB_PROFILE_ROOT_KEY", base64.b64encode(os.urandom(16)).decode())
    with pytest.raises(RootKeyMissingError, match="32-byte"):
        ProfileRootKey.from_env()


def test_a_root_key_that_is_neither_base64_nor_hex_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERODB_PROFILE_ROOT_KEY", "not-a-key-at-all!!!")
    with pytest.raises(RootKeyMissingError):
        ProfileRootKey.from_env()


def test_the_refusal_message_does_not_echo_the_bad_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mistyped prod key is still a prod key, and this message reaches logs."""
    monkeypatch.setenv("ZERODB_PROFILE_ROOT_KEY", "deadbeef" * 3)
    with pytest.raises(RootKeyMissingError) as caught:
        ProfileRootKey.from_env()
    assert "deadbeef" not in str(caught.value)


def test_a_data_key_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        ProfileCipher(os.urandom(16))


def test_the_keyring_repr_carries_no_secrets(keyring: ProfileKeyring) -> None:
    assert ROOT.hex() not in repr(keyring)
    assert base64.b64encode(ROOT).decode() not in repr(keyring)


def test_wrap_key_for_reuses_the_stored_salt(keyring: ProfileKeyring) -> None:
    """Once a wrap exists its stored salt derives the KEK — otherwise a key
    could be wrapped under one KEK and unwrapped with another.
    """
    keyring.ensure_data_key(USER)
    wrap = keyring.stored_wrap(USER)
    assert wrap is not None
    salt = base64.b64decode(wrap.salt)
    assert keyring.wrap_key_for(USER) == keyring.wrap_key_for(USER, salt=salt)


def test_wrap_key_for_works_before_a_first_save(keyring: ProfileKeyring) -> None:
    """Callable with no stored wrap, from a deterministic placeholder salt, so
    it never wraps under a random salt it did not store.
    """
    assert len(keyring.wrap_key_for("synthetic-user-never-saved")) == DATA_KEY_BYTES


# --- no plaintext, no key material, in logs -------------------------------


def test_the_crypto_layer_logs_no_key_material(
    keyring: ProfileKeyring, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        data_key = keyring.ensure_data_key(USER)
        keyring.data_key(USER)
        keyring.row_id(USER)
        keyring.shred(USER)
    text = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (
        ROOT.hex(),
        base64.b64encode(ROOT).decode(),
        data_key.hex(),
        base64.b64encode(data_key).decode(),
        USER,
    ):
        assert secret not in text
