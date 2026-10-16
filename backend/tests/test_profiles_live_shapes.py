"""Live-confirmed facts about encrypted profile storage (story 14.1).

Same purpose as `test_repository_live_shapes.py`: each test here pins a
property that was **measured against the live "informed" project**
(`bb8c620a-…`) on 2026-10-16 via `backend/scripts/verify_profiles_live.py`,
rather than inferred from the fake. The run's full output is in the story's PR.

What the live run established, and why each matters enough to be locked in:

  1. **The row as stored in ZeroDB is genuinely ciphertext.** Read back through
     the raw client — no service, no decryption — the row's full JSON contained
     none of the synthetic topic ids, stance ids, the free-text note, or the
     user id. This is *the* acceptance criterion ("even the database operator
     can't read them") and the only one a live read can actually answer: a fake
     that stores whatever it is handed proves nothing about what the real API
     persists.

  2. **The payload survives trivial reversal.** Base64-decode, single-byte XOR
     across all 255 keys, and rot13 recovered nothing. "Encrypted" has to mean
     more than "not equal to the plaintext".

  3. **Delete genuinely destroys the data.** With the live root key in hand and
     the ciphertext retained in memory, decryption after the delete failed —
     and a freshly generated data key for the same voter did not open the old
     payload either. That is the property datamodel.md's retention rule ("key
     shredded on delete") claims, and the reason the data key is random rather
     than HKDF-derived: a derived key is re-derivable forever, so shredding it
     would be a false claim.

  4. **ZeroDB's live project needed `voter_profile_keys` created.** The live run
     reported `created missing table voter_profile_keys`, confirming the
     bootstrap change in this story is required rather than cosmetic, and that
     the table did not already exist under some other name.

  5. **One row per voter, not one per save.** The second save updated the
     existing row (live count stayed at 1) rather than appending — ZeroDB's
     `POST /rows` always appends (story 1.2), so this depends entirely on
     `upsert_row`'s find-then-replace and had to be confirmed live.

Everything below runs against the fake in CI; the live script is the thing that
measured it. Each test's docstring says what the live run showed.
"""

from __future__ import annotations

import base64
import codecs
import json

import pytest

from app.bootstrap import TABLE_SCHEMAS
from app.profiles import PROFILE_TABLES
from app.profiles.crypto import KEY_WRAP_TABLE, ProfileCipher, ProfileKeyring, ProfileRootKey
from app.profiles.models import VoterProfileRow
from app.profiles.service import VoterProfileService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

ROOT = bytes(range(32))
USER = "synthetic-live-shapes-user"
TOPIC_ID = "test:live-shapes-alpha:v1"
STANCE_ID = "test:live-shapes-alpha:v1:test-stance"
NOTE = "synthetic live shapes note"

#: Everything that must never appear in a stored row, in any reachable form.
SECRETS = (TOPIC_ID, STANCE_ID, NOTE, USER)


def synthetic_payload() -> dict[str, object]:
    return {
        "interests": [
            {
                "topic_id": TOPIC_ID,
                "stance_id": STANCE_ID,
                "tier": "primary",
                "intensity": 3,
                "dealbreaker": True,
                "note": NOTE,
            }
        ],
        "evidence_mode": "blend",
    }


@pytest.fixture
def client() -> ZeroDBClient:
    zdb = ZeroDBClient(
        transport=FakeZeroDBTransport(), project_id="proj-test", sleep=lambda _: None
    )
    for table in PROFILE_TABLES:
        zdb.create_table(table, TABLE_SCHEMAS[table])
    return zdb


@pytest.fixture
def keyring(client: ZeroDBClient) -> ProfileKeyring:
    return ProfileKeyring(client=client, root_key=ProfileRootKey(ROOT))


@pytest.fixture
def service(client: ZeroDBClient, keyring: ProfileKeyring) -> VoterProfileService:
    return VoterProfileService(client=client, keyring=keyring)


# --- 1. the stored row is ciphertext --------------------------------------


