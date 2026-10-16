"""Steps for story 14.1's encrypted profile storage feature.

Every profile built here is synthetic by construction: `test-topic-alpha`,
`synthetic-user-1`. CLAUDE.md forbids real voter data in a fixture, and a
fixture shaped like a real voter's political opinions is exactly the data this
story protects — so the test data is not merely anonymised, it is not political.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError
from pytest_bdd import given, parsers, scenarios, then, when

from app.profiles.crypto import ProfileCipher, ProfileKeyring, ProfileRootKey
from app.profiles.models import Interest, InterestProfile, VoterProfileRow
from app.profiles.service import AnonymousModeError, VoterProfileService
from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBNotFoundError
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/encrypted_profile_storage.feature")

#: A root key fixed for the suite. 32 bytes of an obvious test pattern, never a
#: real key, and never written to a file the repo tracks.
TEST_ROOT_KEY = bytes(range(32))

USER_ID = "synthetic-user-1"
TOPIC = "test-topic-alpha"


def synthetic_interest(
    index: int = 0, *, tier: str = "primary", dealbreaker: bool = False
) -> dict[str, Any]:
    """One obviously-synthetic interest. Not a political position."""
    topic = f"test-topic-{index}" if index else TOPIC
    return {
        "topic_id": f"test:{topic}:v1",
        "stance_id": f"test:{topic}:v1:test-stance",
        "tier": tier,
        "intensity": 2,
        "dealbreaker": dealbreaker,
    }


@dataclass
class ProfileContext:
    """State carried across one scenario's steps."""

    service: VoterProfileService | None = None
    keyring: ProfileKeyring | None = None
    client: ZeroDBClient | None = None
    profile_payload: dict[str, Any] | None = None
    validation_error: Exception | None = None
    validated: InterestProfile | None = None
    stored_rows: list[VoterProfileRow] = field(default_factory=list)
    row_ids: list[str] = field(default_factory=list)
    retained_ciphertext: str | None = None
    retained_row_id: str | None = None
    log_text: str = ""
    anonymous_error: Exception | None = None

    @property
    def row(self) -> VoterProfileRow:
        assert self.stored_rows, "no profile has been saved in this scenario"
        return self.stored_rows[-1]


@pytest.fixture
def profiles() -> ProfileContext:
    return ProfileContext()


@given("a profile store backed by a per-user envelope encryption scheme")
def profile_store(profiles: ProfileContext) -> None:
    transport = FakeZeroDBTransport()
    client = ZeroDBClient(transport=transport, project_id="proj-test", sleep=lambda _: None)
    root = ProfileRootKey(TEST_ROOT_KEY)
    keyring = ProfileKeyring(client=client, root_key=root)
    profiles.client = client
    profiles.keyring = keyring
    profiles.service = VoterProfileService(client=client, keyring=keyring)


# --- building profiles ----------------------------------------------------


