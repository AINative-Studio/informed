"""Step definitions for backend/tests/features/zerodb_client.feature (story 1.2).

Every scenario runs against the in-memory fake transport, never live ZeroDB, so
CI needs no credentials (story 1.2 acceptance: "contract tests run against an
in-memory fake").
"""

from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/zerodb_client.feature")


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(transport=fake, project_id="proj-test")


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between When and Then steps."""
    return {}


def _fields(spec: str) -> dict[str, str]:
    return {name: "string" for name in spec.split(",")}


@given("a ZeroDB client backed by an in-memory fake")
def client_with_fake(client: ZeroDBClient) -> ZeroDBClient:
    return client


# --- tables ---------------------------------------------------------------


@given(parsers.parse('the table "{table}" exists'))
def table_exists(client: ZeroDBClient, table: str) -> None:
    client.create_table(table, {"id": "string", "name": "string", "active": "string"})


@given(parsers.parse('the row "{row_id}" was already inserted into "{table}"'))
@when(parsers.parse('I insert the row "{row_id}" into "{table}"'))
@when(parsers.parse('I insert the row "{row_id}" into "{table}" again'))
def insert_row(client: ZeroDBClient, row_id: str, table: str) -> None:
    client.upsert_row(table, row_id, {"id": row_id, "name": row_id, "active": "true"})


@given(parsers.parse('the rows "{row_ids}" were inserted into "{table}"'))
def insert_rows(client: ZeroDBClient, row_ids: str, table: str) -> None:
    for row_id in row_ids.split(","):
        client.upsert_row(table, row_id, {"id": row_id, "name": row_id})


@when(parsers.parse('I create the table "{table}" with fields "{spec}"'))
def create_table(
    client: ZeroDBClient, result: dict[str, Any], table: str, spec: str
) -> None:
    result["table"] = client.create_table(table, _fields(spec))


@when(parsers.parse('I query "{table}" filtered by id "{row_id}"'))
def query_rows(
    client: ZeroDBClient, result: dict[str, Any], table: str, row_id: str
) -> None:
    result["rows"] = client.query_rows(table, filter={"id": row_id})


@when(
    parsers.parse(
        'I update the row "{row_id}" in "{table}" setting "{field}" to "{value}"'
    )
)
def update_row(
    client: ZeroDBClient, row_id: str, table: str, field: str, value: str
) -> None:
    client.update_row(table, row_id, {field: value})


@when(parsers.parse('I delete the row "{row_id}" from "{table}"'))
def delete_row(client: ZeroDBClient, row_id: str, table: str) -> None:
    client.delete_row(table, row_id)


@then(parsers.parse('the fake recorded a POST to "/projects/{{project}}/database/tables"'))
def recorded_table_post(fake: FakeZeroDBTransport) -> None:
    assert any(
        call.method == "POST" and call.path.endswith("/database/tables")
        for call in fake.calls
    )


@then(parsers.parse('the created table is named "{table}"'))
def created_table_named(result: dict[str, Any], table: str) -> None:
    assert result["table"].table_name == table


@then(parsers.parse('the row "{row_id}" is readable from "{table}"'))
def row_is_readable(client: ZeroDBClient, row_id: str, table: str) -> None:
    row = client.get_row(table, row_id)
    assert row is not None
    assert row.row_data["id"] == row_id


@then(parsers.parse('"{table}" holds exactly {count:d} row with id "{row_id}"'))
@then(parsers.parse('"{table}" holds exactly {count:d} rows with id "{row_id}"'))
def table_holds_n_rows(
    client: ZeroDBClient, table: str, count: int, row_id: str
) -> None:
    rows = [r for r in client.query_rows(table).rows if r.row_data.get("id") == row_id]
    assert len(rows) == count


@then(parsers.parse("the query returns {count:d} row"))
@then(parsers.parse("the query returns {count:d} rows"))
def query_returned_n(result: dict[str, Any], count: int) -> None:
    assert len(result["rows"].rows) == count


@then(parsers.parse('the returned row has id "{row_id}"'))
def returned_row_id(result: dict[str, Any], row_id: str) -> None:
    assert result["rows"].rows[0].row_data["id"] == row_id


@then(parsers.parse('the row "{row_id}" in "{table}" has "{field}" equal to "{value}"'))
def row_field_equals(
    client: ZeroDBClient, row_id: str, table: str, field: str, value: str
) -> None:
    row = client.get_row(table, row_id)
    assert row is not None
    assert row.row_data[field] == value


# --- vectors --------------------------------------------------------------


@given(parsers.parse('the text "{text}" is stored in namespace "{namespace}"'))
@when(parsers.parse('I embed and store "{text}" in namespace "{namespace}"'))
def embed_and_store(
    client: ZeroDBClient, result: dict[str, Any], text: str, namespace: str
) -> None:
    result["embed"] = client.embed_and_store(
        texts=[text], namespace=namespace, metadata_list=[{"promise_id": "prm:1"}]
    )


@when(parsers.parse('I semantically search "{query}" in namespace "{namespace}"'))
def semantic_search(
    client: ZeroDBClient, result: dict[str, Any], query: str, namespace: str
) -> None:
    result["hits"] = client.semantic_search(query=query, namespace=namespace, limit=5)


@then(parsers.parse('{count:d} vector is stored in namespace "{namespace}"'))
def vectors_stored(result: dict[str, Any], count: int, namespace: str) -> None:
    assert result["embed"].vectors_stored == count
    assert result["embed"].namespace == namespace


@then(parsers.parse("the search returns at least {count:d} hit"))
def search_returns_hits(result: dict[str, Any], count: int) -> None:
    assert len(result["hits"].results) >= count


@then("each hit carries the metadata it was stored with")
def hits_carry_metadata(result: dict[str, Any]) -> None:
    assert all(hit.metadata.get("promise_id") == "prm:1" for hit in result["hits"].results)


# --- graph ----------------------------------------------------------------


@when(parsers.parse('I upsert an ontology with entity types "{types}"'))
def upsert_ontology(
    client: ZeroDBClient, result: dict[str, Any], types: str
) -> None:
    result["ontology"] = client.graph_upsert_ontology(
        entity_types=types.split(","),
        predicates={"tested_by": ["promise", "roll_call"]},
    )


@given(parsers.parse('the graph entities "{names}" exist'))
def graph_entities_exist(client: ZeroDBClient, names: str) -> None:
    for name in names.split(","):
        client.graph_upsert_entity(canonical_name=name, entity_type="promise")


@when(parsers.parse('I upsert the graph entity "{name}" of type "{entity_type}"'))
def upsert_entity(client: ZeroDBClient, name: str, entity_type: str) -> None:
    client.graph_upsert_entity(canonical_name=name, entity_type=entity_type)


@given(parsers.parse('an edge "{predicate}" exists from "{source}" to "{target}"'))
@when(parsers.parse('I create the edge "{predicate}" from "{source}" to "{target}"'))
def create_edge(
    client: ZeroDBClient, predicate: str, source: str, target: str
) -> None:
    client.graph_create_edge(
        source_name=source, target_name=target, predicate=predicate, confidence=0.9
    )


@when(parsers.parse('I run a graph RAG search for "{query}"'))
def graph_rag_search(client: ZeroDBClient, result: dict[str, Any], query: str) -> None:
    result["rag"] = client.graph_rag_search(query=query, limit=5)


@when(parsers.parse('I traverse the graph from "{entity}"'))
def graph_traverse(client: ZeroDBClient, result: dict[str, Any], entity: str) -> None:
    result["traverse"] = client.graph_traverse(entity=entity, max_hops=2)


@then(parsers.parse('the ontology records the entity type "{entity_type}"'))
def ontology_records_type(result: dict[str, Any], entity_type: str) -> None:
    assert entity_type in result["ontology"].entity_types


@then(parsers.parse('the graph holds the entity "{name}"'))
def graph_holds_entity(fake: FakeZeroDBTransport, name: str) -> None:
    assert name in fake.graph_entities


@then(parsers.parse('the graph holds an edge "{predicate}" from "{source}" to "{target}"'))
def graph_holds_edge(
    fake: FakeZeroDBTransport, predicate: str, source: str, target: str
) -> None:
    assert (source, predicate, target) in fake.graph_edges


@then("the RAG search returns a result set")
def rag_returns_results(result: dict[str, Any]) -> None:
    assert result["rag"].results is not None


@then(parsers.parse('the traversal reaches "{target}"'))
def traversal_reaches(result: dict[str, Any], target: str) -> None:
    assert target in [node.entity for node in result["traverse"].nodes]


# --- files ----------------------------------------------------------------


@given(parsers.parse('the file "{file_key}" was uploaded'))
@when(parsers.parse('I upload the file "{file_key}" with sha256 "{sha}"'))
def upload_file(
    client: ZeroDBClient, result: dict[str, Any], file_key: str, sha: str = "abc123"
) -> None:
    result["file"] = client.upload_file(
        file_key=file_key,
        file_name="campaign-page.html",
        content_type="text/html",
        size_bytes=2048,
        file_metadata={
            "sha256": sha,
            "source_url": "https://example.org/platform",
            "captured_at": "2026-10-15T00:00:00Z",
        },
    )


@when(parsers.parse("I generate a presigned URL for that file expiring in {secs:d} seconds"))
def generate_presigned(
    client: ZeroDBClient, result: dict[str, Any], secs: int
) -> None:
    result["presigned"] = client.generate_presigned_url(
        file_id=result["file"].file_id, expiration=secs
    )


@then(parsers.parse('the stored file carries sha256 "{sha}"'))
def file_carries_sha(result: dict[str, Any], sha: str) -> None:
    assert result["file"].file_metadata["sha256"] == sha


@then("a presigned URL is returned")
def presigned_returned(result: dict[str, Any]) -> None:
    assert result["presigned"].url.startswith("http")


# --- events ---------------------------------------------------------------


@when(parsers.parse('I publish the event "{topic}" with payload key "{key}"'))
def publish_event(client: ZeroDBClient, topic: str, key: str) -> None:
    client.create_event(topic=topic, payload={key: "us-house:119-2:452"})


@given(parsers.parse('the events "{topics}" were published'))
def publish_events(client: ZeroDBClient, topics: str) -> None:
    for topic in topics.split(","):
        client.create_event(topic=topic, payload={"n": 1})


@when(parsers.parse('I list events with topic "{topic}"'))
def list_events(client: ZeroDBClient, result: dict[str, Any], topic: str) -> None:
    result["events"] = client.list_events(topic=topic)


@when(parsers.parse('I subscribe to the event types "{types}"'))
def subscribe_events(client: ZeroDBClient, result: dict[str, Any], types: str) -> None:
    result["subscription"] = client.subscribe_events(event_types=types.split(","))


@then(parsers.parse('{count:d} event with topic "{topic}" was published'))
def event_published(fake: FakeZeroDBTransport, count: int, topic: str) -> None:
    assert len([e for e in fake.events if e["topic"] == topic]) == count


@then(parsers.parse('the event list contains only topic "{topic}"'))
def event_list_only_topic(result: dict[str, Any], topic: str) -> None:
    assert result["events"].events
    assert all(e.topic == topic for e in result["events"].events)


@then(parsers.parse('a subscription for "{topic}" is registered'))
def subscription_registered(result: dict[str, Any], topic: str) -> None:
    assert topic in result["subscription"].event_types
