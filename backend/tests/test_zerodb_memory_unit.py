"""The ZeroDB client's memory methods (story 13.5).

Story 1.2 built the typed client for every ZeroDB capability datamodel.md names
EXCEPT memory — `zerodb_store_memory` is listed in datamodel.md's "Files,
events, memory and RLHF" section but had no caller until the Q&A agent's
session memory needed one. These tests pin the three methods story 13.5 adds
(`store_memory`, `list_memories`, `delete_memory`) against the live request and
response shapes probed during this story; see
tests/test_zerodb_live_shapes.py for the captured live payloads themselves.

The shapes that matter, every one of them measured live rather than read off the
OpenAPI schema:

  * `session_id` MUST be a UUID. A readable `"qa:session-1"` is rejected 422
    with `uuid_parsing`, so the client validates it before spending a request.
  * the request carries no TTL of any kind. `ttl_seconds` and a top-level
    `expires_at` are accepted by the endpoint and SILENTLY DROPPED — they do
    not appear in the response and nothing expires. That is why story 13.5's
    1-day retention is a sweep and not a parameter; see
    `app.agent_memory.retention`.
  * `memory_metadata` DOES round-trip verbatim, which is where the sweep's
    `expires_at` marker lives.
  * the list endpoint's `session_id` filter works SERVER-SIDE here, unlike
    `/rows`' filter (datamodel.md's open question) — confirmed live by writing
    one row under a fresh session uuid and reading exactly it back.
"""

from __future__ import annotations

import uuid

import pytest

from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBValidationError
from app.zerodb.testing import FakeZeroDBTransport

SESSION = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def transport() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(transport: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(
        api_key="test-key",
        project_id=FakeZeroDBTransport.PROJECT_ID,
        transport=transport,
    )


# --- store_memory ---------------------------------------------------------


def test_store_memory_posts_to_the_project_memory_route(
    client: ZeroDBClient, transport: FakeZeroDBTransport
) -> None:
    record = client.store_memory(session_id=SESSION, content="what did she vote on?", role="user")

    call = transport.calls[-1]
    assert call.method == "POST"
    assert call.path == f"/projects/{FakeZeroDBTransport.PROJECT_ID}/database/memory"
    assert call.json is not None
    assert call.json["session_id"] == SESSION
    assert call.json["content"] == "what did she vote on?"
    assert call.json["role"] == "user"
    assert record.session_id == SESSION
    assert record.memory_id


def test_store_memory_round_trips_metadata(
    client: ZeroDBClient, transport: FakeZeroDBTransport
) -> None:
    record = client.store_memory(
        session_id=SESSION,
        content="q",
        role="user",
        memory_metadata={"expires_at": "2026-10-18T00:00:00Z"},
    )
    assert record.memory_metadata == {"expires_at": "2026-10-18T00:00:00Z"}


def test_store_memory_rejects_a_session_id_that_is_not_a_uuid(client: ZeroDBClient) -> None:
    # Measured live: the endpoint answers 422 `uuid_parsing`. Validating here
    # turns a wasted round trip into an immediate, readable error — the same
    # thing `generate_presigned_url` does for an out-of-range expiration.
    with pytest.raises(ZeroDBValidationError, match="session_id"):
        client.store_memory(session_id="qa:session-1", content="q", role="user")


def test_store_memory_rejects_empty_content(client: ZeroDBClient) -> None:
    with pytest.raises(ZeroDBValidationError, match="content"):
        client.store_memory(session_id=SESSION, content="  ", role="user")


def test_store_memory_sends_no_ttl_field(
    client: ZeroDBClient, transport: FakeZeroDBTransport
) -> None:
    """The client must not pretend ZeroDB has a TTL it does not have.

    Probed live: `ttl_seconds` and a top-level `expires_at` are both accepted
    and silently dropped. A client that sent either would read as "retention is
    enforced server-side" to every later reader, which is exactly the false
    claim story 13.5 must not make.
    """
    client.store_memory(session_id=SESSION, content="q", role="user")
    body = transport.calls[-1].json or {}
    assert "ttl" not in body
    assert "ttl_seconds" not in body
    assert "expires_at" not in body


# --- list_memories --------------------------------------------------------


def test_list_memories_filters_by_session_server_side(
    client: ZeroDBClient, transport: FakeZeroDBTransport
) -> None:
    client.store_memory(session_id=SESSION, content="one", role="user")
    client.store_memory(session_id=SESSION, content="two", role="assistant")
    client.store_memory(session_id=OTHER_SESSION, content="elsewhere", role="user")

    page = client.list_memories(session_id=SESSION)

    assert [record.content for record in page.memories] == ["one", "two"]
    assert page.total == 2
    assert transport.calls[-1].params is not None
    assert transport.calls[-1].params["session_id"] == SESSION


def test_list_memories_returns_created_at_for_every_record(client: ZeroDBClient) -> None:
    """The sweep computes expiry from `created_at`, so it has to be present."""
    client.store_memory(session_id=SESSION, content="one", role="user")
    page = client.list_memories(session_id=SESSION)
    assert page.memories[0].created_at


def test_list_memories_without_a_session_lists_the_whole_project(client: ZeroDBClient) -> None:
    client.store_memory(session_id=SESSION, content="one", role="user")
    client.store_memory(session_id=OTHER_SESSION, content="two", role="user")
    page = client.list_memories()
    assert page.total == 2


def test_list_memories_pages(client: ZeroDBClient, transport: FakeZeroDBTransport) -> None:
    for index in range(5):
        client.store_memory(session_id=SESSION, content=f"turn {index}", role="user")

    first = client.list_memories(session_id=SESSION, limit=2)
    assert [record.content for record in first.memories] == ["turn 0", "turn 1"]
    assert first.has_more is True

    second = client.list_memories(session_id=SESSION, limit=2, skip=2)
    assert [record.content for record in second.memories] == ["turn 2", "turn 3"]


def test_list_memories_rejects_a_session_id_that_is_not_a_uuid(client: ZeroDBClient) -> None:
    with pytest.raises(ZeroDBValidationError, match="session_id"):
        client.list_memories(session_id="qa:session-1")


# --- delete_memory --------------------------------------------------------


def test_delete_memory_removes_one_record(client: ZeroDBClient) -> None:
    record = client.store_memory(session_id=SESSION, content="one", role="user")
    client.store_memory(session_id=SESSION, content="two", role="user")

    client.delete_memory(memory_id=record.memory_id)

    page = client.list_memories(session_id=SESSION)
    assert [item.content for item in page.memories] == ["two"]


def test_delete_memory_targets_the_per_record_route(
    client: ZeroDBClient, transport: FakeZeroDBTransport
) -> None:
    record = client.store_memory(session_id=SESSION, content="one", role="user")
    client.delete_memory(memory_id=record.memory_id)
    call = transport.calls[-1]
    assert call.method == "DELETE"
    assert call.path.endswith(f"/database/memory/{record.memory_id}")


def test_delete_memory_rejects_a_memory_id_that_is_not_a_uuid(client: ZeroDBClient) -> None:
    with pytest.raises(ZeroDBValidationError, match="memory_id"):
        client.delete_memory(memory_id="not-a-uuid")


def test_a_fresh_uuid_is_an_acceptable_session_id(client: ZeroDBClient) -> None:
    """The sessions the service mints are `uuid4()`, so that form must pass."""
    session = str(uuid.uuid4())
    record = client.store_memory(session_id=session, content="q", role="user")
    assert record.session_id == session
