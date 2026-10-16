"""Steps for story 15.1's follow-an-incumbent feature.

Every person id used as test *voter data* here is obviously synthetic
(`person:zzz-synthetic-B000001`, `person:zzz-does-not-exist`) — CLAUDE.md
forbids real voter data in a fixture, and "who a voter follows" is itself a
political signal datamodel.md calls out explicitly (see `InterestProfile`'s
`followed_person_ids` docstring). The one real legislator this story touches
lives only in `scripts/verify_follow_incumbent_live.py`, run against the live
project, never in a fixture.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import TABLE_SCHEMAS
from app.profiles import PROFILE_TABLES
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.models import InterestProfile, VoterProfileRow
from app.profiles.service import PersonNotFoundError, VoterProfileService
from app.repositories.base import Repository
from app.repositories.models import Person
from app.zerodb.client import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/follow_incumbent.feature")

#: Fixed test root key, same pattern as the sibling 14.1 steps file — never a
#: real key, never written to a tracked file.
TEST_ROOT_KEY = bytes(range(32))

USER_ID = "synthetic-user-15.1"
TOPIC = "test-topic-alpha"


@dataclass
class FollowContext:
    """State carried across one scenario's steps."""

    client: ZeroDBClient | None = None
    service: VoterProfileService | None = None
    profile_payload: dict[str, Any] | None = None
    follow_error: Exception | None = None
    loaded: InterestProfile | None = None
    stored_rows: list[VoterProfileRow] = field(default_factory=list)

    @property
    def row(self) -> VoterProfileRow:
        assert self.stored_rows, "no profile has been saved in this scenario"
        return self.stored_rows[-1]


@pytest.fixture
def follow_ctx() -> FollowContext:
    return FollowContext()


@given("a profile store backed by a per-user envelope encryption scheme")
def profile_store(follow_ctx: FollowContext) -> None:
    transport = FakeZeroDBTransport()
    client = ZeroDBClient(transport=transport, project_id="proj-test", sleep=lambda _: None)
    for table in (*PROFILE_TABLES, Person.table_name):
        client.create_table(table, TABLE_SCHEMAS[table])
    root = ProfileRootKey(TEST_ROOT_KEY)
    keyring = ProfileKeyring(client=client, root_key=root)
    follow_ctx.client = client
    follow_ctx.service = VoterProfileService(client=client, keyring=keyring)


@given(parsers.parse('a real legislator "{person_id}" exists in the people table'))
def seed_person(follow_ctx: FollowContext, person_id: str) -> None:
    assert follow_ctx.client is not None
    people = Repository(client=follow_ctx.client, model=Person)
    people.upsert({"id": person_id, "canonical_name": "Synthetic Test Legislator"})


@given(parsers.parse('a synthetic voter profile with a primary interest "{topic}"'))
def profile_with_one_primary(follow_ctx: FollowContext, topic: str) -> None:
    follow_ctx.profile_payload = {
        "interests": [
            {
                "topic_id": f"test:{topic}:v1",
                "stance_id": f"test:{topic}:v1:test-stance",
                "tier": "primary",
                "intensity": 3,
                "dealbreaker": False,
            }
        ],
        "evidence_mode": "blend",
    }


@given("the voter has saved their profile")
def save_profile(follow_ctx: FollowContext) -> None:
    assert follow_ctx.service is not None and follow_ctx.profile_payload is not None
    row = follow_ctx.service.save(USER_ID, follow_ctx.profile_payload)
    follow_ctx.stored_rows.append(row)


@given(parsers.parse('the voter follows "{person_id}"'))
@when(parsers.parse('the voter follows "{person_id}"'))
def voter_follows(follow_ctx: FollowContext, person_id: str) -> None:
    assert follow_ctx.service is not None and follow_ctx.client is not None
    follow_ctx.loaded = follow_ctx.service.follow(USER_ID, person_id)
    # The row as actually stored, read through the raw client rather than the
    # service (which would decrypt it) — the same "read the real row, not
    # the service's view of it" discipline the sibling 14.1 steps use for
    # the "is this genuinely ciphertext" assertions.
    row = follow_ctx.client.get_row(VoterProfileRow.table_name, _row_id(follow_ctx))
    assert row is not None
    follow_ctx.stored_rows.append(VoterProfileRow.model_validate(row.row_data))


def _row_id(follow_ctx: FollowContext) -> str:
    assert follow_ctx.client is not None
    # HMAC derivation needs the keyring, which the service holds privately by
    # design (see `VoterProfileService.__repr__`'s `<redacted>`). Re-deriving
    # it here with the same fixed test root key is simpler than widening the
    # service's public surface just for a test's own row lookup.
    keyring = ProfileKeyring(client=follow_ctx.client, root_key=ProfileRootKey(TEST_ROOT_KEY))
    return keyring.row_id(USER_ID)


@when(parsers.parse('the voter tries to follow "{person_id}"'))
def voter_tries_to_follow(follow_ctx: FollowContext, person_id: str) -> None:
    assert follow_ctx.service is not None
    try:
        follow_ctx.loaded = follow_ctx.service.follow(USER_ID, person_id)
    except PersonNotFoundError as exc:
        follow_ctx.follow_error = exc


@when(parsers.parse('the voter unfollows "{person_id}"'))
def voter_unfollows(follow_ctx: FollowContext, person_id: str) -> None:
    assert follow_ctx.service is not None
    follow_ctx.loaded = follow_ctx.service.unfollow(USER_ID, person_id)


@when("the voter's profile is reloaded fresh from storage")
def reload_profile(follow_ctx: FollowContext) -> None:
    assert follow_ctx.service is not None
    follow_ctx.loaded = follow_ctx.service.load(USER_ID)


@then(parsers.parse('"{person_id}" is in the voter\'s followed persons'))
def person_is_followed(follow_ctx: FollowContext, person_id: str) -> None:
    assert follow_ctx.loaded is not None
    assert person_id in follow_ctx.loaded.followed_person_ids


@then(parsers.parse('"{person_id}" is not in the voter\'s followed persons'))
def person_is_not_followed(follow_ctx: FollowContext, person_id: str) -> None:
    # Re-read live from storage rather than trusting the in-memory value, so
    # a rejected follow is checked against what was actually (not) written.
    assert follow_ctx.service is not None
    loaded = follow_ctx.service.load(USER_ID)
    assert loaded is not None
    assert person_id not in loaded.followed_person_ids


@then("the follow is rejected because the person does not exist")
def follow_was_rejected(follow_ctx: FollowContext) -> None:
    assert follow_ctx.follow_error is not None, "the follow succeeded but should have been rejected"
    assert isinstance(follow_ctx.follow_error, PersonNotFoundError)


@then("the stored row's payload is not readable as plaintext")
def payload_is_ciphertext(follow_ctx: FollowContext) -> None:
    import base64

    raw = base64.b64decode(follow_ctx.row.payload)
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return
    with pytest.raises(json.JSONDecodeError):
        json.loads(decoded)


@then("the stored row contains no followed person id anywhere in it")
def row_leaks_no_followed_person(follow_ctx: FollowContext) -> None:
    serialized = json.dumps(follow_ctx.row.row_data())
    assert "zzz-synthetic-B000001" not in serialized


@then(
    "the stored row's fields are exactly id, version, locale, jurisdiction_ids, "
    "updated_at, key_ref and payload"
)
def row_shape_matches_datamodel(follow_ctx: FollowContext) -> None:
    assert set(follow_ctx.row.row_data()) == {
        "id",
        "version",
        "locale",
        "jurisdiction_ids",
        "updated_at",
        "key_ref",
        "payload",
    }
