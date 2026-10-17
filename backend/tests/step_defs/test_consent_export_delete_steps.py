"""Step definitions for tests/features/consent_export_delete.feature
(stories 14.2, 14.3, 14.4).

Runs the real FastAPI app against story 1.2's in-memory ZeroDB fake, exactly as
`test_profile_tiers_dealbreaker_and_load_steps.py` does — no network call and
no live credentials in CI. The live half (that the real project actually stores
the consent row, that export round-trips through real ciphertext, and that
delete finishes inside a minute against the real API) is
`backend/scripts/verify_consent_export_delete_live.py`.

Every interest here is synthetic (`test:14-2-*`). CLAUDE.md forbids real voter
data in a fixture, a log or test output.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.main import create_app
from app.profiles.consent import (
    CONSENT_TABLE,
    CURRENT_CONSENT_VERSION,
    LAWFUL_BASIS,
    ConsentRecord,
)
from app.profiles.crypto import KEY_WRAP_TABLE, ProfileCipher, ProfileKeyring, ProfileRootKey
from app.profiles.methodology import TABLE_NAME as METHODOLOGY_TABLE
from app.profiles.models import VoterProfileRow
from app.profiles.router import profile_service, zerodb_client_for_profiles
from app.profiles.service import VoterProfileService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/consent_export_delete.feature")

TEST_ROOT_KEY = bytes(range(32))
SYNTHETIC_USER = "synthetic-14-2-voter"
SYNTHETIC_OTHER_USER = "synthetic-14-2-other-voter"

#: A version string this build deliberately does not know, for the
#: stale-client scenario. Dated in the past so it reads as a superseded
#: release rather than a typo.
UNKNOWN_CONSENT_VERSION = "1999.0"


@pytest.fixture
def transport() -> FakeZeroDBTransport:
    fake = FakeZeroDBTransport()
    # Story 1.2 found the live API can ignore a server-side filter. Breaking it
    # in the fake means `ConsentLedger.history`'s own defensive re-filter is
    # exercised here rather than only live — and a cross-voter consent leak
    # would fail these scenarios rather than waiting for production.
    fake.break_server_side_filter()
    return fake


@pytest.fixture
def zdb(transport: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    for table in ("voter_profiles", KEY_WRAP_TABLE, CONSENT_TABLE, METHODOLOGY_TABLE):
        client.create_table(table, {"id": "string"})
    return client


@pytest.fixture
def keyring(zdb: ZeroDBClient) -> ProfileKeyring:
    return ProfileKeyring(client=zdb, root_key=ProfileRootKey(TEST_ROOT_KEY))


@pytest.fixture
def api(zdb: ZeroDBClient, keyring: ProfileKeyring) -> Any:
    app = create_app()
    app.dependency_overrides[zerodb_client_for_profiles] = lambda: zdb
    app.dependency_overrides[profile_service] = lambda: VoterProfileService(
        client=zdb, keyring=keyring
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def service(zdb: ZeroDBClient, keyring: ProfileKeyring) -> VoterProfileService:
    """The service directly, for the steps that assert on stored rows rather
    than on HTTP — "no row of any kind was written", and the retained-ciphertext
    check that proves the key shred."""
    return VoterProfileService(client=zdb, keyring=keyring)


@pytest.fixture
def result() -> dict[str, Any]:
    return {}


@given("a ZeroDB client backed by an in-memory fake")
def given_client(zdb: ZeroDBClient) -> None:
    del zdb


# --- Building synthetic selections -------------------------------------------


def _interest(index: int, tier: str, *, dealbreaker: bool = False) -> dict[str, Any]:
    topic = f"test-14-2-{tier}-{index}"
    return {
        "topic_id": f"test:{topic}:v1",
        "stance_id": f"test:{topic}:v1:test-stance",
        "tier": tier,
        "intensity": 2,
        "dealbreaker": dealbreaker,
    }


@given(parsers.parse("a synthetic tiered selection with {primary:d} primary interest"))
@given(parsers.parse("a synthetic tiered selection with {primary:d} primary interests"))
def selection_primary_only(result: dict[str, Any], primary: int) -> None:
    result["profile_body"] = {
        "interests": [_interest(i, "primary") for i in range(primary)],
        "evidence_mode": "blend",
    }


@given(
    parsers.parse(
        "a synthetic tiered selection with {primary:d} primary, {secondary:d} secondary "
        "and {nice_to_have:d} nice-to-have interests"
    )
)
def selection_three_tiers(
    result: dict[str, Any], primary: int, secondary: int, nice_to_have: int
) -> None:
    result["profile_body"] = {
        "interests": (
            [_interest(i, "primary") for i in range(primary)]
            + [_interest(i, "secondary") for i in range(secondary)]
            + [_interest(i, "nice_to_have") for i in range(nice_to_have)]
        ),
        "evidence_mode": "blend",
    }


# --- Submitting --------------------------------------------------------------


def _submit(
    api: TestClient,
    result: dict[str, Any],
    *,
    user_id: str | None = SYNTHETIC_USER,
    consent: bool = True,
    consent_version: str = CURRENT_CONSENT_VERSION,
) -> None:
    body: dict[str, Any] = {
        "user_id": user_id,
        "consent": consent,
        "consent_version": consent_version,
        "profile": result["profile_body"],
    }
    result["response"] = api.post("/v1/profiles", json=body)


@when("the voter submits the profile with consent given")
@when("the voter submits the profile with consent given again")
@given("the voter has submitted the profile with consent given")
def submit_with_consent(api: TestClient, result: dict[str, Any]) -> None:
    _submit(api, result)


@when("the voter submits the profile with consent given again a day later")
def submit_with_consent_a_day_later(
    api: TestClient, result: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine re-consent, a day after the first.

    The consent id is content-addressed over (profile_ref, version, action,
    recorded_at) at second resolution, so two saves inside the same second are
    deliberately ONE consent event (see the sibling scenario). To exercise the
    append path, the clock behind the ledger is moved rather than the test
    sleeping or the id scheme being widened: the behaviour under test is "a
    later consent appends", and what makes it later is the timestamp, not the
    wall-clock delay.
    """
    from app.profiles import consent as consent_module

    monkeypatch.setattr(consent_module, "_now", lambda: "2026-10-17T12:00:00Z")
    _submit(api, result)


