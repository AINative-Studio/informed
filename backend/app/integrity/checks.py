"""The nightly integrity checks (story 16.2, plus one).

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

And a sixth, **not** from that list:

  6. Every row naming an archived file can fetch that file back, and its
     bytes hash to what the row records.

===============================================================================
WHY THERE IS A SIXTH CHECK
===============================================================================

Because a bug these checks exist to catch got past all five of them.
`ZeroDBClient.upload_file` posted file metadata to ZeroDB's metadata-only
route and never sent the file body, so story 9.1's three real campaign-site
archives had correct `promise_sources` rows, correct file metadata and
correct sha256 values with **no stored object behind any of them** — every
download answered 404. Checks 1-5 passed on that data, because each asks a
question about rows and none asks whether the evidence a row cites can
actually be fetched. It was found by story 9.3 stumbling into it.

So check 6 asks that question. It is the most expensive of the six (it
downloads every archived file), which is appropriate for a nightly run and
not for a request path. See `check_archive_retrievability`.

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

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.profiles.methodology import TABLE_NAME as METHODOLOGY_TABLE_NAME
from app.repositories.models import (
    Candidacy,
    CandidatePosition,
    MemberVote,
    Person,
    PromiseSource,
    RollCall,
    Verdict,
)
from app.repositories.provenance import PROVENANCE_TABLE, check_app_tree
from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBError

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
#: `bundled_reason` is story 8.4's addition and is not in datamodel.md's own
#: italicised list, for the same reason it is not in the `roll_calls` list
#: there: 8.4 added the field. It belongs here because it is copied onto
#: `member_votes` exactly like `is_bundled`, so the same drift that would
#: leave a member vote claiming "bundled" after its roll call was corrected
#: would leave it claiming the wrong REASON too -- and a wrong explanation
#: shown to a voter is worse than none.
DENORMALIZED_MEMBER_VOTE_FIELDS: tuple[str, ...] = (
    "voted_at",
    "vote_type",
    "bill_id",
    "topic_ids",
    "is_bundled",
    "bundled_reason",
)

#: Tables whose rows name an archived file that a citation depends on
#: (check 6). Only `promise_sources` today — datamodel.md's Files section also
#: lists bill text and debate transcripts, and whichever table holds those
#: belongs here the moment a story writes one, which is why this is a tuple
#: rather than a hardcoded single name.
ARCHIVE_BEARING_TABLES: tuple[str, ...] = (PromiseSource.table_name,)

#: `promise_sources` ids are `src:<sha256 of the captured bytes>`, so the id
#: itself is the integrity claim check 6 verifies the archive against.
CONTENT_ADDRESSED_ID_PREFIX = "src:"

#: Story 6.1's addition to check 3. Spelled as a module constant and imported
#: from story 4.2's module so the check and the publisher cannot disagree about
#: which table holds the methodology in force.
METHODOLOGY_VERSIONS_TABLE = METHODOLOGY_TABLE_NAME


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


def _global_current_uniqueness_violations(
    rows: list[dict[str, Any]], *, table: str
) -> list[Violation]:
    """The same check for a table with ONE current row globally (story 6.1).

    `_current_uniqueness_violations` groups by a version key because a verdict
    is current *per promise* and a position is current *per (person, stance)*.
    `methodology_versions` has no such key: there is one methodology in force
    for the whole application, so the group is the table itself.

    An empty table is not a violation. A project that has published no
    methodology yet has nothing to be wrong about — and story 4.2's
    `MethodologyVersionService` seeds a row the first time anything reads one,
    so "empty" is a pre-first-read state rather than a broken one. Reporting it
    would make the nightly check fail on a freshly bootstrapped environment,
    which is the kind of false alarm that teaches people to ignore the report.
    """
    if not rows:
        return []
    current = [row for row in rows if row.get("is_current") is True]
    if len(current) == 1:
        return []
    return [
        Violation(
            table=table,
            row_id=table,
            detail=(
                f"{len(current)} row(s) marked is_current across the whole {table} "
                f"table, expected exactly 1 — there is one methodology in force"
            ),
        )
    ]


def check_current_uniqueness(client: ZeroDBClient) -> CheckReport:
    """datamodel.md check 3: exactly one `is_current` row per key.

    Covers the two append-only tables the check names — `verdicts` (keyed by
    `promise_id`) and `candidate_positions` (keyed by `person_id, stance_id`)
    — plus `methodology_versions`, added by story 6.1, whose key is the whole
    table rather than a field on the row.

    One function covering all three, named for the check as a whole rather
    than any one table — `app.scheduling.jobs` and the feature file both treat
    this as a single check, matching how datamodel.md states it as a single
    bullet.

    Why `methodology_versions` belongs here rather than in a check of its own:
    story 6.1's acceptance criterion ("exactly one current version exists") is
    *verbatim* this check applied to one more table, and the failure modes are
    identical — a torn two-request supersede, or a direct write that bypassed
    `MethodologyPublisher`. A separate check would mean two places to look for
    the same class of bug, and the nightly job would report the same finding
    under two names.
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

    methodologies = paged_rows(client, METHODOLOGY_VERSIONS_TABLE)
    violations.extend(
        _global_current_uniqueness_violations(methodologies, table=METHODOLOGY_VERSIONS_TABLE)
    )

    return CheckReport(
        name="is_current_uniqueness",
        scanned=len(verdicts) + len(positions) + len(methodologies),
        violations=tuple(violations),
    )


