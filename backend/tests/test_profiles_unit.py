"""Unit tests for story 14.1's profile models and encrypting service.

Every interest here is obviously synthetic (`test:test-topic-*`). CLAUDE.md:
never put real voter data in a fixture — and a fixture shaped like real
political opinions is precisely the data this story exists to protect.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import pytest
from pydantic import ValidationError

from app.bootstrap import TABLE_SCHEMAS
from app.profiles import PROFILE_TABLES
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.models import (
    MAX_PRIMARY_INTERESTS,
    MAX_SECONDARY_INTERESTS,
    MIN_PRIMARY_INTERESTS,
    Interest,
    InterestProfile,
    VoterProfileRow,
)
from app.profiles.service import AnonymousModeError, VoterProfileService
from app.zerodb.client import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

ROOT = bytes(range(32))
USER = "synthetic-user-1"


def interest(index: int = 0, *, tier: str = "primary", **overrides: Any) -> dict[str, Any]:
    topic = f"test-topic-{index}"
    row: dict[str, Any] = {
        "topic_id": f"test:{topic}:v1",
        "stance_id": f"test:{topic}:v1:test-stance",
        "tier": tier,
        "intensity": 2,
        "dealbreaker": False,
    }
    row.update(overrides)
    return row


def profile(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"interests": [interest()], "evidence_mode": "blend"}
    payload.update(overrides)
    return payload


@pytest.fixture
def client() -> ZeroDBClient:
    zdb = ZeroDBClient(
        transport=FakeZeroDBTransport(), project_id="proj-test", sleep=lambda _: None
    )
    # The fake 404s on an unknown table, exactly as live ZeroDB does, so the
    # tables bootstrap creates have to exist here too. `PROFILE_TABLES` is the
    # single list both this fixture and `app.bootstrap` read, so a table added
    # to one cannot be forgotten in the other.
    for table in PROFILE_TABLES:
        zdb.create_table(table, TABLE_SCHEMAS[table])
    return zdb


@pytest.fixture
def service(client: ZeroDBClient) -> VoterProfileService:
    keyring = ProfileKeyring(client=client, root_key=ProfileRootKey(ROOT))
    return VoterProfileService(client=client, keyring=keyring)


# =========================================================================
# InterestProfile — datamodel.md's "Voter profile document" rules
# =========================================================================


def test_a_minimal_profile_validates() -> None:
    parsed = InterestProfile.model_validate(profile())
    assert len(parsed.interests) == 1
    assert parsed.evidence_mode == "blend"
    assert parsed.followed_person_ids == []
    assert parsed.history == []


def test_the_documented_payload_shape_validates() -> None:
    """datamodel.md's own example payload, with synthetic topic ids."""
    parsed = InterestProfile.model_validate(
        {
            "interests": [
                {
                    "topic_id": "test:test-topic-1:v1",
                    "stance_id": "test:test-topic-1:v1:test-stance",
                    "tier": "primary",
                    "intensity": 3,
                    "dealbreaker": True,
                    "note": None,
                },
                {
                    "topic_id": "test:test-topic-2:v1",
                    "stance_id": "test:test-topic-2:v1:test-stance",
                    "tier": "secondary",
                    "intensity": 2,
                    "dealbreaker": False,
                },
                {
                    "topic_id": "test:test-topic-3:v1",
                    "stance_id": "test:test-topic-3:v1:test-stance",
                    "tier": "nice_to_have",
                    "intensity": 1,
                    "dealbreaker": False,
                },
            ],
            "evidence_mode": "blend",
            "followed_person_ids": ["person:TEST0001"],
            "history": [{"version": 6, "changed_at": "2026-10-10T02:11:00Z"}],
        }
    )
    assert [item.tier for item in parsed.interests] == ["primary", "secondary", "nice_to_have"]
    assert parsed.history[0].version == 6


@pytest.mark.parametrize("count", [MIN_PRIMARY_INTERESTS, 3, MAX_PRIMARY_INTERESTS])
def test_primary_counts_inside_the_limit_are_accepted(count: int) -> None:
    InterestProfile.model_validate(profile(interests=[interest(index) for index in range(count)]))


def test_no_primary_interest_is_rejected() -> None:
    """prd.md: no candidate score is shown until at least one primary exists,
    so a stored profile with none would be unscoreable.
    """
    with pytest.raises(ValidationError, match="primary"):
        InterestProfile.model_validate(profile(interests=[interest(1, tier="secondary")]))


def test_too_many_primary_interests_are_rejected() -> None:
    interests = [interest(index) for index in range(MAX_PRIMARY_INTERESTS + 1)]
    with pytest.raises(ValidationError, match="primary"):
        InterestProfile.model_validate(profile(interests=interests))