@given("a different voter has also submitted a profile with consent given")
def submit_other_voter(api: TestClient, result: dict[str, Any]) -> None:
    _submit(api, result, user_id=SYNTHETIC_OTHER_USER)
    assert result["response"].status_code == 200, result["response"].text


@when("the voter submits the profile with consent refused")
def submit_consent_refused(api: TestClient, result: dict[str, Any]) -> None:
    _submit(api, result, consent=False)


@when("the voter submits the profile consenting to an unknown consent version")
def submit_unknown_version(api: TestClient, result: dict[str, Any]) -> None:
    _submit(api, result, consent_version=UNKNOWN_CONSENT_VERSION)


@when("an anonymous voter submits the profile with consent given")
def submit_anonymous(api: TestClient, result: dict[str, Any]) -> None:
    _submit(api, result, user_id=None)


# --- Consent offer ------------------------------------------------------------


@when("a voter asks what they are being asked to consent to")
@when("an anonymous voter asks what they are being asked to consent to")
def request_consent_offer(api: TestClient, result: dict[str, Any]) -> None:
    # No identity on the request, deliberately, in both cases: the endpoint
    # takes none, which is itself the thing the anonymous scenario asserts.
    result["consent_response"] = api.get("/v1/profiles/consent")


@then("the response names the current consent version")
def offer_names_version(result: dict[str, Any]) -> None:
    response = result["consent_response"]
    assert response.status_code == 200, response.text
    assert response.json()["consent_version"] == CURRENT_CONSENT_VERSION


@then("the consent text says the interests are political-opinion data")
def offer_says_political_opinion(result: dict[str, Any]) -> None:
    body = result["consent_response"].json()
    text = " ".join([body["summary"], *body["what_is_stored"]]).lower()
    assert "political" in text
    assert "special-category" in text or "special category" in text


@then("the consent text offers anonymous mode as the alternative")
def offer_offers_anonymous(result: dict[str, Any]) -> None:
    body = result["consent_response"].json()
    assert "anonymous" in body["anonymous_alternative"].lower()
    # The alternative must be a real alternative, not a footnote: it has to say
    # the browser keeps the data and the server does not.
    assert "browser" in body["anonymous_alternative"].lower()


# --- Consent records ----------------------------------------------------------


def _consent_rows(zdb: ZeroDBClient, keyring: ProfileKeyring, user_id: str) -> list[ConsentRecord]:
    page = zdb.query_rows(CONSENT_TABLE, limit=100)
    ref = keyring.row_id(user_id)
    return [
        ConsentRecord.model_validate(row.row_data)
        for row in page.rows
        if row.row_data.get("profile_ref") == ref
    ]


