"""Response-shape regression tests against captured live payloads (story 1.2).

Each payload below was copied verbatim from a real response from the "informed"
ZeroDB project, with the project id, credentials and URL signatures redacted.
They pin the field names the models depend on, because almost every one of these
was first modelled wrongly from the OpenAPI schema, and only a live call
revealed the truth:

  * the presigned URL is `presigned_url`, not `url`, with the lifetime as
    `expiry_minutes` rather than a seconds `expiration`;
  * a graph edge response identifies endpoints by `source_id`/`target_id` and
    omits the canonical names and properties entirely;
  * traversal nodes name the entity in `label`, with its type in `type`;
  * `file_metadata` comes back null, not `{}`, when none was supplied;
  * `POST /database/files` stores file METADATA ONLY — no object behind it —
    while `POST /files/upload` takes the bytes as multipart and is the only
    route that stores anything retrievable (found 2026-10-17, after story 9.1
    archived six real campaign-site files that all 404ed on download);
  * the events list is a bare JSON array, not a `{"total", "data"}` envelope.

If ZeroDB changes any of these, these tests fail here rather than at 3am in an
ingest run, which is the whole point of pinning them.
"""

from typing import Any

import pytest

from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBNotFoundError, ZeroDBRateLimitError
from app.zerodb.models import (
    Event,
    GraphEdge,
    MemoryPage,
    MemoryRecord,
    Ontology,
    PresignedUrl,
    Row,
    StoredFile,
    Table,
    Traversal,
)
from app.zerodb.testing import FakeZeroDBTransport

LIVE_TABLE: dict[str, Any] = {
    "table_name": "live_check_112",
    "schema_definition": {"id": "string", "name": "string"},
    "table_id": "2f61b122-82f0-4ba0-9d9a-89cca3c2f3b3",
    "project_id": "00000000-0000-0000-0000-000000000000",
    "created_at": "2026-10-15T22:51:02.248708Z",
    "updated_at": "2026-10-15T22:51:02.248708Z",
}

LIVE_ROW: dict[str, Any] = {
    "row_data": {"id": "us-federal", "name": "United States"},
    "row_id": "ec3b58e5-b8af-4ab3-814a-0222e35ab0b5",
    "project_id": "00000000-0000-0000-0000-000000000000",
    "table_id": "2f61b122-82f0-4ba0-9d9a-89cca3c2f3b3",
    "table_name": "live_check_112",
    "created_at": "2026-10-15T22:51:02.706632Z",
    "updated_at": "2026-10-15T22:51:02.706632Z",
}

LIVE_EDGE: dict[str, Any] = {
    "id": "32b80f50-2509-4bf3-8b79-0b4061b4154e",
    "source_id": "9c3670fc-84df-40ac-a916-dc5d2fee2785",
    "target_id": "fa4407d9-6dba-4c22-b4ce-c60e5f738ec9",
    "predicate": "tested_by",
    "confidence": 0.9,
}

LIVE_TRAVERSAL: dict[str, Any] = {
    "nodes": [
        {
            "id": "9c3670fc-84df-40ac-a916-dc5d2fee2785",
            "label": "prm:1",
            "type": "promise",
            "properties": {},
        },
        {
            "id": "fa4407d9-6dba-4c22-b4ce-c60e5f738ec9",
            "label": "us-house:119-2:452",
            "type": "roll_call",
            "properties": {},
        },
    ],
    "edges": [
        {
            "id": "32b80f50-2509-4bf3-8b79-0b4061b4154e",
            "source": "9c3670fc-84df-40ac-a916-dc5d2fee2785",
            "target": "fa4407d9-6dba-4c22-b4ce-c60e5f738ec9",
            "predicate": "tested_by",
            "confidence": 0.9,
            "depth": 1,
        }
    ],
    "paths": [],
}

