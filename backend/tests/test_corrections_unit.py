"""Unit tests for the corrections service, models and retention sweep (12.4).

The feature file covers the acceptance criteria end to end over HTTP. These
cover the edges a scenario would read badly as Gherkin: the status-transition
arithmetic around `resolved_at`, the paging ceiling, the dry run, and the
privacy properties of the stored row.

Every email here is `.invalid` (RFC 2606) and no reporter is real, per prd.md's
rule about test data.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.bootstrap import TABLE_SCHEMAS
from app.corrections.models import (
    CLOSED_STATUSES,
    CORRECTION_ID_PATTERN,
    INITIAL_STATUS,
    MAX_BODY_LENGTH,
    TABLE_NAME,
    CorrectionRequest,
    CorrectionRow,
    CorrectionStatus,
    CorrectionStatusValue,
    is_email_shaped,
)
from app.corrections.retention import (
    RETENTION_DAYS,
    SweepResult,
    sweep_expired_contact_emails,
)
from app.corrections.service import (
    PAGE_SIZE,
    SLA_HOURS,
    CorrectionService,
    UnknownCorrectionError,
    new_correction_id,
    token_digest,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport


@pytest.fixture
def zerodb() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    # Live ZeroDB's server-side filter was measured to drop matching rows
    # (datamodel.md's open question), so every read here rests on the client's
    # own paging fallback — the path production actually takes.
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table(TABLE_NAME, TABLE_SCHEMAS[TABLE_NAME])
    return client


@pytest.fixture
def service(zerodb: ZeroDBClient) -> CorrectionService:
    return CorrectionService(zerodb)


def _submit(
    service: CorrectionService,
    *,
    email: str | None = None,
    hours_ago: float = 0.0,
    body: str = "The cited roll call is from the wrong session.",
):
    return service.submit(
        target_table="stances",
        target_id="en-US:housing:v1:build-more-homes",
        body=body,
        contact_email=email,
        now=datetime.now(UTC) - timedelta(hours=hours_ago),
    )


# --- the documented id and SLA shapes -------------------------------------


def test_the_id_matches_datamodel_mds_documented_pattern() -> None:
    """datamodel.md: `cor:{uuid}`. Pinned against the pattern the model
    publishes, so a change to either has to be a change to both.
    """
    for _ in range(20):
        assert re.fullmatch(CORRECTION_ID_PATTERN, new_correction_id())


def test_two_submissions_of_the_same_report_get_two_rows(service: CorrectionService) -> None:
    """Two reporters spotting the same error is corroboration, not a collision.

    The reason this table's id is random rather than content-addressed the way
    `panel_reviews` and `provenance_log` are: a content hash would silently
    discard the second report of the same mistake, which is precisely the
    signal the table exists to collect.
    """
    first = _submit(service, body="Same wording from two readers.")
    second = _submit(service, body="Same wording from two readers.")
    assert first.id != second.id
    assert first.tracking_token != second.tracking_token
    assert len(service.all_corrections()) == 2


def test_the_receipt_publishes_the_sla_the_code_enforces(service: CorrectionService) -> None:
    receipt = _submit(service)
    assert receipt.sla_hours == SLA_HOURS == 72
    due = datetime.fromisoformat(receipt.sla_due_at.replace("Z", "+00:00"))
    assert timedelta(hours=71) < due - datetime.now(UTC) <= timedelta(hours=72)


def test_the_status_path_in_the_receipt_is_the_route_that_serves_it(
    service: CorrectionService,
) -> None:
    """The link handed to the reporter is built from one definition.

    A hand-built URL in the receipt is how a renamed route silently breaks
    every status link already in the wild — and those links cannot be
    re-sent, because the token is not stored.
    """
    receipt = _submit(service)
    assert receipt.status_path == f"/v1/corrections/{receipt.tracking_token}"


# --- the token is a secret, and the row does not hold it -------------------


def test_the_stored_row_holds_a_digest_and_never_the_token(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    receipt = _submit(service)
    row = zerodb.get_row(TABLE_NAME, receipt.id)
    assert row is not None
    stored = row.row_data
    assert receipt.tracking_token not in str(stored)
    assert stored["tracking_token_hash"] == token_digest(receipt.tracking_token)
    assert len(stored["tracking_token_hash"]) == 64


def test_the_token_carries_real_entropy(service: CorrectionService) -> None:
    """Unguessable on its own merits: the lookup is unauthenticated and
    nothing in code rate-limits it today (see the router's docstring).
    """
    tokens = {_submit(service).tracking_token for _ in range(25)}
    assert len(tokens) == 25
    assert all(len(token) >= 40 for token in tokens)


def test_the_row_id_does_not_resolve_the_status_lookup(service: CorrectionService) -> None:
    """Ids travel — dashboards, logs, tickets, this story's own verification
    output. A separate secret is what keeps a leaked id from exposing a
    stranger's report.
    """
    receipt = _submit(service)
    with pytest.raises(UnknownCorrectionError):
        service.status_for_token(receipt.id)


def test_the_digest_does_not_resolve_the_status_lookup(service: CorrectionService) -> None:
    """A database dump yields digests, not tokens, so a dump cannot be
    replayed against the endpoint — the same posture `voter_profiles` takes
    with its HMAC'd id (story 14.1).
    """
    receipt = _submit(service)
    with pytest.raises(UnknownCorrectionError):
        service.status_for_token(token_digest(receipt.tracking_token))


# --- the reporter's status view withholds everything else ------------------


def test_the_status_view_carries_no_body_email_target_or_id(
    service: CorrectionService,
) -> None:
    receipt = _submit(service, email="reporter@example.invalid", body="SECRET-REPORT-TEXT")
    status = service.status_for_token(receipt.tracking_token)
    serialized = status.model_dump_json()
    assert "SECRET-REPORT-TEXT" not in serialized
    assert "reporter@example.invalid" not in serialized
    assert "build-more-homes" not in serialized
    assert receipt.id not in serialized


def test_the_status_model_cannot_grow_a_leaky_field() -> None:
    """`extra="forbid"`, asserted rather than assumed.

    This model is the whole privacy boundary of an unauthenticated lookup, so
    a future story adding `body` to it has to delete this test and say why.
    """
    assert set(CorrectionStatus.model_fields) == {
        "status",
        "submitted_at",
        "sla_due_at",
        "sla_hours",
        "past_sla",
        "resolved_at",
    }


def test_the_reporter_is_told_when_their_own_report_is_past_sla(
    service: CorrectionService,
) -> None:
    """An SLA the person waiting cannot check is a promise to ourselves."""
    fresh = _submit(service, hours_ago=1)
    overdue = _submit(service, hours_ago=SLA_HOURS + 1)
    assert service.status_for_token(fresh.tracking_token).past_sla is False
    assert service.status_for_token(overdue.tracking_token).past_sla is True


def test_a_triaged_report_is_not_past_sla_however_old(service: CorrectionService) -> None:
    """prd.md promises 72 hours to TRIAGE, not to fix. A report that has been
    read and classified has met the commitment.
    """
    receipt = _submit(service, hours_ago=SLA_HOURS * 10)
    service.set_status(receipt.id, status="triaged")
    assert service.status_for_token(receipt.tracking_token).past_sla is False
    assert service.past_sla() == []


# --- status transitions and resolved_at -----------------------------------


def test_closing_sets_resolved_at_and_reopening_clears_it(
    service: CorrectionService,
) -> None:
    """The case worth being careful about.

    A sweep keyed on "was once resolved" would erase the contact email of a
    reporter whose report is currently open again — a worse service, and a
    silent one.
    """
    receipt = _submit(service, email="reporter@example.invalid")
    assert service.get(receipt.id).resolved_at is None
    closed = service.set_status(receipt.id, status="resolved")
    assert closed.resolved_at is not None
    reopened = service.set_status(receipt.id, status="triaged")
    assert reopened.resolved_at is None


def test_restating_a_closed_status_does_not_move_the_retention_clock(
    service: CorrectionService,
) -> None:
    """A double-click must not buy the reporter's email another 90 days."""
    receipt = _submit(service, email="reporter@example.invalid")
    first = service.set_status(
        receipt.id, status="resolved", now=datetime.now(UTC) - timedelta(days=89)
    )
    again = service.set_status(receipt.id, status="resolved")
    assert again.resolved_at == first.resolved_at


def test_rejecting_also_starts_the_retention_clock(service: CorrectionService) -> None:
    """A reporter whose report was closed without a change has the same
    retention promise as one whose report was acted on.
    """
    receipt = _submit(service, email="reporter@example.invalid")
    rejected = service.set_status(receipt.id, status="rejected")
    assert rejected.status in CLOSED_STATUSES
    assert rejected.resolved_at is not None


@pytest.mark.parametrize("bad", ["mostly-fine", "pending", "approved", "RESOLVED", ""])
def test_an_unknown_status_is_refused(service: CorrectionService, bad: str) -> None:
    """`pending` and `approved` are in the list on purpose: they are
    `qa_status`'s vocabulary, and accepting one here is exactly the near-miss
    `models.py` chose `received` to avoid.
    """
    receipt = _submit(service)
    with pytest.raises(ValueError, match="not a correction status"):
        service.set_status(receipt.id, status=bad)
    assert service.get(receipt.id).status == INITIAL_STATUS


def test_triaging_an_unknown_correction_raises(service: CorrectionService) -> None:
    with pytest.raises(UnknownCorrectionError):
        service.set_status("cor:00000000-0000-4000-8000-000000000000", status="triaged")


def test_the_vocabulary_is_exactly_the_four_documented_values() -> None:
    assert set(CorrectionStatusValue.__args__) == {
        "received",
        "triaged",
        "resolved",
        "rejected",
    }
    assert sorted(CLOSED_STATUSES) == ["rejected", "resolved"]
    assert INITIAL_STATUS == "received"


# --- the SLA and triage queues --------------------------------------------


def test_the_sla_queue_is_oldest_deadline_first(service: CorrectionService) -> None:
    """The order to work them in."""
    older = _submit(service, hours_ago=SLA_HOURS + 48)
    newer = _submit(service, hours_ago=SLA_HOURS + 1)
    _submit(service, hours_ago=1)
    assert [row.id for row in service.past_sla()] == [older.id, newer.id]


def test_a_closed_correction_never_appears_in_the_sla_queue(
    service: CorrectionService,
) -> None:
    for status in ("resolved", "rejected"):
        receipt = _submit(service, hours_ago=SLA_HOURS + 1)
        service.set_status(receipt.id, status=status)
    assert service.past_sla() == []


def test_the_open_queue_is_oldest_submission_first(service: CorrectionService) -> None:
    older = _submit(service, hours_ago=10)
    newer = _submit(service, hours_ago=1)
    closed = _submit(service, hours_ago=5)
    service.set_status(closed.id, status="resolved")
    assert [row.id for row in service.open_corrections()] == [older.id, newer.id]


def test_a_scan_reads_past_the_first_page(service: CorrectionService) -> None:
    """The one table in this codebase whose size is set by the public.

    `PAGE_SIZE` rows would fit in one read; this writes more than that so the
    paging loop is exercised rather than assumed. Without it, the SLA queue
    and the retention sweep would both silently stop at row 200.
    """
    planted = {_submit(service, hours_ago=SLA_HOURS + 1).id for _ in range(PAGE_SIZE + 5)}
    assert len(service.all_corrections()) == PAGE_SIZE + 5
    assert {row.id for row in service.past_sla()} == planted


# --- the target ------------------------------------------------------------


def test_a_correction_about_a_nonexistent_row_is_stored(service: CorrectionService) -> None:
    """ "There is no such stance on my ballot" is a real report.

    Refusing it would discard the report that matters most. See `models.py`.
    """
    receipt = service.submit(
        target_table="stances",
        target_id="en-US:housing:v1:no-such-stance-anywhere",
        body="This stance is not on my ballot at all.",
    )
    assert service.get(receipt.id).target_id == "en-US:housing:v1:no-such-stance-anywhere"


@pytest.mark.parametrize(
    "table", ["not_a_real_table", "voter_profiles_backup", "", "STANCES", "stances; drop"]
)
def test_an_unknown_target_table_is_refused(service: CorrectionService, table: str) -> None:
    """The one check this endpoint makes about its target: `target_table` is
    what an ops queue groups by, so an invented name makes the queue unreadable.
    """
    with pytest.raises(ValueError, match="not a table in the Informed schema"):
        service.submit(target_table=table, target_id="x", body="Something is wrong.")


def test_every_real_table_is_a_valid_correction_target(service: CorrectionService) -> None:
    """Checked against `bootstrap.TABLE_SCHEMAS` rather than a list in the
    corrections module, so a table added by a later epic is immediately
    correctable without touching this story.
    """
    for table in TABLE_SCHEMAS:
        receipt = service.submit(
            target_table=table, target_id="synthetic-target", body="Synthetic report."
        )
        assert service.get(receipt.id).target_table == table


# --- the stored row's shape and privacy -----------------------------------


def test_the_row_holds_nothing_identifying_about_the_reporter() -> None:
    """No IP, no user agent, no account id, no `user_id`.

    A correction is political speech about a candidate; a table pairing it
    with an identifier is the record prd.md's privacy section exists to
    prevent. `extra="forbid"` means a write carrying one is refused rather
    than stored and ignored.
    """
    fields = set(CorrectionRow.model_fields)
    assert fields == {
        "id",
        "target_table",
        "target_id",
        "body",
        "contact_email",
        "status",
        "created_at",
        "sla_due_at",
        "resolved_at",
        "tracking_token_hash",
    }
    for forbidden in ("user_id", "ip_address", "user_agent", "session_id", "payload"):
        assert forbidden not in fields


def test_the_row_is_not_append_only() -> None:
    """datamodel.md rule 6 names verdicts, positions and the provenance log.

    This table's status, resolution time and email erasure are all defined by
    being updated in place; a `version_key` here would make the retention
    sweep append a new row carrying the email it was erasing.
    """
    assert CorrectionRow.version_key is None
    assert CorrectionRow.references == {}


def test_the_bootstrap_schema_matches_the_model() -> None:
    """The ZeroDB column list and the Pydantic model cannot drift."""
    assert set(TABLE_SCHEMAS[TABLE_NAME]) == set(CorrectionRow.model_fields)


def test_a_blank_body_is_refused_everywhere(service: CorrectionService) -> None:
    """Both at the request boundary and on the row.

    The 72-hour clock must not run on a report with no content, and the rule
    has to hold for a direct service call as well as for an HTTP one.
    """
    for blank in ("", "   ", "\n\t "):
        with pytest.raises(ValueError):
            CorrectionRequest(target_table="stances", target_id="x", body=blank)
    with pytest.raises(Exception, match="empty body|invalid"):
        service.submit(target_table="stances", target_id="x", body="   ")


def test_a_body_is_trimmed_not_merely_accepted(service: CorrectionService) -> None:
    receipt = _submit(service, body="  The date is wrong.  ")
    assert service.get(receipt.id).body == "The date is wrong."


def test_an_over_long_body_is_refused() -> None:
    """Bounds what one uncredentialed request can push into the table."""
    with pytest.raises(ValueError):
        CorrectionRequest(target_table="stances", target_id="x", body="x" * (MAX_BODY_LENGTH + 1))
    assert CorrectionRequest(target_table="stances", target_id="x", body="x" * MAX_BODY_LENGTH)


def test_a_submission_cannot_smuggle_server_owned_fields() -> None:
    """`status`, `resolved_at` and `tracking_token_hash` are the server's.

    `extra="forbid"` refuses rather than ignores, which matters: an ignored
    field is a caller who believes they set it.
    """
    for smuggled in ("status", "resolved_at", "tracking_token_hash", "sla_due_at", "id"):
        with pytest.raises(ValueError):
            CorrectionRequest.model_validate(
                {
                    "target_table": "stances",
                    "target_id": "x",
                    "body": "Something is wrong.",
                    smuggled: "injected",
                }
            )


# --- contact_email: optional, shape-checked -------------------------------


@pytest.mark.parametrize("email", ["a@b.co", "reporter.name+tag@example.invalid"])
def test_a_plausible_email_is_accepted(email: str) -> None:
    assert is_email_shaped(email)


@pytest.mark.parametrize(
    "email", ["not-an-email", "@example.invalid", "a@b", "a b@example.invalid", "a@@b.co", "a@b."]
)
def test_an_implausible_email_is_refused(email: str) -> None:
    """A shape check, never a claim of deliverability — see `models.py`.

    An unusable address is worse than none: it carries the 90-day retention
    obligation and a privacy risk while never reaching the reporter.
    """
    assert not is_email_shaped(email)
    with pytest.raises(ValueError):
        CorrectionRequest(target_table="stances", target_id="x", body="Wrong.", contact_email=email)


def test_a_blank_email_is_treated_as_absent() -> None:
    """An empty form field is "I would rather not", not an empty address."""
    request = CorrectionRequest(
        target_table="stances", target_id="x", body="Wrong.", contact_email="   "
    )
    assert request.contact_email is None


def test_a_reporter_who_gave_no_email_gets_the_same_status_page(
    service: CorrectionService,
) -> None:
    """What makes "optional" true rather than nominal: the token is not
    derived from the email, so anonymity costs the reporter nothing.
    """
    anonymous = _submit(service, email=None)
    named = _submit(service, email="reporter@example.invalid")
    assert (
        service.status_for_token(anonymous.tracking_token).status
        == service.status_for_token(named.tracking_token).status
    )


# --- the 90-day retention sweep -------------------------------------------


def test_the_retention_window_is_the_documented_90_days() -> None:
    assert RETENTION_DAYS == 90


def test_the_sweep_erases_only_what_is_past_the_window(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    expired = _submit(service, email="expired@example.invalid", hours_ago=24 * 200)
    service.set_status(
        expired.id, status="resolved", now=datetime.now(UTC) - timedelta(days=RETENTION_DAYS + 1)
    )
    recent = _submit(service, email="recent@example.invalid", hours_ago=24 * 10)
    service.set_status(recent.id, status="resolved")
    open_report = _submit(service, email="open@example.invalid", hours_ago=24 * 400)

    result = sweep_expired_contact_emails(zerodb)

    assert result.erased == 1
    assert result.erased_ids == [expired.id]
    assert service.get(expired.id).contact_email is None
    assert service.get(recent.id).contact_email == "recent@example.invalid"
    assert service.get(open_report.id).contact_email == "open@example.invalid"


def test_the_sweep_is_exactly_at_the_boundary(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    """Day 89 is kept and day 91 is erased.

    Pinned because an off-by-one here either breaks the promise or breaks it
    early, and neither is visible without a boundary test.
    """
    day89 = _submit(service, email="day89@example.invalid", hours_ago=24 * 120)
    service.set_status(day89.id, status="resolved", now=datetime.now(UTC) - timedelta(days=89))
    day91 = _submit(service, email="day91@example.invalid", hours_ago=24 * 120)
    service.set_status(day91.id, status="resolved", now=datetime.now(UTC) - timedelta(days=91))

    sweep_expired_contact_emails(zerodb)

    assert service.get(day89.id).contact_email == "day89@example.invalid"
    assert service.get(day91.id).contact_email is None


def test_a_reopened_correction_is_not_swept(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    """The case a naive "was once resolved" sweep gets wrong."""
    receipt = _submit(service, email="reporter@example.invalid", hours_ago=24 * 200)
    service.set_status(receipt.id, status="resolved", now=datetime.now(UTC) - timedelta(days=150))
    service.set_status(receipt.id, status="triaged")

    result = sweep_expired_contact_emails(zerodb)

    assert result.erased == 0
    assert result.open_corrections == 1
    assert service.get(receipt.id).contact_email == "reporter@example.invalid"


def test_the_sweep_is_idempotent(service: CorrectionService, zerodb: ZeroDBClient) -> None:
    """The property that makes it safe to schedule later, whatever the
    cadence: no cursor, no watermark, no state a missed run desynchronises.
    """
    receipt = _submit(service, email="reporter@example.invalid", hours_ago=24 * 200)
    service.set_status(receipt.id, status="resolved", now=datetime.now(UTC) - timedelta(days=100))
    first = sweep_expired_contact_emails(zerodb)
    second = sweep_expired_contact_emails(zerodb)
    third = sweep_expired_contact_emails(zerodb)
    assert (first.erased, second.erased, third.erased) == (1, 0, 0)
    assert second.already_clear == third.already_clear == 1


def test_the_sweep_preserves_everything_except_the_email(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    """`update_row` read-merge-writes; an `upsert_row` here would erase the
    report along with the address (story 1.2 measured that live).
    """
    receipt = _submit(
        service, email="reporter@example.invalid", hours_ago=24 * 200, body="The date is wrong."
    )
    service.set_status(receipt.id, status="resolved", now=datetime.now(UTC) - timedelta(days=120))
    before = service.get(receipt.id)

    sweep_expired_contact_emails(zerodb)
    after = service.get(receipt.id)

    assert after.contact_email is None
    assert after.body == before.body == "The date is wrong."
    assert after.status == "resolved"
    assert after.resolved_at == before.resolved_at
    assert after.created_at == before.created_at
    assert after.sla_due_at == before.sla_due_at
    assert after.tracking_token_hash == before.tracking_token_hash


def test_the_erased_column_is_null_rather_than_absent(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    """A row where the column is missing is indistinguishable from one written
    before the field existed.
    """
    receipt = _submit(service, email="reporter@example.invalid", hours_ago=24 * 200)
    service.set_status(receipt.id, status="resolved", now=datetime.now(UTC) - timedelta(days=120))
    sweep_expired_contact_emails(zerodb)
    row = zerodb.get_row(TABLE_NAME, receipt.id)
    assert row is not None
    assert "contact_email" in row.row_data
    assert row.row_data["contact_email"] is None


def test_a_swept_reporter_can_still_check_their_status(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    """The token is not derived from the email and does not expire with it."""
    receipt = _submit(service, email="reporter@example.invalid", hours_ago=24 * 200)
    service.set_status(receipt.id, status="resolved", now=datetime.now(UTC) - timedelta(days=120))
    sweep_expired_contact_emails(zerodb)
    assert service.status_for_token(receipt.tracking_token).status == "resolved"


def test_a_dry_run_writes_nothing_but_reports_what_it_would_erase(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    """For a first run against a live project, where "how many addresses is
    this about to delete" is worth answering before deleting them.
    """
    receipt = _submit(service, email="reporter@example.invalid", hours_ago=24 * 200)
    service.set_status(receipt.id, status="resolved", now=datetime.now(UTC) - timedelta(days=120))
    dry = sweep_expired_contact_emails(zerodb, dry_run=True)
    assert dry.erased == 1
    assert dry.erased_ids == [receipt.id]
    assert service.get(receipt.id).contact_email == "reporter@example.invalid"

    live = sweep_expired_contact_emails(zerodb)
    assert live.erased == 1
    assert service.get(receipt.id).contact_email is None


def test_the_sweep_distinguishes_nothing_to_do_from_nothing_to_look_at(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    """A run of all zeros should be readable: an empty table and a table of
    corrections all inside their window are different operational facts.
    """
    empty = sweep_expired_contact_emails(zerodb)
    assert (empty.scanned, empty.erased) == (0, 0)

    receipt = _submit(service, email="reporter@example.invalid")
    service.set_status(receipt.id, status="resolved")
    populated = sweep_expired_contact_emails(zerodb)
    assert populated.scanned == 1
    assert populated.within_window == 1
    assert populated.erased == 0


def test_a_sweep_summary_is_safe_to_paste_into_a_ticket(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    """The whole point of this module is that an address stops existing; a
    summary quoting the address it just erased would defeat it.
    """
    receipt = _submit(service, email="reporter@example.invalid", hours_ago=24 * 200)
    service.set_status(receipt.id, status="resolved", now=datetime.now(UTC) - timedelta(days=120))
    summary = sweep_expired_contact_emails(zerodb).summary()
    assert "reporter@example.invalid" not in summary
    assert "erased 1 contact email" in summary


def test_the_sweep_result_carries_ids_not_addresses() -> None:
    result = SweepResult(scanned=3, erased=1, erased_ids=["cor:x"], within_window=2)
    assert result.erased_ids == ["cor:x"]
    assert "@" not in result.summary()


# --- the sweep and the scan agree about paging ----------------------------


def test_the_sweep_reads_past_the_first_page(
    service: CorrectionService, zerodb: ZeroDBClient
) -> None:
    """A sweep that stopped at row 200 would silently keep addresses past
    their retention window — a broken promise that nothing would report.
    """
    closed_long_ago = datetime.now(UTC) - timedelta(days=120)
    planted = []
    for index in range(PAGE_SIZE + 3):
        receipt = _submit(service, email=f"r{index}@example.invalid", hours_ago=24 * 200)
        service.set_status(receipt.id, status="resolved", now=closed_long_ago)
        planted.append(receipt.id)

    result = sweep_expired_contact_emails(zerodb)

    assert result.scanned == PAGE_SIZE + 3
    assert result.erased == PAGE_SIZE + 3
    assert all(service.get(row_id).contact_email is None for row_id in planted)