def test_the_stored_row_contains_no_plaintext(
    service: VoterProfileService, client: ZeroDBClient, keyring: ProfileKeyring
) -> None:
    """Live: the row's full JSON contained no topic, stance, note or user id.

    Read through the raw client deliberately. Going through `service.load`
    would decrypt and prove the opposite of what is being asserted.
    """
    service.save(USER, synthetic_payload())
    row = client.get_row(VoterProfileRow.table_name, keyring.row_id(USER))
    assert row is not None
    row_json = json.dumps(row.row_data)
    for secret in SECRETS:
        assert secret not in row_json, f"{secret!r} is stored in clear text"


def test_the_stored_payload_resists_trivial_reversal(
    service: VoterProfileService, client: ZeroDBClient, keyring: ProfileKeyring
) -> None:
    """Live: base64-decode, 255 single-byte XORs and rot13 recovered nothing.

    The three things an operator with the dump would try first. A payload that
    falls to any of them is obfuscated, not encrypted.
    """
    service.save(USER, synthetic_payload())
    row = client.get_row(VoterProfileRow.table_name, keyring.row_id(USER))
    assert row is not None
    raw = base64.b64decode(str(row.row_data["payload"]))

    candidates = [raw.decode("latin-1"), codecs.encode(raw.decode("latin-1"), "rot13")]
    candidates += [bytes(byte ^ key for byte in raw).decode("latin-1") for key in range(1, 256)]
    for secret in SECRETS:
        for candidate in candidates:
            assert secret not in candidate, f"{secret!r} recovered by a trivial transform"


def test_the_stored_payload_is_not_json(
    service: VoterProfileService, client: ZeroDBClient, keyring: ProfileKeyring
) -> None:
    """Live: the decoded payload did not parse as JSON."""
    service.save(USER, synthetic_payload())
    row = client.get_row(VoterProfileRow.table_name, keyring.row_id(USER))
    assert row is not None
    raw = base64.b64decode(str(row.row_data["payload"]))
    with pytest.raises((UnicodeDecodeError, json.JSONDecodeError)):
        json.loads(raw.decode("utf-8"))


def test_the_live_row_shape_is_datamodels_seven_fields(
    service: VoterProfileService, client: ZeroDBClient, keyring: ProfileKeyring
) -> None:
    """Live: the row carried exactly these seven keys, nothing more.

    A field beyond these is somewhere a future change could put an opinion in
    clear text, which is why the set is asserted exactly rather than as a
    subset.
    """
    service.save(USER, synthetic_payload(), jurisdiction_ids=["us-federal"])
    row = client.get_row(VoterProfileRow.table_name, keyring.row_id(USER))
    assert row is not None
    assert set(row.row_data) == {
        "id",
        "version",
        "locale",
        "jurisdiction_ids",
        "updated_at",
        "key_ref",
        "payload",
    }


def test_the_live_row_records_its_key_scheme(
    service: VoterProfileService, client: ZeroDBClient, keyring: ProfileKeyring
) -> None:
    """Live: `key_ref` read back as `local-envelope:informed/profiles/v1`.

    Per row rather than from config, so a table part-way through a migration to
    a real KMS stays readable row by row instead of needing a flag day.
    """
    service.save(USER, synthetic_payload())
    row = client.get_row(VoterProfileRow.table_name, keyring.row_id(USER))
    assert row is not None
    assert str(row.row_data["key_ref"]).startswith("local-envelope:")


# --- 3. delete destroys the data ------------------------------------------


