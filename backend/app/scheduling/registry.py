"""A scheduler-agnostic job registry and runner (infrastructure, Epic 16).

Three stories independently flagged the same gap and each named this exact
shape as the fix:

  * `app/corrections/retention.py` (story 12.4) — the 90-day contact-email
    erasure "is a privacy promise currently kept by a human running a
    script", and names "one scheduled runner for every periodic task in this
    codebase" as the tracked follow-up.
  * `app/ingest/candidacies.py` (story 2.6) — "refreshed weekly" has no
    scheduler behind it either, in the same words.
  * `backend/scripts/load_house_rollcalls.py` (story 5.1) — ready for story
    5.6's hourly incremental sync, which also needs a scheduler that does not
    exist yet.

**What this module is not: a scheduler.** There is still no cron, worker or
queue running inside this codebase, and standing one up is a deployment
decision (which host, who pays, what triggers it) this module deliberately
does not make — see `docs/environments.md`'s "deployment targets: none
provisioned yet" for why that decision is out of scope for application code.

**What this module is:** the one correct, tested thing an external trigger
can invoke blindly. `ScheduledJobRunner.run()` knows every job's cadence,
checks a stored "last successful run" timestamp per job, runs only the ones
that are due, and reports what happened. A system cron entry, a Railway cron
service, or (as wired up in `.github/workflows/scheduled-jobs.yml`) a GitHub
Actions `schedule:` trigger can all call `scripts/run_scheduled_jobs.py` on
whatever cadence is convenient for them — hourly is enough to serve a job
whose own interval is 7 days, because "due" is a function of elapsed time
against the stored timestamp, not of the caller's own cadence matching it
exactly. This is what "safe to invoke from ANY real scheduler with no
per-job awareness" means in practice.

Design decisions, each one deliberate:

  * **State lives in ZeroDB, not in a file or in-process.** A `scheduled_jobs`
    table (one row per job, keyed by job name) is the only record of "when did
    this last succeed" — the same bookkeeping pattern `app.bootstrap` uses for
    `schema_versions`: a plain upsert-by-id row, read first and written last.
    This means the runner is itself stateless across invocations: any process,
    on any machine, that can reach the same ZeroDB project sees the same "is
    this due" answer. That is what makes "run this daily from GitHub Actions"
    and "run this by hand from a laptop" interchangeable.
  * **A job's last-run timestamp updates only on success.** A job that raises
    leaves its stored timestamp untouched, so the NEXT invocation (whenever
    that is) sees it as still due and retries rather than silently skipping a
    missed run for a full interval. This is the same principle
    `sweep_correction_emails` already relies on: idempotent, no cursor, safe
    to re-run.
  * **One job's exception never stops the loop.** Each job runs inside its own
    try/except; a failure is recorded in the report and the runner moves on.
    The alternative — one broken job aborting every later one — would turn a
    single credential gap (no FEC key, no Congress.gov key, both real today)
    into an outage for the sweep job that actually matters most.
  * **A job that cannot run for a structural reason (a missing credential) is
    reported as `blocked`, not `failed`.** Both are distinguishable from
    `ran` and `skipped` in the report, and both leave the exit code nonzero —
    an operator watching CI should see "something needs attention" either
    way — but `blocked` says "nothing is broken, a human needs to configure
    this" where `failed` says "this raised and should be investigated",
    which matters when triaging a red run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError

logger = logging.getLogger(__name__)

#: The one table this module owns. Infrastructure, like `schema_versions`,
#: `voter_profile_keys` and `panel_reviews` — no section of datamodel.md
#: describes it, and `app.bootstrap.INFRASTRUCTURE_TABLES` lists it for the
#: same reason those three are listed: a documented operational promise
#: (here, "this job runs on schedule") needs somewhere real to keep state.
TABLE_NAME = "scheduled_jobs"

#: The codebase's one timestamp spelling, matching
#: `app.corrections.retention._iso` and `app.corrections.service`.
_TIMESPEC = "microseconds"


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec=_TIMESPEC).replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class JobStatus(StrEnum):
    """What happened to one job on one run of the registry."""

    #: It was due, and it ran to completion without raising.
    RAN = "ran"
    #: It was not due yet — its interval has not elapsed since the last
    #: recorded success.
    SKIPPED_NOT_DUE = "skipped_not_due"
    #: It was due, but a precondition check (e.g. a required credential) said
    #: it cannot run. Distinct from FAILED: nothing raised, nothing is broken,
    #: an operator needs to configure something before this can proceed.
    BLOCKED = "blocked"
    #: It was due, attempted, and raised. The exception is recorded.
    FAILED = "failed"


@dataclass(frozen=True)
class JobRegistration:
    """One job this runner knows about.

    Attributes:
        name: stable identifier, used as both the registry's dict key and the
            `scheduled_jobs` row id. Never rename a shipped job's `name` —
            doing so loses its last-run history and the next run will treat
            it as never having run.
        interval: how often this job is due, as a `timedelta`. The runner
            compares this against elapsed time since the last recorded
            success — not against the caller's own invocation cadence, which
            is why a daily GitHub Actions trigger correctly serves a job
            whose own `interval` is 7 days (it is simply reported SKIPPED on
            the 6 days out of 7 it is not due).
        run: the callable that does the job's actual work. Takes the
            `ZeroDBClient` to run against and returns nothing; raises on
            failure. Each job in `app.scheduling.jobs` wraps an existing
            script's already-tested entry point rather than reimplementing
            its logic here.
        precondition: optional callable returning `None` if the job may run,
            or a human-readable reason string if it is structurally blocked
            (e.g. "FEC_API_KEY is not set"). Checked before `run`, and checked
            EVERY time the job is due — not just once — so a credential that
            is missing today and supplied tomorrow is picked up on the very
            next due run with no code change.
    """

    name: str
    interval: timedelta
    run: Callable[[ZeroDBClient], None]
    precondition: Callable[[], str | None] | None = None


@dataclass(frozen=True)
class JobOutcome:
    """What happened to one job on one run of the registry. Carries no
    secrets and nothing from the job's own domain (see `sweep_correction_emails`'s
    rule about never logging an email) — only the job's name, its status, and
    a short reason/error string safe to print and paste into a ticket.
    """

    name: str
    status: JobStatus
    detail: str = ""


@dataclass(frozen=True)
class RunReport:
    """Every job's outcome from one invocation of the runner."""

    outcomes: list[JobOutcome] = field(default_factory=list)

    @property
    def ran(self) -> list[JobOutcome]:
        return [o for o in self.outcomes if o.status is JobStatus.RAN]

    @property
    def skipped(self) -> list[JobOutcome]:
        return [o for o in self.outcomes if o.status is JobStatus.SKIPPED_NOT_DUE]

    @property
    def blocked(self) -> list[JobOutcome]:
        return [o for o in self.outcomes if o.status is JobStatus.BLOCKED]

    @property
    def failed(self) -> list[JobOutcome]:
        return [o for o in self.outcomes if o.status is JobStatus.FAILED]

    @property
    def ok(self) -> bool:
        """True only if nothing failed. `blocked` does not fail the run by
        itself being present in the report, but the CLI script exits nonzero
        whenever `blocked` or `failed` is non-empty — see
        `scripts/run_scheduled_jobs.py` for the exit-code policy, which is
        deliberately stricter than this property so a human watching exit
        codes still sees "needs attention" for a blocked job.
        """
        return not self.failed

    def summary(self) -> str:
        return (
            f"{len(self.ran)} ran, {len(self.skipped)} skipped (not due), "
            f"{len(self.blocked)} blocked, {len(self.failed)} failed"
        )


