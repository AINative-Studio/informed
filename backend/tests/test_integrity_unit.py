"""Unit tests for `app.integrity.checks`/`app.integrity.repair` internals not
reached by the BDD suite (stories 16.2, 16.4).

`tests/features/integrity_checks.feature` and `tests/features/drift_repair.feature`
cover the five checks' and the repair's observable behaviour, including
sabotage scenarios that prove each check catches a real violation. This file
covers the shared plumbing those scenarios exercise only incidentally:
`paged_rows`' own pagination boundaries (this codebase has a documented
history of live ZeroDB paging bugs — see datamodel.md's "Open questions" —
so the loop logic earns a direct test rather than relying on it working by
accident of small fixture sizes), and the small report/summary dunder methods
("safe to paste into a ticket" strings) that no scenario's assertions happen
to call.
"""

from __future__ import annotations

from app.integrity.checks import (
    MAX_ROWS_SCANNED,
    SCAN_PAGE_SIZE,
    CheckReport,
    CombinedReport,
    Violation,
    check_current_verdict_uniqueness,
    paged_rows,
)
from app.integrity.repair import RepairResult, repair_member_votes_drift
from app.repositories import Repository
from app.repositories.models import MemberVote, Person, RollCall
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

# --- paged_rows pagination boundaries ---------------------------------------


def _client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table("roll_calls", {"id": "string"})
    return client


def test_paged_rows_is_empty_for_a_table_with_no_rows() -> None:
    client = _client()
    assert paged_rows(client, "roll_calls") == []


def test_paged_rows_reads_a_single_row() -> None:
    client = _client()
    client.insert_row("roll_calls", {"id": "us-house:119-2:1"})
    rows = paged_rows(client, "roll_calls")
    assert [row["id"] for row in rows] == ["us-house:119-2:1"]


def test_paged_rows_reads_across_multiple_pages() -> None:
    """More rows than `SCAN_PAGE_SIZE` must still all come back, exactly
    once each — the loop's whole reason to exist rather than a single
    `query_rows` call.
    """
    client = _client()
    total = SCAN_PAGE_SIZE + 7
    for index in range(total):
        client.insert_row("roll_calls", {"id": f"us-house:119-2:{index}"})

    rows = paged_rows(client, "roll_calls")

    assert len(rows) == total
    assert {row["id"] for row in rows} == {f"us-house:119-2:{i}" for i in range(total)}


def test_paged_rows_stops_at_the_scan_cap_rather_than_looping_forever(monkeypatch) -> None:
    """A pathological table (or a broken fake) that never runs out of pages
    must not hang the check forever: the loop bails out once
    `MAX_ROWS_SCANNED` rows have been read, logged as a capped scan rather
    than silently claimed complete.
    """
    client = _client()

    class _InfinitePage:
        rows = [type("R", (), {"row_data": {"id": "x"}})() for _ in range(SCAN_PAGE_SIZE)]
        has_more = True

    monkeypatch.setattr(client, "query_rows", lambda *a, **k: _InfinitePage())

    rows = paged_rows(client, "roll_calls")

    assert len(rows) >= MAX_ROWS_SCANNED


# --- report/summary plumbing -------------------------------------------------


def test_violation_str_includes_table_row_and_detail() -> None:
    violation = Violation(table="member_votes", row_id="abc", detail="something is wrong")
    text = str(violation)
    assert "member_votes" in text
    assert "abc" in text
    assert "something is wrong" in text


def test_check_report_summary_reports_scanned_and_violation_counts() -> None:
    report = CheckReport(
        name="references",
        scanned=5,
        violations=(Violation(table="t", row_id="1", detail="d"),),
    )
    summary = report.summary()
    assert "references" in summary
    assert "5" in summary
    assert "1 violation" in summary


def test_check_report_ok_is_false_when_there_are_violations() -> None:
    report = CheckReport(name="x", scanned=1, violations=(Violation("t", "1", "d"),))
    assert report.ok is False


def test_check_report_ok_is_true_with_no_violations() -> None:
    report = CheckReport(name="x", scanned=0, violations=())
    assert report.ok is True


