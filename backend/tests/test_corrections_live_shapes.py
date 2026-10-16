"""Live-confirmed behaviour public corrections depends on (story 12.4).

Same purpose as `test_public_methodology_live_shapes.py` and
`test_profiles_live_shapes.py`: each test here pins something MEASURED against
the live "informed" project during story 12.4, not inferred from the docs or
from the fake. The measurement output is in the story's PR.

What was measured, on 2026-10-16, with
`scripts/verify_public_corrections_live.py` and a raw-shape probe. The live
`corrections` table held **0 rows** before and after (this story is the first
to write to it, and every synthetic row written was deleted). One submitted row
came back verbatim as:

    {
      "body": "SYNTHETIC PROBE (12.4) raw-shape capture.",
      "contact_email": "probe@example.invalid",
      "created_at": "2026-10-16T23:12:01.361036Z",
      "id": "cor:b95d1100-073a-4525-b13d-3d2d1f5892ac",
      "resolved_at": null,
      "sla_due_at": "2026-10-19T23:12:01.361036Z",
      "status": "received",
      "target_id": "en-US:housing:v1:probe",
      "target_table": "stances",
      "tracking_token_hash": "<64 hex>"
    }

and from it, plus the 36 checks in the verification script:

  * **`cor:{uuid}` survives as a live row id.** The first id in this codebase
    whose natural key contains a uuid's hyphens — every other is
    colon-segmented (`us-house:119-2:452`) or dotted (`2026.1`) — and
    `ZeroDBClient`'s find-then-write resolved it by matching `row_data.id`
    without the hyphens being treated as anything structural.
  * **`sla_due_at` is exactly 72 hours after `created_at`, to the
    microsecond.** Both came back as full ISO-8601 strings with the `Z` suffix
    and six-digit microseconds intact — not truncated to seconds, not
    reformatted to an offset, not converted to a number. The whole SLA read
    rests on lexicographic string comparison of these, so a reformatted
    timestamp would silently break `past_sla()`.
  * **`resolved_at` round-trips as a real JSON `null` while open**, and as a
    timestamp string once closed. Not the string `"None"`, not absent. The
    retention sweep keys on `resolved_at` being falsy, so a `"None"` here
    would make every open correction look closed and sweep every email.
  * **`contact_email` round-trips as `null` when the reporter gives none, and
    the column is PRESENT rather than missing.** The sweep and
    `CorrectionRow.contact_email is None` both depend on that distinction.
  * **Nulling `contact_email` through `update_row` erases the address and
    nothing else** — live ZeroDB read-merge-wrote, so the body, status,
    `resolved_at`, `created_at`, `sla_due_at` and token digest all survived.
    A wholesale `PUT` would have destroyed the report (story 1.2 measured that
    behaviour); this is the measurement that makes the retention sweep safe.
  * **The erased column came back as `null`, not missing**, so a swept row is
    distinguishable from one written before the field existed.
  * **The 90-day sweep is idempotent live**: the second run over the same data
    reported `erased 0` and moved the row into `already_clear`.
  * **The live SLA queue found a backdated untriaged correction, and excluded
    both a fresh one and the closed ones** — over the client's paged scan,
    which is the path that matters because datamodel.md records that ZeroDB's
    server-side `/query` filter drops matching rows.
  * **Every status in the vocabulary persisted** (`received` -> `triaged` ->
    `resolved`), and closing set `resolved_at` on the live row.
  * **The whole HTTP surface answered over a live ZeroDB client with no
    `Authorization` header**: `POST /v1/corrections` 201, the receipt's own
    `status_path` 200, the row id on that path 404, an unknown token 404, a
    target table outside the schema 422, and the three malformed tokens
    (`../../etc/passwd`, `short`, 200 x's) 404 or 422 at the edge — never
    reaching ZeroDB.

Nothing here makes a live call. The payload is the live one, replayed through
the fake, which is what makes these runnable in CI without credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.bootstrap import TABLE_SCHEMAS
from app.corrections.models import TABLE_NAME, CorrectionRow
from app.corrections.retention import RETENTION_DAYS, sweep_expired_contact_emails
from app.corrections.service import SLA_HOURS, CorrectionService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

#: Copied verbatim from a live submitted row on 2026-10-16 (project id
#: redacted; the token digest replaced with a same-length placeholder, since a
#: digest is the one field a live capture should not carry into a repo).
#: Synthetic throughout: the body is marked as a probe and the address is
#: `.invalid` (RFC 2606). There is no real reporter data in this table at all —
#: the live table held 0 rows before and after story 12.4's verification.
LIVE_ROW: dict[str, Any] = {
    "body": "SYNTHETIC PROBE (12.4) raw-shape capture.",
    "contact_email": "probe@example.invalid",
    "created_at": "2026-10-16T23:12:01.361036Z",
    "id": "cor:b95d1100-073a-4525-b13d-3d2d1f5892ac",
    "resolved_at": None,
    "sla_due_at": "2026-10-19T23:12:01.361036Z",
    "status": "received",
    "target_id": "en-US:housing:v1:probe",
    "target_table": "stances",
    "tracking_token_hash": "a" * 64,
}

#: The live `resolved_at` measured after a status move to `resolved`.
LIVE_RESOLVED_AT = "2026-10-16T23:12:02.083936Z"


@pytest.fixture
def zerodb() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table(TABLE_NAME, TABLE_SCHEMAS[TABLE_NAME])
    client.upsert_row(TABLE_NAME, str(LIVE_ROW["id"]), dict(LIVE_ROW))
    return client


@pytest.fixture
def service(zerodb: ZeroDBClient) -> CorrectionService:
    return CorrectionService(zerodb)


def test_the_live_row_validates_as_the_model(service: CorrectionService) -> None:
    stored = service.get(str(LIVE_ROW["id"]))
    assert stored.status == "received"
    assert stored.target_table == "stances"
    assert stored.contact_email == "probe@example.invalid"


def test_a_uuid_id_resolves_as_a_live_row_id(zerodb: ZeroDBClient) -> None:
    """`cor:b95d1100-073a-…` is the first natural key here containing hyphens.

    Every other id in this codebase is colon-segmented or dotted. Confirmed
    live that the hyphens are not treated as structural by ZeroDB or by the
    client's find-then-write id resolution.
    """
    assert zerodb.get_row(TABLE_NAME, str(LIVE_ROW["id"])) is not None


def test_live_timestamps_keep_microseconds_and_the_z_suffix(
    zerodb: ZeroDBClient,
) -> None:
    """Pinned on the raw `row_data`, not through the model.

    Pydantic would happily accept a truncated or offset-formatted timestamp,
    hiding what is being measured. The entire SLA read compares these strings
    lexicographically, so a reformatted one would silently break `past_sla()`.
    """
    row = zerodb.get_row(TABLE_NAME, str(LIVE_ROW["id"]))
    assert row is not None
    for field in ("created_at", "sla_due_at"):
        stamp = row.row_data[field]
        assert isinstance(stamp, str)
        assert stamp.endswith("Z"), stamp
        assert len(stamp.split(".")[-1]) == 7, stamp  # six digits + "Z"


def test_the_live_sla_gap_is_exactly_72_hours(zerodb: ZeroDBClient) -> None:
    row = zerodb.get_row(TABLE_NAME, str(LIVE_ROW["id"]))
    assert row is not None
    created = datetime.fromisoformat(str(row.row_data["created_at"]).replace("Z", "+00:00"))
    due = datetime.fromisoformat(str(row.row_data["sla_due_at"]).replace("Z", "+00:00"))
    assert due - created == timedelta(hours=SLA_HOURS)


def test_live_resolved_at_is_a_real_null_while_open(zerodb: ZeroDBClient) -> None:
    """Measured live: JSON `null`, not the string `"None"` and not absent.

    The retention sweep keys on `resolved_at` being falsy. A `"None"` string
    here would make every open correction look closed, and the sweep would
    erase the email of every reporter still waiting for an answer.
    """
    row = zerodb.get_row(TABLE_NAME, str(LIVE_ROW["id"]))
    assert row is not None
    assert "resolved_at" in row.row_data
    assert row.row_data["resolved_at"] is None


def test_live_absent_contact_email_is_null_with_the_column_present(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    """Measured live for a submission carrying no address at all."""
    receipt = service.submit(
        target_table="stances", target_id="en-US:housing:v1:probe", body="No address given."
    )
    row = zerodb.get_row(TABLE_NAME, receipt.id)
    assert row is not None
    assert "contact_email" in row.row_data
    assert row.row_data["contact_email"] is None


def test_live_status_moves_persist_and_set_resolved_at(service: CorrectionService) -> None:
    """The live sequence: received -> triaged -> resolved, with the timestamp.

    `LIVE_RESOLVED_AT` is the real measured value; the point of pinning it is
    its SHAPE — a full microsecond `Z` timestamp, like every other one — since
    the retention window is computed by comparing it to a cutoff string.
    """
    row_id = str(LIVE_ROW["id"])
    assert service.set_status(row_id, status="triaged").resolved_at is None
    closed = service.set_status(row_id, status="resolved")
    assert closed.resolved_at is not None
    assert closed.resolved_at.endswith("Z")
    assert LIVE_RESOLVED_AT.endswith("Z")
    assert len(LIVE_RESOLVED_AT.split(".")[-1]) == len(closed.resolved_at.split(".")[-1])


def test_live_nulling_the_email_preserves_every_other_field(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    """The measurement the whole retention sweep rests on.

    Live ZeroDB's `update_row` read-merge-wrote: the address went and the
    report, status, timestamps and token digest all survived. A wholesale
    `PUT` would have destroyed the report — the behaviour story 1.2 measured
    on this same API.
    """
    row_id = str(LIVE_ROW["id"])
    service.set_status(
        row_id, status="resolved", now=datetime.now(UTC) - timedelta(days=RETENTION_DAYS + 10)
    )
    before = service.get(row_id)

    result = sweep_expired_contact_emails(zerodb)
    after = service.get(row_id)

    assert result.erased == 1
    assert after.contact_email is None
    assert after.body == before.body
    assert after.status == "resolved"
    assert after.resolved_at == before.resolved_at
    assert after.created_at == LIVE_ROW["created_at"]
    assert after.sla_due_at == LIVE_ROW["sla_due_at"]
    assert after.tracking_token_hash == before.tracking_token_hash


def test_live_erased_column_is_null_not_missing(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    """A swept row must stay distinguishable from one written before the field
    existed. Measured live after the sweep.
    """
    row_id = str(LIVE_ROW["id"])
    service.set_status(
        row_id, status="resolved", now=datetime.now(UTC) - timedelta(days=RETENTION_DAYS + 10)
    )
    sweep_expired_contact_emails(zerodb)
    row = zerodb.get_row(TABLE_NAME, row_id)
    assert row is not None
    assert "contact_email" in row.row_data
    assert row.row_data["contact_email"] is None


def test_the_live_sweep_is_idempotent(service: CorrectionService, zerodb: ZeroDBClient) -> None:
    """Measured live: the second run reported `erased 0` and moved the row
    into `already_clear`.
    """
    service.set_status(
        str(LIVE_ROW["id"]),
        status="resolved",
        now=datetime.now(UTC) - timedelta(days=RETENTION_DAYS + 10),
    )
    first = sweep_expired_contact_emails(zerodb)
    second = sweep_expired_contact_emails(zerodb)
    assert (first.erased, second.erased) == (1, 0)
    assert second.already_clear == 1


def test_the_live_table_schema_carries_this_storys_two_added_columns() -> None:
    """`created_at` and `tracking_token_hash` were added to `TABLE_SCHEMAS` by
    this story and created live. Pinned because no earlier story had a reason
    to write this table at all, so a live environment bootstrapped before 12.4
    would be missing both.
    """
    schema = TABLE_SCHEMAS[TABLE_NAME]
    assert schema["created_at"] == "string"
    assert schema["tracking_token_hash"] == "string"
    assert set(schema) == set(CorrectionRow.model_fields)


def test_the_live_row_contains_no_reporter_identifier() -> None:
    """The live bytes, not just the model: no IP, user agent or account id
    reached the live project.
    """
    for forbidden in ("ip", "user_agent", "user_id", "session", "profile"):
        assert not any(forbidden in key for key in LIVE_ROW), forbidden