def test_secondary_interests_at_the_limit_are_accepted() -> None:
    interests = [interest()] + [
        interest(index + 1, tier="secondary") for index in range(MAX_SECONDARY_INTERESTS)
    ]
    InterestProfile.model_validate(profile(interests=interests))


def test_too_many_secondary_interests_are_rejected() -> None:
    interests = [interest()] + [
        interest(index + 1, tier="secondary") for index in range(MAX_SECONDARY_INTERESTS + 1)
    ]
    with pytest.raises(ValidationError, match="secondary"):
        InterestProfile.model_validate(profile(interests=interests))


def test_nice_to_have_interests_are_unlimited() -> None:
    """prd.md's tier table: "Unlimited"."""
    interests = [interest()] + [interest(index + 1, tier="nice_to_have") for index in range(50)]
    InterestProfile.model_validate(profile(interests=interests))


def test_a_dealbreaker_on_a_primary_interest_is_allowed() -> None:
    parsed = InterestProfile.model_validate(profile(interests=[interest(dealbreaker=True)]))
    assert parsed.interests[0].dealbreaker is True


@pytest.mark.parametrize("tier", ["secondary", "nice_to_have"])
def test_a_dealbreaker_outside_primary_is_rejected(tier: str) -> None:
    """datamodel.md: "`dealbreaker` allowed only on primary interests"."""
    interests = [interest(), interest(1, tier=tier, dealbreaker=True)]
    with pytest.raises(ValidationError, match="dealbreaker"):
        InterestProfile.model_validate(profile(interests=interests))


def test_an_unknown_tier_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InterestProfile.model_validate(profile(interests=[interest(tier="urgent")]))


@pytest.mark.parametrize("intensity", [0, 4, -1])
def test_intensity_outside_one_to_three_is_rejected(intensity: int) -> None:
    """prd.md: "Intensity inside its tier (1–3)"."""
    with pytest.raises(ValidationError):
        InterestProfile.model_validate(profile(interests=[interest(intensity=intensity)]))


def test_a_duplicate_topic_is_rejected() -> None:
    """One stance per topic: two tiers for one topic would double-count it."""
    duplicate = [interest(), interest(tier="secondary")]
    with pytest.raises(ValidationError, match="topic"):
        InterestProfile.model_validate(profile(interests=duplicate))


def test_an_unknown_field_is_rejected() -> None:
    """`extra="forbid"`, as every Informed model: a typo'd field in a payload
    nothing validates is a silent data-loss bug.
    """
    with pytest.raises(ValidationError):
        InterestProfile.model_validate(profile(intrests=[]))


def test_an_unknown_evidence_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InterestProfile.model_validate(profile(evidence_mode="vibes"))


def test_an_interest_carries_no_weight_field() -> None:
    """datamodel.md: tier weights "come from `methodology_versions` and are
    never hard-coded" — so the profile must not carry one at all.
    """
    assert "weight" not in Interest.model_fields
    assert not any("weight" in name for name in InterestProfile.model_fields), (
        "a weight on the profile would freeze the methodology into stored data"
    )


def test_a_free_text_note_is_optional_and_kept() -> None:
    parsed = InterestProfile.model_validate(
        profile(interests=[interest(note="synthetic test note")])
    )
    assert parsed.interests[0].note == "synthetic test note"


def test_a_very_long_note_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InterestProfile.model_validate(profile(interests=[interest(note="x" * 5_000)]))


# =========================================================================
# VoterProfileRow — the outer, stored row
# =========================================================================


def test_the_row_matches_the_documented_fields() -> None:
    row = VoterProfileRow(
        id="vp:" + "a" * 64,
        version=7,
        locale="en-US",
        jurisdiction_ids=["us-federal"],
        updated_at="2026-10-15T19:20:00Z",
        key_ref="local-envelope:informed/profiles/v1",
        payload=base64.b64encode(b"not-really-ciphertext").decode(),
    )
    assert set(row.row_data()) == {
        "id",
        "version",
        "locale",
        "jurisdiction_ids",
        "updated_at",
        "key_ref",
        "payload",
    }


def test_the_row_rejects_a_version_below_one() -> None:
    with pytest.raises(ValidationError):
        VoterProfileRow(
            id="vp:" + "a" * 64,
            version=0,
            locale="en-US",
            updated_at="2026-10-15T19:20:00Z",
            key_ref="local-envelope:informed/profiles/v1",
            payload="AAAA",
        )


def test_the_row_rejects_an_id_that_is_not_an_hmac() -> None:
    """A plain user id in the `id` field is the exact leak the HMAC prevents,
    so the model refuses it rather than trusting the caller.
    """
    with pytest.raises(ValidationError):
        VoterProfileRow(
            id="vp:synthetic-user-1",
            version=1,
            locale="en-US",
            updated_at="2026-10-15T19:20:00Z",
            key_ref="local-envelope:informed/profiles/v1",
            payload="AAAA",
        )


