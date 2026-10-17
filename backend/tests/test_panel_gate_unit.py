"""Unit tests for `app.panel_review.gate` internals not reached by the BDD
suite (story 12.5).

`tests/features/panel_gate_in_release.feature` and its step defs cover the
gate's observable pass/fail behaviour, including sabotage scenarios. This
file covers the small report/violation string helpers no scenario's
assertions happen to call — the same split `tests/test_integrity_unit.py`
draws for `app.integrity.checks`'s own `Violation`/`CheckReport` dunders.
"""

from __future__ import annotations

from app.panel_review.gate import GateReport, Violation


def test_violation_str_is_safe_to_paste_into_a_ticket() -> None:
    violation = Violation(
        table="stances", row_id="en-US:topic:v1:card", detail="has no panel review"
    )
    assert str(violation) == "stances/en-US:topic:v1:card: has no panel review"


def test_gate_report_ok_is_true_with_no_violations() -> None:
    report = GateReport(scanned=281, grandfathered=281, violations=())
    assert report.ok is True


def test_gate_report_ok_is_false_with_any_violation() -> None:
    violation = Violation(table="stances", row_id="x", detail="y")
    report = GateReport(scanned=282, grandfathered=281, violations=(violation,))
    assert report.ok is False


def test_gate_report_summary_reports_scanned_grandfathered_and_violation_counts() -> None:
    violation = Violation("stances", "x", "y")
    report = GateReport(scanned=282, grandfathered=281, violations=(violation,))
    summary = report.summary()
    assert "scanned 282" in summary
    assert "281 grandfathered" in summary
    assert "1 violation" in summary


def test_gate_report_summary_is_informative_on_a_clean_pass() -> None:
    report = GateReport(scanned=281, grandfathered=281, violations=())
    summary = report.summary()
    assert "0 violation" in summary
    assert "tolerated, not endorsed" in summary
