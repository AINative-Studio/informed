"""Argument guards, optional-argument handling and the real transport (story 1.2).

Split from `test_zerodb_unit.py` to keep that file about endpoint shapes and
error mapping. Nothing here opens a socket: the httpx transport is exercised
through `httpx.MockTransport`.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBConnectionError, ZeroDBValidationError
from app.zerodb.testing import FakeZeroDBTransport
from app.zerodb.transport import HttpxZeroDBTransport, ZeroDBTransportError

BASE_URL = "https://api.ainative.studio/api/v1"


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )


# --- argument guards: reject a bad call before spending a round trip ------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.create_table("", {"id": "string"}), id="create_table"),
        pytest.param(lambda c: c.upsert_row("", "id:1", {}), id="upsert_row-table"),
        pytest.param(lambda c: c.upsert_row("people", "  ", {}), id="upsert_row-id"),
        pytest.param(lambda c: c.query_rows(""), id="query_rows"),
        pytest.param(lambda c: c.update_row("people", "", {}), id="update_row"),
        pytest.param(lambda c: c.delete_row("", "id:1"), id="delete_row"),
        pytest.param(lambda c: c.delete_table(""), id="delete_table"),
        pytest.param(
            lambda c: c.semantic_search(query="", namespace="promises"),
            id="semantic_search",
        ),
        pytest.param(
            lambda c: c.graph_upsert_ontology(entity_types=[], predicates={}),
            id="ontology-no-types",
        ),
        pytest.param(
            lambda c: c.graph_upsert_entity(canonical_name="", entity_type="person"),
            id="entity-name",
        ),
        pytest.param(
            lambda c: c.graph_upsert_entity(canonical_name="prm:1", entity_type=""),
            id="entity-type",
        ),
        pytest.param(
            lambda c: c.graph_create_edge(source_name="a", target_name="b", predicate=""),
            id="edge-predicate",
        ),
        pytest.param(lambda c: c.graph_rag_search(query=""), id="rag"),
        pytest.param(lambda c: c.graph_traverse(entity=""), id="traverse"),
        pytest.param(lambda c: c.upload_file(file_key="", content=b"x"), id="upload_file"),
        # An empty capture means the fetch failed upstream; storing one would
        # produce a citation pointing at nothing.
        pytest.param(
            lambda c: c.upload_file(file_key="k", content=b""), id="upload_file-no-content"
        ),
        pytest.param(lambda c: c.download_file(file_id=""), id="download_file"),
        pytest.param(lambda c: c.create_event(topic="", payload={}), id="create_event"),
        pytest.param(lambda c: c.subscribe_events(event_types=[]), id="subscribe"),
    ],
)
def test_an_invalid_argument_is_rejected_without_a_request(
    client: ZeroDBClient,
    fake: FakeZeroDBTransport,
    call: Callable[[ZeroDBClient], Any],
) -> None:
    with pytest.raises(ZeroDBValidationError):
        call(client)

    assert fake.calls == []


@pytest.mark.parametrize("expiration", [0, -1, 3601])
def test_a_presigned_url_lifetime_outside_the_allowed_window_is_rejected(
    client: ZeroDBClient, expiration: int
) -> None:
    """A citation link that outlives its page view can be shared past its check."""
    with pytest.raises(ZeroDBValidationError):
        client.generate_presigned_url(file_id="file-1", expiration=expiration)


# --- optional arguments reach the wire only when supplied ----------------


def test_optional_arguments_are_omitted_rather_than_sent_as_null(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """ZeroDB treats an absent key and an explicit null differently."""
    client.create_table("people", {"id": "string"})
    client.query_rows("people", limit=10)

    body = fake.calls[-1].json
    assert body is not None
    assert "filter" not in body
    assert "sort" not in body
    assert "projection" not in body


def test_supplied_optional_arguments_do_reach_the_wire(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    client.graph_upsert_ontology(
        entity_types=["person"],
        predicates={"voted": ["person", "roll_call"]},
        constraints={"voted": {"max": 1}},
    )
    assert fake.calls[-1].json["constraints"] == {"voted": {"max": 1}}

    client.graph_upsert_entity(
        canonical_name="person:B000944",
        entity_type="person",
        aliases=["Baldwin"],
        properties={"party": "D"},
    )
    assert fake.calls[-1].json["aliases"] == ["Baldwin"]

    client.embed_and_store(texts=["a"], namespace="promises", model="bge-m3")
    assert fake.calls[-1].json["model"] == "bge-m3"

    client.subscribe_events(event_types=["verdict.published"], filter={"person_id": "p"})
    assert fake.calls[-1].json["filter"] == {"person_id": "p"}

    client.graph_traverse(entity="prm:1", predicates=["tested_by"])
    assert fake.calls[-1].json["predicates"] == ["tested_by"]

    # An upload's optional arguments travel as multipart form fields and the
    # part's own content type, not as a JSON body — see `upload_file`.
    client.upload_file(
        file_key="k", content=b"<html></html>", file_name="n.html", content_type="text/html"
    )
    call = fake.calls[-1]
    assert call.json is None
    assert call.files is not None
    assert call.files["file"][0] == "n.html"
    assert call.files["file"][2] == "text/html"


def test_delete_table_targets_the_table_path(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    client.create_table("scratch", {"id": "string"})
    client.delete_table("scratch")

    assert fake.calls[-1].method == "DELETE"
    assert fake.calls[-1].path.endswith("/database/tables/scratch")
    assert client.list_tables() == []


def test_a_query_against_a_missing_table_raises_not_found(
    client: ZeroDBClient,
) -> None:
    from app.zerodb.errors import ZeroDBNotFoundError

    with pytest.raises(ZeroDBNotFoundError):
        client.query_rows("no_such_table")


# --- from_env and the real transport -------------------------------------


def test_from_env_builds_a_rest_client_from_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The credential path story 1.6 feeds from local.env, CI secrets or a deploy."""
    monkeypatch.setenv("ZERODB_API_KEY", "key-from-env")
    monkeypatch.setenv("ZERODB_PROJECT_ID", "project-from-env")

    with ZeroDBClient.from_env() as client:
        assert client.project_id == "project-from-env"