LIVE_FILE_WITH_NULL_METADATA: dict[str, Any] = {
    "file_name": "probe.html",
    "content_type": "text/html",
    "size_bytes": None,
    "file_metadata": None,
    "file_id": "a2d0a9f4-5e5c-465c-b6a2-22350b98b104",
    "project_id": "00000000-0000-0000-0000-000000000000",
    "file_key": "shape/probe.html",
    "created_at": "2026-10-15T23:08:29.843601Z",
    "updated_at": "2026-10-15T23:08:29.843601Z",
}

#: Captured live from the real "informed" project on 2026-10-17, from
#: `POST /projects/{id}/files/upload` — the MULTIPART binary endpoint, whose
#: existence this fix turned on. Bucket name, ids and timings are redacted.
#:
#: Pinning this matters more than most shapes here, because the shape is how
#: the two upload routes are told apart at runtime. The metadata-only route
#: (`LIVE_FILE_WITH_NULL_METADATA`, above) echoes `file_metadata` and a
#: caller-supplied `size_bytes` and never reports a hash; this one reports the
#: `size_bytes` and `sha256_hash` the SERVER measured over the bytes it
#: received, and does not echo `file_metadata` at all. A response carrying a
#: `sha256_hash` is proof an object was actually stored.
LIVE_BINARY_UPLOAD: dict[str, Any] = {
    "success": True,
    "file_id": "465c1ccc-553c-4638-b35c-de76141b6562",
    "file_name": "probe-a.txt",
    "file_key": "probe/roundtrip-a.txt",
    "size_bytes": 31,
    "size_mb": 0.0,
    "content_type": "text/plain",
    "bucket": "zerodb-REDACTED",
    "storage_usage_mb": 0.0,
    "minio_result": {
        "bucket_name": "zerodb-REDACTED",
        "file_key": "probe/roundtrip-a.txt",
        "project_id": "00000000-0000-0000-0000-000000000000",
        "size_bytes": 31,
        "content_type": "text/plain",
        "upload_time_ms": 38.63,
        "status": "uploaded",
        "sha256_hash": "70604ebc0c7d03143e78bc6de805a94c1b807605c79143c6405070cb8129de8d",
    },
    "message": "File uploaded successfully to MinIO via streaming (memory-efficient)",
    "sha256_hash": "70604ebc0c7d03143e78bc6de805a94c1b807605c79143c6405070cb8129de8d",
}

LIVE_PRESIGNED: dict[str, Any] = {
    "presigned_url": "https://bucket.example/zerodb-x/shape/probe.html?X-Amz-Signature=REDACTED",
    "file_id": "a2d0a9f4-5e5c-465c-b6a2-22350b98b104",
    "file_name": "probe.html",
    "expires_at": "2026-10-15T23:13:35.434055",
    "expiry_minutes": 5,
    "operation_type": "download",
}

#: Captured live from the real "informed" project during story 1.3, after a
#: full bootstrap run had registered all 8 entity types / 11 predicates.
#: `id`, `project_id` and `user_id` are redacted like every other payload
#: here. This also pins the live-confirmed fact that drives
#: `ZeroDBClient.graph_upsert_ontology`'s REPLACE-not-merge warning: the
#: response is a plain echo of whatever is currently registered, with no
#: field distinguishing "just written" from "already there" — which is
#: exactly why `_bootstrap_ontology` has to read this shape BEFORE writing
#: rather than infer "already current" from the write's own response.
LIVE_ONTOLOGY: dict[str, Any] = {
    "id": "00000000-0000-0000-0000-000000000000",
    "project_id": "00000000-0000-0000-0000-000000000000",
    "user_id": "00000000-0000-0000-0000-000000000000",
    "entity_types": [
        "person",
        "candidacy",
        "promise",
        "stance",
        "roll_call",
        "bill",
        "verdict",
        "topic",
    ],
    "predicates": {
        "on": {"source": "roll_call", "target": "bill"},
        "about": {"source": "promise", "target": "stance"},
        "under": {"source": "stance", "target": "topic"},
        "voted": {"source": "person", "target": "roll_call"},
        "ran_in": {"source": "person", "target": "candidacy"},
        "opposes": {"source": "bill", "target": "stance"},
        "advances": {"source": "bill", "target": "stance"},
        "sponsored": {"source": "person", "target": "bill"},
        "tested_by": {"source": "promise", "target": "roll_call"},
        "has_verdict": {"source": "promise", "target": "verdict"},
        "made_promise": {"source": "candidacy", "target": "promise"},
    },
    "constraints": {},
    "version": 4,
    "created_at": "2026-10-16T06:11:03.795738+00:00",
    "updated_at": "2026-10-16T06:20:32.407064+00:00",
}

