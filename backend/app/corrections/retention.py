"""Erasing a reporter's contact email 90 days after resolution (story 12.4).

datamodel.md, "Privacy, retention and integrity":

    | `corrections.contact_email` | 90 days after resolution | Optional field |

That is a promise made to every reporter who gives an address, and backlog.md
12.4 states it as an acceptance criterion ("contact email is ... deleted 90
days after resolution"). This module is the mechanism that keeps it: a
callable, idempotent sweep that finds corrections closed more than 90 days ago
and nulls `contact_email` on each, leaving everything else about the row
exactly as it was.

===============================================================================
WHY A CALLABLE FUNCTION AND NOT A SCHEDULED JOB
===============================================================================

**There is no scheduler, worker, cron or queue in this repository.** Every
periodic thing to date is a hand-run script — `scripts/load_legislators.py`,
`scripts/load_candidacies.py`, `scripts/archive_campaign_sites.py` — and
`app.ingest.candidacies` documents the same gap for its own "refreshed weekly"
criterion in the same terms. Standing up a scheduler is a deployment concern
spanning every periodic story in the backlog (ingest refreshes, the nightly
symmetry audit of prd.md, the nightly integrity checks of datamodel.md, and
this sweep), and building a bespoke one here would be a fifth way to run a
background task.

So this story does the half that is actually hard — making the sweep correct,
idempotent and tested — and names the other half as the infrastructure it is:

  * **Correct and safe to re-run.** The sweep is a pure function of the stored
    rows and the clock. Running it twice erases nothing the first run left, and
    running it never leaves the rows intact; there is no cursor, no watermark
    and no state outside ZeroDB that a missed run can desynchronise. So a
    scheduler that fires it daily, hourly or erratically all produce the same
    end state, which is the property that makes it safe to schedule *later*.
  * **Actually invoking it on a schedule is infrastructure that does not
    exist.** Flagged as a follow-up in this story's PR, exactly as story 2.6
    flagged weekly candidacy refreshes: one scheduled runner for every periodic
    task in this codebase. `scripts/sweep_correction_emails.py` is ready for
    it with no code change — a cron entry, a Railway cron service or a GitHub
    Actions `schedule:` trigger can call it as-is.

**The honest consequence, stated plainly:** until that runner exists, the
90-day erasure happens when someone runs the script. An email that should have
been erased on day 90 sits there until the next run. That is a real gap
between the documented promise and the deployed behaviour, it is why the
follow-up is in the PR rather than in a backlog someone may not read, and the
mitigation available today is that the script is one command and reports
exactly what it did.

===============================================================================
WHAT "90 DAYS AFTER RESOLUTION" MEANS HERE
===============================================================================

From `resolved_at`, not from `created_at` and not from the last status change.
`CorrectionService.set_status` is what maintains that field, and it maintains
it for both terminal statuses:

  * `resolved` and `rejected` both set it, because a reporter whose report was
    closed without a change has the same retention promise as one whose report
    was acted on;
  * reopening a correction CLEARS it, so a report that is live again is not
    swept. This is the case worth being careful about: a sweep keyed on "status
    was once resolved" would erase the contact email of a reporter whose
    report is currently open, which is both a worse service and a silent one.

A correction that has never been closed is never swept, however old it is.
That is deliberate and is the one place this module is more conservative than
a naive reading of the retention table: the clock in datamodel.md starts "after
resolution", so an unresolved report has no clock running. Scenario "An
unresolved correction's contact email is never swept" pins it. The ops
consequence — a report left open for a year keeps its reporter's email — is
addressed by triaging it, which is what the 72-hour SLA is for, not by
erasing the address of someone still waiting for an answer.

===============================================================================
WHY THE ROW IS PATCHED RATHER THAN REWRITTEN
===============================================================================

The sweep writes through `ZeroDBClient.update_row`, which read-merge-writes.
`upsert_row` would replace the row wholesale, and story 1.2 measured live that
a `PUT` of a partial body destroys the fields it omits — which here would mean
erasing an email and taking the status, the resolution time and the report
itself with it. The scenario "Erasing the email leaves the correction's status
and history intact" is what keeps that honest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.corrections.models import CLOSED_STATUSES, TABLE_NAME, CorrectionRow
from app.corrections.service import CorrectionService
from app.zerodb.client import ZeroDBClient

#: datamodel.md's retention window for `corrections.contact_email`: "90 days
#: after resolution". One definition, used by the sweep and pinned by a test
#: against the documented promise.
RETENTION_DAYS = 90

#: Row ids and counts only — never an email, never a body. The whole point of
#: this module is that an address stops existing, and a log line quoting the
#: address it just erased would defeat it.
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepResult:
    """What one retention sweep did.

    Returned rather than only logged so a caller — the script, a test, a future
    scheduled runner — can assert on it and so an ops run has a number to
    report. `erased_ids` carries row ids, which are not sensitive; the emails
    erased are deliberately not recorded anywhere.
    """

    scanned: int = 0
    erased: int = 0
    #: Corrections whose email was erased by THIS run. Empty on a second run
    #: over the same data, which is what idempotence looks like here.
    erased_ids: list[str] = field(default_factory=list)
    #: Closed, past the window, and had no email to begin with (or had it
    #: erased by an earlier run). Reported separately so a run of all zeros can
    #: be told apart from a run that found nothing to look at.
    already_clear: int = 0
    #: Closed but still inside the 90-day window — kept on purpose.
    within_window: int = 0
    #: Never closed, so no retention clock is running.
    open_corrections: int = 0

    def summary(self) -> str:
        """One line for an operator, safe to paste into a ticket."""
        return (
            f"scanned {self.scanned} correction(s): erased {self.erased} contact "
            f"email(s); {self.within_window} still within the {RETENTION_DAYS}-day "
            f"window, {self.open_corrections} not yet closed, "
            f"{self.already_clear} already clear"
        )


def sweep_expired_contact_emails(
    client: ZeroDBClient,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> SweepResult:
    """Erase `contact_email` on every correction closed over 90 days ago.

    Idempotent: a second run over the same data erases nothing and reports
    `erased == 0`. See the module docstring for why that property is what makes
    this safe to schedule later.

    Args:
        client: the ZeroDB client to read and write through.
        now: the moment to measure the window from. Injectable because the
            behaviour under test is entirely about elapsed time; see
            `CorrectionService`'s docstring for why backdating through the real
            write path beats patching rows after the fact.
        dry_run: report what would be erased and write nothing. For a first
            run against a live project, where "how many addresses is this about
            to delete" is a question worth answering before deleting them.

    Returns:
        A `SweepResult`. `erased_ids` holds row ids only.
    """
    moment = now or datetime.now(UTC)
    cutoff = _iso(moment - timedelta(days=RETENTION_DAYS))
    service = CorrectionService(client)

    result_scanned = 0
    erased_ids: list[str] = []
    already_clear = 0
    within_window = 0
    open_corrections = 0

    for row in service.all_corrections():
        result_scanned += 1
        if row.status not in CLOSED_STATUSES or not row.resolved_at:
            # Never closed, so no clock is running. A correction reopened after
            # resolution has had `resolved_at` cleared by `set_status` and
            # lands here too, which is the point.
            open_corrections += 1
            continue
        if row.resolved_at >= cutoff:
            within_window += 1
            continue
        if row.contact_email is None:
            # Closed, past the window, nothing to erase: either the reporter
            # gave no address or an earlier run already handled it. This is the
            # branch that makes a second run a no-op.
            already_clear += 1
            continue
        if not dry_run:
            _erase_contact_email(client, row)
        erased_ids.append(str(row.id))

    result = SweepResult(
        scanned=result_scanned,
        erased=len(erased_ids),
        erased_ids=erased_ids,
        already_clear=already_clear,
        within_window=within_window,
        open_corrections=open_corrections,
    )
    logger.info(
        "correction email retention sweep (%s): %s",
        "dry run" if dry_run else "live",
        result.summary(),
    )
    return result


def _erase_contact_email(client: ZeroDBClient, row: CorrectionRow) -> None:
    """Null one row's `contact_email`, leaving every other field alone.

    `update_row` read-merge-writes; see the module docstring for why a
    wholesale `upsert_row` here would erase the report along with the address.

    The erasure is a write of `None` rather than a dropped key:
    `RowModel.row_data` keeps `None`s for exactly this reason (ZeroDB's PUT
    replaces the row, so an omitted key would vanish rather than clear), and a
    row where the column is *absent* is indistinguishable from one written
    before the field existed.
    """
    client.update_row(TABLE_NAME, str(row.id), {"contact_email": None})
    # Id only. Never the address that was just erased.
    logger.info("erased the contact email on correction %s (retention)", row.id)


def _iso(moment: datetime) -> str:
    """The codebase's one timestamp spelling; see `app.corrections.service`."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["RETENTION_DAYS", "SweepResult", "sweep_expired_contact_emails"]