def test_combined_report_ok_requires_every_check_clean() -> None:
    clean = CheckReport(name="a", scanned=0, violations=())
    dirty = CheckReport(name="b", scanned=1, violations=(Violation("t", "1", "d"),))

    assert CombinedReport(reports=(clean,)).ok is True
    assert CombinedReport(reports=(clean, dirty)).ok is False


def test_combined_report_violations_flattens_every_check() -> None:
    v1 = Violation("t", "1", "d1")
    v2 = Violation("t", "2", "d2")
    report = CombinedReport(
        reports=(
            CheckReport(name="a", scanned=1, violations=(v1,)),
            CheckReport(name="b", scanned=1, violations=(v2,)),
        )
    )
    assert report.violations == (v1, v2)


def test_combined_report_summary_joins_each_checks_summary() -> None:
    report = CombinedReport(
        reports=(
            CheckReport(name="a", scanned=2, violations=()),
            CheckReport(name="b", scanned=3, violations=()),
        )
    )
    summary = report.summary()
    assert "a:" in summary
    assert "b:" in summary


def test_repair_result_summary_reports_dangling_rows() -> None:
    result = RepairResult(scanned=4, repaired=1, records=(), dangling=2)
    summary = result.summary()
    assert "4" in summary
    assert "1" in summary
    assert "2" in summary
    assert "dangling" in summary


# --- is_current uniqueness check: a malformed version-key row is skipped ---


def _bootstrapped_client() -> ZeroDBClient:
    from app.bootstrap import Bootstrapper

    client = _client()
    Bootstrapper(client, pace=lambda: None).run()
    return client


def test_is_current_uniqueness_skips_a_row_with_no_version_key_value() -> None:
    """A verdict row written with a null `promise_id` (malformed, but not
    this check's job to validate shape) must not crash the grouping logic —
    it is skipped rather than grouped under a `None` key, which would
    otherwise falsely merge every such row into one fake "promise".
    """
    client = _bootstrapped_client()
    client.insert_row(
        "verdicts",
        {
            "id": "orphan:v1",
            "promise_id": None,
            "person_id": "person:B000944",
            "verdict": "kept",
            "confidence": 0.5,
            "methodology_version": "v1",
            "qa_status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "is_current": True,
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    report = check_current_verdict_uniqueness(client)
    assert report.ok, report.violations


# --- drift repair: dangling reference and dry_run (16.4) -------------------


def test_repair_skips_a_member_vote_whose_roll_call_does_not_exist() -> None:
    """Check 1's territory, not this repair's: there is no "correct" value
    to copy from a roll call that is not stored, so the row is counted as
    dangling and left untouched rather than guessed at.
    """
    client = _bootstrapped_client()
    client.insert_row(
        MemberVote.table_name,
        {
            "id": "us-house:119-2:999:B000944",
            "roll_call_id": "us-house:119-2:999",
            "person_id": "person:B000944",
            "position": "yea",
            "voted_at": "2026-01-01T00:00:00Z",
            "vote_type": "final_passage",
            "bill_id": None,
            "topic_ids": [],
            "is_bundled": False,
        },
    )
    result = repair_member_votes_drift(client)
    assert result.repaired == 0
    assert result.dangling == 1


def test_repair_dry_run_reports_drift_without_writing_it() -> None:
    client = _bootstrapped_client()
    Repository(client=client, model=RollCall).upsert(
        {
            "id": "us-house:119-2:452",
            "voted_at": "2026-01-01T00:00:00Z",
            "question": "On Passage",
            "vote_type": "final_passage",
            "result": "Passed",
            "topic_ids": [],
            "is_bundled": False,
        }
    )
    Repository(client=client, model=Person).upsert(
        {"id": "person:B000944", "canonical_name": "Test Person"}
    )
    Repository(client=client, model=MemberVote).upsert(
        {
            "roll_call_id": "us-house:119-2:452",
            "person_id": "person:B000944",
            "position": "yea",
            "voted_at": "2026-01-01T00:00:00Z",
            "vote_type": "cloture",  # already drifted from the roll call's final_passage
        }
    )

    result = repair_member_votes_drift(client, dry_run=True)

    assert result.repaired == 1
    assert result.records[0].fields == ("vote_type",)
    # Nothing was actually written: a second, real run still finds the drift.
    stored = client.get_row(MemberVote.table_name, "us-house:119-2:452:B000944")
    assert stored.row_data["vote_type"] == "cloture"
