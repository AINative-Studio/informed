"""Live-confirmed facts about follow/unfollow (story 15.1).

Same purpose as `test_profiles_live_shapes.py`: each test here pins a
property that was **measured against the live "informed" project**
(`bb8c620a-1fe7-4892-918a-7d8724906e5e`) on 2026-10-16 via
`backend/scripts/verify_follow_incumbent_live.py`, rather than inferred from
the fake. All 12 checks in that script's run passed with zero failures.

What the live run established, and why each matters enough to be locked in:

  1. **Senator Maria Cantwell's real row (`person:C000127`) is live in
     `people`**, written by story 2.2's earlier live verification — so
     `VoterProfileService.follow`'s existence check has a real row to
     resolve against, not just a fake's.

  2. **Following that real legislator succeeds live**, and following a
     synthetic, nonexistent `person_id` is rejected live with
     `PersonNotFoundError` — and changes nothing: a fresh live `load()`
     after the rejection still showed exactly the one real follow, never a
     partial or corrupted list.

  3. **The follow round-trips through a FRESH `load()`** — not the
     in-memory object `follow()` returned, which would prove nothing about
     persistence — confirming the write actually reached ZeroDB.

  4. **The follow genuinely lives inside the encrypted payload.** Read back
     through the raw client (no service, no decryption): the stored row's
     fields were exactly datamodel.md's seven, Senator Cantwell's
     `person_id` was absent from the row's raw JSON AND from a
     base64-decode of `payload`, and the decoded payload bytes were not
     readable as plaintext JSON — the same "ciphertext, not just not-equal"
     bar story 14.1's live run set.

  5. **Unfollowing removes the person, confirmed by a fresh live `load()`.**

  6. **Cleanup fully succeeded.** The synthetic test voter's profile row and
     key wrap were deleted and a live re-read confirmed both gone. Senator
     Cantwell's `people` row was read-only throughout and was never
     written to or deleted.

Everything below runs against the fake in CI; the live script is the thing
that measured it.
"""

from __future__ import annotations

import base64
import json

import pytest

from app.bootstrap import TABLE_SCHEMAS
from app.profiles import PROFILE_TABLES
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.models import VoterProfileRow
from app.profiles.service import PersonNotFoundError, VoterProfileService
from app.repositories.base import Repository
from app.repositories.models import Person
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

ROOT = bytes(range(32))
USER_ID = "fake-voter-15.1"
REAL_LEGISLATOR_ID = "person:C000127"
NONEXISTENT_PERSON_ID = "person:does-not-exist-15.1"


@pytest.fixture
def client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    zerodb = ZeroDBClient(transport=transport, project_id="proj-test", sleep=lambda _: None)
    for table in (*PROFILE_TABLES, Person.table_name):
        zerodb.create_table(table, TABLE_SCHEMAS[table])
    Repository(client=zerodb, model=Person).upsert(
        {"id": REAL_LEGISLATOR_ID, "canonical_name": "Maria Cantwell"}
    )
    return zerodb


@pytest.fixture
def service(client: ZeroDBClient) -> VoterProfileService:
    keyring = ProfileKeyring(client=client, root_key=ProfileRootKey(ROOT))
    return VoterProfileService(client=client, keyring=keyring)


def _seed_profile(service: VoterProfileService) -> None:
    service.save(
        USER_ID,
        {
            "interests": [
                {
                    "topic_id": "test:alpha:v1",
                    "stance_id": "test:alpha:v1:test-stance",
                    "tier": "primary",
                    "intensity": 2,
                    "dealbreaker": False,
                }
            ]
        },
    )


def test_following_a_real_live_legislator_succeeds(service: VoterProfileService) -> None:
    """Live: following person:C000127 (Sen. Maria Cantwell) succeeded."""
    _seed_profile(service)
    followed = service.follow(USER_ID, REAL_LEGISLATOR_ID)
    assert REAL_LEGISLATOR_ID in followed.followed_person_ids


def test_following_a_nonexistent_person_is_rejected_and_changes_nothing(
    service: VoterProfileService,
) -> None:
    """Live: a synthetic nonexistent person_id was rejected, and a fresh
    load() afterward showed the profile unchanged."""
    _seed_profile(service)
    service.follow(USER_ID, REAL_LEGISLATOR_ID)

    with pytest.raises(PersonNotFoundError):
        service.follow(USER_ID, NONEXISTENT_PERSON_ID)

    reloaded = service.load(USER_ID)
    assert reloaded is not None
    assert reloaded.followed_person_ids == [REAL_LEGISLATOR_ID]


def test_the_follow_round_trips_through_a_fresh_load(service: VoterProfileService) -> None:
    """Live: a follow survived being read back through an independent load(),
    not just the in-memory object follow() returned."""
    _seed_profile(service)
    service.follow(USER_ID, REAL_LEGISLATOR_ID)

    reloaded = service.load(USER_ID)
    assert reloaded is not None
    assert REAL_LEGISLATOR_ID in reloaded.followed_person_ids


def test_the_stored_row_hides_the_followed_person_id(
    client: ZeroDBClient, service: VoterProfileService
) -> None:
    """Live: the raw stored row's fields were exactly datamodel.md's seven,
    and the followed person's id appeared in none of: the row's raw JSON,
    a base64-decode of the payload, or readable plaintext."""
    _seed_profile(service)
    service.follow(USER_ID, REAL_LEGISLATOR_ID)

    row_id = ProfileKeyring(client=client, root_key=ProfileRootKey(ROOT)).row_id(USER_ID)
    raw = client.get_row(VoterProfileRow.table_name, row_id)
    assert raw is not None
    stored = VoterProfileRow.model_validate(raw.row_data)

    assert set(stored.row_data()) == {
        "id",
        "version",
        "locale",
        "jurisdiction_ids",
        "updated_at",
        "key_ref",
        "payload",
    }
    serialized = json.dumps(stored.row_data())
    assert REAL_LEGISLATOR_ID not in serialized

    decoded = base64.b64decode(stored.payload)
    assert REAL_LEGISLATOR_ID.encode() not in decoded
    try:
        json.loads(decoded.decode("utf-8"))
        is_plaintext = True
    except (UnicodeDecodeError, json.JSONDecodeError):
        is_plaintext = False
    assert not is_plaintext


def test_unfollowing_removes_the_person_confirmed_by_a_fresh_load(
    service: VoterProfileService,
) -> None:
    """Live: unfollowing removed the person, confirmed by an independent
    load() afterward, not just the return value of unfollow() itself."""
    _seed_profile(service)
    service.follow(USER_ID, REAL_LEGISLATOR_ID)

    service.unfollow(USER_ID, REAL_LEGISLATOR_ID)

    reloaded = service.load(USER_ID)
    assert reloaded is not None
    assert REAL_LEGISLATOR_ID not in reloaded.followed_person_ids
