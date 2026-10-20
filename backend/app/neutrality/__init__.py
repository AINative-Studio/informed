"""The nightly symmetry audit: neutrality checked operationally (story 12.1).

prd.md, "Neutrality": "One scoring codebase for all candidates; no
party-specific rules. A nightly symmetry audit runs the same synthetic
profiles against all candidates and flags skew."

Module map:
    audit.py — the mirrored fixture, the auditor, the report and the gate

See `audit.py`'s module docstring for the design: why this exists when
`tests/test_alignment_formula_unit.py` already proves the formula is
antisymmetric, exactly what "mirrored" means (two independent sign flips, both
exercised), why the audit also fails on badges, reasons and dealbreaker
warnings rather than scores alone, and what it honestly does not cover.
"""

from app.neutrality.audit import (
    MARKER,
    MAX_AUDIT_AGE,
    SYMMETRY_TOLERANCE,
    MirroredPair,
    MirroredPairResult,
    SymmetryAuditor,
    SymmetryAuditReport,
    SymmetryGateReport,
    SyntheticInterest,
    SyntheticVote,
    Violation,
    audit_runs_table_row_id,
    check_symmetry_gate,
    default_pairs,
    mirrored_direction_code,
    mirrored_position,
    run_symmetry_audit,
)

__all__ = [
    "MARKER",
    "MAX_AUDIT_AGE",
    "SYMMETRY_TOLERANCE",
    "MirroredPair",
    "MirroredPairResult",
    "SymmetryAuditReport",
    "SymmetryAuditor",
    "SymmetryGateReport",
    "SyntheticInterest",
    "SyntheticVote",
    "Violation",
    "audit_runs_table_row_id",
    "check_symmetry_gate",
    "default_pairs",
    "mirrored_direction_code",
    "mirrored_position",
    "run_symmetry_audit",
]
