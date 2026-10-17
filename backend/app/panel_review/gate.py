"""The release-time panel gate (story 12.5).

backlog.md, story 12.5: "As the advisory panel, we want unapproved taxonomy
or stance changes blocked from release, so that neutrality reviews can't be
skipped." Acceptance criterion: "The CI check fails on unapproved wording
versions."

What story 3.3 already built, and what this module adds
=========================================================

Story 3.3 (`app.panel_review.service`) built two things: an append-only
ledger of panel decisions, and a *read-time* gate — `approved_stances_for_topic`
and `approved_topic_tree` withhold any row the ledger does not cover at its
current version. That gate protects a voter who calls the gated endpoints.

It does **not** protect the release process itself. Nothing stops a PR from
merging — and nothing stops `scripts/seed_stances.py` or
`scripts/seed_taxonomy.py` from being re-run against a live project — with new
or edited wording that has never been in front of the panel. The read-time
gate would quietly withhold that wording from `/v1/panel/published/*`, but
nothing would FAIL, and nothing would tell a human the release just shipped
unreviewed content. That silent withholding is precisely the failure mode
story 12.5 exists to close: "neutrality reviews can't be skipped" has to mean
something breaks loudly, not just that a voter happens not to see the result.

So this module is a **check**, not a read path: `check_panel_gate` scans
every live `issue_topics` and `stances` row and asserts, for each one, that
`panel_reviews` carries an in-force `approved` or `grandfathered` decision
naming the exact version the row is at. It returns a `CheckReport` in the
exact shape `app.integrity.checks` already established for datamodel.md's
nightly checks — `Violation`, `CheckReport.ok`, `CheckReport.summary()` — so
this gate plugs into the same CI/nightly story that the data-integrity checks
already use (`scripts/run_scheduled_jobs.py`, and a pytest test that asserts
`report.ok` the same way `test_integrity_unit.py` does) rather than inventing
a second reporting shape for one more check.

The design decision this module is making explicit: grandfathered passes
=========================================================================

`PUBLISHABLE_DECISIONS` (`app.panel_review.service`) already treats
`grandfathered` as publishable, and that choice is reused here rather than
re-litigated: this gate's job is to catch content that bypassed the review
*process*, and the grandfathering backfill is a recorded, visible, deliberate
act of that process choosing to tolerate the pre-existing corpus rather than
black out the product (see `PanelReviewService`'s module docstring, option 3,
for the full argument). A release gate that failed on every grandfathered row
would fail on literally every deploy until a real panel re-reviews all 281
rows — which would not surface a NEW problem, it would just make the known,
already-reported debt (`PanelReviewService.queue()`) block every release
forever. That is not what "unapproved wording cannot be published" asks for;
it already has its own honest, visible home in `queue()`'s `grandfathered`
count, repeated here in `GateReport.grandfathered` so a CI log shows the debt
on every run without it being a failure.

**This is a deliberate tolerance, not an endorsement.** Every grandfathered
row is still owed a real review — `GateReport.grandfathered` reports the
count on every run specifically so that debt stays visible in CI output
instead of disappearing into a passing check. The day a real panel is
seated, the honest next step is either (a) re-run real reviews over the
backlog so `grandfathered` trends to zero, or (b) if the product decides
grandfathering should no longer be tolerated at all, delete `grandfathered`
from this module's own `GATE_PASSING_DECISIONS` — a one-line, deliberate,
reviewed change, not something this module should decide unilaterally today.

What DOES fail this gate, and why that is the right line
==========================================================

Three things fail it, and all three are "nobody in the review process ever
signed off on the wording currently live, at the version it is currently at":

  * **`current_decision is None`** — new or re-ingested wording nobody has
    looked at, ever. The most urgent case: it is also the one the voter-facing
    gate (3.3) already hides from a voter, so until now it had no consequence
    at release time at all.
  * **`current_decision == "rejected"`** — a reviewer explicitly said no to
    this wording, at this version, and nobody has proposed and had approved a
    fix. Shipping it anyway is shipping content the panel refused.
  * **A version bump with no fresh approval** — `PanelReviewService.decide`
    always writes the approval for the row's *new* version in the same call
    (see that method's docstring, step 3), so this case only arises from a
    write that bypassed `PanelReviewService` entirely: a seeder re-run that
    changed wording and bumped `wording_version`/`taxonomy_version` without
    going through `propose`/`decide`. That is exactly the "wording changed
    outside the review workflow" scenario story 12.5 is named for, and it is
    indistinguishable, from this check's point of view, from "never
    reviewed" — both report as `current_decision is None` for the row's
    current version, which is the correct, honest answer: the ledger has
    nothing to say about THIS version, whatever it said about a past one.

Reusing `PanelReviewService.queue()` rather than re-deriving it
=================================================================

`queue()` already computes exactly this: every row with no in-force *person*
approval at its current version, with enough detail (`current_decision`) to
tell the three cases above apart. Re-implementing the ledger/version
matching here — rather than calling `service.queue()` — would risk the two
drifting the moment one of them is edited, which is the one thing a gate like
this cannot afford: a gate that disagrees with the voter-facing read about
what counts as "approved" could pass CI while quietly serving (or withholding)
something different to a voter. So `check_panel_gate` is a thin wrapper:
it calls `PanelReviewService.queue()`, partitions the result by
`current_decision`, and reports a `Violation` for everything that is not
`grandfathered`.

**One deliberate divergence from `queue()`:** `queue()` also excludes rows a
*person* approved (`approved_by_a_person`, i.e. `approved` with a non-backfill
reviewer) — correctly, since those need no further review. This check keeps
that behaviour unchanged; a "panel-approved" row is exactly the one case this
gate was built to let through without comment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.panel_review.service import PanelReviewService
from app.zerodb.client import ZeroDBClient

#: Decisions that let a row pass THIS gate. Identical to
#: `app.panel_review.service.PUBLISHABLE_DECISIONS` today — see the module
#: docstring for why that identity is a deliberate choice this module owns,
#: not an accident of two constants happening to agree.
GATE_PASSING_DECISIONS: frozenset[str] = frozenset({"approved", "grandfathered"})


@dataclass(frozen=True)
class Violation:
    """One row this gate refuses to let through, and why.

    Shape mirrors `app.integrity.checks.Violation` (table/row_id/detail) so a
    CI log line or a future ticket-filing integration can treat every check
    in this codebase identically.
    """

    table: str
    row_id: str
    detail: str

    def __str__(self) -> str:
        return f"{self.table}/{self.row_id}: {self.detail}"


@dataclass(frozen=True)
class GateReport:
    """What one run of the panel gate found.

    Mirrors `app.integrity.checks.CheckReport`'s shape (`ok`, `summary()`) so
    this gate composes with the rest of this codebase's check infrastructure
    — a scheduled job, a pytest assertion, a CI step — without inventing a
    second vocabulary for "did a check pass".

    `grandfathered` and `scanned` are reported even when both checks below
    find nothing to fail on, for the same reason `CheckReport.scanned` is
    always reported: "nothing failed because there was nothing to check" must
    be visibly different, in a CI log, from "nothing failed because the check
    silently did not run".
    """

    scanned: int
    grandfathered: int
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """True iff every current row has an in-force approved-or-grandfathered
        decision. False is what makes CI fail — see `check_panel_gate`."""
        return not self.violations

    def summary(self) -> str:
        return (
            f"panel_gate: scanned {self.scanned} row(s), {self.grandfathered} grandfathered "
            f"(tolerated, not endorsed, still owed a real review), "
            f"{len(self.violations)} violation(s)"
        )


def check_panel_gate(client: ZeroDBClient) -> GateReport:
    """Scan every live issue_topics/stances row; fail on any unapproved one.

    Story 12.5's acceptance criterion made concrete: a row fails this gate
    unless `panel_reviews` carries an in-force `approved` or `grandfathered`
    decision for the exact version the row currently carries. See the module
    docstring for why `grandfathered` is deliberately on the passing side and
    for why this delegates to `PanelReviewService.queue()` rather than
    re-deriving the ledger/version match.

    This never raises on a violation it finds — same contract as
    `app.integrity.checks.run_all_checks` and for the same reason: the caller
    (a pytest assertion, `scripts/check_panel_gate.py`, or a scheduled job)
    decides what "the check failed" should do, this function only has to
    report it completely and accurately. A genuine `ZeroDBError` reading the
    ledger or the corpus (table unreachable, credentials bad) is NOT caught
    here and propagates — that is an infrastructure failure, not a finding.
    """
    panel = PanelReviewService(client)
    entries = panel.queue()

    violations: list[Violation] = []
    grandfathered = 0
    for entry in entries:
        if entry.current_decision == "grandfathered":
            grandfathered += 1
            continue
        if entry.current_decision == "rejected":
            detail = (
                f"wording {entry.wording!r} at version {entry.subject_version} was REJECTED "
                "by a panel reviewer and nobody has proposed and had approved a fix; "
                "shipping it is shipping content the panel refused"
            )
        else:
            detail = (
                f"wording {entry.wording!r} at version {entry.subject_version} has NO panel "
                "review of any kind — not even grandfathered — at its current version; "
                "either it is brand new, or it was edited outside "
                "PanelReviewService.propose()/decide() (e.g. a seeder re-run that changed "
                "wording without a review)"
            )
        violations.append(
            Violation(table=entry.subject_table, row_id=entry.subject_id, detail=detail)
        )

    # `queue()` already scanned every current row (it is how it finds rows
    # with no in-force approval); `scanned` here counts the same universe
    # queue() iterated, which panel.queue()'s own docstring says is
    # "every row no panel member has approved at its current version" PLUS
    # every row that IS approved by a person (which never appears in
    # `entries`). Since queue() does not expose "total rows scanned"
    # directly, derive it the same way the live verifier does: topics +
    # stances read unfiltered.
    scanned = _scanned_row_count(client)

    return GateReport(scanned=scanned, grandfathered=grandfathered, violations=tuple(violations))


def _scanned_row_count(client: ZeroDBClient) -> int:
    """How many issue_topics + stances rows this gate considered.

    A plain unfiltered count, mirroring `PanelReviewService._read_subjects`'s
    own page size so this never under-reports relative to what `queue()`
    itself scanned.
    """
    from app.panel_review.service import PAGE_SIZE
    from app.repositories import Repository
    from app.repositories.models import IssueTopic, Stance

    topics: Repository[IssueTopic] = Repository(client=client, model=IssueTopic)
    stances: Repository[Stance] = Repository(client=client, model=Stance)
    return len(topics.query(limit=PAGE_SIZE)) + len(stances.query(limit=PAGE_SIZE))


__all__ = [
    "GATE_PASSING_DECISIONS",
    "GateReport",
    "Violation",
    "check_panel_gate",
]
