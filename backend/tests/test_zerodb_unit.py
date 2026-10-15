"""Unit tests for the typed ZeroDB client (story 1.2).

These cover the request-building and response-parsing details the Gherkin
features do not spell out: endpoint paths, payload shapes, settings loading and
the REST/MCP transport seam. Nothing here touches live ZeroDB.
"""

import httpx
import pytest

from app.zerodb import ZeroDBClient
from app.zerodb.errors import (
    ZeroDBAuthError,
    ZeroDBConflictError,
    ZeroDBConnectionError,
    ZeroDBError,
    ZeroDBNotFoundError,
    ZeroDBProtocolError,
    ZeroDBRateLimitError,
    ZeroDBServerError,
    ZeroDBValidationError,
)
from app.zerodb.settings import ZeroDBSettings
from app.zerodb.testing import FakeZeroDBTransport
from app.zerodb.transport import HttpxZeroDBTransport, ZeroDBTransport


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )


# --- settings -------------------------------------------------------------


def test_settings_read_credentials_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials come from env vars, never from a file the client reads itself.

    Story 1.6 swaps where the env vars come from (local.env, CI secrets) without
    touching this code.
    """
    monkeypatch.setenv("ZERODB_API_KEY", "key-from-env")
    monkeypatch.setenv("ZERODB_PROJECT_ID", "project-from-env")
    monkeypatch.setenv("ZERODB_WORKSPACE_ID", "workspace-from-env")

    settings = ZeroDBSettings()

    assert settings.api_key.get_secret_value() == "key-from-env"
    assert settings.project_id == "project-from-env"
    assert settings.workspace_id == "workspace-from-env"
    assert settings.base_url == "https://api.ainative.studio/api/v1"


def test_settings_reject_a_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZERODB_API_KEY", raising=False)
    monkeypatch.setenv("ZERODB_PROJECT_ID", "project-from-env")

    with pytest.raises(ValueError):
        ZeroDBSettings()


def test_the_api_key_never_appears_in_a_settings_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settings object lands in tracebacks; the key must not ride along."""
    monkeypatch.setenv("ZERODB_API_KEY", "super-secret-key")
    monkeypatch.setenv("ZERODB_PROJECT_ID", "project-from-env")

    assert "super-secret-key" not in repr(ZeroDBSettings())


# --- endpoint paths and payload shapes (verified against the live API) ----


