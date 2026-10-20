"""The mirrored fixture, and what makes it a real test of neutrality (12.1).

Why this file exists alongside `test_alignment_formula_unit.py`
===============================================================

That file already proves the FORMULA is antisymmetric: mirror every `s_i` and
a score of `x` becomes exactly `100 - x`. It does so with four hand-written
numbers per term and no database, which is the right way to test arithmetic
and is deliberately all it tests.

What it cannot see is everything between a vote and a number:

  * `app.scoring.positions.compute_position` turning votes into `s_value`,
  * `_stance_direction` orienting a vote against a stance's own axis,
  * `app.scoring.service._terms_for` reading tier weights and intensities,
  * `app.scoring.badges`, `app.scoring.dealbreakers`, `app.scoring.reasons`.

Each is code a voter's number passes through, and a skew in any of them is
invisible to a formula test. This file's job is to pin the properties of the
*mirrored fixture itself* — that it really is a mirror — and the audit's own
comparison arithmetic. The end-to-end run over the real pipeline is
`tests/step_defs/test_symmetry_audit_steps.py`'s.

Everything here is synthetic. No real member, no real topic, no real vote.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.bootstrap import Bootstrapper
from app.neutrality.audit import (
    SYMMETRY_TOLERANCE,
    MirroredPairResult,
    SymmetryAuditReport,
    audit_runs_table_row_id,
    check_symmetry_gate,
    mirrored_direction_code,
    mirrored_position,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

# --- the mirror operation itself -------------------------------------------


@pytest.mark.parametrize(
    ("original", "mirror"),
    [("+1", "-1"), ("-1", "+1"), ("+2", "-2"), ("-2", "+2")],
)
def test_mirroring_a_direction_code_flips_its_sign_and_keeps_its_magnitude(
    original: str, mirror: str
) -> None:
    """A mirrored stance is the opposite position on the SAME axis at the SAME
    strength. Flipping the magnitude too would make the pair unequal in a
    second way, and the audit could no longer tell which difference caused a
    delta.
    """
    assert mirrored_direction_code(original) == mirror
    assert mirrored_direction_code(mirror) == original


def test_mirroring_a_neutral_direction_code_is_refused() -> None:
    """`direction_code` `0` means "this position does not move along the axis",
    and `app.scoring.positions._stance_direction` returns 0.0 for it, so
    `compute_position` declines to compute at all. A `0` stance has no mirror —
    its reflection is itself — so putting one in the fixture would add a pair
    that can never produce a score, and the audit would quietly compare two
    `None`s and call it symmetric. Refused loudly instead.
    """
    with pytest.raises(ValueError, match="no mirror"):
        mirrored_direction_code("0")


@pytest.mark.parametrize(("position", "mirror"), [("yea", "nay"), ("nay", "yea")])
def test_mirroring_a_vote_flips_yea_and_nay(position: str, mirror: str) -> None:
    assert mirrored_position(position) == mirror
    assert mirrored_position(mirror) == position


@pytest.mark.parametrize("position", ["present", "not_voting"])
def test_mirroring_a_recorded_non_position_leaves_it_alone(position: str) -> None:
    """`present` and `not_voting` contribute nothing to a position
    (`app.scoring.positions._vote_direction` returns 0.0), so their mirror is
    themselves. Flipping them to each other would change nothing arithmetically
    but would make the fixture claim a mirror relationship it does not have.
    """
    assert mirrored_position(position) == position


# --- the audit's comparison arithmetic -------------------------------------


def _pair(left: float | None, right: float | None, **overrides: object) -> MirroredPairResult:
    defaults: dict[str, object] = {
        "name": "pair-under-test",
        "left_candidacy_id": "left",
        "right_candidacy_id": "right",
        "methodology_version": "2027.1",
        "scorer": "AlignmentService",
        "left_score": left,
        "right_score": right,
        "mirrored_voter_score": left,
        "left_badge": "high",
        "right_badge": "high",
        "left_evidence_count": 3,
        "right_evidence_count": 3,
        "left_reason_stance_ids": ("s",),
        "right_reason_stance_ids": ("s",),
        "left_reason_directions": {"s": "raised"},
        "right_reason_directions": {"s": "lowered"},
        "left_warning_count": 0,
        "right_warning_count": 0,
        "has_dealbreaker": False,
    }
    defaults.update(overrides)
    return MirroredPairResult(**defaults)  # type: ignore[arg-type]


def test_a_perfectly_mirrored_pair_has_a_zero_delta() -> None:
    """70 and 30 sum to 100: the exact relationship
    `test_the_score_is_antisymmetric_under_flipping_every_position` pins, now
    measured as a delta the audit can threshold.
    """
    assert _pair(70.0, 30.0).complement_delta == pytest.approx(0.0)


def test_the_delta_is_how_far_the_two_scores_are_from_summing_to_100() -> None:
    assert _pair(70.0, 31.0).complement_delta == pytest.approx(1.0)
    assert _pair(70.0, 29.0).complement_delta == pytest.approx(1.0)
    assert _pair(50.0, 50.0).complement_delta == pytest.approx(0.0)


def test_an_unscoreable_pair_reports_an_infinite_delta_not_a_zero_one() -> None:
    """A pair where either side scored `None` is NOT symmetric — it is
    unmeasured, which is the one answer an audit must never read as a pass.
    Two `None`s would compare equal and silently report a perfect run over a
    fixture that produced nothing: the exact false-confidence failure this
    whole story exists to prevent.
    """
    assert _pair(None, None).complement_delta == float("inf")
    assert _pair(70.0, None).complement_delta == float("inf")
    assert _pair(None, 30.0).complement_delta == float("inf")


def test_the_mirrored_voter_delta_is_how_far_the_two_scores_are_from_identical() -> None:
    """prd.md's own scenario: "each voter's score for their aligned candidate is
    identical within 0.5 points". Both flips applied (candidate record AND
    voter stance direction) double-negate `s_value`, so the two scores must be
    EQUAL — not complementary.
    """
    assert _pair(70.0, 30.0, mirrored_voter_score=70.0).mirrored_voter_delta == pytest.approx(0.0)
    assert _pair(70.0, 30.0, mirrored_voter_score=69.0).mirrored_voter_delta == pytest.approx(1.0)
    assert _pair(70.0, 30.0, mirrored_voter_score=None).mirrored_voter_delta == float("inf")


def test_a_pairs_delta_is_the_worst_of_its_two_relationships() -> None:
    """One number per pair, so the report's `max_delta` is a single threshold
    rather than two that could disagree about whether the run passed.
    """
    assert _pair(70.0, 30.0, mirrored_voter_score=68.0).delta == pytest.approx(2.0)
    assert _pair(70.0, 33.0, mirrored_voter_score=70.0).delta == pytest.approx(3.0)


def test_a_badge_mismatch_makes_a_pair_asymmetric_even_at_a_zero_score_delta() -> None:
    """The acceptance criterion names the SCORES, and a reader could stop
    there. But a mirrored record rests on exactly as much evidence, so a
    different badge would tell a voter one direction of the axis is better
    evidenced than the other — a partisan claim with a symmetric number
    underneath it. The audit therefore fails on it, and says so rather than
    folding it into the delta (which is a points measure and has no room for
    a categorical difference).
    """
    pair = _pair(70.0, 30.0, right_badge="low")
    assert pair.delta == pytest.approx(0.0)
    assert not pair.symmetric
    assert "badge" in " ".join(pair.asymmetries).lower()


def test_an_evidence_count_mismatch_makes_a_pair_asymmetric() -> None:
    pair = _pair(70.0, 30.0, right_evidence_count=2)
    assert not pair.symmetric
    assert "evidence" in " ".join(pair.asymmetries).lower()


def test_differing_reason_interests_make_a_pair_asymmetric() -> None:
    pair = _pair(70.0, 30.0, right_reason_stance_ids=("other",))
    assert not pair.symmetric
    assert "reason" in " ".join(pair.asymmetries).lower()


def test_a_reason_that_moved_both_candidates_the_same_way_is_asymmetric() -> None:
    """If an interest raised BOTH a candidate's and their mirror's score, one
    of the two explanations a voter reads is wrong — and `direction` is derived
    from the sign of the contribution, so it is also evidence the arithmetic
    diverged somewhere the score happened to absorb.
    """
    pair = _pair(70.0, 30.0, right_reason_directions={"s": "raised"})
    assert not pair.symmetric
    assert "direction" in " ".join(pair.asymmetries).lower()


def test_dealbreaker_warnings_on_both_sides_are_asymmetric() -> None:
    pair = _pair(70.0, 30.0, has_dealbreaker=True, left_warning_count=1, right_warning_count=1)
    assert not pair.symmetric
    assert "dealbreaker" in " ".join(pair.asymmetries).lower()


def test_dealbreaker_warnings_on_neither_side_are_asymmetric() -> None:
    """One of the pair acted against the stance; a silent pair means the check
    did not fire at all, which is as wrong as firing on both.
    """
    pair = _pair(70.0, 30.0, has_dealbreaker=True, left_warning_count=0, right_warning_count=0)
    assert not pair.symmetric
    assert "dealbreaker" in " ".join(pair.asymmetries).lower()


def test_exactly_one_dealbreaker_warning_is_symmetric() -> None:
    assert _pair(
        70.0, 30.0, has_dealbreaker=True, left_warning_count=0, right_warning_count=1
    ).symmetric
    assert _pair(
        70.0, 30.0, has_dealbreaker=True, left_warning_count=1, right_warning_count=0
    ).symmetric


# --- the report -------------------------------------------------------------


def _report(*pairs: MirroredPairResult) -> SymmetryAuditReport:
    return SymmetryAuditReport(
        run_at=datetime(2026, 10, 20, 3, 0, tzinfo=UTC),
        methodology_version="2027.1",
        pairs=pairs,
        max_delta=max((pair.delta for pair in pairs), default=float("inf")),
        tolerance=SYMMETRY_TOLERANCE,
    )


def test_the_tolerance_is_the_acceptance_criterions_own_number() -> None:
    """backlog.md 12.1: "Fails if mirrored scores differ by more than 0.5"."""
    assert SYMMETRY_TOLERANCE == 0.5


def test_a_report_over_perfectly_mirrored_pairs_passes() -> None:
    report = _report(_pair(70.0, 30.0), _pair(12.5, 87.5))
    assert report.passed
    assert report.max_delta == pytest.approx(0.0)


def test_a_delta_exactly_at_the_tolerance_passes() -> None:
    """ "more than 0.5" fails, so 0.5 itself does not. Pinned because the
    inequality's direction is the whole acceptance criterion and an off-by-one
    here would either hide a real skew or block every release.
    """
    assert _report(_pair(70.0, 30.5)).passed


def test_a_delta_just_over_the_tolerance_fails() -> None:
    assert not _report(_pair(70.0, 30.6)).passed


def test_a_report_with_no_pairs_at_all_fails() -> None:
    """An audit that scored nothing has proven nothing. Reporting a pass on an
    empty run is how a neutrality check becomes decoration — the same reason
    `scripts/check_panel_gate.py` self-checks its own failing path before
    trusting a clean result.
    """
    report = _report()
    assert not report.passed
    assert "no mirrored pair" in report.summary().lower()


def test_a_report_fails_on_a_categorical_asymmetry_at_a_zero_delta() -> None:
    report = _report(_pair(70.0, 30.0, right_badge="low"))
    assert report.max_delta == pytest.approx(0.0)
    assert not report.passed


def test_the_report_row_carries_exactly_datamodel_md_s_documented_fields() -> None:
    row = _report(_pair(70.0, 30.0)).row()
    assert set(row) == {
        "id",
        "run_at",
        "methodology_version",
        "max_delta",
        "passed",
        "report_file_id",
    }
    assert row["id"] == "aud:2026-10-20"
    assert row["passed"] is True
    assert isinstance(row["max_delta"], float)


def test_the_row_id_is_one_per_day_as_datamodel_md_documents() -> None:
    """datamodel.md: `audit_runs` id pattern `aud:{date}`. One row per day, so
    a second run on the same day restates it rather than appending — which is
    what makes "Latest" (the table's documented query) answerable by sorting
    ids.
    """
    morning = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
    evening = datetime(2026, 10, 20, 23, 59, tzinfo=UTC)
    assert audit_runs_table_row_id(morning) == audit_runs_table_row_id(evening) == "aud:2026-10-20"
    assert audit_runs_table_row_id(datetime(2026, 10, 21, 0, 1, tzinfo=UTC)) == "aud:2026-10-21"


def test_the_row_id_is_utc_regardless_of_the_callers_timezone() -> None:
    """A naive datetime is read as UTC, and an offset one is converted, so a
    runner in a different timezone cannot write two rows for one UTC day.
    """
    from datetime import timedelta as _timedelta
    from datetime import timezone as _timezone

    assert audit_runs_table_row_id(datetime(2026, 10, 20, 12, 0)) == "aud:2026-10-20"
    tokyo = _timezone(_timedelta(hours=9))
    assert audit_runs_table_row_id(datetime(2026, 10, 21, 5, 0, tzinfo=tokyo)) == "aud:2026-10-20"


def test_the_summary_names_the_delta_the_tolerance_and_the_verdict() -> None:
    summary = _report(_pair(70.0, 31.0)).summary()
    assert "1.0" in summary
    assert "0.5" in summary
    assert "FAIL" in summary or "fail" in summary


def test_an_unscoreable_run_still_writes_a_row_rather_than_losing_the_failure() -> None:
    """An `inf` delta is the worst possible result and must still be recorded.

    JSON has no infinity, so a naive `float(inf)` would fail the write and
    lose the record of exactly the run that went wrongest — leaving the
    release gate to see "no recent audit" (which it blocks on, correctly) but
    with no trace of WHY. A sentinel three orders of magnitude above the
    0-100 score range is recorded instead: it cannot be misread as a
    measurement, and `passed: False` carries the verdict regardless.
    """
    row = _report(_pair(None, None)).row()
    assert row["passed"] is False
    assert row["max_delta"] == 999.0
    # The real test: it serialises.
    import json

    assert json.loads(json.dumps(row))["max_delta"] == 999.0


def test_the_asymmetries_of_an_unscoreable_pair_say_so_in_words() -> None:
    details = " ".join(_pair(None, None).asymmetries)
    assert "could not be scored" in details
    assert "unmeasured, not symmetric" in details


def test_a_pair_whose_mirrored_voter_alone_is_unscoreable_is_reported() -> None:
    """Distinguished from the both-sides case: the two candidates scored
    fine, so the fault is in the mirrored PROFILE's path (flip 2) and not in
    the candidates' records (flip 1). Saying which halves a human's search.
    """
    details = " ".join(_pair(70.0, 30.0, mirrored_voter_score=None).asymmetries)
    assert "mirrored voter could not be scored" in details
    assert "could not be scored at all (left=" not in details


# --- the release gate's own edges ------------------------------------------
#
# The passing and blocking paths are in symmetry_audit.feature. These are the
# edges a feature file would read badly for: a malformed stored timestamp, and
# a stored failure the gate must relay when it cannot re-run the audit itself.


def _gate_client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-gate-edges", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(client, pace=lambda: None).run()
    return client


def _store(client: ZeroDBClient, **overrides: object) -> None:
    row: dict[str, object] = {
        "id": "aud:2026-10-20",
        "run_at": "2026-10-20T03:00:00Z",
        "methodology_version": "2027.1",
        "max_delta": 0.0,
        "passed": True,
        "report_file_id": None,
    }
    row.update(overrides)
    client.upsert_row("audit_runs", str(row["id"]), row)


def test_the_gate_blocks_on_a_stored_run_at_it_cannot_read() -> None:
    """A row whose `run_at` is not a timestamp cannot be aged, so the gate
    cannot tell whether it is current. It blocks and says which field it could
    not read, rather than treating an unreadable row as fresh — which would
    make a corrupted write indistinguishable from a healthy nightly run.
    """
    client = _gate_client()
    _store(client, run_at="not-a-timestamp")
    report = check_symmetry_gate(client, profiles=None, now=lambda: _NOW)
    assert not report.ok
    assert "could not be read as a timestamp" in " ".join(str(v) for v in report.violations)


def test_the_gate_blocks_on_a_stored_run_at_that_is_missing_entirely() -> None:
    client = _gate_client()
    _store(client, run_at="")
    report = check_symmetry_gate(client, profiles=None, now=lambda: _NOW)
    assert not report.ok


def test_the_gate_relays_a_stored_failure_when_it_cannot_rerun_the_audit() -> None:
    """In recorded-only mode the stored verdict is all there is, so the gate
    must block on it. (When it runs a fresh audit, that audit's own violations
    already say what is wrong in detail, and repeating the stored verdict
    would double every line — which is why this branch is conditional.)
    """
    client = _gate_client()
    _store(client, passed=False, max_delta=7.5)
    report = check_symmetry_gate(client, profiles=None, now=lambda: _NOW)
    assert not report.ok
    assert "did not pass" in " ".join(str(v) for v in report.violations)


def test_the_gate_allows_on_a_fresh_passing_stored_row() -> None:
    client = _gate_client()
    _store(client)
    report = check_symmetry_gate(client, profiles=None, now=lambda: _NOW)
    assert report.ok, report.summary()
    assert report.report is None, "recorded-only mode must not run an audit"
    assert "the recorded audit_runs row" in report.summary()


def test_the_gate_reads_the_newest_row_not_whichever_one_came_back_first() -> None:
    """datamodel.md's documented query for this table is "Latest", and the id
    is an ISO date — so lexicographic order IS chronological order. Sorted
    locally rather than trusting the store's order, for story 1.4's reason:
    live ZeroDB returns rows in whatever order it likes, and a gate that read
    an old passing row over a new failing one would ship the skew.
    """
    client = _gate_client()
    _store(client, id="aud:2026-10-18", passed=True, run_at="2026-10-18T03:00:00Z")
    _store(client, id="aud:2026-10-20", passed=False, max_delta=4.0)
    report = check_symmetry_gate(client, profiles=None, now=lambda: _NOW)
    assert not report.ok
    assert "aud:2026-10-20" in " ".join(str(v) for v in report.violations)


_NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