@then("the profile is saved")
def profile_saved(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 200, response.text
    assert response.json()["version"] >= 1


@then(parsers.parse("the profile is saved at version {version:d}"))
def profile_saved_at_version(result: dict[str, Any], version: int) -> None:
    response = result["response"]
    assert response.status_code == 200, response.text
    assert response.json()["version"] == version


@then("a consent record exists for that voter")
def consent_record_exists(
    zdb: ZeroDBClient, keyring: ProfileKeyring, result: dict[str, Any]
) -> None:
    records = _consent_rows(zdb, keyring, SYNTHETIC_USER)
    assert len(records) == 1, records
    result["consent_records"] = records


@then("the consent record names the consent version the voter was shown")
def consent_record_names_version(result: dict[str, Any]) -> None:
    assert result["consent_records"][0].consent_version == CURRENT_CONSENT_VERSION


@then("the consent record carries the time consent was given")
def consent_record_has_time(result: dict[str, Any]) -> None:
    recorded_at = result["consent_records"][0].recorded_at
    assert recorded_at.endswith("Z"), recorded_at
    assert recorded_at.startswith("20"), recorded_at


@then("the consent record names the lawful basis as explicit consent")
def consent_record_names_basis(result: dict[str, Any]) -> None:
    assert result["consent_records"][0].lawful_basis == LAWFUL_BASIS
    assert "explicit-consent" in LAWFUL_BASIS


@then("the consent record contains no interest topic id")
def consent_record_has_no_interests(zdb: ZeroDBClient, keyring: ProfileKeyring) -> None:
    records = _consent_rows(zdb, keyring, SYNTHETIC_USER)
    assert records
    blob = " ".join(str(record.row_data()) for record in records)
    assert "test-14-2-" not in blob, "a consent record leaked an interest topic id"


@then("the consent record does not contain the voter's user id")
def consent_record_has_no_user_id(zdb: ZeroDBClient, keyring: ProfileKeyring) -> None:
    records = _consent_rows(zdb, keyring, SYNTHETIC_USER)
    assert records
    blob = " ".join(str(record.row_data()) for record in records)
    assert SYNTHETIC_USER not in blob, "a consent record leaked the user id"
    # And the handle it does carry is the HMAC the profile row uses, so this
    # table is no more joinable to accounts than `voter_profiles` is.
    assert records[0].profile_ref == keyring.row_id(SYNTHETIC_USER)


@then("no consent record exists for that voter")
def no_consent_record(zdb: ZeroDBClient, keyring: ProfileKeyring) -> None:
    assert _consent_rows(zdb, keyring, SYNTHETIC_USER) == []


@then(parsers.parse("exactly {count:d} consent record exists for that voter"))
@then(parsers.parse("exactly {count:d} consent records exist for that voter"))
def n_consent_records(
    zdb: ZeroDBClient, keyring: ProfileKeyring, result: dict[str, Any], count: int
) -> None:
    records = _consent_rows(zdb, keyring, SYNTHETIC_USER)
    assert len(records) == count, [record.row_data() for record in records]
    result["consent_records"] = records


@then("two consent records exist for that voter")
def two_consent_records(zdb: ZeroDBClient, keyring: ProfileKeyring, result: dict[str, Any]) -> None:
    records = _consent_rows(zdb, keyring, SYNTHETIC_USER)
    assert len(records) == 2, [record.row_data() for record in records]
    result["consent_records"] = records


@then("both consent records are still readable")
def both_records_readable(result: dict[str, Any]) -> None:
    records = result["consent_records"]
    assert all(record.consent_version == CURRENT_CONSENT_VERSION for record in records)
    assert all(record.action == "granted" for record in records)
    # Append-only means the earlier record was not replaced: two distinct ids.
    assert len({record.id for record in records}) == len(records)


@then("the earlier consent record was not overwritten")
def earlier_record_intact(result: dict[str, Any]) -> None:
    """The ledger holds both timestamps, oldest first, unmodified.

    This is the actual append-only assertion: a `record()` that upserted under
    a per-voter id would leave exactly one row carrying only the newer time,
    and every other assertion in this scenario would still pass.
    """
    records = result["consent_records"]
    times = [record.recorded_at for record in records]
    assert times == sorted(times), times
    assert times[0] != times[1], times
    assert times[1] == "2026-10-17T12:00:00Z", times


@then("a withdrawal record exists for that voter")
def withdrawal_record_exists(
    zdb: ZeroDBClient, keyring: ProfileKeyring, result: dict[str, Any]
) -> None:
    records = _consent_rows(zdb, keyring, SYNTHETIC_USER)
    withdrawals = [record for record in records if record.action == "withdrawn"]
    assert len(withdrawals) == 1, [record.row_data() for record in records]
    # The grant it withdraws is still there: append-only, not replace.
    assert any(record.action == "granted" for record in records)
    result["withdrawal"] = withdrawals[0]


@then("the withdrawal record carries the time consent was withdrawn")
def withdrawal_has_time(result: dict[str, Any]) -> None:
    assert result["withdrawal"].recorded_at.endswith("Z")
    # And it names the version that was granted, not just whatever is current —
    # so the ledger reads as a pair.
    assert result["withdrawal"].consent_version == CURRENT_CONSENT_VERSION


# --- Refusals -----------------------------------------------------------------


@then("the save is refused with a 422 naming consent")
def refused_naming_consent(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 422, response.text
    assert "consent" in response.json()["detail"].lower()


@then("the save is refused with a 422 naming the consent version")
def refused_naming_consent_version(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert UNKNOWN_CONSENT_VERSION in detail, detail
    assert "consent version" in detail.lower()


@then("the save is refused with a 422 naming the primary limit")
def refused_primary_limit(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 422, response.text
    assert "primary" in response.json()["detail"].lower()


@then("the save is refused with a 422 naming anonymous mode")
def refused_anonymous(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 422, response.text
    assert "anonymous" in response.json()["detail"].lower()


@then("no profile row is written for that voter")
def no_profile_row(zdb: ZeroDBClient, keyring: ProfileKeyring) -> None:
    assert zdb.get_row(VoterProfileRow.table_name, keyring.row_id(SYNTHETIC_USER)) is None


@then("no row of any kind was written")
def no_rows_at_all(zdb: ZeroDBClient) -> None:
    """Story 14.3's actual guarantee, asserted over every table the profile
    feature touches rather than just the profile table: an anonymous voter
    must leave no profile row, no key wrap AND (new in 14.2) no consent row.
    The consent row is the one a naive 14.2 implementation would have written
    before noticing the voter is anonymous, which is exactly why it is here."""
    for table in (VoterProfileRow.table_name, KEY_WRAP_TABLE, CONSENT_TABLE):
        page = zdb.query_rows(table, limit=100)
        assert page.rows == [], f"anonymous mode wrote {len(page.rows)} row(s) to {table}"


# --- Anonymous-mode validation (no storage) -----------------------------------


@then("validating that selection without storing it is refused, naming the primary limit")
def anonymous_validation_refused(service: VoterProfileService, result: dict[str, Any]) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as caught:
        service.validate(result["profile_body"])
    assert "primary" in str(caught.value).lower()


@then("validating that selection without storing it is accepted")
def anonymous_validation_accepted(service: VoterProfileService, result: dict[str, Any]) -> None:
    profile = service.validate(result["profile_body"])
    assert profile.interests


# --- Export -------------------------------------------------------------------


@when("the voter exports their profile")
def export_profile(api: TestClient, result: dict[str, Any]) -> None:
    result["export_response"] = api.get(f"/v1/profiles/{SYNTHETIC_USER}/export")


@when("a voter with no saved profile exports their profile")
@when("a different voter exports their profile")
def export_other_voter(api: TestClient, result: dict[str, Any]) -> None:
    result["export_response"] = api.get(f"/v1/profiles/{SYNTHETIC_OTHER_USER}/export")


@then("the export is JSON with a 200")
def export_is_json(result: dict[str, Any]) -> None:
    response = result["export_response"]
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert isinstance(response.json(), dict)


@then("the exported profile has the same interests, tiers and stances as saved")
def export_round_trips(result: dict[str, Any]) -> None:
    exported = result["export_response"].json()["profile"]["interests"]
    saved = result["profile_body"]["interests"]

    def key(item: dict[str, Any]) -> tuple[str, str, str]:
        return (item["topic_id"], item["stance_id"], item["tier"])

    def normalized(item: dict[str, Any]) -> dict[str, Any]:
        # The save request omits the optional `note`; the export always states
        # it. Not a round-trip difference in the stored data — the same
        # normalisation story 4.4's own round-trip step applies.
        return {"note": None, **item}

    assert sorted((normalized(i) for i in exported), key=key) == sorted(
        (normalized(i) for i in saved), key=key
    )


@then("the export states which version was exported")
def export_states_version(result: dict[str, Any]) -> None:
    assert result["export_response"].json()["version"] >= 1


@then("the export states when it was produced")
def export_states_time(result: dict[str, Any]) -> None:
    assert result["export_response"].json()["exported_at"].endswith("Z")


@then("the export includes the consent history")
def export_includes_consent(result: dict[str, Any]) -> None:
    consent = result["export_response"].json()["consent"]
    assert len(consent) >= 1, consent
    assert consent[0]["action"] == "granted"
    assert consent[0]["lawful_basis"] == LAWFUL_BASIS
    # The HMAC handle must NOT travel in a document the voter may forward.
    assert "profile_ref" not in consent[0], consent[0]


@then("the consent history in the export names the consent version")
def export_consent_names_version(result: dict[str, Any]) -> None:
    consent = result["export_response"].json()["consent"]
    assert consent[0]["consent_version"] == CURRENT_CONSENT_VERSION


@then("the export returns no profile, with a 404")
def export_404(result: dict[str, Any]) -> None:
    response = result["export_response"]
    assert response.status_code == 404, response.text
    # And the message tells an anonymous-mode voter why there is nothing here,
    # rather than implying their data was lost.
    assert "anonymous" in response.json()["detail"].lower()


# --- Delete -------------------------------------------------------------------


@when("the voter deletes their profile")
@when("the voter deletes their profile again")
def delete_profile(
    api: TestClient,
    service: VoterProfileService,
    keyring: ProfileKeyring,
    zdb: ZeroDBClient,
    result: dict[str, Any],
) -> None:
    # Retain the ciphertext BEFORE the delete, so the "decrypting a retained
    # copy now fails" step has something to try — the same technique
    # story 14.1's own feature and live script use, and the strongest available
    # form of the key-shred claim: holding the ciphertext and the root key,
    # decryption must still fail.
    row = zdb.get_row(VoterProfileRow.table_name, keyring.row_id(SYNTHETIC_USER))
    if row is not None:
        result.setdefault("retained_ciphertext", row.row_data["payload"])

    started = time.monotonic()
    result["delete_response"] = api.request("DELETE", f"/v1/profiles/{SYNTHETIC_USER}")
    result["delete_elapsed"] = time.monotonic() - started
    del service


@when("a voter with no saved profile deletes their profile")
def delete_never_saved(api: TestClient, result: dict[str, Any]) -> None:
    started = time.monotonic()
    result["delete_response"] = api.request("DELETE", f"/v1/profiles/{SYNTHETIC_OTHER_USER}")
    result["delete_elapsed"] = time.monotonic() - started


@then("the delete succeeds")
def delete_succeeds(result: dict[str, Any]) -> None:
    response = result["delete_response"]
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True, body
    assert body["key_shredded"] is True, body
    assert body["consent_withdrawal_recorded"] is True, body


@then("the profile row is gone")
def profile_row_gone(zdb: ZeroDBClient, keyring: ProfileKeyring) -> None:
    assert zdb.get_row(VoterProfileRow.table_name, keyring.row_id(SYNTHETIC_USER)) is None


@then("the wrapped data key for that voter is gone")
def key_wrap_gone(keyring: ProfileKeyring) -> None:
    assert keyring.stored_wrap(SYNTHETIC_USER) is None


@then("decrypting a retained copy of the ciphertext now fails")
def retained_ciphertext_undecryptable(keyring: ProfileKeyring, result: dict[str, Any]) -> None:
    ciphertext = result["retained_ciphertext"]
    row_id = keyring.row_id(SYNTHETIC_USER)
    with pytest.raises(Exception):  # noqa: B017, PT011 - any failure is the point
        # A fresh key for the same voter must not open the old ciphertext
        # either: the data key is random, not derived, so a replacement is a
        # different key (app.profiles.crypto's module docstring).
        ProfileCipher(keyring.ensure_data_key(SYNTHETIC_USER)).decrypt(ciphertext, aad=row_id)


@then(parsers.parse("the delete took less than {seconds:d} seconds"))
def delete_under_budget(result: dict[str, Any], seconds: int) -> None:
    elapsed = result["delete_elapsed"]
    assert elapsed < seconds, f"delete took {elapsed:.3f}s, budget {seconds}s"
    # The server's own measurement agrees with the wall clock around the call.
    reported = result["delete_response"].json()["elapsed_seconds"]
    assert reported <= elapsed + 0.5, (reported, elapsed)


@when("the voter loads their profile")
def load_profile(api: TestClient, result: dict[str, Any]) -> None:
    result["load_response"] = api.get(f"/v1/profiles/{SYNTHETIC_USER}")


@then("the load returns no profile, with a 200 rather than an error")
def load_returns_nothing(result: dict[str, Any]) -> None:
    response = result["load_response"]
    assert response.status_code == 200, response.text
    assert response.json() is None


@then("the different voter's profile is still loadable")
def other_voter_still_loadable(api: TestClient) -> None:
    response = api.get(f"/v1/profiles/{SYNTHETIC_OTHER_USER}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body is not None, "deleting one voter's profile destroyed another's"
    assert body["interests"]