def test_create_table_posts_the_documented_payload(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    client.create_table("roll_calls", {"voted_at": "string", "result": "string"})

    call = fake.calls[-1]
    assert call.method == "POST"
    assert call.path == "/projects/proj-test/database/tables"
    assert call.json == {
        "table_name": "roll_calls",
        "schema_definition": {"voted_at": "string", "result": "string"},
    }


def test_upsert_row_sends_row_data_keyed_by_the_natural_key_id(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """datamodel.md rule 1: the id is part of the row, built from natural keys."""
    client.create_table("roll_calls", {"id": "string"})
    client.upsert_row("roll_calls", "us-house:119-2:452", {"result": "passed"})

    call = fake.calls[-1]
    assert call.path == "/projects/proj-test/database/tables/roll_calls/rows"
    assert call.json["row_data"]["id"] == "us-house:119-2:452"
    assert call.json["row_data"]["result"] == "passed"


def test_upsert_row_replaces_an_existing_row_instead_of_duplicating_it(
    client: ZeroDBClient,
) -> None:
    """Re-ingesting a record is an idempotent upsert, never a duplicate."""
    client.create_table("roll_calls", {"id": "string"})
    client.upsert_row("roll_calls", "us-house:119-2:452", {"result": "passed"})
    client.upsert_row("roll_calls", "us-house:119-2:452", {"result": "failed"})

    rows = client.query_rows("roll_calls").rows
    assert len(rows) == 1
    assert rows[0].row_data["result"] == "failed"


def test_query_rows_sends_filter_sort_limit_and_skip(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    client.create_table("member_votes", {"id": "string"})
    client.query_rows(
        "member_votes",
        filter={"person_id": "person:B000944"},
        sort={"voted_at": -1},
        limit=50,
        skip=100,
    )

    # Not calls[-1]: an empty filtered result triggers the documented
    # client-side filter fallback, which issues a second /rows read.
    query_calls = [
        call
        for call in fake.calls
        if call.path == "/projects/proj-test/database/tables/member_votes/query"
    ]
    assert len(query_calls) == 1
    assert query_calls[0].json == {
        "filter": {"person_id": "person:B000944"},
        "sort": {"voted_at": -1},
        "limit": 50,
        "skip": 100,
    }


def test_query_rows_reports_pagination_metadata(client: ZeroDBClient) -> None:
    """Story 16.1 needs total/has_more to page ~600k member_votes per Congress."""
    client.create_table("member_votes", {"id": "string"})
    for i in range(3):
        client.upsert_row("member_votes", f"vote:{i}", {"n": i})

    page = client.query_rows("member_votes", limit=2)

    assert page.total == 3
    assert page.has_more is True
    assert len(page.rows) == 2


def test_get_row_returns_none_rather_than_raising_when_absent(
    client: ZeroDBClient,
) -> None:
    """The repository layer (1.4) distinguishes "absent" from "broken"."""
    client.create_table("people", {"id": "string"})

    assert client.get_row("people", "person:NOBODY") is None


def test_update_row_puts_the_merged_row_not_just_the_changed_fields(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """ZeroDB's PUT replaces `row_data`, so the client must send it complete.

    Sending only the changed field would drop the row's id and every other
    field — see tests/test_zerodb_idempotency.py.
    """
    client.create_table("verdicts", {"id": "string"})
    client.upsert_row("verdicts", "prm:1:v1", {"is_current": "true", "verdict": "kept"})
    client.update_row("verdicts", "prm:1:v1", {"is_current": "false"})

    call = fake.calls[-1]
    assert call.method == "PUT"
    assert call.json == {"row_data": {"id": "prm:1:v1", "is_current": "false", "verdict": "kept"}}


def test_delete_row_targets_the_row_path(client: ZeroDBClient, fake: FakeZeroDBTransport) -> None:
    """The path carries ZeroDB's internal UUID, not Informed's natural-key id.

    The live API answers 422 for a non-UUID in that segment; see
    tests/test_zerodb_idempotency.py for the resolution behaviour in full.
    """
    client.create_table("people", {"id": "string"})
    inserted = client.upsert_row("people", "person:B000944", {})
    client.delete_row("people", "person:B000944")

    call = fake.calls[-1]
    assert call.method == "DELETE"
    assert call.path.endswith(f"/database/tables/people/rows/{inserted.row_id}")


def test_list_tables_parses_the_envelope(client: ZeroDBClient) -> None:
    client.create_table("people", {"id": "string"})
    client.create_table("districts", {"id": "string"})

    assert {t.table_name for t in client.list_tables()} == {"people", "districts"}


# --- vectors --------------------------------------------------------------


def test_embed_and_store_posts_texts_and_parallel_metadata(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    client.embed_and_store(
        texts=["promise one", "promise two"],
        namespace="promises",
        metadata_list=[{"promise_id": "prm:1"}, {"promise_id": "prm:2"}],
    )

    call = fake.calls[-1]
    assert call.path == "/projects/proj-test/embeddings/embed-and-store"
    assert call.json["texts"] == ["promise one", "promise two"]
    assert call.json["namespace"] == "promises"
    assert len(call.json["metadata_list"]) == 2


def test_embed_and_store_rejects_mismatched_metadata(client: ZeroDBClient) -> None:
    """A metadata list out of step with the texts silently mislabels vectors."""
    with pytest.raises(ZeroDBValidationError):
        client.embed_and_store(
            texts=["a", "b"], namespace="promises", metadata_list=[{"only": "one"}]
        )


def test_embed_and_store_rejects_an_empty_text_list(client: ZeroDBClient) -> None:
    with pytest.raises(ZeroDBValidationError):
        client.embed_and_store(texts=[], namespace="promises")


def test_semantic_search_passes_threshold_and_metadata_filter(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """Mapping keeps hits above 0.75 and sends anything under 0.85 to QA."""
    client.semantic_search(
        query="drug pricing",
        namespace="bill_summaries",
        limit=10,
        threshold=0.75,
        filter_metadata={"body_id": "us-house"},
    )

    call = fake.calls[-1]
    assert call.path == "/projects/proj-test/embeddings/search"
    assert call.json["threshold"] == 0.75
    assert call.json["limit"] == 10
    assert call.json["filter_metadata"] == {"body_id": "us-house"}


# --- graph ----------------------------------------------------------------


def test_graph_ontology_upsert_sends_the_project_and_predicates(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    client.graph_upsert_ontology(
        entity_types=["person", "promise", "roll_call"],
        predicates={"tested_by": ["promise", "roll_call"]},
    )

    call = fake.calls[-1]
    assert call.path == "/public/memory/v2/graph/ontology"
    assert call.json["project_id"] == "proj-test"
    assert "tested_by" in call.json["predicates"]


def test_graph_upsert_entity_uses_the_nosql_id_as_canonical_name(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """datamodel.md: canonical_name is the NoSQL id so answers resolve to rows."""
    client.graph_upsert_entity(
        canonical_name="us-house:119-2:452",
        entity_type="roll_call",
        properties={"result": "passed"},
    )

    call = fake.calls[-1]
    assert call.path == "/public/memory/v2/graph/entity"
    assert call.json["canonical_name"] == "us-house:119-2:452"
    assert call.json["project_id"] == "proj-test"


def test_graph_create_edge_sends_predicate_confidence_and_properties(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    client.graph_upsert_entity(canonical_name="prm:1", entity_type="promise")
    client.graph_upsert_entity(canonical_name="us-house:119-2:452", entity_type="roll_call")

    edge = client.graph_create_edge(
        source_name="prm:1",
        target_name="us-house:119-2:452",
        predicate="tested_by",
        confidence=0.82,
        properties={"effect": "advances", "qa_status": "approved"},
    )

    call = fake.calls[-1]
    assert call.path == "/public/memory/v2/graph/edge"
    assert call.json["predicate"] == "tested_by"
    assert call.json["confidence"] == 0.82
    assert call.json["properties"]["effect"] == "advances"

    # The response omits the canonical names and properties; the client
    # restores them from the request so the caller sees the edge it asked for.
    assert edge.source_name == "prm:1"
    assert edge.target_name == "us-house:119-2:452"
    assert edge.properties["effect"] == "advances"
    assert edge.source_id is not None


def test_creating_an_edge_to_a_missing_entity_raises_not_found(
    client: ZeroDBClient,
) -> None:
    """The live API rejects an edge whose endpoint does not exist."""
    client.graph_upsert_entity(canonical_name="prm:1", entity_type="promise")

    with pytest.raises(ZeroDBNotFoundError):
        client.graph_create_edge(
            source_name="prm:1", target_name="no:such:entity", predicate="tested_by"
        )


def test_graph_traverse_sends_hop_and_time_bounds(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """Superseded verdict history is queried with as_of (datamodel.md)."""
    client.graph_traverse(
        entity="prm:1",
        max_hops=3,
        predicates=["tested_by", "has_verdict"],
        min_confidence=0.5,
        as_of="2026-10-01T00:00:00Z",
    )

    call = fake.calls[-1]
    assert call.path == "/public/memory/v2/graph/traverse"
    assert call.json["max_hops"] == 3
    assert call.json["as_of"] == "2026-10-01T00:00:00Z"


# --- files ----------------------------------------------------------------


def test_upload_file_registers_provenance_metadata(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    client.upload_file(
        file_key="archive/bill-text.pdf",
        file_name="bill-text.pdf",
        content_type="application/pdf",
        size_bytes=102400,
        file_metadata={"sha256": "deadbeef", "source_url": "https://congress.gov/x"},
    )

    call = fake.calls[-1]
    assert call.path == "/projects/proj-test/database/files"
    assert call.json["file_key"] == "archive/bill-text.pdf"
    assert call.json["file_metadata"]["sha256"] == "deadbeef"


def test_generate_presigned_url_defaults_to_a_short_lived_download(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """The UI serves citations through short-lived URLs (datamodel.md)."""
    uploaded = client.upload_file(file_key="archive/page.html")
    client.generate_presigned_url(file_id=uploaded.file_id)

    call = fake.calls[-1]
    assert call.path.endswith(f"/database/files/{uploaded.file_id}/presigned-url")
    assert call.json["operation"] == "get_object"
    assert 0 < call.json["expiration"] <= 3600


# --- events ---------------------------------------------------------------


def test_create_event_posts_topic_and_payload(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    client.create_event(
        topic="verdict.published",
        payload={"verdict_id": "prm:1:v2", "person_id": "person:B000944"},
    )

    call = fake.calls[-1]
    assert call.path == "/projects/proj-test/database/events"
    assert call.json == {
        "topic": "verdict.published",
        "event_payload": {"verdict_id": "prm:1:v2", "person_id": "person:B000944"},
    }


def test_list_events_sends_topic_and_paging_as_query_params(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    client.list_events(topic="rollcall.ingested", limit=25, skip=50)

    call = fake.calls[-1]
    assert call.method == "GET"
    assert call.params == {"topic": "rollcall.ingested", "limit": 25, "skip": 50}


def test_subscribe_events_posts_event_types_and_webhook(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    client.subscribe_events(
        event_types=["verdict.published"], webhook_url="https://informed.test/hook"
    )

    call = fake.calls[-1]
    assert call.path == "/projects/proj-test/database/events/subscriptions"
    assert call.json["event_types"] == ["verdict.published"]
    assert call.json["webhook_url"] == "https://informed.test/hook"


# --- error mapping --------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, ZeroDBValidationError),
        (401, ZeroDBAuthError),
        (403, ZeroDBAuthError),
        (404, ZeroDBNotFoundError),
        (409, ZeroDBConflictError),
        (422, ZeroDBValidationError),
        (429, ZeroDBRateLimitError),
        (500, ZeroDBServerError),
        (503, ZeroDBServerError),
    ],
)
def test_http_statuses_map_to_domain_exceptions(
    fake: FakeZeroDBTransport, status: int, expected: type[ZeroDBError]
) -> None:
    fake.always_fail(status=status)
    client = ZeroDBClient(
        transport=fake,
        project_id="proj-test",
        max_attempts=2,
        backoff_base=0.0,
        sleep=lambda _: None,
    )

    with pytest.raises(expected):
        client.create_table("people", {"id": "string"})


def test_a_domain_error_carries_the_status_and_zerodb_error_code(
    fake: FakeZeroDBTransport,
) -> None:
    fake.always_fail(status=404)
    client = ZeroDBClient(transport=fake, project_id="proj-test", max_attempts=1)

    with pytest.raises(ZeroDBNotFoundError) as caught:
        client.create_table("people", {"id": "string"})

    assert caught.value.status_code == 404
    assert caught.value.error_code == "NOT_FOUND"


def test_a_connection_error_is_wrapped_not_leaked(fake: FakeZeroDBTransport) -> None:
    fake.always_fail(exception=httpx.ConnectError("refused"))
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", max_attempts=1, sleep=lambda _: None
    )

    with pytest.raises(ZeroDBConnectionError) as caught:
        client.create_table("people", {"id": "string"})

    assert isinstance(caught.value.__cause__, httpx.ConnectError)


def test_a_non_json_body_becomes_a_protocol_error(fake: FakeZeroDBTransport) -> None:
    fake.always_return_non_json()
    client = ZeroDBClient(transport=fake, project_id="proj-test", max_attempts=1)

    with pytest.raises(ZeroDBProtocolError):
        client.create_table("people", {"id": "string"})


def test_a_response_missing_required_fields_becomes_a_protocol_error(
    fake: FakeZeroDBTransport,
) -> None:
    """A shape change upstream must fail loudly, not hand callers junk."""
    fake.always_return_json({"unexpected": "shape"})
    client = ZeroDBClient(transport=fake, project_id="proj-test", max_attempts=1)

    with pytest.raises(ZeroDBProtocolError):
        client.create_table("people", {"id": "string"})


# --- retry policy ---------------------------------------------------------


def test_retries_stop_at_the_configured_attempt_budget(
    fake: FakeZeroDBTransport,
) -> None:
    fake.always_fail(status=503)
    sleeps: list[float] = []
    client = ZeroDBClient(
        transport=fake,
        project_id="proj-test",
        max_attempts=3,
        backoff_base=0.01,
        sleep=sleeps.append,
    )

    with pytest.raises(ZeroDBServerError):
        client.create_table("people", {"id": "string"})

    assert len(fake.calls) == 3
    assert len(sleeps) == 2  # one fewer sleep than attempts


def test_backoff_is_capped_so_a_long_outage_does_not_stall_a_request(
    fake: FakeZeroDBTransport,
) -> None:
    fake.always_fail(status=503)
    sleeps: list[float] = []
    client = ZeroDBClient(
        transport=fake,
        project_id="proj-test",
        max_attempts=10,
        backoff_base=1.0,
        backoff_max=4.0,
        sleep=sleeps.append,
    )

    with pytest.raises(ZeroDBServerError):
        client.create_table("people", {"id": "string"})

    assert max(sleeps) <= 4.0


def test_a_validation_error_is_not_retried(fake: FakeZeroDBTransport) -> None:
    fake.always_fail(status=422)
    sleeps: list[float] = []
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", max_attempts=5, sleep=sleeps.append
    )

    with pytest.raises(ZeroDBValidationError):
        client.create_table("people", {"id": "string"})

    assert len(fake.calls) == 1
    assert sleeps == []


# --- the REST/MCP transport seam -----------------------------------------


def test_the_real_transport_satisfies_the_same_protocol_as_the_fake() -> None:
    """Swapping REST for MCP means writing one more ZeroDBTransport."""
    real = HttpxZeroDBTransport(
        base_url="https://api.ainative.studio/api/v1", api_key="unused-in-this-test"
    )

    assert isinstance(real, ZeroDBTransport)
    assert isinstance(FakeZeroDBTransport(), ZeroDBTransport)


def test_the_client_never_imports_httpx_itself() -> None:
    """The client talks to a transport; only the transport knows about HTTP.

    This is the property that keeps the MCP swap a one-file change.
    """
    import app.zerodb.client as client_module

    assert not hasattr(client_module, "httpx")


def test_the_module_docstring_flags_the_tracked_mcp_deviation() -> None:
    """CLAUDE.md says MCP only; this client speaks REST. That must be stated."""
    import app.zerodb.client as client_module

    doc = (client_module.__doc__ or "").upper()
    assert "MCP" in doc
    assert "REST" in doc
    assert "CLAUDE.MD" in doc
