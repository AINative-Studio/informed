"""Step definitions for tests/features/free_text_interest_mapping.feature (3.5).

Runs the real FastAPI app against story 1.2's in-memory ZeroDB fake, through
the real `TopicService`, exactly as `test_quick_onboarding_steps.py` runs the
topics/stances/profiles endpoints — CI makes no network call and no live
credentials are needed. The live confirmation of real semantic relevance
(the fake only does crude token overlap, never real embeddings — see
`app.zerodb.testing`'s module docstring) lives in
`scripts/verify_free_text_interest_mapping_live.py`.

Every free-text string used here is synthetic and invented for this test
file — CLAUDE.md forbids real voter data in a fixture, and a voter's
free-text concern is exactly the kind of political-opinion data this story's
whole point is to never store or log.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.embed_taxonomy import TOPICS_NAMESPACE, TaxonomyEmbedder
from app.main import create_app
from app.seed_taxonomy import TaxonomySeeder
from app.topics.router import topic_service, zerodb_client_for_topics
from app.topics.service import TopicService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/free_text_interest_mapping.feature")

ALL_TABLES = ("issue_topics", "stances", "voter_profiles")


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
    for table in ALL_TABLES:
        client.create_table(table, {"id": "string"})
    return client


@pytest.fixture
def api(zdb: ZeroDBClient) -> Any:
    """The real app, wired to the fake ZeroDB client for the topics router."""
    app = create_app()
    app.dependency_overrides[zerodb_client_for_topics] = lambda: zdb
    app.dependency_overrides[topic_service] = lambda: TopicService(zdb)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def result() -> dict[str, Any]:
    return {}


# --- background --------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="suggest_client")
def suggest_client(zdb: ZeroDBClient) -> ZeroDBClient:
    return zdb


@given("the issue taxonomy has been seeded")
def taxonomy_seeded(zdb: ZeroDBClient, transport: FakeZeroDBTransport) -> None:
    TaxonomySeeder(zdb, pace=lambda: None).run()
    transport.calls.clear()


@given("every topic has been embedded into the topics vector namespace")
def topics_embedded(zdb: ZeroDBClient, transport: FakeZeroDBTransport) -> None:
    TaxonomyEmbedder(zdb, pace=lambda: None).embed_topics()
    transport.calls.clear()


# --- snapshots, taken before the call under test ------------------------


@given("a snapshot of every table's row count")
def snapshot_row_counts(zdb: ZeroDBClient, result: dict[str, Any]) -> None:
    result["row_counts_before"] = {
        table: zdb.query_rows(table, limit=1000).total for table in ALL_TABLES
    }


@given("a snapshot of the topics namespace's vector count")
def snapshot_vector_count(zdb: ZeroDBClient, result: dict[str, Any]) -> None:
    result["vector_count_before"] = zdb.get_vector_stats(namespace=TOPICS_NAMESPACE).namespaces.get(
        TOPICS_NAMESPACE, 0
    )


# --- the call under test -------------------------------------------------


#: `parsers.parse`'s `{text}` placeholder requires at least one character, so
#: an *empty* quoted string ("") needs its own exact-match step rather than
#: falling through the parameterized one below.
@when('a voter describes their concern as ""')
def describe_concern_blank(
    api: TestClient, result: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = api.post("/v1/topics/suggest", json={"text": ""})
    result["response"] = response
    result["free_text"] = ""
    result["log_records"] = list(caplog.records)


@when(parsers.parse('a voter describes their concern as "{text}"'))
def describe_concern(
    api: TestClient, text: str, result: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = api.post("/v1/topics/suggest", json={"text": text})
    result["response"] = response
    result["free_text"] = text
    result["log_records"] = list(caplog.records)


# --- the acceptance criterion: top 3 suggestions --------------------------


@then(parsers.parse("at most {maximum:d} topic suggestions are returned"))
def at_most_n_suggestions(result: dict[str, Any], maximum: int) -> None:
    response = result["response"]
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["suggestions"]) <= maximum


@then("each suggestion carries a topic_id, a name and a similarity score")
def each_suggestion_is_well_formed(result: dict[str, Any]) -> None:
    """`score` is present as a field on every suggestion but is nullable.

    Confirmed live against the real "informed" project: the live search
    endpoint's response carries no numeric score at all (results still
    arrive correctly ranked, closest match first) — see
    `app.topics.models.TopicSuggestion.score`'s docstring. The in-memory
    fake DOES report a real float, which is what this scenario runs
    against, so the stronger "is a number" check belongs here; a `None`
    live score is still a well-formed suggestion and is covered by
    `test_topics_suggest_unit.py`'s own test for that case instead of here.
    """
    body = result["response"].json()
    assert body["suggestions"], "expected at least one suggestion"
    for suggestion in body["suggestions"]:
        assert suggestion["topic_id"]
        assert suggestion["name"]
        assert "score" in suggestion
        assert isinstance(suggestion["score"], (int, float))


@then(parsers.parse('"{name}" is the top-ranked suggestion'))
def named_topic_is_top_ranked(result: dict[str, Any], name: str) -> None:
    body = result["response"].json()
    assert body["suggestions"], "no suggestions were returned"
    assert body["suggestions"][0]["name"] == name, (
        f"top suggestion was {body['suggestions'][0]['name']!r}, expected {name!r}: "
        f"{body['suggestions']}"
    )


@then("the request is refused with a 422 naming the missing text")
def refused_missing_text(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 422, response.text
    assert "text" in response.text.lower()


# --- "nothing is saved until the voter confirms" --------------------------


@then("no table's row count has changed since the snapshot")
def row_counts_unchanged(zdb: ZeroDBClient, result: dict[str, Any]) -> None:
    assert result["response"].status_code == 200, result["response"].text
    after = {table: zdb.query_rows(table, limit=1000).total for table in ALL_TABLES}
    assert after == result["row_counts_before"], (
        f"row counts changed: before={result['row_counts_before']}, after={after}"
    )


@then("the topics namespace's vector count has not changed since the snapshot")
def vector_count_unchanged(zdb: ZeroDBClient, result: dict[str, Any]) -> None:
    assert result["response"].status_code == 200, result["response"].text
    after = zdb.get_vector_stats(namespace=TOPICS_NAMESPACE).namespaces.get(TOPICS_NAMESPACE, 0)
    assert after == result["vector_count_before"], (
        f"vector count changed: before={result['vector_count_before']}, after={after}"
    )


@then("the response confirms nothing was saved and a separate confirm step is required")
def response_confirms_no_save(result: dict[str, Any]) -> None:
    body = result["response"].json()
    assert body["saved"] is False
    assert body["confirmation_required"] is True


# --- "the text itself is not stored" --------------------------------------


@then("no stored row in any table contains that free text")
def no_row_contains_free_text(zdb: ZeroDBClient, result: dict[str, Any]) -> None:
    free_text = result["free_text"]
    for table in ALL_TABLES:
        page = zdb.query_rows(table, limit=1000)
        for row in page.rows:
            for value in row.row_data.values():
                assert free_text not in str(value), (
                    f"table {table!r} row {row.row_data.get('id')!r} contains the free text"
                )


@then("no log record contains that free text")
def no_log_contains_free_text(result: dict[str, Any]) -> None:
    free_text = result["free_text"]
    for record in result["log_records"]:
        assert free_text not in record.getMessage(), (
            f"log record {record.name!r} contains the voter's free text"
        )
        assert free_text not in str(record.args), (
            f"log record {record.name!r} args contain the voter's free text"
        )
