"""The five nightly integrity checks datamodel.md names (story 16.2).

datamodel.md, "Integrity checks" (written test-first, run in CI and nightly
against production):

  1. Every `member_votes.roll_call_id` resolves to a `roll_calls` row, and
     every `candidacies.person_id` resolves to a `people` row.
  2. Every `kept`/`partial`/`broken` verdict has at least one approved mapping
     to a recorded roll call.
  3. Exactly one `is_current = true` row exists per (promise) in `verdicts`
     and per (person, stance) in `candidate_positions`.
  4. The denormalized fields on `member_votes` match their `roll_calls` row;
     any drift triggers a re-copy.
  5. Every NoSQL row with a `source_url` has a matching `provenance_log`
     entry, so no evidence goes untraced.

===============================================================================
WHAT "CORRECTLY BUILT BUT CURRENTLY VACUOUS" MEANS FOR CHECKS 2 AND 3
===============================================================================

`verdicts` and `candidate_positions` hold zero real rows today — Epic 6/11
haven't built real scoring/verdict computation yet (see `Verdict`'s and
`CandidatePosition`'s own docstrings in `app.repositories.models`). A check
that reads an empty table and finds no violation is not a check that "passes
by not running" — it is the correct answer to "are there any violations in
this table", which for an empty table is trivially none. This is the ordinary
meaning of "vacuously true" in logic: a universally-quantified claim over an
empty set holds.

What makes that claim credible rather than an untested assumption is the
sabotage tests in `tests/step_defs/test_integrity_checks_steps.py`: synthetic
rows are written that simulate exactly the violation each check claims to
catch (two verdicts for one promise both marked current, a verdict approved
against no recorded roll call, and so on), and the test asserts the check
actually flags them. The check logic is proven sound against synthetic data
today; it will run for real the day Epic 6/11 write real rows, with no code
change required.

===============================================================================
WHY NONE OF THIS PAGES ON-CALL OR OPENS A TICKET ITSELF
===============================================================================

Story 16.2's acceptance criterion is "failures page on-call and open a
ticket". No paging system (PagerDuty, Opsgenie, ...) and no ticket tracker
integration (the GitHub Issues API, Jira, ...) exists anywhere in this
codebase — see `app.scheduling.jobs`'s module docstring for the identical gap
already flagged for `load_candidacies`/`load_house_rollcalls` (no credential,
no account, no vendor chosen). Faking a page or a ticket here — logging a line
that says "PAGED on-call" and calling it done — would be worse than the honest
gap: it would make dashboards green while nobody is actually notified.

So this module does the half that is real and testable: each check returns a
structured `CheckReport` that is unambiguous, greppable and ready to hand to
*any* paging or ticketing integration once one is chosen — a nonempty
`violations` list is exactly the trigger condition such an integration would
gate on, and each `Violation` carries enough (table, row id, detail) to open
an actionable ticket from. `app.scheduling.jobs.run_nightly_integrity_checks`
raises when any check has violations, which is what makes a failed nightly run
visible as a `FAILED` job in `ScheduledJobRunner`'s report today, and is the
hook a real paging/ticketing step attaches to tomorrow. See that module's
docstring for the follow-up this names explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.repositories.models import (
    Candidacy,
    CandidatePosition,
    MemberVote,
    Person,
    RollCall,
    Verdict,
)
from app.repositories.provenance import PROVENANCE_TABLE, check_app_tree
from app.zerodb.client import ZeroDBClient

#: `promises` is a real table in datamodel.md ("Promises, verdicts and
#: positions": id `prm:{uuid}`; fields include `mappings[]` of
#: `(bill_id, roll_call_id, effect, confidence, qa_status)`), referenced
#: already by `Verdict.references["promise_id"]`, but it has no Pydantic
#: model of its own yet — no story has ingested or written a real `promises`
#: row (Epic 6's extractor, which produces them, has not been built). Spelled
#: as a bare string here rather than imported from `app.repositories.models`
#: for that reason: adding a `Promise` model is a future story's job once it
#: has a real writer to validate, not this check's to invent.
PROMISES_TABLE = "promises"

#: `verdict` values datamodel.md's check 2 requires an approved mapping for.
#: `no_opportunity`, `absent` and `not_testable` are deliberately excluded:
#: each names a reason no vote could be checked against, so requiring a
#: roll-call mapping for them would demand evidence that cannot exist by
#: definition of the verdict itself.
VERDICT_VALUES_REQUIRING_MAPPING = frozenset({"kept", "partial", "broken"})

#: One page of a full-table scan. Mirrors `app.districts.store`'s
#: `_all_districts` convention: large enough that real tables read in one
#: request today, a loop kept anyway so growth degrades to more requests
#: rather than a silent truncation.
SCAN_PAGE_SIZE = 500

#: Hard ceiling on rows scanned per table in one check run, so a check against
#: a table that has outgrown a full scan fails loudly (logged, reported as a
#: capped scan) rather than silently reading a subset and calling it complete.
MAX_ROWS_SCANNED = 100_000

#: The denormalized fields datamodel.md documents as copied onto member_votes
#: from its referenced roll_calls row (the italicised fields in that table's
#: field list). Shared by the detection check (16.2) and the repair (16.4) so
#: the two cannot drift from each other about which fields are in scope.
DENORMALIZED_MEMBER_VOTE_FIELDS: tuple[str, ...] = (
    "voted_at",
    "vote_type",
    "bill_id",
    "topic_ids",
    "is_bundled",
)


@dataclass(frozen=True)
class Violation:
    """One integrity violation found by one check.

    Carries enough to act on without a second lookup: which table, which row,
    and a human-readable detail — the same shape `ProvenanceViolation` and
    every `RepositoryError` already use in this codebase, so a ticket opened
    from this is as actionable as one opened from a repository exception.
    """

    table: str
    row_id: str
    detail: str

    def __str__(self) -> str:
        return f"{self.table}/{self.row_id}: {self.detail}"


@dataclass(frozen=True)
class CheckReport:
    """What one check found.

    `scanned` is reported even when it is zero, so "this check found nothing
    because the table is empty" is distinguishable in a log line from "this
    check silently didn't run" — the same distinction
    `SweepResult.already_clear`/`open_corrections` draw for the retention
    sweep.
    """

    name: str
    scanned: int
    violations: tuple[Violation, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        return f"{self.name}: scanned {self.scanned} row(s), {len(self.violations)} violation(s)"


def paged_rows(client: ZeroDBClient, table: str) -> list[dict[str, Any]]:
    """Every row in `table`, paged to the end or to `MAX_ROWS_SCANNED`.

    The one full-table-scan primitive every check below shares, matching
    `app.districts.store._all_districts`'s convention: a page is still a page,
    the loop exists so growth degrades gracefully, and hitting the cap is
    logged rather than silently truncating what a check claims to have
    scanned completely.
    """
    rows: list[dict[str, Any]] = []
    skip = 0
    while skip < MAX_ROWS_SCANNED:
        page = client.query_rows(table, limit=SCAN_PAGE_SIZE, skip=skip)
        if not page.rows:
            break
        rows.extend(row.row_data for row in page.rows)
        skip += len(page.rows)
        if not page.has_more and len(page.rows) < SCAN_PAGE_SIZE:
            break
    return rows


# ===========================================================================
# Check 1: dangling references
# ===========================================================================


def check_references(client: ZeroDBClient) -> CheckReport:
    """datamodel.md check 1: member_votes -> roll_calls, candidacies -> people.

    A plain existence check over every live row, independent of
    `Repository._check_references` (which only guards the write *path* — this
    check is the backstop for data written before a reference rule existed, a
    torn write, or a direct `ZeroDBClient` write that bypassed the repository
    entirely, exactly the class of gap `app.repositories.provenance`'s module
    docstring describes for provenance).
    """
    violations: list[Violation] = []
    scanned = 0

    member_votes = paged_rows(client, MemberVote.table_name)
    scanned += len(member_votes)
    for row in member_votes:
        roll_call_id = row.get("roll_call_id")
        if roll_call_id and client.get_row(RollCall.table_name, str(roll_call_id)) is None:
            violations.append(
                Violation(
                    table=MemberVote.table_name,
                    row_id=str(row.get("id")),
                    detail=(
                        f"roll_call_id {roll_call_id!r} does not resolve to a "
                        f"{RollCall.table_name} row"
                    ),
                )
            )

    candidacies = paged_rows(client, Candidacy.table_name)
    scanned += len(candidacies)
    for row in candidacies:
        person_id = row.get("person_id")
        if person_id and client.get_row(Person.table_name, str(person_id)) is None:
            violations.append(
                Violation(
                    table=Candidacy.table_name,
                    row_id=str(row.get("id")),
                    detail=(
                        f"person_id {person_id!r} does not resolve to a {Person.table_name} row"
                    ),
                )
            )

    return CheckReport(name="references", scanned=scanned, violations=tuple(violations))


# ===========================================================================
# Check 2: verdict -> approved roll-call mapping
# ===========================================================================


def check_verdict_roll_call_mapping(client: ZeroDBClient) -> CheckReport:
    """datamodel.md check 2: every testable verdict has an approved mapping.

    "An approved mapping to a recorded roll call" is datamodel.md's own
    vocabulary: `promises` carries `mappings[]`, an embedded list of
    `(bill_id, roll_call_id, effect, confidence, qa_status)` (datamodel.md,
    "Promises, verdicts and positions" — the mapping agent's output, reviewed
    per datamodel.md's `mapping.reviewed` event). A verdict is for one
    promise (`verdicts.promise_id`); this check reads that promise's row and
    requires at least one entry in its `mappings[]` with
    `qa_status == "approved"` AND a `roll_call_id` that resolves to a stored
    `roll_calls` row — approved-but-dangling is still a violation, because an
    approved mapping to a roll call that was since deleted is not evidence of
    anything.

    `promises` has no Pydantic model yet (see `PROMISES_TABLE`'s comment) so
    this reads the row as a plain dict, the same posture
    `check_references` and the other checks here take toward every table —
    none of these checks require a model, deliberately, since their whole
    point is to catch violations a model's own write-time validation could
    not have (history already in storage, a torn write, a direct bypass).

    A verdict whose `promise_id` does not resolve to any `promises` row is
    also a violation here (not just check 1's territory, since check 1 does
    not scan `promises`): a verdict for a promise that does not exist cannot
    possibly have an approved mapping, and silently skipping it would hide
    exactly the dangling reference this check exists to find for verdicts.

    Verdicts whose value is not in `VERDICT_VALUES_REQUIRING_MAPPING` need no
    mapping at all — `no_opportunity`, `absent` and `not_testable` each say,
    by definition, that no vote exists to cite.
    """
    violations: list[Violation] = []
    scanned = 0

    verdicts = paged_rows(client, Verdict.table_name)
    scanned += len(verdicts)

    for row in verdicts:
        value = row.get("verdict")
        if value not in VERDICT_VALUES_REQUIRING_MAPPING:
            continue
        promise_id = row.get("promise_id")
        promise = client.get_row(PROMISES_TABLE, str(promise_id)) if promise_id else None
        if promise is None:
            violations.append(
                Violation(
                    table=Verdict.table_name,
                    row_id=str(row.get("id")),
                    detail=(
                        f"verdict {value!r} references promise_id {promise_id!r}, which "
                        f"does not resolve to a {PROMISES_TABLE} row"
                    ),
                )
            )
            continue

        mappings = promise.row_data.get("mappings") or []
        has_approved_mapping = any(
            isinstance(mapping, dict)
            and mapping.get("qa_status") == "approved"
            and mapping.get("roll_call_id")
            and client.get_row(RollCall.table_name, str(mapping["roll_call_id"])) is not None
            for mapping in mappings
        )
        if not has_approved_mapping:
            violations.append(
                Violation(
                    table=Verdict.table_name,
                    row_id=str(row.get("id")),
                    detail=(
                        f"verdict {value!r} for promise {promise_id!r} has no approved "
                        "mapping (promises.mappings[].qa_status == 'approved') to a "
                        "recorded roll call"
                    ),
                )
            )

    return CheckReport(
        name="verdict_roll_call_mapping", scanned=scanned, violations=tuple(violations)
    )


# ===========================================================================
# Check 3: exactly one is_current row per key
# ===========================================================================


def _current_uniqueness_violations(
    rows: list[dict[str, Any]], *, table: str, key_fields: tuple[str, ...]
) -> list[Violation]:
    """Shared logic for both append-only tables check 3 covers.

    Groups every stored row (current or not) by its version key and counts
    how many in each group carry `is_current is True`. Exactly one is
    correct; zero or more than one is a violation — both are real failure
    modes `Repository.append_version`'s own docstring names as possible under
    a torn write or a direct bypass of the repository layer.
    """
    groups: dict[tuple[Any, ...], int] = {}
    for row in rows:
        key = tuple(row.get(field_name) for field_name in key_fields)
        if any(value is None for value in key):
            continue
        if row.get("is_current") is True:
            groups[key] = groups.get(key, 0) + 1
        else:
            groups.setdefault(key, groups.get(key, 0))

    violations: list[Violation] = []
    for key, current_count in groups.items():
        if current_count == 1:
            continue
        key_label = "/".join(repr(value) for value in key)
        violations.append(
            Violation(
                table=table,
                row_id=key_label,
                detail=(
                    f"{current_count} row(s) marked is_current for key "
                    f"{dict(zip(key_fields, key, strict=True))}, expected exactly 1"
                ),
            )
        )
    return violations


def check_current_verdict_uniqueness(client: ZeroDBClient) -> CheckReport:
    """datamodel.md check 3: exactly one `is_current` row per key.

    Covers both append-only tables the check names: `verdicts` (keyed by
    `promise_id`) and `candidate_positions` (keyed by `person_id, stance_id`).
    One function covering both, named for the check as a whole rather than
    either table alone — `app.scheduling.jobs` and the feature file both treat
    this as a single check, matching how datamodel.md states it as a single
    bullet.
    """
    violations: list[Violation] = []

    verdicts = paged_rows(client, Verdict.table_name)
    violations.extend(
        _current_uniqueness_violations(
            verdicts, table=Verdict.table_name, key_fields=("promise_id",)
        )
    )

    positions = paged_rows(client, CandidatePosition.table_name)
    violations.extend(
        _current_uniqueness_violations(
            positions,
            table=CandidatePosition.table_name,
            key_fields=("person_id", "stance_id"),
        )
    )

    return CheckReport(
        name="is_current_uniqueness",
        scanned=len(verdicts) + len(positions),
        violations=tuple(violations),
    )


# ===========================================================================
# Check 4: member_votes denormalized-field drift (detection only — see
# app.integrity.repair for the actual re-copy, story 16.4)
# ===========================================================================


def drifted_fields(
    vote_row: dict[str, Any], roll_call_row: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    """Which of `DENORMALIZED_MEMBER_VOTE_FIELDS` disagree, and the two values.

    Shared by the check (16.2, read-only) and the repair (16.4, which also
    needs to know the correct value to re-copy) so the two cannot define
    "drift" differently.
    """
    drifted: dict[str, tuple[Any, Any]] = {}
    for field_name in DENORMALIZED_MEMBER_VOTE_FIELDS:
        vote_value = vote_row.get(field_name)
        roll_call_value = roll_call_row.get(field_name)
        if vote_value != roll_call_value:
            drifted[field_name] = (vote_value, roll_call_value)
    return drifted


def check_member_votes_drift(client: ZeroDBClient) -> CheckReport:
    """datamodel.md check 4, detection half: find rows whose denormalized
    fields disagree with their referenced `roll_calls` row.

    Read-only. `app.integrity.repair.repair_member_votes_drift` is the same
    detection plus the actual re-copy story 16.4 asks for; this function
    exists separately so the nightly *check* (which should report, not write)
    and the nightly *repair* (which should fix) are two distinct, separately
    testable and separately schedulable operations — matching how
    datamodel.md states them as two different acceptance criteria (16.2's
    "failures page on-call", 16.4's "finds mismatches and re-copies them").
    """
    violations: list[Violation] = []

    member_votes = paged_rows(client, MemberVote.table_name)
    roll_calls_by_id: dict[str, dict[str, Any]] = {}

    for vote_row in member_votes:
        roll_call_id = vote_row.get("roll_call_id")
        if not roll_call_id:
            continue
        roll_call_id = str(roll_call_id)
        if roll_call_id not in roll_calls_by_id:
            roll_call = client.get_row(RollCall.table_name, roll_call_id)
            if roll_call is None:
                # Check 1's job, not this one's: a dangling reference is
                # reported there, and there is nothing to compare drift
                # against here.
                continue
            roll_calls_by_id[roll_call_id] = roll_call.row_data

        drifted = drifted_fields(vote_row, roll_calls_by_id[roll_call_id])
        for field_name, (vote_value, roll_call_value) in drifted.items():
            violations.append(
                Violation(
                    table=MemberVote.table_name,
                    row_id=str(vote_row.get("id")),
                    detail=(
                        f"field {field_name!r} is {vote_value!r}, but roll call "
                        f"{roll_call_id!r} has {roll_call_value!r}"
                    ),
                )
            )

    return CheckReport(
        name="member_votes_drift", scanned=len(member_votes), violations=tuple(violations)
    )


# ===========================================================================
# Check 5: provenance coverage
# ===========================================================================


def check_provenance_coverage(client: ZeroDBClient) -> CheckReport:
    """datamodel.md check 5: every sourced row has a provenance_log entry.

    Two layers, matching the two enforcement designs story 16.3 already built
    and documented as complementary (`app.repositories.provenance`'s module
    docstring):

      * a **live data audit** — every row in every table is read, and any row
        carrying a `source_url` with no matching `provenance_log` entry is a
        violation. This is the thing datamodel.md's check actually asks for:
        a property of the DATA, checkable at any time regardless of how it
        got written (ingest, a hand fix, a future adapter).
      * the **static code check** (`check_app_tree`, promoted verbatim from
        16.3) — a sourced write with no `ProvenanceContext`, or a direct
        `ZeroDBClient` write into a sourced table, bypassing the repository
        layer that would have written the provenance entry. Running it here
        too means a code-level regression shows up in the same nightly run as
        a data-level one, not only in CI.

    Both run; violations from either are reported together.
    """
    violations: list[Violation] = []
    scanned = 0

    provenance_rows = paged_rows(client, PROVENANCE_TABLE)
    provenance_by_entity: dict[tuple[str, str], int] = {}
    for row in provenance_rows:
        key = (str(row.get("entity_table")), str(row.get("entity_id")))
        provenance_by_entity[key] = provenance_by_entity.get(key, 0) + 1

    for table in sorted(table.table_name for table in client.list_tables(limit=200)):
        if table == PROVENANCE_TABLE:
            continue
        rows = paged_rows(client, table)
        scanned += len(rows)
        for row in rows:
            source_url = row.get("source_url")
            if not source_url:
                continue
            row_id = str(row.get("id"))
            if (table, row_id) not in provenance_by_entity:
                violations.append(
                    Violation(
                        table=table,
                        row_id=row_id,
                        detail=(
                            f"carries source_url {source_url!r} with no {PROVENANCE_TABLE} entry"
                        ),
                    )
                )

    for code_violation in check_app_tree():
        violations.append(
            Violation(
                table=code_violation.table,
                row_id=f"{code_violation.module}:{code_violation.line}",
                detail=code_violation.detail,
            )
        )

    return CheckReport(name="provenance_coverage", scanned=scanned, violations=tuple(violations))


# ===========================================================================
# Running them all together
# ===========================================================================

#: Every check, in datamodel.md's documented order. One list, so the nightly
#: job, the combined-report test and a future paging integration all iterate
#: the same five functions rather than each keeping its own copy.
ALL_CHECKS = (
    check_references,
    check_verdict_roll_call_mapping,
    check_current_verdict_uniqueness,
    check_member_votes_drift,
    check_provenance_coverage,
)


@dataclass(frozen=True)
class CombinedReport:
    """Every check's report from one nightly run."""

    reports: tuple[CheckReport, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(report.ok for report in self.reports)

    @property
    def violations(self) -> tuple[Violation, ...]:
        return tuple(v for report in self.reports for v in report.violations)

    def summary(self) -> str:
        return "; ".join(report.summary() for report in self.reports)


def run_all_checks(client: ZeroDBClient) -> CombinedReport:
    """Run every integrity check once; never raises on a check's own findings.

    Mirrors `ScheduledJobRunner`'s own principle: one check's violations must
    not prevent the others from running and reporting. A check that itself
    raises (a genuine `ZeroDBError` reading the table) is NOT caught here —
    that is a real infrastructure failure, distinct from "the check ran and
    found a violation", and the caller (the scheduled job) should see it as
    the job failing rather than as a reported violation.
    """
    return CombinedReport(reports=tuple(check(client) for check in ALL_CHECKS))


__all__ = [
    "ALL_CHECKS",
    "DENORMALIZED_MEMBER_VOTE_FIELDS",
    "PROMISES_TABLE",
    "VERDICT_VALUES_REQUIRING_MAPPING",
    "CheckReport",
    "CombinedReport",
    "Violation",
    "check_current_verdict_uniqueness",
    "check_member_votes_drift",
    "check_provenance_coverage",
    "check_references",
    "check_verdict_roll_call_mapping",
    "drifted_fields",
    "paged_rows",
    "run_all_checks",
]
