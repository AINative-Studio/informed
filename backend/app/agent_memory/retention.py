"""Deleting Q&A turns past their one-day window (story 13.5).

datamodel.md's retention table: `Q&A memory | 1 day | Never written in
anonymous mode`.

`app.agent_memory.service`'s read guard already means an expired turn is never
served. This module is the other half: the turn stops existing. See that
module's docstring for why both are needed and why ZeroDB's memory API cannot
do it for us (there is no TTL field; one sent is silently dropped).

Shaped exactly like `app.corrections.retention.sweep_expired_contact_emails`
(story 12.4), and for the same reasons:

  * **Idempotent, no cursor, no watermark.** A pure function of the stored rows
    and the clock. Running it twice deletes nothing the first run left; running
    it erratically and running it on a schedule reach the same end state. That
    is the property that makes it safe to schedule.
  * **Counts only in the result and the log.** Never a question, never a
    session id. The whole point is that a conversation stops existing, and a
    log line quoting the question it just deleted would defeat it — the same
    rule 12.4 follows for the email addresses it erases.
  * **One row's failure does not abandon the rest.** A `ZeroDBNotFoundError` on
    a delete means a concurrent run (or a voter's own "forget this
    conversation") got there first, which is success by this sweep's own
    standard; it is counted as already-gone and the loop continues.

**Unlike story 12.4, this sweep is wired to the scheduler from day one.**
`app.scheduling.jobs.default_jobs()` registers it as `sweep_qa_session_memory`.
12.4 documented at length that its retention promise was "kept by a human
running a script" because no scheduler existed; story 16.x's
`ScheduledJobRunner` closed that gap, so there is no reason for this story to
reopen it. The cadence is 6 hours rather than daily: a daily sweep means a turn
can sit up to 48 hours (its own day, plus up to a day waiting for the sweep),
which would make "retention is 1 day" false of the database for a full extra
day. Six hours bounds the overshoot at a quarter-day, and the read guard means
the overshoot is never voter-visible.

===============================================================================
THE ONE HONEST GAP
===============================================================================

This sweep lists the project's memory rows and ages each one. ZeroDB's list
endpoint caps `limit` at 100 and pages with `skip`, so the sweep's cost grows
with the total number of stored turns, not with the number expired. That is
fine at Informed's current scale (162 memory rows live today) and fine at the
scale a 1-day window implies — a day's conversations, not a year's. It would
NOT be fine if the sweep stopped running for a long period while traffic grew,
which is precisely the situation `MAX_SWEEP_PAGES` bounds: the sweep stops
after 500 pages (50,000 rows) and reports that it did, rather than running
unboundedly. A sweep that hits that ceiling is a signal that the scheduler has
been down, and the next run picks up where this one stopped — there is no
cursor to lose, because "expired" is a function of the row and the clock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.agent_memory.service import RETENTION_DAYS, expiry_of
from app.zerodb.client import DEFAULT_MEMORY_PAGE_SIZE, ZeroDBClient
from app.zerodb.errors import ZeroDBNotFoundError

logger = logging.getLogger(__name__)

#: Pages read in one sweep: 500 x 100 = 50,000 turns. See the module
#: docstring's "one honest gap" for why there is a ceiling and what hitting it
#: means.
MAX_SWEEP_PAGES = 500


@dataclass(frozen=True)
class SessionMemorySweepResult:
    """What one retention sweep did.

    Counts and nothing else — no session ids, no question text. `summary()` is
    safe to paste into a ticket, which is the test
    `test_the_sweep_reports_a_line_safe_to_paste_in_a_ticket` checks.
    """

    scanned: int = 0
    deleted: int = 0
    #: Still inside the one-day window — kept on purpose.
    kept: int = 0
    #: Listed but already gone by the time the delete was attempted: a
    #: concurrent sweep, or a voter's own "forget this conversation". Success,
    #: not failure.
    already_gone: int = 0
    #: Rows carrying neither a usable `expires_at` marker nor a usable
    #: `created_at`, so their age cannot be established. Left alone and
    #: reported rather than deleted (which would destroy data on the strength
    #: of a parse failure) or silently kept.
    undateable: int = 0
    #: True when the page ceiling stopped the sweep before it ran out of rows.
    truncated: bool = False
    #: Non-fatal problems, as short strings. Never carries row content.
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        line = (
            f"scanned {self.scanned} stored turn(s): deleted {self.deleted} past the "
            f"{RETENTION_DAYS}-day window, kept {self.kept} still inside it, "
            f"{self.already_gone} already gone, {self.undateable} undateable"
        )
        if self.truncated:
            line += f" (stopped at the {MAX_SWEEP_PAGES}-page ceiling; re-run)"
        return line


def sweep_expired_session_memory(
    client: ZeroDBClient,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> SessionMemorySweepResult:
    """Delete every stored Q&A turn past its one-day window.

    Idempotent: a second run over the same data deletes nothing.

    Args:
        client: the ZeroDB client to read and delete through.
        now: the moment to measure the window from. Injectable because the
            behaviour under test is entirely about elapsed time.
        dry_run: report what would be deleted and delete nothing. For a first
            run against a live project, where "how many conversations is this
            about to remove" is worth answering before removing them — the same
            affordance `sweep_expired_contact_emails` provides.

    Returns:
        A `SessionMemorySweepResult`, carrying counts only.
    """
    moment = now or datetime.now(UTC)
    scanned = 0
    deleted = 0
    kept = 0
    already_gone = 0
    undateable = 0
    errors: list[str] = []
    truncated = False

    # `skip` advances only by the rows THIS page left in place, because a
    # delete shifts every later row down by one. Advancing by the full page
    # size would step over exactly as many unexamined rows as this page
    # deleted — the bug `_fallback_filtered_rows` documents for `/rows`, in a
    # different shape. A page whose rows were all deleted leaves `skip` where
    # it was, which is correct: the next page's rows have moved into this
    # window.
    #
    # `seen` is tracked separately from `skip` so the loop can tell "no rows
    # left" from "this page deleted everything" without re-reading.
    skip = 0
    for _ in range(MAX_SWEEP_PAGES):
        page = client.list_memories(limit=DEFAULT_MEMORY_PAGE_SIZE, skip=skip)
        if not page.memories:
            break

        left_in_place = 0
        for record in page.memories:
            scanned += 1
            expiry = expiry_of(record.memory_metadata, record.created_at)
            if expiry is None:
                undateable += 1
                left_in_place += 1
                continue
            if expiry > moment:
                kept += 1
                left_in_place += 1
                continue
            if dry_run:
                # Nothing is removed, so this row stays in the window and must
                # be skipped past like any other survivor.
                deleted += 1
                left_in_place += 1
                continue
            try:
                client.delete_memory(memory_id=record.memory_id)
            except ZeroDBNotFoundError:
                # Already deleted by a concurrent run or by the voter's own
                # "forget this conversation". Success by this sweep's standard:
                # the row is gone. It is also no longer in the window, so it
                # does NOT count as left in place.
                already_gone += 1
                continue
            deleted += 1

        if not page.has_more and left_in_place == len(page.memories):
            # Nothing was removed and the server says this is the last page, so
            # there is nothing further to examine.
            break
        skip += left_in_place
    else:
        truncated = True
        logger.warning(
            "the Q&A memory sweep stopped at the %d-page ceiling; re-run it", MAX_SWEEP_PAGES
        )

    result = SessionMemorySweepResult(
        scanned=scanned,
        deleted=deleted,
        kept=kept,
        already_gone=already_gone,
        undateable=undateable,
        truncated=truncated,
        errors=errors,
    )
    logger.info(
        "Q&A session memory retention sweep (%s): %s",
        "dry run" if dry_run else "live",
        result.summary(),
    )
    return result


__all__ = [
    "MAX_SWEEP_PAGES",
    "SessionMemorySweepResult",
    "sweep_expired_session_memory",
]