class ScheduledJobRunner:
    """Runs every registered job that is due; records what happened.

    See the module docstring for the full argument. In short: this is the
    thing a cron entry, a Railway cron service or a GitHub Actions `schedule:`
    trigger calls, with no awareness of what jobs exist or how often each one
    should run — that knowledge lives entirely in the `JobRegistration` list
    passed to this class and in the `scheduled_jobs` table's stored
    timestamps.
    """

    def __init__(self, client: ZeroDBClient, jobs: list[JobRegistration]) -> None:
        self._client = client
        self._jobs = jobs

    def run(self, *, now: datetime | None = None) -> RunReport:
        """Run every job that is due; skip, block or fail the rest.

        Never raises on a job's own failure — see the module docstring for
        why one job's exception must not stop the loop. Can still raise a
        `ZeroDBError` from the bookkeeping reads/writes themselves (listing
        or recording a last-run timestamp), because a registry that cannot
        tell what is due has nothing honest to report.
        """
        moment = now or datetime.now(UTC)
        outcomes: list[JobOutcome] = []
        for job in self._jobs:
            outcomes.append(self._run_one(job, moment))
        report = RunReport(outcomes=outcomes)
        logger.info("scheduled job run complete: %s", report.summary())
        return report

    def _run_one(self, job: JobRegistration, moment: datetime) -> JobOutcome:
        last_run = self._last_run(job.name)
        if last_run is not None and moment - last_run < job.interval:
            logger.info(
                "job %r not due yet (last ran %s, interval %s)", job.name, last_run, job.interval
            )
            return JobOutcome(job.name, JobStatus.SKIPPED_NOT_DUE, detail=f"last ran {last_run}")

        if job.precondition is not None:
            reason = job.precondition()
            if reason is not None:
                logger.warning("job %r blocked: %s", job.name, reason)
                return JobOutcome(job.name, JobStatus.BLOCKED, detail=reason)

        try:
            job.run(self._client)
        except Exception as exc:  # noqa: BLE001 - one job's failure must not stop the loop
            logger.error("job %r failed: %s", job.name, exc)
            return JobOutcome(job.name, JobStatus.FAILED, detail=str(exc))

        self._record_success(job.name, moment)
        logger.info("job %r ran and succeeded", job.name)
        return JobOutcome(job.name, JobStatus.RAN, detail=f"ran at {_iso(moment)}")

    # --- bookkeeping ---------------------------------------------------

    def _last_run(self, job_name: str) -> datetime | None:
        """The stored last-success timestamp for one job, or None if it has
        never recorded one (a brand-new job, or a project that has never
        bootstrapped the `scheduled_jobs` table at all — both are "due").
        """
        return last_success_at(self._client, job_name)

    def _record_success(self, job_name: str, moment: datetime) -> None:
        """Upsert this job's last-run timestamp. Idempotent by `job_name`,
        like every other infrastructure row in this codebase
        (`schema_versions`'s `upsert_row` is the direct precedent).
        """
        payload: dict[str, Any] = {"id": job_name, "last_run_at": _iso(moment)}
        self._client.upsert_row(TABLE_NAME, job_name, payload)