LIVE_EVENT: dict[str, Any] = {
    "topic": "live.check",
    "event_payload": {"roll_call_id": "x"},
    "event_id": "9afc3326-78a6-4925-a259-3ad159145892",
    "project_id": "00000000-0000-0000-0000-000000000000",
    "published_at": "2026-10-15T22:51:10.681438Z",
}

#: Captured live during story 13.5 from `POST
#: /projects/{id}/database/memory`. Three facts worth pinning, each of which
#: shapes `ZeroDBClient.store_memory` and `app.agent_memory`:
#:
#:   * the probe request included `ttl_seconds: 86400` AND a top-level
#:     `expires_at`. **Neither appears here.** The endpoint answered 200 and
#:     dropped both, which is why `store_memory` takes no TTL parameter and
#:     why retention is a sweep rather than a server-side promise.
#:   * `memory_metadata` DOES round-trip verbatim, which is where
#:     `app.agent_memory` keeps its own `expires_at` marker.
#:   * `embedding` comes back as a long all-zeros vector for content stored
#:     without one (truncated here). `MemoryRecord` does not model it, so
#:     `extra="ignore"` has to drop it — this payload is what proves that.
LIVE_MEMORY_RECORD: dict[str, Any] = {
    "agent_id": None,
    "session_id": "5ae00e47-3008-437c-ab37-5aae4b44d8c0",
    "role": "user",
    "content": "SYNTHETIC PROBE: uuid session id + ttl attempt",
    "memory_metadata": {"probe": "13.5", "expires_at": "2026-10-18T00:00:00Z"},
    "memory_id": "828091b7-ba66-44e1-96a5-f8d4b8e2848e",
    "project_id": "00000000-0000-0000-0000-000000000000",
    "embedding": [0.0] * 110,
    "created_at": "2026-10-17T09:05:59.050155+00:00",
}

#: Captured live from `GET /projects/{id}/database/memory?session_id=...`.
#: The list envelope is `{data, total, limit, offset, has_more}` — note
#: `offset`, not the `skip` the request uses — and each item carries BOTH `id`
#: and `memory_id` holding the same uuid. `MemoryPage` renames `data` to
#: `memories` and `MemoryRecord` models only `memory_id`, deliberately: two
#: names for one value is how a caller ends up comparing the wrong one.
#:
#: This also pins that the `session_id` filter works SERVER-SIDE on this
#: endpoint — one row written under a fresh session uuid came back by exactly
#: that filter — unlike `/rows`' filter, which matches nothing (datamodel.md's
#: open question). No client-side fallback is needed or built here.
LIVE_MEMORY_PAGE: dict[str, Any] = {
    "data": [
        {
            "id": "828091b7-ba66-44e1-96a5-f8d4b8e2848e",
            "memory_id": "828091b7-ba66-44e1-96a5-f8d4b8e2848e",
            "content": "SYNTHETIC PROBE: uuid session id + ttl attempt",
            "role": "user",
            "session_id": "5ae00e47-3008-437c-ab37-5aae4b44d8c0",
            "agent_id": None,
            "memory_metadata": {"probe": "13.5", "expires_at": "2026-10-18T00:00:00Z"},
            "created_at": "2026-10-17T09:05:59.050155+00:00",
        }
    ],
    "total": 1,
    "limit": 20,
    "offset": 0,
    "has_more": False,
}