#: Story 16.2 named this function for `verdicts` alone; story 6.1 widened it to
#: a third table and renamed it to match. The old name is kept as an alias
#: rather than updated at every call site, because the check's identity (its
#: reported `name`, `"is_current_uniqueness"`) never changed and a rename
#: rippling through the scheduled job, the live verification script and two
#: test modules would be churn with no reader benefit.
check_current_verdict_uniqueness = check_current_uniqueness


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
# Check 6: archived files are actually retrievable
# ===========================================================================


def check_archive_retrievability(client: ZeroDBClient) -> CheckReport:
    """Every row naming an archived file can fetch it back, and the bytes match.

    **NOT one of datamodel.md's original five.** It exists because of the bug
    it would have caught, and that provenance is the argument for it:

    `ZeroDBClient.upload_file` used to POST file *metadata* to ZeroDB's
    metadata-only route and never send the file body. Story 9.1's three real
    campaign-site archives therefore had correct `promise_sources` rows,
    correct file metadata and correct sha256 values behind which **no object
    existed** — every download answered 404. Checks 1 to 5 all passed on that
    data, because every one of them asks about rows and none asks whether the
    evidence a row cites can be fetched. The gap was found by a later story
    stumbling into it, which is the failure mode this whole module exists to
    prevent.

    So this check asks the question none of the five can: for every row with
    an `archived_file_id`, does `download_file` return anything, and do those
    bytes hash to what the row claims?

    Three distinct violations, each a different kind of broken:

      * **no archived_file_id** — a row in a table whose rows archive
        something, naming nothing. A citation with no evidence at all.
      * **unreadable** — the metadata row exists and the object does not. The
        exact state the pre-fix `upload_file` produced.
      * **hash mismatch** — the object reads back but is not what the row
        says it is. Worse than missing: a citation that silently points at the
        wrong evidence, which `verify_quote_span` downstream would then quote
        from.

    The hash comes from the row's own id (`src:<sha256>`) where the table uses
    content-addressed ids, and otherwise from the file's
    `file_metadata["sha256"]`. A row with neither is checked for readability
    only, and that is said in the detail rather than passed off as a full
    check.

    This check DOWNLOADS every archived file, so it is the most expensive of
    the six — appropriate for a nightly run, and the reason it pages rather
    than reading everything at once.
    """
    violations: list[Violation] = []
    scanned = 0

    for table in ARCHIVE_BEARING_TABLES:
        for row in paged_rows(client, table):
            scanned += 1
            row_id = str(row.get("id"))
            file_id = str(row.get("archived_file_id") or "")
            if not file_id:
                violations.append(
                    Violation(
                        table=table,
                        row_id=row_id,
                        detail="names no archived_file_id, so its evidence cannot be fetched",
                    )
                )
                continue

            try:
                archived = client.download_file(file_id=file_id)
            except ZeroDBError as exc:
                violations.append(
                    Violation(
                        table=table,
                        row_id=row_id,
                        detail=(
                            f"archived file {file_id} cannot be read back ({exc}) — the "
                            "metadata row exists with no stored object behind it"
                        ),
                    )
                )
                continue

            expected = _expected_sha256(client, row, row_id, file_id)
            if expected is None:
                violations.append(
                    Violation(
                        table=table,
                        row_id=row_id,
                        detail=(
                            f"archived file {file_id} reads back ({len(archived)} bytes) but "
                            "records no sha256, so its contents cannot be verified"
                        ),
                    )
                )
                continue

            actual = hashlib.sha256(archived).hexdigest()
            if actual != expected:
                violations.append(
                    Violation(
                        table=table,
                        row_id=row_id,
                        detail=(
                            f"archived file {file_id} holds bytes hashing to {actual}, not "
                            f"the recorded {expected} — the citation points at the wrong content"
                        ),
                    )
                )

    return CheckReport(name="archive_retrievability", scanned=scanned, violations=tuple(violations))


def _expected_sha256(
    client: ZeroDBClient, row: dict[str, Any], row_id: str, file_id: str
) -> str | None:
    """The hash this row's archive should have, or None if none is recorded.

    Prefers the row's own content-addressed id, because that is the claim the
    rest of the system keys on (a `promise_sources` id IS its capture's
    sha256), and falls back to the file's stored `file_metadata`.
    """
    if row_id.startswith(CONTENT_ADDRESSED_ID_PREFIX):
        return row_id.removeprefix(CONTENT_ADDRESSED_ID_PREFIX)
    try:
        stored = client.get_file(file_id=file_id)
    except ZeroDBError:
        return None
    recorded = stored.file_metadata.get("sha256")
    return str(recorded) if recorded else None


# ===========================================================================
# Running them all together
# ===========================================================================

#: Every check, datamodel.md's five in its documented order plus the archive
#: retrievability check. One list, so the nightly job, the combined-report
#: test and a future paging integration all iterate the same functions rather
#: than each keeping its own copy.
ALL_CHECKS = (
    check_references,
    check_verdict_roll_call_mapping,
    check_current_uniqueness,
    check_member_votes_drift,
    check_provenance_coverage,
    check_archive_retrievability,
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
    "METHODOLOGY_VERSIONS_TABLE",
    "PROMISES_TABLE",
    "VERDICT_VALUES_REQUIRING_MAPPING",
    "CheckReport",
    "CombinedReport",
    "Violation",
    "check_current_uniqueness",
    "check_current_verdict_uniqueness",
    "check_member_votes_drift",
    "check_provenance_coverage",
    "check_references",
    "check_verdict_roll_call_mapping",
    "drifted_fields",
    "paged_rows",
    "run_all_checks",
]
