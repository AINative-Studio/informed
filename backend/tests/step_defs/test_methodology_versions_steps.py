"""Step definitions for tests/features/methodology_versions.feature (story 6.1).

Runs against story 1.2's in-memory fake transport, never live ZeroDB, so CI
needs no credentials — the rule every backend feature in this suite follows.
The live confirmation lives in `scripts/verify_methodology_versions_live.py`.

`break_server_side_filter()` is applied for the reason stories 3.1, 3.2 and
12.2 apply it: live ZeroDB's server-side `/query` filter matches nothing
(story 1.4's finding), so every read here rests on the client's own paging
fallback. Without it, "exactly one current version" would pass through a
`filter={"is_current": True}` that does not actually work in production — the
single most important thing in this story to not prove for the wrong reason.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import TABLE_SCHEMAS, Bootstrapper
from app.integrity.checks import check_current_uniqueness, run_all_checks
from app.main import create_app
from app.methodology.models import DOCUMENTED_WEIGHT_GROUPS
from app.methodology.publish import (
    DEFAULT_EVIDENCE_BLEND,
    DEFAULT_VOTE_TYPE_WEIGHTS,
    MethodologyPublisher,
    MethodologyPublishError,
)
from app.methodology.router import zerodb_client_for_methodology
from app.methodology.service import PublicMethodologyService
from app.profiles.methodology import TABLE_NAME, MethodologyVersionService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/methodology_versions.feature")

#: Words that would mean voter data had reached an event payload. datamodel.md
#: is explicit that `methodology.published` carries `version` and nothing else;
#: checked against the serialized payload rather than the model, because the
#: question is what bytes a consumer actually receives.
VOTER_WORDS = ("voter", "profile", "interest", "payload", "dealbreaker", "user_id", "stance")

#: The event topic datamodel.md's Events table documents for this story.
TOPIC = "methodology.published"


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    """A fake-backed client with the whole schema bootstrapped.

    The full `Bootstrapper` rather than just `methodology_versions`, the way
    story 16.2's own steps do it: this story's integrity scenarios run
    `run_all_checks`, and every other check reads a table of its own. Creating
    only this story's table would make those checks raise "table not found"
    and the scenario would fail for a reason that has nothing to do with
    methodology versions.
    """
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(client, pace=lambda: None).run()
    assert TABLE_NAME in TABLE_SCHEMAS
    return client


@pytest.fixture
def publisher(zerodb: ZeroDBClient) -> MethodologyPublisher:
    return MethodologyPublisher(zerodb)


@pytest.fixture
def methodology_api(zerodb: ZeroDBClient) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[zerodb_client_for_methodology] = lambda: zerodb
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seen() -> dict[str, Any]:
    """Carries results between When and Then."""
    return {}


def _stored(zerodb: ZeroDBClient) -> list[dict[str, Any]]:
    return [row.row_data for row in zerodb.query_rows(TABLE_NAME, limit=500).rows]


def _events(zerodb: ZeroDBClient, topic: str = TOPIC) -> list[Any]:
    return list(zerodb.list_events(topic=topic, limit=500).events)


# --- Given ----------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _client_backed_by_fake(zerodb: ZeroDBClient) -> None:
    assert zerodb is not None


@given("the methodology_versions table exists")
def _table_exists(zerodb: ZeroDBClient) -> None:
    tables = zerodb.list_tables()
    assert any(table.table_name == TABLE_NAME for table in tables)


@given("the methodology_versions table is empty")
def _table_empty(zerodb: ZeroDBClient) -> None:
    assert not _stored(zerodb)


@given("the current methodology version has been published")
def _current_published(zerodb: ZeroDBClient) -> None:
    """Story 4.2's own seed path, used verbatim — not a hand-written row.

    The scene these scenarios set is "a project as story 4.2 left it", which
    is exactly what the live project was before this story: one `2026.1` row
    carrying `tiers` and nothing else.
    """
    MethodologyVersionService(zerodb).current_version()


@given("two methodology versions are both marked current")
def _two_current(zerodb: ZeroDBClient) -> None:
    """The torn-write state the integrity check exists to catch.

    Written directly through the client rather than through the publisher,
    deliberately: the publisher is what makes this state unreachable, so the
    check can only be exercised by bypassing it — the same way story 16.2's
    own steps construct a double-current `verdicts` table.
    """
    MethodologyVersionService(zerodb).current_version()
    zerodb.upsert_row(
        TABLE_NAME,
        "2027.1",
        {
            "id": "2027.1",
            "published_at": "2027-01-01T00:00:00Z",
            "weights": {"tiers": {"primary": 3.0, "secondary": 1.5, "nice_to_have": 0.5}},
            "changelog_url": "https://informed.example/methodology/2027.1",
            "is_current": True,
        },
    )


@given("a methodology version is stored with no current version marked")
def _none_current(zerodb: ZeroDBClient) -> None:
    MethodologyVersionService(zerodb).current_version()
    zerodb.update_row(TABLE_NAME, "2026.1", {"is_current": False})


# --- When -----------------------------------------------------------------


@when(parsers.parse('the methodology owner publishes version "{version}"'))
def _publish(publisher: MethodologyPublisher, seen: dict[str, Any], version: str) -> None:
    seen["published"] = publisher.publish_new_version(version)


@when(parsers.parse('the methodology owner tries to publish version "{version}" again'))
@when(parsers.parse('the methodology owner tries to publish version "{version}"'))
def _try_publish(publisher: MethodologyPublisher, seen: dict[str, Any], version: str) -> None:
    seen["events_before"] = len(_events(publisher.client))
    try:
        seen["published"] = publisher.publish_new_version(version)
    except MethodologyPublishError as error:
        seen["error"] = error


@when("something reads the current methodology and the default is seeded")
def _seed_read(zerodb: ZeroDBClient) -> None:
    MethodologyVersionService(zerodb).current_version()


@when("the integrity checks run")
def _run_checks(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    seen["report"] = check_current_uniqueness(zerodb)
    # Also run the whole suite, so a new check wired into `ALL_CHECKS` is
    # proven to actually be in the nightly run rather than only callable.
    seen["combined"] = run_all_checks(zerodb)


@when("anyone requests the methodology with no authentication")
def _request_current(methodology_api: TestClient, seen: dict[str, Any]) -> None:
    seen["response"] = methodology_api.get("/v1/methodology")


@when(parsers.parse('anyone requests methodology version "{version}"'))
def _request_version(methodology_api: TestClient, seen: dict[str, Any], version: str) -> None:
    seen["response"] = methodology_api.get(f"/v1/methodology/{version}")


# --- Then: the weights ----------------------------------------------------


@then(parsers.parse('the published version carries weight groups "{groups}"'))
def _carries_groups(seen: dict[str, Any], groups: str) -> None:
    expected = [name.strip() for name in groups.split(",")]
    weights = seen["published"].weights
    assert sorted(weights) == sorted(expected), weights
    for name in expected:
        assert weights[name], f"{name} is present but empty, which is not a published weighting"


@then("no weight group is reported as not yet published")
def _nothing_unpublished(zerodb: ZeroDBClient) -> None:
    published = PublicMethodologyService(zerodb).current()
    assert published.weights_not_yet_published == []
    assert sorted(published.weights) == sorted(DOCUMENTED_WEIGHT_GROUPS)


@then(
    parsers.parse(
        "the evidence blend weights votes {votes:g}, sponsorships {sponsorships:g} "
        "and statements {statements:g}"
    )
)
def _blend_is(seen: dict[str, Any], votes: float, sponsorships: float, statements: float) -> None:
    blend = seen["published"].weights["evidence_blend"]
    assert blend == {
        "votes": votes,
        "sponsorships": sponsorships,
        "statements": statements,
    }, blend
    # Pinned against the module constant too, so prd.md's numbers and the
    # seeded default cannot drift from each other silently.
    assert blend == DEFAULT_EVIDENCE_BLEND


@then(parsers.parse("the evidence blend weights sum to {total:g}"))
def _blend_sums(seen: dict[str, Any], total: float) -> None:
    blend = seen["published"].weights["evidence_blend"]
    assert sum(blend.values()) == pytest.approx(total)


@then(parsers.parse('the vote type weights cover "{types}"'))
def _vote_types_cover(seen: dict[str, Any], types: str) -> None:
    expected = [name.strip() for name in types.split(",")]
    vote_types = seen["published"].weights["vote_types"]
    assert sorted(vote_types) == sorted(expected), vote_types
    assert sorted(vote_types) == sorted(DEFAULT_VOTE_TYPE_WEIGHTS)


@then(parsers.parse("final passage carries weight {weight:g}"))
def _final_passage_weight(seen: dict[str, Any], weight: float) -> None:
    assert seen["published"].weights["vote_types"]["final_passage"] == weight


@then("every other vote type carries less weight than final passage")
def _final_passage_highest(seen: dict[str, Any]) -> None:
    vote_types = dict(seen["published"].weights["vote_types"])
    final = vote_types.pop("final_passage")
    for name, weight in vote_types.items():
        assert weight < final, f"{name} ({weight}) must weigh less than final_passage ({final})"


@then(parsers.parse('"{lighter}" is weighted lower than "{heavier}"'))
def _ordered(seen: dict[str, Any], lighter: str, heavier: str) -> None:
    vote_types = seen["published"].weights["vote_types"]
    assert vote_types[lighter] < vote_types[heavier], vote_types


@then(parsers.parse("every vote type weight is greater than 0 and at most {ceiling:g}"))
def _usable_multipliers(seen: dict[str, Any], ceiling: float) -> None:
    for name, weight in seen["published"].weights["vote_types"].items():
        assert 0 < weight <= ceiling, f"{name} = {weight} is not a usable multiplier"


@then(
    parsers.parse(
        "the published tier weights are primary {primary:g}, secondary {secondary:g} "
        "and nice_to_have {nice:g}"
    )
)
def _tier_weights(seen: dict[str, Any], primary: float, secondary: float, nice: float) -> None:
    tiers = seen["published"].weights["tiers"]
    assert tiers == {"primary": primary, "secondary": secondary, "nice_to_have": nice}


# --- Then: the event ------------------------------------------------------


@then(parsers.parse('exactly one "{topic}" event has been emitted'))
def _one_event(zerodb: ZeroDBClient, topic: str) -> None:
    events = _events(zerodb, topic)
    assert len(events) == 1, f"expected exactly 1 {topic} event, got {len(events)}"


@then(parsers.parse('no "{topic}" event has been emitted'))
def _no_event(zerodb: ZeroDBClient, topic: str) -> None:
    assert _events(zerodb, topic) == []


@then(parsers.parse('no new "{topic}" event has been emitted'))
def _no_new_event(zerodb: ZeroDBClient, seen: dict[str, Any], topic: str) -> None:
    assert len(_events(zerodb, topic)) == seen["events_before"]


@then(parsers.parse('the event names version "{version}"'))
def _event_names_version(zerodb: ZeroDBClient, version: str) -> None:
    (event,) = _events(zerodb)
    assert event.event_payload.get("version") == version, event.event_payload


@then("the emitted event mentions no voter, profile or interest data")
def _event_is_clean(zerodb: ZeroDBClient) -> None:
    (event,) = _events(zerodb)
    # datamodel.md's payload for this event is `version` and nothing else.
    assert set(event.event_payload) == {"version"}, event.event_payload
    # Scanned over the PAYLOAD, not the whole envelope: ZeroDB's own wrapper
    # field is literally named `event_payload`, so scanning the envelope would
    # match the word "payload" every time and prove nothing about the contents.
    serialized = json.dumps(event.event_payload).lower()
    for word in VOTER_WORDS:
        assert word not in serialized, f"{word!r} appears in a published event payload"


# --- Then: exactly one current --------------------------------------------


@then("exactly one stored version is marked current")
def _one_current(zerodb: ZeroDBClient) -> None:
    current = [row for row in _stored(zerodb) if row.get("is_current") is True]
    assert len(current) == 1, [row.get("id") for row in current]


@then(parsers.parse('the current version is "{version}"'))
def _current_is(zerodb: ZeroDBClient, version: str) -> None:
    assert MethodologyVersionService(zerodb).current_version().id == version


@then(parsers.parse('version "{version}" is still stored and is not current'))
def _still_stored(zerodb: ZeroDBClient, version: str) -> None:
    row = zerodb.get_row(TABLE_NAME, version)
    assert row is not None, f"{version} was not kept — history must never be deleted"
    assert row.row_data["is_current"] is False


@then(
    parsers.parse(
        'version "{version}" still reports tier weights primary {primary:g}, '
        "secondary {secondary:g} and nice_to_have {nice:g}"
    )
)
def _superseded_keeps_weights(
    zerodb: ZeroDBClient, version: str, primary: float, secondary: float, nice: float
) -> None:
    published = PublicMethodologyService(zerodb).version(version)
    assert published.weights["tiers"] == {
        "primary": primary,
        "secondary": secondary,
        "nice_to_have": nice,
    }


@then("the publication is refused")
def _refused(seen: dict[str, Any]) -> None:
    assert isinstance(seen.get("error"), MethodologyPublishError), seen


# --- Then: the integrity check --------------------------------------------


@then("the is_current uniqueness check passes")
def _check_passes(seen: dict[str, Any]) -> None:
    assert seen["report"].ok, seen["report"].summary()


@then(parsers.parse("the is_current uniqueness check reports a {table} violation"))
def _check_reports(seen: dict[str, Any], table: str) -> None:
    violations = [v for v in seen["report"].violations if v.table == table]
    assert violations, seen["report"].summary()
    # And the same violation is visible through the whole nightly suite, not
    # only through the check called directly.
    combined = [v for v in seen["combined"].violations if v.table == table]
    assert combined, seen["combined"].summary()


# --- Then: the public page ------------------------------------------------


@then(parsers.parse("the response is {status:d}"))
def _status_is(seen: dict[str, Any], status: int) -> None:
    assert seen["response"].status_code == status, seen["response"].text


@then(parsers.parse('the response names methodology version "{version}"'))
def _response_names(seen: dict[str, Any], version: str) -> None:
    assert seen["response"].json()["version"] == version


@then("the response publishes every weight group")
def _response_all_groups(seen: dict[str, Any]) -> None:
    body = seen["response"].json()
    assert sorted(body["weights"]) == sorted(DOCUMENTED_WEIGHT_GROUPS), body["weights"]
    assert body["weights_not_yet_published"] == []


@then("the response says this version is not current")
def _response_not_current(seen: dict[str, Any]) -> None:
    assert seen["response"].json()["is_current"] is False