def last_success_at(client: ZeroDBClient, job_name: str) -> datetime | None:
    """The stored last-success timestamp for `job_name`, or `None` if it has
    never recorded one.

    Public (story 16.5) so `app.observability.ingest_lag` can read the exact
    same bookkeeping `ScheduledJobRunner` itself relies on to decide "is this
    due" without duplicating the read or reaching into a private method —
    one ingest-lag reading and one due/not-due decision, same source of
    truth, same row shape.
    """
    try:
        row = client.get_row(TABLE_NAME, job_name)
    except ZeroDBNotFoundError:
        return None
    if row is None:
        return None
    last_run_at = row.row_data.get("last_run_at")
    if not last_run_at:
        return None
    return _parse_iso(last_run_at)


def run_due_jobs(
    client: ZeroDBClient, jobs: list[JobRegistration], *, now: datetime | None = None
) -> RunReport:
    """Convenience wrapper: build a runner and run it once.

    Exists so a caller that only needs one run — the CLI script, a test —
    does not have to spell out `ScheduledJobRunner(client, jobs).run()`
    itself. Propagates a `ZeroDBError` from the bookkeeping calls exactly as
    `ScheduledJobRunner.run` does.
    """
    return ScheduledJobRunner(client, jobs).run(now=now)


__all__ = [
    "TABLE_NAME",
    "JobOutcome",
    "JobRegistration",
    "JobStatus",
    "RunReport",
    "ScheduledJobRunner",
    "last_success_at",
    "run_due_jobs",
    "ZeroDBError",
]
