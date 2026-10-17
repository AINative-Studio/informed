"""Nightly data-integrity checks and drift repair (stories 16.2, 16.4).

datamodel.md's "Integrity checks" section names five checks to run nightly
against production. This package is those five, plus the repair mechanism for
the one check (member_votes drift) whose acceptance criterion asks for more
than detection.

See `app.integrity.checks` for the five check functions and
`app.integrity.repair` for the drift repair. `app.scheduling.jobs` registers
both as nightly scheduled jobs.
"""

from app.integrity.checks import (
    CheckReport,
    Violation,
    check_current_verdict_uniqueness,
    check_member_votes_drift,
    check_provenance_coverage,
    check_references,
    check_verdict_roll_call_mapping,
    run_all_checks,
)
from app.integrity.repair import DriftRecord, RepairResult, repair_member_votes_drift

__all__ = [
    "CheckReport",
    "DriftRecord",
    "RepairResult",
    "Violation",
    "check_current_verdict_uniqueness",
    "check_member_votes_drift",
    "check_provenance_coverage",
    "check_references",
    "check_verdict_roll_call_mapping",
    "repair_member_votes_drift",
    "run_all_checks",
]
