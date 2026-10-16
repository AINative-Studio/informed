"""Step definitions for tests/features/quick_onboarding.feature (story 4.1).

Runs the real FastAPI app against story 1.2's in-memory ZeroDB fake, through
the real `TopicService`, `StanceService` and `VoterProfileService`, exactly
as `test_ballot_lookup_steps.py` runs the ballot endpoint — CI makes no
network call and no live credentials are needed.

Every voter and profile here is synthetic (`test:4-1-*`). CLAUDE.md forbids
real voter data in a fixture, and a fixture shaped like a real voter's
political opinions is exactly the data story 14.1 encrypts.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.main import create_app
from app.profiles.crypto import KEY_WRAP_TABLE, ProfileKeyring, ProfileRootKey
from app.profiles.router import profile_service, zerodb_client_for_profiles
from app.profiles.service import VoterProfileService
from app.seed_stances import StanceSeeder
from app.seed_taxonomy import TaxonomySeeder
from app.stances.router import stance_service, zerodb_client_for_stances
from app.stances.service import StanceService
from app.topics.router import topic_service, zerodb_client_for_topics
from app.topics.service import TopicService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/quick_onboarding.feature")

#: A 32-byte test root key, obvious and never written to a file the repo
#: tracks — the same pattern story 14.1's own tests use.
TEST_ROOT_KEY = bytes(range(32))

#: Three synthetic subtopics with real stance cards, so "3 primary topics with
#: one stance each" is an onboarding selection that actually validates.
PRIMARY_SUBTOPICS = (
    "en-US:prescription-drug-pricing:v1",
    "en-US:broadband-access:v1",
    "en-US:public-lands-and-conservation:v1",
)

SYNTHETIC_USER = "synthetic-4-1-voter"


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
    for table in ("issue_topics", "stances", "voter_profiles", KEY_WRAP_TABLE):
        client.create_table(table, {"id": "string"})
    return client


@pytest.fixture
def keyring(zdb: ZeroDBClient) -> ProfileKeyring:
    return ProfileKeyring(client=zdb, root_key=ProfileRootKey(TEST_ROOT_KEY))


@pytest.fixture
def api(zdb: ZeroDBClient, keyring: ProfileKeyring) -> Any:
    """The real app, wired to the fake ZeroDB client for all three new routers."""
    app = create_app()
    for dependency in (
        zerodb_client_for_topics,
        zerodb_client_for_stances,
        zerodb_client_for_profiles,
    ):
        app.dependency_overrides[dependency] = lambda: zdb
    app.dependency_overrides[topic_service] = lambda: TopicService(zdb)
    app.dependency_overrides[stance_service] = lambda: StanceService(zdb)
    app.dependency_overrides[profile_service] = lambda: VoterProfileService(
        client=zdb, keyring=keyring
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def result() -> dict[str, Any]:
    return {}


# --- Background -------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def given_client(zdb: ZeroDBClient) -> None:
    # Materialised by the `zdb`/`api` fixtures; nothing to do but depend on it
    # so the Background line reads naturally, matching every other feature's
    # step library in this suite.
    del zdb


@given("the issue taxonomy has been seeded")
def taxonomy_seeded(zdb: ZeroDBClient, transport: FakeZeroDBTransport) -> None:
    TaxonomySeeder(zdb, pace=lambda: None).run()
    transport.calls.clear()


@given("the stance cards have been seeded")
def stances_seeded(zdb: ZeroDBClient, transport: FakeZeroDBTransport) -> None:
    StanceSeeder(zdb, pace=lambda: None).run()
    transport.calls.clear()


# --- GET /v1/topics ----------------------------------------------------------


@when("a voter requests the topic tree")
def request_topic_tree(api: TestClient, result: dict[str, Any]) -> None:
    result["response"] = api.get("/v1/topics")


@then(parsers.parse("the response lists {count:d} top-level topics"))
def topic_tree_top_level_count(result: dict[str, Any], count: int) -> None:
    body = result["response"].json()
    assert result["response"].status_code == 200, result["response"].text
    assert len(body["topics"]) == count, [topic["id"] for topic in body["topics"]]


@then("every top-level topic carries its subtopics nested under it")
def every_topic_has_subtopics(result: dict[str, Any]) -> None:
    body = result["response"].json()
    empty = [topic["id"] for topic in body["topics"] if not topic["subtopics"]]
    assert not empty, f"top-level topics with no nested subtopics: {empty}"


@then(parsers.parse("the tree holds {count:d} subtopics in total"))
def total_subtopic_count(result: dict[str, Any], count: int) -> None:
    body = result["response"].json()
    total = sum(len(topic["subtopics"]) for topic in body["topics"])
    assert total == count, f"got {total} subtopics across the tree"


@then("no topic or subtopic name in the tree reads as a suggestion")
def tree_names_are_neutral(result: dict[str, Any]) -> None:
    """The same neutrality rule story 3.1 already pins, re-checked over the
    wire response rather than over the seed's own in-memory rows — so a bug
    in this story's own serialization (a hard-coded label, say) cannot slip
    past 3.1's test by construction.
    """
    from app.seed_taxonomy import contains_stance_presupposing_word

    body = result["response"].json()
    offenders = []
    for topic in body["topics"]:
        if contains_stance_presupposing_word(topic["name"]):
            offenders.append(topic["name"])
        offenders.extend(
            sub["name"]
            for sub in topic["subtopics"]
            if contains_stance_presupposing_word(sub["name"])
        )
    assert not offenders, f"stance-presupposing names in the tree response: {offenders}"


# --- GET /v1/topics/{id}/stances ---------------------------------------------


@when(parsers.parse('a voter requests stance options for "{topic_id}"'))
def request_stance_options(api: TestClient, result: dict[str, Any], topic_id: str) -> None:
    result["response"] = api.get(f"/v1/topics/{topic_id}/stances")


@then(parsers.parse("at least {minimum:d} stance options are returned"))
def minimum_stance_options(result: dict[str, Any], minimum: int) -> None:
    body = result["response"].json()
    assert result["response"].status_code == 200, result["response"].text
    assert len(body["options"]) >= minimum, body["options"]


@then("none of the returned stance options is marked as selected or default")
def no_default_marked(result: dict[str, Any]) -> None:
    body = result["response"].json()
    for option in body["options"]:
        assert set(option) == {"id", "label", "description"}, option


@then("an empty list of stance options is returned")
def empty_stance_options(result: dict[str, Any]) -> None:
    body = result["response"].json()
    assert result["response"].status_code == 200, result["response"].text
    assert body["options"] == []


# --- POST /v1/profiles: building a quick-onboarding selection ---------------


def _interest(topic_id: str, stance_id: str, *, tier: str = "primary") -> dict[str, Any]:
    return {
        "topic_id": topic_id,
        "stance_id": stance_id,
        "tier": tier,
        "intensity": 2,
        "dealbreaker": False,
    }


@given("a completed quick-onboarding selection of 3 primary topics with one stance each")
def completed_selection(api: TestClient, result: dict[str, Any]) -> None:
    interests = []
    for topic_id in PRIMARY_SUBTOPICS:
        stances = api.get(f"/v1/topics/{topic_id}/stances").json()["options"]
        assert stances, f"no stance options for {topic_id}; fixture topic is wrong"
        interests.append(_interest(topic_id, stances[0]["id"]))
    result["profile_body"] = {"interests": interests, "evidence_mode": "blend"}


@given("a quick-onboarding selection with no primary topics chosen")
def incomplete_selection(result: dict[str, Any]) -> None:
    result["profile_body"] = {
        "interests": [
            _interest(PRIMARY_SUBTOPICS[0], "unused-stance-id", tier="secondary"),
        ],
        "evidence_mode": "blend",
    }


# --- POST /v1/profiles: submitting ------------------------------------------


@when("the voter submits the profile without the consent field")
def submit_without_consent(api: TestClient, result: dict[str, Any]) -> None:
    body = {"user_id": SYNTHETIC_USER, "profile": result["profile_body"]}
    result["response"] = api.post("/v1/profiles", json=body)


@when("the voter submits the profile with consent explicitly false")
def submit_consent_false(api: TestClient, result: dict[str, Any]) -> None:
    body = {"user_id": SYNTHETIC_USER, "consent": False, "profile": result["profile_body"]}
    result["response"] = api.post("/v1/profiles", json=body)


@when("the voter submits the profile with consent given")
def submit_consent_true(api: TestClient, result: dict[str, Any]) -> None:
    body = {"user_id": SYNTHETIC_USER, "consent": True, "profile": result["profile_body"]}
    result["response"] = api.post("/v1/profiles", json=body)


@when("the voter submits the profile with consent given but no account")
def submit_consent_true_anonymous(api: TestClient, result: dict[str, Any]) -> None:
    body = {"user_id": None, "consent": True, "profile": result["profile_body"]}
    result["response"] = api.post("/v1/profiles", json=body)


# --- Then --------------------------------------------------------------------


@then(parsers.parse("the save is refused with a {status:d} naming the missing consent"))
def refused_missing_consent(result: dict[str, Any], status: int) -> None:
    """Two valid shapes name "consent", and both are acceptable here.

    Omitting the field entirely never reaches `save_profile`'s own check —
    FastAPI's request-body validation rejects it first, as a missing
    required field, and reports that as a 422 whose `detail` is pydantic's
    own list of errors rather than a plain string. Sending `consent: false`
    DOES reach the handler, whose own 422 carries a plain string `detail`.
    Both are "a 422 naming consent", just shaped differently by which layer
    caught it, so this step accepts either.
    """
    response = result["response"]
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    text = detail.lower() if isinstance(detail, str) else str(detail).lower()
    assert "consent" in text, detail


@then(parsers.parse("the save is refused with a {status:d} naming the missing primary interest"))
def refused_missing_primary(result: dict[str, Any], status: int) -> None:
    response = result["response"]
    assert response.status_code == status, response.text
    assert "primary" in response.json()["detail"].lower()


@then(parsers.parse("the profile is saved at version {version:d}"))
def saved_at_version(result: dict[str, Any], version: int) -> None:
    response = result["response"]
    assert response.status_code == 200, response.text
    assert response.json()["version"] == version


@then("no candidate score is shown until at least one primary interest exists")
def no_score_in_save_response(result: dict[str, Any]) -> None:
    """Scoring (epic 6) does not exist yet, so the only claim this story can
    make is the one actually checkable today: a successful save's own
    response carries no score-shaped field at all.
    """
    body = result["response"].json()
    assert set(body) == {"version", "locale", "updated_at"}, body


@then("loading the voter's profile returns the same 3 primary topics")
def profile_round_trips(result: dict[str, Any], zdb: ZeroDBClient, keyring: ProfileKeyring) -> None:
    service = VoterProfileService(client=zdb, keyring=keyring)
    loaded = service.load(SYNTHETIC_USER)
    assert loaded is not None
    assert {interest.topic_id for interest in loaded.by_tier("primary")} == set(PRIMARY_SUBTOPICS)


@then("the save is refused because anonymous mode writes no row")
def refused_anonymous(result: dict[str, Any], zdb: ZeroDBClient) -> None:
    response = result["response"]
    assert response.status_code == 422, response.text
    assert "anonymous" in response.json()["detail"].lower()
    assert zdb.query_rows("voter_profiles", limit=10).rows == []
