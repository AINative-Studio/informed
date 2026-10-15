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
  * the events list is a bare JSON array, not a `{"total", "data"}` envelope.

If ZeroDB changes any of these, these tests fail here rather than at 3am in an
ingest run, which is the whole point of pinning them.
"""

from typing import Any

import pytest

from app.zerodb import ZeroDBClient
from app.zerodb.models import (
    Event,
    GraphEdge,
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

LIVE_PRESIGNED: dict[str, Any] = {
    "presigned_url": "https://bucket.example/zerodb-x/shape/probe.html?X-Amz-Signature=REDACTED",
    "file_id": "a2d0a9f4-5e5c-465c-b6a2-22350b98b104",
    "file_name": "probe.html",
    "expires_at": "2026-10-15T23:13:35.434055",
    "expiry_minutes": 5,
    "operation_type": "download",
}

LIVE_EVENT: dict[str, Any] = {
    "topic": "live.check",
    "event_payload": {"roll_call_id": "x"},
    "event_id": "9afc3326-78a6-4925-a259-3ad159145892",
    "project_id": "00000000-0000-0000-0000-000000000000",
    "published_at": "2026-10-15T22:51:10.681438Z",
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


def test_a_live_file_payload_with_null_metadata_parses_to_an_empty_dict() -> None:
    stored = StoredFile.model_validate(LIVE_FILE_WITH_NULL_METADATA)

    assert stored.file_metadata == {}
    assert stored.file_key == "shape/probe.html"


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


@pytest.mark.parametrize(
    "payload",
    [LIVE_TABLE, LIVE_ROW, LIVE_EDGE, LIVE_FILE_WITH_NULL_METADATA, LIVE_PRESIGNED],
)
def test_unknown_extra_fields_never_break_parsing(payload: dict[str, Any]) -> None:
    """ZeroDB adds fields over time; a new one must not fail an ingest run."""
    models = {
        "table_name": Table,
        "row_data": Row,
        "predicate": GraphEdge,
        "file_key": StoredFile,
        "presigned_url": PresignedUrl,
    }
    model = next(m for key, m in models.items() if key in payload)

    model.model_validate({**payload, "a_field_added_next_quarter": "surprise"})