#: Captured live during story 13.5: DELETE on a memory row that no longer
#: exists answers **500**, not 404, with a 404 hiding inside the detail
#: string. The only place in this API where the status code lies about what
#: happened, and the reason `ZeroDBClient.delete_memory` passes a
#: `reinterpret` hook — see that method's docstring. Reported to AINative.
LIVE_MEMORY_DELETE_ALREADY_GONE: dict[str, Any] = {
    "detail": "Failed to delete memory: 404: Memory not found",
    "error_code": "INTERNAL_ERROR",
    "next_action": "retry",
}


def test_a_live_table_payload_parses() -> None:
    table = Table.model_validate(LIVE_TABLE)

    assert table.table_name == "live_check_112"
    assert table.table_id == "2f61b122-82f0-4ba0-9d9a-89cca3c2f3b3"


def test_a_live_row_payload_keeps_informeds_id_separate_from_zerodbs() -> None:
    row = Row.model_validate(LIVE_ROW)

    assert row.row_data["id"] == "us-federal"
    assert row.row_id == "ec3b58e5-b8af-4ab3-814a-0222e35ab0b5"


def test_a_live_edge_payload_parses_without_canonical_names() -> None:
    edge = GraphEdge.model_validate(LIVE_EDGE)

    assert edge.predicate == "tested_by"
    assert edge.source_id == "9c3670fc-84df-40ac-a916-dc5d2fee2785"
    assert edge.source_name is None
    assert edge.target_name is None


def test_a_live_traversal_reads_the_entity_id_out_of_label() -> None:
    traversal = Traversal.model_validate(LIVE_TRAVERSAL)

    assert [node.entity for node in traversal.nodes] == [
        "prm:1",
        "us-house:119-2:452",
    ]
    assert traversal.nodes[1].entity_type == "roll_call"


def test_a_live_memory_record_carries_no_ttl_the_request_asked_for() -> None:
    """The request sent `ttl_seconds` and `expires_at`; the response has neither.

    This is the whole justification for story 13.5's sweep. If ZeroDB ever
    starts honouring a TTL, this test is where that shows up — the payload
    would gain a field, and the sweep could then be deleted in favour of it.
    """
    assert "ttl_seconds" not in LIVE_MEMORY_RECORD
    assert "expires_at" not in LIVE_MEMORY_RECORD
    assert "ttl" not in LIVE_MEMORY_RECORD

    record = MemoryRecord.model_validate(LIVE_MEMORY_RECORD)
    assert record.memory_id == "828091b7-ba66-44e1-96a5-f8d4b8e2848e"
    assert record.session_id == "5ae00e47-3008-437c-ab37-5aae4b44d8c0"
    # The metadata DID survive, which is where the sweep's marker lives.
    assert record.memory_metadata["expires_at"] == "2026-10-18T00:00:00Z"
    assert record.created_at == "2026-10-17T09:05:59.050155+00:00"


def test_a_live_memory_record_drops_the_all_zeros_embedding() -> None:
    """110 floats per turn through every list page is pure weight, and nothing
    in Informed reads them. `extra="ignore"` has to drop them.
    """
    record = MemoryRecord.model_validate(LIVE_MEMORY_RECORD)
    assert not hasattr(record, "embedding")


def test_a_live_memory_page_renames_data_and_keeps_offset() -> None:
    page = MemoryPage.model_validate(LIVE_MEMORY_PAGE)

    assert len(page.memories) == 1
    assert page.memories[0].memory_id == "828091b7-ba66-44e1-96a5-f8d4b8e2848e"
    assert page.total == 1
    # The response says `offset` where the request says `skip`.
    assert page.offset == 0
    assert page.has_more is False