def test_the_httpx_transport_sends_a_bearer_token_and_parses_json() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"table_name": "people"})

    http_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=BASE_URL,
        headers={"Authorization": "Bearer live-key"},
    )
    with HttpxZeroDBTransport(
        base_url=BASE_URL, api_key="live-key", client=http_client
    ) as transport:
        response = transport.request("POST", "/projects/p/database/tables", json={})

    assert response.status_code == 200
    assert response.json_body == {"table_name": "people"}
    assert seen["auth"] == "Bearer live-key"


def test_the_httpx_transport_reports_a_non_json_body_without_raising() -> None:
    """Deciding what a bad body means is the client's job, not the transport's."""
    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="<html>")),
        base_url="https://example.test",
    )
    transport = HttpxZeroDBTransport(
        base_url="https://example.test", api_key="k", client=http_client
    )

    response = transport.request("GET", "/anything")

    assert response.status_code == 200
    assert response.json_body is None
    assert response.text == "<html>"


def test_the_httpx_transport_does_not_raise_for_an_error_status() -> None:
    """A 404 comes back as a response so only the client decides what it means."""
    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(404, json={"detail": "nope"})),
        base_url="https://example.test",
    )
    transport = HttpxZeroDBTransport(
        base_url="https://example.test", api_key="k", client=http_client
    )

    assert transport.request("GET", "/missing").status_code == 404


def test_a_transport_level_exception_is_mapped_to_a_connection_error() -> None:
    """An MCP transport will raise ZeroDBTransportError; it maps like httpx's."""

    class BrokenTransport:
        def request(self, method: str, path: str, **kwargs: Any) -> Any:
            raise ZeroDBTransportError("mcp server unreachable")

    client = ZeroDBClient(
        transport=BrokenTransport(),  # type: ignore[arg-type]
        project_id="proj-test",
        max_attempts=1,
        sleep=lambda _: None,
    )

    with pytest.raises(ZeroDBConnectionError):
        client.create_table("people", {"id": "string"})


def test_a_bug_in_the_transport_is_not_swallowed_as_a_connection_error() -> None:
    """Only transport failures are wrapped; a real bug must surface as itself."""

    class BuggyTransport:
        def request(self, method: str, path: str, **kwargs: Any) -> Any:
            raise KeyError("a bug, not a network problem")

    client = ZeroDBClient(
        transport=BuggyTransport(),  # type: ignore[arg-type]
        project_id="proj-test",
        max_attempts=3,
        sleep=lambda _: None,
    )

    with pytest.raises(KeyError):
        client.create_table("people", {"id": "string"})


def test_closing_a_client_whose_transport_has_no_close_is_harmless() -> None:
    """The fake has no close(); the client must not insist on one."""
    client = ZeroDBClient(transport=FakeZeroDBTransport(), project_id="p")

    client.close()  # must not raise