def test_delete_makes_a_retained_ciphertext_unrecoverable(
    service: VoterProfileService, client: ZeroDBClient, keyring: ProfileKeyring
) -> None:
    """Live: with the real root key and the exact ciphertext, decryption after
    the delete failed.

    The strongest form of this check available — the author of the code,
    holding the key material and the bytes, cannot read the profile. It is also
    the check that forced the design: an HKDF-derived data key would have
    decrypted this payload happily, since the root key re-derives it forever.
    """
    row_id = keyring.row_id(USER)
    service.save(USER, synthetic_payload())
    stored = client.get_row(VoterProfileRow.table_name, row_id)
    assert stored is not None
    retained = str(stored.row_data["payload"])

    service.delete(USER)

    with pytest.raises(LookupError):
        keyring.data_key(USER)

    # A replacement key for the same voter must not open it either.
    replacement = keyring.ensure_data_key(USER)
    with pytest.raises(Exception):  # noqa: B017, PT011 - any failure is the pass
        ProfileCipher(replacement).decrypt(retained, aad=row_id)


def test_delete_removes_both_the_row_and_the_key(
    service: VoterProfileService, client: ZeroDBClient, keyring: ProfileKeyring
) -> None:
    """Live: both `voter_profiles` and `voter_profile_keys` read back empty."""
    row_id = keyring.row_id(USER)
    service.save(USER, synthetic_payload())
    assert client.get_row(KEY_WRAP_TABLE, row_id) is not None

    service.delete(USER)

    assert client.get_row(VoterProfileRow.table_name, row_id) is None
    assert client.get_row(KEY_WRAP_TABLE, row_id) is None


# --- 5. one row per voter, not one per save -------------------------------


def test_resaving_updates_the_one_row_rather_than_appending(
    service: VoterProfileService, client: ZeroDBClient, keyring: ProfileKeyring
) -> None:
    """Live: after two saves the table still held exactly one row for the voter.

    ZeroDB's `POST /rows` always appends (story 1.2), so this rests entirely on
    `upsert_row`'s find-then-replace — the same mechanism whose first-page-only
    filter fallback was a real duplicate-row bug in story 1.4. Worth pinning
    here because a duplicated profile row is worse than a duplicated public
    record: the stale row is an older version of someone's political opinions
    that no delete would find.
    """
    row_id = keyring.row_id(USER)
    service.save(USER, synthetic_payload())
    service.save(USER, synthetic_payload())

    page = client.query_rows(VoterProfileRow.table_name, filter={"id": row_id}, limit=100)
    matching = [row for row in page.rows if row.row_data.get("id") == row_id]
    assert len(matching) == 1
    assert matching[0].row_data["version"] == 2


def test_the_profile_tables_are_in_the_bootstrap_inventory() -> None:
    """Live: the run had to create `voter_profile_keys`, which did not exist.

    Confirms the bootstrap change in this story is required rather than
    cosmetic — and that a fresh environment gets both tables from
    `scripts/bootstrap.py` rather than from a verification script's side
    effect.
    """
    for table in PROFILE_TABLES:
        assert table in TABLE_SCHEMAS


def test_server_side_filter_breakage_does_not_hide_a_profile(
    keyring: ProfileKeyring,
) -> None:
    """A profile must still be findable when ZeroDB's `/query` filter matches
    nothing — the live behaviour story 1.4 measured.

    `voter_profiles` is one row per voter, so at scale it is a *large* table
    and a lookup is `get_row`, whose filter ZeroDB ignores. Were the
    client-side fallback to regress to a single page, a voter past the first
    page would appear to have no profile — and then `save` would mint a second
    row and `delete` would miss one. Reproduced here with the fake's
    live-faithful filter breakage.
    """
    transport = FakeZeroDBTransport()
    client = ZeroDBClient(transport=transport, project_id="proj-test", sleep=lambda _: None)
    for table in PROFILE_TABLES:
        client.create_table(table, TABLE_SCHEMAS[table])
    ring = ProfileKeyring(client=client, root_key=ProfileRootKey(ROOT))
    service = VoterProfileService(client=client, keyring=ring)

    # Decoys first, so the row under test is not the table's first row — the
    # arrangement whose absence produced story 1.4's false green.
    for index in range(5):
        service.save(f"synthetic-decoy-{index}", synthetic_payload())
    service.save(USER, synthetic_payload())

    transport.break_server_side_filter()

    loaded = service.load(USER)
    assert loaded is not None
    assert loaded.interests[0].topic_id == TOPIC_ID