def test_a_memory_delete_500_that_means_404_becomes_not_found() -> None:
    """The live "already gone" payload, end to end through the client.

    Not retried: the correction happens inside `_request`'s retry loop, so a
    row that will never come back costs one call rather than four against a
    ~60 req/min ceiling.
    """
    fake = FakeZeroDBTransport()
    client = ZeroDBClient(transport=fake, project_id="proj-test", sleep=lambda _: None)
    fake.always_fail(status=500, body=LIVE_MEMORY_DELETE_ALREADY_GONE)

    with pytest.raises(ZeroDBNotFoundError):
        client.delete_memory(memory_id="828091b7-ba66-44e1-96a5-f8d4b8e2848e")

    assert len(fake.calls) == 1


def test_a_live_ontology_payload_parses() -> None:
    ontology = Ontology.model_validate(LIVE_ONTOLOGY)

    assert set(ontology.entity_types) == {
        "person",
        "candidacy",
        "promise",
        "stance",
        "roll_call",
        "bill",
        "verdict",
        "topic",
    }
    assert "made_promise" in ontology.predicates
    assert ontology.predicates["made_promise"] == {
        "source": "candidacy",
        "target": "promise",
    }


def test_graph_get_ontology_reads_the_live_shape_through_the_client() -> None:
    """`graph_get_ontology` must parse the exact shape the GET endpoint
    returns — pinned here rather than only unit-tested against the fake."""
    fake = FakeZeroDBTransport()
    fake.always_return_json(LIVE_ONTOLOGY)
    client = ZeroDBClient(transport=fake, project_id="proj-test")

    ontology = client.graph_get_ontology()

    assert ontology is not None
    assert "ran_in" in ontology.predicates


def test_a_live_file_payload_with_null_metadata_parses_to_an_empty_dict() -> None:
    stored = StoredFile.model_validate(LIVE_FILE_WITH_NULL_METADATA)

    assert stored.file_metadata == {}
    assert stored.file_key == "shape/probe.html"


def test_a_live_binary_upload_payload_carries_the_servers_own_size_and_hash() -> None:
    """The response shape is how a stored object is distinguished from a bare row.

    `LIVE_FILE_WITH_NULL_METADATA` (the metadata-only route) reports neither a
    hash nor a server-measured size; this one reports both. Story 9.1's broken
    archives were created by the former, which is why none of their bytes
    could be fetched back.
    """
    stored = StoredFile.model_validate(LIVE_BINARY_UPLOAD)

    assert stored.size_bytes == 31
    assert stored.sha256_hash == LIVE_BINARY_UPLOAD["minio_result"]["sha256_hash"]
    # The binary endpoint does not echo the metadata the caller sent, so the
    # client carries it through itself (see `ZeroDBClient.upload_file`).
    assert stored.file_metadata == {}
    assert StoredFile.model_validate(LIVE_FILE_WITH_NULL_METADATA).sha256_hash is None


def test_a_live_presigned_payload_reads_the_url_out_of_presigned_url() -> None:
    presigned = PresignedUrl.model_validate(LIVE_PRESIGNED)

    assert presigned.url.startswith("https://")
    assert presigned.expiry_minutes == 5
    assert presigned.operation_type == "download"


def test_a_live_event_payload_parses() -> None:
    event = Event.model_validate(LIVE_EVENT)

    assert event.topic == "live.check"
    assert event.event_payload == {"roll_call_id": "x"}


def test_the_client_parses_a_bare_json_array_from_the_events_endpoint() -> None:
    """The live events list is an array, not a {"total", "data"} envelope."""
    fake = FakeZeroDBTransport()
    fake.always_return_json([LIVE_EVENT])
    client = ZeroDBClient(transport=fake, project_id="proj-test")

    page = client.list_events(topic="live.check")

    assert page.total == 1
    assert page.events[0].topic == "live.check"