def test_the_row_has_no_field_for_interests() -> None:
    """The row is a locator plus ciphertext. A plaintext field here would be a
    place for an interest to land unencrypted.
    """
    for name in VoterProfileRow.model_fields:
        assert name not in {"interests", "evidence_mode", "followed_person_ids", "user_id"}


def test_the_row_declares_the_voter_profiles_table() -> None:
    assert VoterProfileRow.table_name == "voter_profiles"


def test_the_row_declares_no_references() -> None:
    """datamodel.md's diagram: "encrypted; no references in or out"."""
    assert VoterProfileRow.references == {}


# =========================================================================
# VoterProfileService — encrypt before write, decrypt after read
# =========================================================================


def test_save_then_load_round_trips(service: VoterProfileService) -> None:
    service.save(USER, profile())
    loaded = service.load(USER)
    assert loaded is not None
    assert loaded.interests[0].topic_id == "test:test-topic-0:v1"


def test_save_returns_the_stored_row_not_the_plaintext(service: VoterProfileService) -> None:
    row = service.save(USER, profile())
    assert isinstance(row, VoterProfileRow)
    assert "test-topic-0" not in json.dumps(row.row_data())


def test_what_reaches_zerodb_is_ciphertext(
    service: VoterProfileService, client: ZeroDBClient
) -> None:
    service.save(USER, profile())
    page = client.query_rows("voter_profiles", limit=10)
    assert len(page.rows) == 1
    stored = json.dumps(page.rows[0].row_data)
    assert "test-topic-0" not in stored
    assert "test-stance" not in stored
    assert "primary" not in stored


def test_loading_an_absent_profile_returns_none(service: VoterProfileService) -> None:
    assert service.load("synthetic-user-never-saved") is None


def test_save_validates_before_it_encrypts(service: VoterProfileService) -> None:
    """An invalid profile must not produce a row, encrypted or otherwise."""
    with pytest.raises(ValidationError):
        service.save(USER, profile(interests=[interest(tier="secondary")]))
    assert service.load(USER) is None


def test_each_save_bumps_the_version(service: VoterProfileService) -> None:
    assert service.save(USER, profile()).version == 1
    assert service.save(USER, profile()).version == 2
    assert service.save(USER, profile()).version == 3


def test_history_records_versions_without_contents(service: VoterProfileService) -> None:
    """datamodel.md: "History keeps change dates, not prior contents"."""
    service.save(USER, profile())
    service.save(USER, profile(interests=[interest(1)]))
    loaded = service.load(USER)
    assert loaded is not None
    assert [entry.version for entry in loaded.history] == [1]
    assert set(loaded.history[0].model_dump()) == {"version", "changed_at"}
    assert "test-topic-0" not in json.dumps(loaded.model_dump())


def test_history_is_bounded(service: VoterProfileService) -> None:
    """An unbounded list would grow the payload without limit, and the oldest
    change date is not worth a row that cannot be paged.
    """
    for _ in range(60):
        service.save(USER, profile())
    loaded = service.load(USER)
    assert loaded is not None
    assert len(loaded.history) <= 50


def test_resaving_does_not_duplicate_the_row(
    service: VoterProfileService, client: ZeroDBClient
) -> None:
    service.save(USER, profile())
    service.save(USER, profile())
    page = client.query_rows("voter_profiles", limit=10)
    assert len(page.rows) == 1


def test_two_voters_get_two_rows_and_two_keys(
    service: VoterProfileService, client: ZeroDBClient
) -> None:
    service.save(USER, profile())
    service.save("synthetic-user-2", profile())
    assert len(client.query_rows("voter_profiles", limit=10).rows) == 2
    assert len(client.query_rows("voter_profile_keys", limit=10).rows) == 2


def test_one_voters_key_cannot_read_anothers_profile(service: VoterProfileService) -> None:
    """The per-user data key is the isolation boundary, not a convention."""
    service.save(USER, profile())
    service.save("synthetic-user-2", profile(interests=[interest(9)]))
    first = service.load(USER)
    second = service.load("synthetic-user-2")
    assert first is not None and second is not None
    assert first.interests[0].topic_id != second.interests[0].topic_id


def test_delete_removes_the_row_and_the_key(
    service: VoterProfileService, client: ZeroDBClient
) -> None:
    service.save(USER, profile())
    service.delete(USER)
    assert service.load(USER) is None
    assert client.query_rows("voter_profiles", limit=10).rows == []
    assert client.query_rows("voter_profile_keys", limit=10).rows == []