@given(parsers.parse('a synthetic voter profile with a primary interest "{topic}"'))
def profile_with_one_primary(profiles: ProfileContext, topic: str) -> None:
    profiles.profile_payload = {
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


@given("a synthetic voter profile with no primary interests")
def profile_with_no_primary(profiles: ProfileContext) -> None:
    profiles.profile_payload = {
        "interests": [synthetic_interest(1, tier="secondary")],
        "evidence_mode": "blend",
    }
    _try_validate(profiles)


@given(parsers.parse("a synthetic voter profile with {count:d} primary interests"))
def profile_with_n_primary(profiles: ProfileContext, count: int) -> None:
    profiles.profile_payload = {
        "interests": [synthetic_interest(index + 1) for index in range(count)],
        "evidence_mode": "blend",
    }
    _try_validate(profiles)


@given(parsers.parse("a synthetic voter profile with {count:d} secondary interests"))
def profile_with_n_secondary(profiles: ProfileContext, count: int) -> None:
    interests = [synthetic_interest(0)]
    interests += [
        synthetic_interest(index + 1, tier="secondary") for index in range(count)
    ]
    profiles.profile_payload = {"interests": interests, "evidence_mode": "blend"}
    _try_validate(profiles)


@given(parsers.parse("a synthetic voter profile with {count:d} nice-to-have interests"))
def profile_with_n_nice_to_have(profiles: ProfileContext, count: int) -> None:
    interests = [synthetic_interest(0)]
    interests += [
        synthetic_interest(index + 1, tier="nice_to_have") for index in range(count)
    ]
    profiles.profile_payload = {"interests": interests, "evidence_mode": "blend"}
    _try_validate(profiles)


@given(parsers.parse("a synthetic voter profile with a dealbreaker on a {tier} interest"))
def profile_with_dealbreaker(profiles: ProfileContext, tier: str) -> None:
    interests = [synthetic_interest(0, tier="primary", dealbreaker=tier == "primary")]
    if tier != "primary":
        interests.append(synthetic_interest(1, tier=tier, dealbreaker=True))
    profiles.profile_payload = {"interests": interests, "evidence_mode": "blend"}
    _try_validate(profiles)


def _try_validate(profiles: ProfileContext) -> None:
    assert profiles.profile_payload is not None
    try:
        profiles.validated = InterestProfile.model_validate(profiles.profile_payload)
    except ValidationError as exc:
        profiles.validation_error = exc


@then("the profile is rejected as invalid")
def profile_rejected(profiles: ProfileContext) -> None:
    assert profiles.validation_error is not None, "the profile validated but should not have"


@then("the profile is accepted")
def profile_accepted(profiles: ProfileContext) -> None:
    assert profiles.validation_error is None, f"rejected: {profiles.validation_error}"
    assert profiles.validated is not None


@then("no interest carries a weight of its own")
def no_weight_field(profiles: ProfileContext) -> None:
    # datamodel.md: "The tier sets the weight ... The live values come from
    # methodology_versions and are never hard-coded."
    assert "weight" not in Interest.model_fields
    assert profiles.profile_payload is not None
    profile = InterestProfile.model_validate(profiles.profile_payload)
    for interest in profile.interests:
        assert "weight" not in interest.model_dump()


# --- saving ---------------------------------------------------------------


@given("the voter has saved their profile")
@when("the voter saves their profile")
def save_profile(profiles: ProfileContext) -> None:
    assert profiles.service is not None and profiles.profile_payload is not None
    row = profiles.service.save(USER_ID, profiles.profile_payload)
    profiles.stored_rows.append(row)


@when("the voter saves their profile again")
def save_profile_again(profiles: ProfileContext) -> None:
    save_profile(profiles)


@then("the stored row's payload is not readable as plaintext")
def payload_is_ciphertext(profiles: ProfileContext) -> None:
    import base64

    raw = base64.b64decode(profiles.row.payload)
    # Not valid UTF-8 JSON, and not JSON at all: the whole point.
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return
    with pytest.raises(json.JSONDecodeError):
        json.loads(decoded)


@then("the stored row contains no interest topic id anywhere in it")
def row_leaks_nothing(profiles: ProfileContext) -> None:
    serialized = json.dumps(profiles.row.row_data())
    assert TOPIC not in serialized
    assert "test-stance" not in serialized


@then("the stored payload decrypts back to the same profile")
def payload_round_trips(profiles: ProfileContext) -> None:
    assert profiles.service is not None and profiles.profile_payload is not None
    loaded = profiles.service.load(USER_ID)
    assert loaded is not None
    expected = InterestProfile.model_validate(profiles.profile_payload)
    assert loaded.interests == expected.interests
    assert loaded.evidence_mode == expected.evidence_mode


@then(
    "the stored row's fields are exactly id, version, locale, jurisdiction_ids, "
    "updated_at, key_ref and payload"
)
def row_shape_matches_datamodel(profiles: ProfileContext) -> None:
    assert set(profiles.row.row_data()) == {
        "id",
        "version",
        "locale",
        "jurisdiction_ids",
        "updated_at",
        "key_ref",
        "payload",
    }


@then(parsers.parse("the stored version is {version:d}"))
def stored_version(profiles: ProfileContext, version: int) -> None:
    assert profiles.row.version == version


@then("the profile history records the previous version without its contents")
def history_without_contents(profiles: ProfileContext) -> None:
    assert profiles.service is not None
    loaded = profiles.service.load(USER_ID)
    assert loaded is not None
    assert [entry.version for entry in loaded.history] == [1]
    entry = loaded.history[0]
    assert set(entry.model_dump()) == {"version", "changed_at"}


# --- row ids --------------------------------------------------------------


@when(parsers.parse('a row id is derived for user "{user_id}"'))
@when(parsers.parse('a row id is derived again for user "{user_id}"'))
def derive_row_id(profiles: ProfileContext, user_id: str) -> None:
    assert profiles.keyring is not None
    profiles.row_ids.append(profiles.keyring.row_id(user_id))


@then("both row ids are identical")
def row_ids_identical(profiles: ProfileContext) -> None:
    assert len(profiles.row_ids) == 2
    assert profiles.row_ids[0] == profiles.row_ids[1]


@then("the two row ids differ")
def row_ids_differ(profiles: ProfileContext) -> None:
    assert len(profiles.row_ids) == 2
    assert profiles.row_ids[0] != profiles.row_ids[1]


@then(parsers.parse('the row id starts with "{prefix}"'))
def row_id_prefix(profiles: ProfileContext, prefix: str) -> None:
    assert profiles.row_ids[0].startswith(prefix)


@then("the row id does not contain the user id")
def row_id_hides_user_id(profiles: ProfileContext) -> None:
    assert USER_ID not in profiles.row_ids[0]


@then("the row id cannot be derived without the id key")
def row_id_needs_the_key(profiles: ProfileContext) -> None:
    other = ProfileKeyring(
        client=profiles.client,  # type: ignore[arg-type]
        root_key=ProfileRootKey(bytes(32)),
    )
    assert other.row_id(USER_ID) != profiles.row_ids[0]


# --- delete and shred -----------------------------------------------------


@when("the voter deletes their profile")
def delete_profile(profiles: ProfileContext) -> None:
    assert profiles.service is not None
    profiles.retained_ciphertext = profiles.row.payload
    profiles.retained_row_id = profiles.row.id
    profiles.service.delete(USER_ID)


@then("the profile row is gone")
def row_is_gone(profiles: ProfileContext) -> None:
    assert profiles.service is not None
    assert profiles.service.load(USER_ID) is None


@then("the wrapped data key for that voter is gone")
def wrap_is_gone(profiles: ProfileContext) -> None:
    assert profiles.keyring is not None
    assert profiles.keyring.stored_wrap(USER_ID) is None


@then("decrypting a retained copy of the ciphertext now fails")
def retained_ciphertext_undecryptable(profiles: ProfileContext) -> None:
    assert profiles.keyring is not None and profiles.retained_ciphertext is not None
    # Even holding the ciphertext AND the root key, the data key is gone.
    with pytest.raises((LookupError, ZeroDBNotFoundError)):
        key = profiles.keyring.data_key(USER_ID)
        ProfileCipher(key).decrypt(profiles.retained_ciphertext, aad=profiles.retained_row_id or "")


# --- logging --------------------------------------------------------------


@when("the voter saves their profile and reads it back while logs are captured")
def save_and_read_capturing_logs(
    profiles: ProfileContext, caplog: pytest.LogCaptureFixture
) -> None:
    assert profiles.service is not None and profiles.profile_payload is not None
    with caplog.at_level(logging.DEBUG):
        row = profiles.service.save(USER_ID, profiles.profile_payload)
        profiles.stored_rows.append(row)
        profiles.service.load(USER_ID)
        profiles.service.delete(USER_ID)
    profiles.log_text = "\n".join(
        f"{record.name} {record.getMessage()}" for record in caplog.records
    )


@then("no captured log line contains any interest topic id")
def logs_have_no_topics(profiles: ProfileContext) -> None:
    assert TOPIC not in profiles.log_text
    assert "test-stance" not in profiles.log_text


@then("no captured log line contains any key material")
def logs_have_no_key_material(profiles: ProfileContext) -> None:
    import base64

    assert profiles.keyring is not None
    forbidden = [
        base64.b64encode(TEST_ROOT_KEY).decode(),
        TEST_ROOT_KEY.hex(),
    ]
    for secret in forbidden:
        assert secret not in profiles.log_text


# --- anonymous mode -------------------------------------------------------


@when("the voter uses the app in anonymous mode")
def use_anonymous_mode(profiles: ProfileContext) -> None:
    assert profiles.service is not None and profiles.profile_payload is not None
    try:
        profiles.service.save(None, profiles.profile_payload)
    except AnonymousModeError as exc:
        profiles.anonymous_error = exc


@then("no profile row is written")
def no_profile_row(profiles: ProfileContext) -> None:
    assert profiles.anonymous_error is not None, "anonymous save was accepted"
    assert profiles.client is not None
    page = profiles.client.query_rows("voter_profiles", limit=100)
    assert page.rows == []


@then("no wrapped data key is written")
def no_wrap_row(profiles: ProfileContext) -> None:
    assert profiles.client is not None
    page = profiles.client.query_rows("voter_profile_keys", limit=100)
    assert page.rows == []