#: The rate limiter's body, which uses `message`/`error` rather than
#: `detail`/`error_code` like every other ZeroDB error. Captured live.
LIVE_RATE_LIMIT: dict[str, Any] = {
    "error": "rate_limit_exceeded",
    "message": "Too many requests. Please try again later.",
    "client_type": "human",
    "cost_tier": "standard",
    "limit": 60,
    "window_seconds": 60,
    "retry_after": 60,
    "next_steps": {"action": "backoff", "retry_after_ms": 60000},
}


def _client_hitting_rate_limit(
    body: dict[str, Any], sleeps: list[float], *, backoff_max: float = 5.0
) -> ZeroDBClient:
    """A client whose every request is rejected with the given 429 body."""
    fake = FakeZeroDBTransport()
    fake.always_fail(status=429, body=body)
    return ZeroDBClient(
        transport=fake,
        project_id="p",
        max_attempts=2,
        backoff_base=0.01,
        backoff_max=backoff_max,
        sleep=sleeps.append,
    )


def test_the_rate_limit_body_yields_a_readable_message_and_code() -> None:
    """It uses `message`/`error`, not `detail`/`error_code`, so both are read."""
    sleeps: list[float] = []
    client = _client_hitting_rate_limit(LIVE_RATE_LIMIT, sleeps)

    with pytest.raises(ZeroDBRateLimitError) as caught:
        client.create_table("people", {"id": "string"})

    assert "Too many requests" in str(caught.value)
    assert caught.value.error_code == "rate_limit_exceeded"
    assert caught.value.retry_after == 60


def test_the_client_waits_as_long_as_zerodb_asked_capped_at_backoff_max() -> None:
    """The server knows when its throttle clears, so its retry_after wins.

    Capped all the same: a 60s sleep inside a web request is worse than a
    failed request. The live body asks for 60s against a 5s cap.
    """
    sleeps: list[float] = []
    client = _client_hitting_rate_limit(LIVE_RATE_LIMIT, sleeps, backoff_max=5.0)

    with pytest.raises(ZeroDBRateLimitError):
        client.create_table("people", {"id": "string"})

    assert sleeps == [5.0]


def test_a_retry_after_under_the_cap_is_honoured_exactly() -> None:
    sleeps: list[float] = []
    client = _client_hitting_rate_limit({**LIVE_RATE_LIMIT, "retry_after": 2}, sleeps)

    with pytest.raises(ZeroDBRateLimitError):
        client.create_table("people", {"id": "string"})

    assert sleeps == [2.0]


@pytest.mark.parametrize("bad", [None, "soon", -5, 0])
def test_a_malformed_retry_after_falls_back_to_the_clients_own_backoff(bad: Any) -> None:
    """A bad value would drive a worker's sleep, so it is discarded, not trusted."""
    sleeps: list[float] = []
    client = _client_hitting_rate_limit(
        {**LIVE_RATE_LIMIT, "retry_after": bad}, sleeps, backoff_max=5.0
    )

    with pytest.raises(ZeroDBRateLimitError):
        client.create_table("people", {"id": "string"})

    # The client's own jittered backoff, not the junk value.
    assert sleeps and all(0 < s <= 5.0 for s in sleeps)


@pytest.mark.parametrize(
    "payload",
    [
        LIVE_TABLE,
        LIVE_ROW,
        LIVE_EDGE,
        LIVE_FILE_WITH_NULL_METADATA,
        LIVE_BINARY_UPLOAD,
        LIVE_PRESIGNED,
        LIVE_ONTOLOGY,
    ],
)
def test_unknown_extra_fields_never_break_parsing(payload: dict[str, Any]) -> None:
    """ZeroDB adds fields over time; a new one must not fail an ingest run."""
    models = {
        "table_name": Table,
        "row_data": Row,
        "predicate": GraphEdge,
        "file_key": StoredFile,
        "presigned_url": PresignedUrl,
        "entity_types": Ontology,
    }
    model = next(m for key, m in models.items() if key in payload)

    model.model_validate({**payload, "a_field_added_next_quarter": "surprise"})
