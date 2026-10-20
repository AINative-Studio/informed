"""Step definitions for tests/features/symmetry_audit.feature (story 12.1).

Runs against story 1.2's in-memory fake transport, never live ZeroDB — the
same rule every backend feature in this suite follows. The live confirmation
(does the audit, driven over the real API against the real published
methodology, still report a zero delta) lives in
`scripts/verify_symmetry_audit_live.py`.

`break_server_side_filter()` is applied for the reason story 1.4 found:
live ZeroDB's server-side `/query` filter matches nothing, so every read in
the audit's own path has to run on the client's paging fallback here too —
otherwise the audit would look sound in CI for a reason that has nothing to
do with the real API, which for a NEUTRALITY check would be the worst
possible kind of false confidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.methodology.publish import MethodologyPublisher
from app.neutrality.audit import (
    SYMMETRY_TOLERANCE,
    SymmetryAuditReport,
    SymmetryAuditor,
    audit_runs_table_row_id,
    check_symmetry_gate,
)
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.service import VoterProfileService
from app.scheduling.jobs import default_jobs
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/symmetry_audit.feature")

TEST_ROOT_KEY = bytes(range(32))


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(client, pace=lambda: None).run()
    return client


@pytest.fixture
def profiles(zerodb: ZeroDBClient) -> VoterProfileService:
    return VoterProfileService(
        client=zerodb,
        keyring=ProfileKeyring(client=zerodb, root_key=ProfileRootKey(TEST_ROOT_KEY)),
    )


@pytest.fixture
def state() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {"skew": 0.0}


# --- background -------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="audit_client")
def audit_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("a published methodology version")
def published_methodology(zerodb: ZeroDBClient) -> None:
    MethodologyPublisher(zerodb).publish_new_version("2027.1")


# --- sabotage ---------------------------------------------------------------


@given("the scorer has been sabotaged to favour positive positions")
def sabotage_large(state: dict[str, Any]) -> None:
    """A deliberate, visible skew injected into the SCORE, after the real
    pipeline produced it.

    This is how the audit's failing path is proven without editing the real
    scorer: `SymmetryAuditor` takes a `skew` hook whose only production value
    is 0.0, and a test can tilt it. The alternative — monkeypatching
    `app.scoring.formula` — would prove the audit catches a broken formula
    but not that it catches a broken *pipeline*, because the skew would be
    applied before anything downstream of the formula ran.
    """
    state["skew"] = 8.0


@given(parsers.parse("the scorer has been sabotaged by {points:f} points"))
def sabotage_by(state: dict[str, Any], points: float) -> None:
    state["skew"] = points


@given("no audit_runs row exists")
def no_audit_run(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    rows = zerodb.query_rows("audit_runs", limit=500).rows
    for row in rows:
        zerodb.delete_row("audit_runs", str(row.row_data["id"]))


@given(parsers.parse("the only audit_runs row is {days:d} days old and passed"))
def stale_audit_run(zerodb: ZeroDBClient, days: int) -> None:
    for row in zerodb.query_rows("audit_runs", limit=500).rows:
        zerodb.delete_row("audit_runs", str(row.row_data["id"]))
    stale = datetime.now(UTC) - timedelta(days=days)
    row_id = audit_runs_table_row_id(stale)
    zerodb.upsert_row(
        "audit_runs",
        row_id,
        {
            "id": row_id,
            "run_at": stale.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "methodology_version": "2027.1",
            "max_delta": 0.0,
            "passed": True,
            "report_file_id": None,
        },
    )


# --- running the audit ------------------------------------------------------


def _run(zerodb: ZeroDBClient, profiles: VoterProfileService, state: dict[str, Any]) -> None:
    auditor = SymmetryAuditor(client=zerodb, profiles=profiles, skew=state["skew"])
    report = auditor.run()
    state.setdefault("reports", []).append(report)
    state["report"] = report


@when("I run the nightly symmetry audit")
def run_audit(
    zerodb: ZeroDBClient, profiles: VoterProfileService, state: dict[str, Any]
) -> None:
    _run(zerodb, profiles, state)


@when("I run the nightly symmetry audit again")
def run_audit_again(
    zerodb: ZeroDBClient, profiles: VoterProfileService, state: dict[str, Any]
) -> None:
    _run(zerodb, profiles, state)


@when("I run the symmetry release gate")
def run_gate(zerodb: ZeroDBClient, profiles: VoterProfileService, state: dict[str, Any]) -> None:
    state["gate"] = check_symmetry_gate(zerodb, profiles=profiles, skew=state["skew"])


@when("I run the symmetry release gate against the recorded result only")
def run_gate_recorded(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    state["gate"] = check_symmetry_gate(zerodb, profiles=None)


# --- the audit's own result -------------------------------------------------


def _report(state: dict[str, Any]) -> SymmetryAuditReport:
    report = state.get("report")
    assert isinstance(report, SymmetryAuditReport), "no audit has been run in this scenario"
    return report


@then("the symmetry audit passes")
def audit_passes(state: dict[str, Any]) -> None:
    report = _report(state)
    assert report.passed, f"the audit failed: {report.summary()}"


@then("the symmetry audit fails")
def audit_fails(state: dict[str, Any]) -> None:
    report = _report(state)
    assert not report.passed, f"the audit passed when it should have failed: {report.summary()}"


@then("the audit's maximum delta is at most 0.5")
def delta_within_tolerance(state: dict[str, Any]) -> None:
    report = _report(state)
    assert report.max_delta <= SYMMETRY_TOLERANCE, report.summary()


@then("the audit's maximum delta is greater than 0.5")
def delta_outside_tolerance(state: dict[str, Any]) -> None:
    report = _report(state)
    assert report.max_delta > SYMMETRY_TOLERANCE, report.summary()


@then("the audit scored at least one mirrored pair")
def scored_a_pair(state: dict[str, Any]) -> None:
    report = _report(state)
    assert report.pairs, "the audit reported no mirrored pairs at all"
    for pair in report.pairs:
        assert pair.left_score is not None, f"{pair.name}: the left candidate got no score"
        assert pair.right_score is not None, f"{pair.name}: the right candidate got no score"


@then("every mirrored candidate pair's two scores sum to 100")
def scores_sum_to_100(state: dict[str, Any]) -> None:
    report = _report(state)
    for pair in report.pairs:
        assert pair.left_score is not None and pair.right_score is not None
        total = pair.left_score + pair.right_score
        assert abs(total - 100.0) <= SYMMETRY_TOLERANCE, (
            f"{pair.name}: {pair.left_score} + {pair.right_score} = {total}, not 100"
        )


@then("the two mirrored candidates' scores were computed by the same code path")
def one_code_path(state: dict[str, Any]) -> None:
    """Structural, not behavioural: the auditor must score both sides through
    the one `AlignmentService` it was handed, with no per-candidate branch.
    Asserted on the report rather than on the code so a regression that
    introduced a second path would have to lie in the report to pass.
    """
    report = _report(state)
    for pair in report.pairs:
        assert pair.methodology_version == report.methodology_version, (
            f"{pair.name}: scored under a different methodology than the run reports"
        )
    assert len({pair.scorer for pair in report.pairs}) == 1, (
        "the pairs were not all scored by the same scorer"
    )


@then("every mirrored voter and candidate pair scores identically")
def mirrored_voter_scores_identically(state: dict[str, Any]) -> None:
    report = _report(state)
    assert report.pairs, "no pairs"
    for pair in report.pairs:
        assert pair.mirrored_voter_score is not None, (
            f"{pair.name}: the mirrored voter got no score"
        )
        assert pair.left_score is not None
        delta = abs(pair.left_score - pair.mirrored_voter_score)
        assert delta <= SYMMETRY_TOLERANCE, (
            f"{pair.name}: voter scored its aligned candidate {pair.left_score} but the "
            f"mirrored voter scored its aligned candidate {pair.mirrored_voter_score} "
            f"(delta {delta})"
        )


@then("both candidates in every mirrored pair carry the same confidence badge")
def same_badge(state: dict[str, Any]) -> None:
    report = _report(state)
    for pair in report.pairs:
        assert pair.left_badge == pair.right_badge, (
            f"{pair.name}: badges differ — {pair.left_badge!r} vs {pair.right_badge!r}. A "
            "mirrored record rests on exactly as much evidence, so a different badge means "
            "one direction of the axis is presented as better evidenced than the other."
        )


@then("both candidates in every mirrored pair rest on the same amount of evidence")
def same_evidence(state: dict[str, Any]) -> None:
    report = _report(state)
    for pair in report.pairs:
        assert pair.left_evidence_count == pair.right_evidence_count, (
            f"{pair.name}: evidence counts differ — {pair.left_evidence_count} vs "
            f"{pair.right_evidence_count}"
        )


@then("every mirrored pair names the same interests as its top reasons")
def same_reason_stances(state: dict[str, Any]) -> None:
    report = _report(state)
    for pair in report.pairs:
        assert pair.left_reason_stance_ids == pair.right_reason_stance_ids, (
            f"{pair.name}: reasons name different interests — "
            f"{pair.left_reason_stance_ids} vs {pair.right_reason_stance_ids}"
        )


@then("each reason that raised one candidate's score lowered the mirror's")
def reasons_are_mirrored(state: dict[str, Any]) -> None:
    report = _report(state)
    for pair in report.pairs:
        for stance_id, left_direction in pair.left_reason_directions.items():
            right_direction = pair.right_reason_directions.get(stance_id)
            assert right_direction is not None, (
                f"{pair.name}: {stance_id} is a reason for the left candidate but not the right"
            )
            assert left_direction != right_direction, (
                f"{pair.name}: {stance_id} {left_direction!r} for both candidates; a mirrored "
                "record must move the score the other way"
            )


@then("exactly one candidate in each mirrored dealbreaker pair carries a warning")
def one_sided_dealbreaker(state: dict[str, Any]) -> None:
    report = _report(state)
    dealbreaker_pairs = [pair for pair in report.pairs if pair.has_dealbreaker]
    assert dealbreaker_pairs, "no pair in the fixture carries a dealbreaker interest"
    for pair in dealbreaker_pairs:
        warned = (pair.left_warning_count > 0, pair.right_warning_count > 0)
        assert warned in {(True, False), (False, True)}, (
            f"{pair.name}: dealbreaker warnings fired on {warned} — a candidate who acted "
            "against a stance and their mirror who acted for it cannot both warn or both stay "
            "silent"
        )


# --- audit_runs -------------------------------------------------------------


def _audit_rows(zerodb: ZeroDBClient) -> list[dict[str, Any]]:
    return [row.row_data for row in zerodb.query_rows("audit_runs", limit=500).rows]


@then("an audit_runs row is written for today")
def audit_row_written(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    report = _report(state)
    expected_id = audit_runs_table_row_id(report.run_at)
    rows = {str(row["id"]) for row in _audit_rows(zerodb)}
    assert expected_id in rows, f"no audit_runs row {expected_id!r}; found {sorted(rows)}"


@then("the audit_runs row records the methodology version that scored it")
def audit_row_methodology(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    report = _report(state)
    row = _one_row(zerodb, report)
    assert row["methodology_version"] == report.methodology_version
    assert row["methodology_version"], "the row names no methodology version"


@then("the audit_runs row records the maximum delta and that it passed")
def audit_row_delta(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    report = _report(state)
    row = _one_row(zerodb, report)
    assert isinstance(row["max_delta"], float), (
        f"max_delta round-tripped as {type(row['max_delta']).__name__}, not a float"
    )
    assert row["max_delta"] == pytest.approx(report.max_delta)
    assert row["passed"] is True


@then("the failing audit_runs row records that it did not pass")
def audit_row_failed(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    report = _report(state)
    row = _one_row(zerodb, report)
    assert row["passed"] is False, "a failing audit wrote passed=True"
    assert row["max_delta"] > SYMMETRY_TOLERANCE


@then("the audit_runs row contains no voter data")
def audit_row_has_no_voter_data(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    """datamodel.md's privacy rule, applied to the one table this story writes.

    The synthetic profiles are not real voters, so nothing here could leak a
    real person's politics — but the row shape is what a future story would
    reuse, and a field that CAN hold a stance id will eventually hold one.
    datamodel.md documents exactly five fields for this table; the row must
    carry those and an id, and nothing else.
    """
    report = _report(state)
    row = _one_row(zerodb, report)
    assert set(row) == {
        "id",
        "run_at",
        "methodology_version",
        "max_delta",
        "passed",
        "report_file_id",
    }, f"audit_runs row carries undocumented fields: {sorted(set(row))}"
    serialized = str(row)
    for forbidden in ("stance_id", "tier", "intensity", "interests", "vp:", "payload"):
        assert forbidden not in serialized, f"{forbidden!r} appears in the audit_runs row"


@then("exactly one audit_runs row exists for today")
def one_row_today(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    report = _report(state)
    expected_id = audit_runs_table_row_id(report.run_at)
    matching = [row for row in _audit_rows(zerodb) if str(row["id"]) == expected_id]
    assert len(matching) == 1, f"{len(matching)} rows for today, expected 1"
    assert len(state["reports"]) == 2, "this scenario is meant to run the audit twice"


def _one_row(zerodb: ZeroDBClient, report: SymmetryAuditReport) -> dict[str, Any]:
    expected_id = audit_runs_table_row_id(report.run_at)
    for row in _audit_rows(zerodb):
        if str(row["id"]) == expected_id:
            return row
    raise AssertionError(f"no audit_runs row {expected_id!r}")


# --- the release gate -------------------------------------------------------


@then("the release gate blocks the release")
def gate_blocks(state: dict[str, Any]) -> None:
    gate = state["gate"]
    assert not gate.ok, f"the gate allowed the release: {gate.summary()}"


@then("the release gate allows the release")
def gate_allows(state: dict[str, Any]) -> None:
    gate = state["gate"]
    assert gate.ok, f"the gate blocked the release: {gate.summary()}"


@then("the release gate explains which mirrored pair was asymmetric")
def gate_explains(state: dict[str, Any]) -> None:
    gate = state["gate"]
    assert gate.violations, "the gate blocked with no violation to read"
    detail = " ".join(str(violation) for violation in gate.violations)
    assert "audit_runs" in detail or "symmetry" in detail.lower()
    report = state.get("report")
    names = [pair.name for pair in (report.pairs if report else [])]
    if names:
        assert any(name in detail for name in names), (
            f"the gate named no asymmetric pair; said: {detail}"
        )


# --- the nightly job --------------------------------------------------------


@then("the job registry contains a nightly symmetry audit job")
def registry_has_the_job() -> None:
    jobs = {job.name: job for job in default_jobs()}
    assert "run_nightly_symmetry_audit" in jobs, (
        f"no nightly symmetry audit job registered; found {sorted(jobs)}"
    )
    job = jobs["run_nightly_symmetry_audit"]
    assert job.interval == timedelta(days=1), (
        f"the symmetry audit is registered at {job.interval}, not nightly"
    )


@then("that job fails loudly when the audit finds skew")
def job_raises_on_skew(zerodb: ZeroDBClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The job must RAISE, not log, so `ScheduledJobRunner` records it FAILED.

    Same contract `run_nightly_integrity_checks` and `run_panel_gate_check`
    already follow: the check itself never raises on its own findings, and the
    job function is where a finding becomes a job failure.
    """
    from app.scheduling import jobs as jobs_module

    def skewed(client: ZeroDBClient) -> SymmetryAuditReport:
        return SymmetryAuditReport(
            run_at=datetime.now(UTC),
            methodology_version="2027.1",
            pairs=(),
            max_delta=9.0,
            tolerance=SYMMETRY_TOLERANCE,
        )

    monkeypatch.setattr(jobs_module, "run_symmetry_audit", skewed)
    with pytest.raises(ZeroDBError, match="symmetry"):
        jobs_module.run_nightly_symmetry_audit(zerodb)
