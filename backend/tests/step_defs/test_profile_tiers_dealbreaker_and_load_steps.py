"""Step definitions for tests/features/profile_tiers_dealbreaker_and_load.feature
(stories 4.2, 4.3, 4.4).

Runs the real FastAPI app against story 1.2's in-memory ZeroDB fake, exactly as
`test_quick_onboarding_steps.py` does for story 4.1 — no network call, no live
credentials needed in CI.

Every interest here is synthetic (`test:4-2-*`). CLAUDE.md forbids real voter
data in a fixture.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.main import create_app
from app.profiles import PROFILE_TABLES
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.methodology import TABLE_NAME as METHODOLOGY_TABLE
from app.profiles.router import profile_service, zerodb_client_for_profiles
from app.profiles.service import VoterProfileService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/profile_tiers_dealbreaker_and_load.feature")

TEST_ROOT_KEY = bytes(range(32))
SYNTHETIC_USER = "synthetic-4-2-voter"
SYNTHETIC_OTHER_USER = "synthetic-4-2-other-voter"


@pytest.fixture
def transport() -> FakeZeroDBTransport:
    fake = FakeZeroDBTransport()
    fake.break_server_side_filter()
    return fake


@pytest.fixture
def zdb(transport: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    # `PROFILE_TABLES` rather than a hand-written list: it is the single
    # sequence `app.bootstrap` and every profile fixture read, so a table added
    # to the feature (story 14.2's `voter_consents`) cannot be forgotten here
    # and surface as a live-only 404.
    for table in (*PROFILE_TABLES, METHODOLOGY_TABLE):
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
def result() -> dict[str, Any]:
    return {}


@given("a ZeroDB client backed by an in-memory fake")
def given_client(zdb: ZeroDBClient) -> None:
    del zdb


# --- GET /v1/profiles/methodology ------------------------------------------


@when("a voter requests the current tier weights")
def request_tier_weights(api: TestClient, result: dict[str, Any]) -> None:
    result["response"] = api.get("/v1/profiles/methodology")


@then(
    parsers.parse(
        "the weights are primary {primary:g}, secondary {secondary:g} and "
        "nice_to_have {nice_to_have:g}"
    )
)
def weights_match(
    result: dict[str, Any], primary: float, secondary: float, nice_to_have: float
) -> None:
    response = result["response"]
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["weights"] == {
        "primary": primary,
        "secondary": secondary,
        "nice_to_have": nice_to_have,
    }


@then("the response names which methodology version they came from")
def weights_name_version(result: dict[str, Any]) -> None:
    body = result["response"].json()
    assert body["methodology_version"], body


# --- Building tiered selections (story 4.2 / 4.3) --------------------------


def _interest(index: int, tier: str, *, dealbreaker: bool = False) -> dict[str, Any]:
    topic = f"test-4-2-{tier}-{index}"
    return {
        "topic_id": f"test:{topic}:v1",
        "stance_id": f"test:{topic}:v1:test-stance",
        "tier": tier,
        "intensity": 2,
        "dealbreaker": dealbreaker,
    }


@given(
    parsers.parse(
        "a tiered selection with {primary:d} primary, {secondary:d} secondary and "
        "{nice_to_have:d} nice-to-have interests"
    )
)
def tiered_selection_three_tiers(
    result: dict[str, Any], primary: int, secondary: int, nice_to_have: int
) -> None:
    interests = (
        [_interest(i, "primary") for i in range(primary)]
        + [_interest(i, "secondary") for i in range(secondary)]
        + [_interest(i, "nice_to_have") for i in range(nice_to_have)]
    )
    result["profile_body"] = {"interests": interests, "evidence_mode": "blend"}


@given(parsers.parse("a tiered selection with {primary:d} primary interests"))
def tiered_selection_primary_only(result: dict[str, Any], primary: int) -> None:
    result["profile_body"] = {
        "interests": [_interest(i, "primary") for i in range(primary)],
        "evidence_mode": "blend",
    }


@given(parsers.parse("a tiered selection with {primary:d} primary interest"))
def tiered_selection_one_primary(result: dict[str, Any], primary: int) -> None:
    tiered_selection_primary_only(result, primary)


@given(
    parsers.parse(
        "a tiered selection with {primary:d} primary and {secondary:d} secondary interests"
    )
)
def tiered_selection_primary_and_secondary(
    result: dict[str, Any], primary: int, secondary: int
) -> None:
    interests = [_interest(i, "primary") for i in range(primary)] + [
        _interest(i, "secondary") for i in range(secondary)
    ]
    result["profile_body"] = {"interests": interests, "evidence_mode": "blend"}


@given(
    parsers.parse(
        "a tiered selection with {primary:d} primary and {nice_to_have:d} nice-to-have interests"
    )
)
def tiered_selection_primary_and_nice_to_have(
    result: dict[str, Any], primary: int, nice_to_have: int
) -> None:
    interests = [_interest(i, "primary") for i in range(primary)] + [
        _interest(i, "nice_to_have") for i in range(nice_to_have)
    ]
    result["profile_body"] = {"interests": interests, "evidence_mode": "blend"}


@given("a tiered selection with a dealbreaker on a primary interest")
def tiered_selection_dealbreaker_primary(result: dict[str, Any]) -> None:
    result["profile_body"] = {
        "interests": [_interest(0, "primary", dealbreaker=True)],
        "evidence_mode": "blend",
    }


@given("a tiered selection with a dealbreaker on a secondary interest")
def tiered_selection_dealbreaker_secondary(result: dict[str, Any]) -> None:
    result["profile_body"] = {
        "interests": [
            _interest(0, "primary"),
            _interest(0, "secondary", dealbreaker=True),
        ],
        "evidence_mode": "blend",
    }


# --- Submitting and loading --------------------------------------------------


@when("the voter submits the profile with consent given")
def submit_consent_true(api: TestClient, result: dict[str, Any]) -> None:
    body = {"user_id": SYNTHETIC_USER, "consent": True, "profile": result["profile_body"]}
    result["response"] = api.post("/v1/profiles", json=body)


@when("the voter loads their profile")
def load_profile(api: TestClient, result: dict[str, Any]) -> None:
    result["load_response"] = api.get(f"/v1/profiles/{SYNTHETIC_USER}")


@when("a different voter loads their profile")
def load_profile_other_voter(api: TestClient, result: dict[str, Any]) -> None:
    result["load_response"] = api.get(f"/v1/profiles/{SYNTHETIC_OTHER_USER}")


@when("a voter with no saved profile loads their profile")
def load_profile_never_saved(api: TestClient, result: dict[str, Any]) -> None:
    result["load_response"] = api.get(f"/v1/profiles/{SYNTHETIC_OTHER_USER}")


# --- Then: save outcomes ------------------------------------------------------


@then(parsers.parse("the profile is saved at version {version:d}"))
def saved_at_version(result: dict[str, Any], version: int) -> None:
    response = result["response"]
    assert response.status_code == 200, response.text
    assert response.json()["version"] == version


@then("the save is refused with a 422 naming the primary limit")
def refused_primary_limit(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 422, response.text
    assert "primary" in response.json()["detail"].lower()


@then("the save is refused with a 422 naming the secondary limit")
def refused_secondary_limit(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 422, response.text
    assert "secondary" in response.json()["detail"].lower()


@then("the save is refused with a 422 naming dealbreaker")
def refused_dealbreaker(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 422, response.text
    assert "dealbreaker" in response.json()["detail"].lower()


# --- Then: load outcomes ------------------------------------------------------


@then("the loaded profile's summary shows that interest as a dealbreaker")
def loaded_profile_shows_dealbreaker(result: dict[str, Any]) -> None:
    response = result["load_response"]
    assert response.status_code == 200, response.text
    body = response.json()
    assert body is not None
    dealbreakers = [item for item in body["interests"] if item["dealbreaker"]]
    assert len(dealbreakers) == 1
    assert dealbreakers[0]["tier"] == "primary"


@then("the load returns no profile, with a 200 rather than an error")
def load_returns_nothing(result: dict[str, Any]) -> None:
    response = result["load_response"]
    assert response.status_code == 200, response.text
    assert response.json() is None


@then("the loaded profile has the same interests, tiers and stances as saved")
def loaded_profile_round_trips(result: dict[str, Any]) -> None:
    response = result["load_response"]
    assert response.status_code == 200, response.text
    loaded = response.json()
    saved_interests = result["profile_body"]["interests"]

    def key(item: dict[str, Any]) -> tuple[str, str, str]:
        return (item["topic_id"], item["stance_id"], item["tier"])

    def normalized(item: dict[str, Any]) -> dict[str, Any]:
        # The save request may omit an optional field (`note`) that the load
        # response always states explicitly — not a round-trip difference in
        # the stored data, just the two JSON shapes' own defaulting.
        return {"note": None, **item}

    assert sorted((normalized(i) for i in loaded["interests"]), key=key) == sorted(
        (normalized(i) for i in saved_interests), key=key
    )


@then("the different voter's load returns no profile")
def different_voter_load_returns_nothing(result: dict[str, Any]) -> None:
    response = result["load_response"]
    assert response.status_code == 200, response.text
    assert response.json() is None