def test_delete_is_idempotent(service: VoterProfileService) -> None:
    """prd.md's one-tap delete: a retried tap cannot raise."""
    service.save(USER, profile())
    service.delete(USER)
    service.delete(USER)


def test_delete_of_an_unknown_voter_is_a_no_op(service: VoterProfileService) -> None:
    service.delete("synthetic-user-never-saved")


def test_delete_leaves_other_voters_intact(service: VoterProfileService) -> None:
    service.save(USER, profile())
    service.save("synthetic-user-2", profile())
    service.delete(USER)
    assert service.load(USER) is None
    assert service.load("synthetic-user-2") is not None


def test_a_save_after_a_delete_starts_at_version_one(service: VoterProfileService) -> None:
    """The deleted profile is gone, history included; the new one is new."""
    service.save(USER, profile())
    service.save(USER, profile())
    service.delete(USER)
    fresh = service.save(USER, profile())
    assert fresh.version == 1
    loaded = service.load(USER)
    assert loaded is not None
    assert loaded.history == []


def test_export_returns_the_plaintext_profile(service: VoterProfileService) -> None:
    """prd.md's one-tap export (story 14.4 builds the endpoint on this)."""
    service.save(USER, profile())
    exported = service.export(USER)
    assert exported is not None
    assert exported["interests"][0]["topic_id"] == "test:test-topic-0:v1"


def test_export_of_an_absent_profile_is_none(service: VoterProfileService) -> None:
    assert service.export("synthetic-user-never-saved") is None


def test_anonymous_mode_writes_nothing(service: VoterProfileService, client: ZeroDBClient) -> None:
    """datamodel.md: "In anonymous mode no row is written"."""
    with pytest.raises(AnonymousModeError):
        service.save(None, profile())
    assert client.query_rows("voter_profiles", limit=10).rows == []
    assert client.query_rows("voter_profile_keys", limit=10).rows == []


def test_anonymous_load_and_delete_write_nothing(
    service: VoterProfileService, client: ZeroDBClient
) -> None:
    assert service.load(None) is None
    service.delete(None)
    assert client.query_rows("voter_profiles", limit=10).rows == []
    assert client.query_rows("voter_profile_keys", limit=10).rows == []


def test_anonymous_mode_still_validates_a_payload(service: VoterProfileService) -> None:
    """A client-side profile is validated by the same model (14.3 depends on
    this), so anonymous mode is not a validation bypass.
    """
    parsed = service.validate(profile())
    assert parsed.interests[0].tier == "primary"
    with pytest.raises(ValidationError):
        service.validate(profile(interests=[interest(tier="secondary")]))


def test_the_row_id_is_the_hmac_of_the_user_id(service: VoterProfileService) -> None:
    row = service.save(USER, profile())
    assert row.id is not None
    assert USER not in row.id
    assert row.id.startswith("vp:")


def test_the_locale_is_carried_on_the_row_not_in_the_ciphertext(
    service: VoterProfileService,
) -> None:
    """datamodel.md puts `locale` on the outer row: it is needed to pick the
    taxonomy before anything is decrypted, and it is not an opinion.
    """
    row = service.save(USER, profile(), locale="en-GB")
    assert row.locale == "en-GB"


def test_the_jurisdictions_are_carried_on_the_row(service: VoterProfileService) -> None:
    row = service.save(USER, profile(), jurisdiction_ids=["us-federal"])
    assert row.jurisdiction_ids == ["us-federal"]


# =========================================================================
# Nothing in the write or read path logs plaintext or key material
# =========================================================================


def test_no_plaintext_reaches_the_logs(
    service: VoterProfileService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        service.save(USER, profile(interests=[interest(note="synthetic test note")]))
        service.load(USER)
        service.export(USER)
        service.delete(USER)
    text = "\n".join(f"{record.name} {record.getMessage()}" for record in caplog.records)
    for forbidden in (
        "test-topic-0",
        "test-stance",
        "synthetic test note",
        USER,
        ROOT.hex(),
    ):
        assert forbidden not in text, f"{forbidden!r} leaked into a log line"


def test_a_failed_save_logs_no_plaintext(
    service: VoterProfileService, caplog: pytest.LogCaptureFixture
) -> None:
    """A validation error's own message must not carry the interests either —
    a traceback is a log line in production.
    """
    with caplog.at_level(logging.DEBUG), pytest.raises(ValidationError) as caught:
        service.save(USER, profile(interests=[interest(1, tier="secondary")]))
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "test-topic-1" not in text
    assert "test-stance" not in str(caught.value)


def test_the_service_repr_carries_no_secrets(service: VoterProfileService) -> None:
    assert ROOT.hex() not in repr(service)
    assert base64.b64encode(ROOT).decode() not in repr(service)
