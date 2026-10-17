"""The real job registry: every periodic task this codebase needs, named once.

Five jobs today, one per story that flagged the scheduling gap:

  * `sweep_correction_emails` — story 12.4's 90-day contact-email retention
    sweep. Daily. The most urgent of the three (see `app.corrections.retention`'s
    module docstring): a GDPR-adjacent privacy promise with no enforcement
    mechanism until this existed.
  * `load_candidacies` — story 2.6's weekly FEC candidacy refresh. Blocked on
    `FEC_API_KEY`, which does not exist in this environment (see
    `app.ingest.candidacies`'s module docstring) — registered with a
    precondition that reports it as `blocked`, not crashed, until a real key
    is configured.
  * `load_house_rollcalls` — story 5.1's roll-call ingest, registered here at
    the hourly cadence story 5.6 specifies ("new votes picked up hourly while
    the House is in session") even though 5.6 itself has not been built yet.
    Blocked on `CONGRESS_GOV_API_KEY` for the same reason as `load_candidacies`.
  * `run_nightly_integrity_checks` — story 16.2's five datamodel.md integrity
    checks (`app.integrity.checks`). Nightly, no precondition: every check
    reads tables that already exist (even if some are empty today), so there
    is no missing credential to block on. See `run_nightly_integrity_checks`'s
    own docstring for how a violation becomes a `FAILED` job in this runner's
    report, and for the honest gap around paging/ticketing this does NOT do.
  * `repair_member_votes_denormalization` — story 16.4's drift repair
    (`app.integrity.repair`). Nightly, after the check above: fixing drift
    the moment it is found (rather than only reporting it) is the point of
    16.4's acceptance criterion, and running it right after the check means
    one nightly invocation both reports and heals.

Each job calls directly into the already-tested `app.ingest.*` / `app.corrections.*`
/ `app.integrity.*` logic, using the `ZeroDBClient` the runner injects — exactly
the pattern `backend/scripts/*.py` already uses, except the CLI scripts build
their own client from the environment where these jobs take one as a
parameter. This is deliberate: CLI scripts in this codebase are
"intentionally thin" wrappers with no logic of their own (see
`scripts/bootstrap.py`'s module docstring), and a scheduled job is just
another caller of the same tested logic, not a third copy of it.

**Nothing here is paced for the live ~60 req/min ZeroDB ceiling beyond what
each callee already does.** `CandidacyLoader` and `HouseRollCallLoader` take
their own `pace` callback exactly as the CLI scripts configure them; this
module wires the same default pacer rather than inventing a second one. The
two integrity jobs are read-mostly paged scans (see `app.integrity.checks.paged_rows`)
over tables that are small today; pacing them is a follow-up once
`member_votes` holds its real ~600k rows per Congress (same open question
datamodel.md already tracks for every other full-table read in this
codebase).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import timedelta

from app.corrections.retention import sweep_expired_contact_emails
from app.ingest.candidacies import (
    ALL_STATES,
    INGESTED_OFFICES,
    CandidacyLoader,
    CandidacySourceError,
    candidacies_from_results,
    fetch_candidates,
)
from app.ingest.house_rollcalls import (
    EARLIEST_CONGRESS,
    HouseRollCallLoader,
    HouseRollCallSourceError,
    fetch_roll_call_item,
    fetch_roll_call_numbers,
    transform_roll_calls,
)
from app.integrity.checks import run_all_checks
from app.integrity.repair import repair_member_votes_drift
from app.scheduling.registry import JobRegistration
from app.seed_elections import UPCOMING_ELECTIONS, ElectionSeeder
from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBError

logger = logging.getLogger(__name__)

#: How many of the most recently listed House roll calls one hourly run loads.
#: Deliberately small, matching `scripts/load_house_rollcalls.py --limit`'s own
#: default reasoning: an hourly job's job is to catch new votes quickly, not
#: to backfill (that is story 5.8's), so a bounded recent window is correct
#: even before any key exists to prove it against the live API.
HOUSE_ROLLCALLS_SAMPLE_SIZE = 20

#: Which congress/session an hourly sync targets. Hard-coded to the current
#: one rather than computed, for the same reason `load_house_rollcalls.py`
#: defaults to `--congress 119 --session 2`: there is no live source yet to
#: derive "the current session" from, and a wrong guess here is caught by the
#: Congress.gov API itself once a key exists (an out-of-range congress/session
#: 404s rather than silently returning nothing).
HOUSE_ROLLCALLS_CONGRESS = 119
HOUSE_ROLLCALLS_SESSION = 2


def _pacer(min_interval_seconds: float) -> Callable[[], None]:
    """The same last-call-relative pacer every load script in this codebase
    inlines; see `scripts/load_candidacies.py`'s own copy for why it is
    inlined rather than imported.
    """
    state = {"last": None}

    def pace() -> None:
        now = time.monotonic()
        last = state["last"]
        if last is not None:
            elapsed = now - last
            remaining = min_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        state["last"] = time.monotonic()

    return pace


# --- story 12.4: sweep_correction_emails -----------------------------------


def _require_env(name: str) -> Callable[[], str | None]:
    """A precondition: block the job with a readable reason if `name` is not
    set in the environment, exactly the check every affected CLI script
    already makes by hand (`load_candidacies.py`, `load_house_rollcalls.py`).
    """

    def check() -> str | None:
        if os.environ.get(name):
            return None
        return f"{name} is not set"

    return check


def run_sweep_correction_emails(client: ZeroDBClient) -> None:
    """Erase contact emails on corrections closed more than 90 days ago.

    Delegates entirely to `app.corrections.retention.sweep_expired_contact_emails`,
    which is idempotent and raises `ZeroDBError` on a genuine write failure —
    exactly the behaviour the registry needs to decide RAN vs FAILED.
    """
    result = sweep_expired_contact_emails(client)
    logger.info("sweep_correction_emails: %s", result.summary())


# --- story 2.6: load_candidacies --------------------------------------------


def run_load_candidacies(client: ZeroDBClient) -> None:
    """Weekly FEC candidacy refresh, exactly like `scripts/load_candidacies.py`
    but against the runner's own injected client and `FEC_API_KEY`.

    Mirrors the script's per-state/office loop and its "one state/office
    failing must not abandon the others" tolerance: a `CandidacySourceError`
    for one state/office is logged and counted as skipped rather than raised,
    so a transient FEC hiccup on one query does not fail the whole job. A
    `ZeroDBError` while writing DOES propagate — that is a real failure the
    registry should report, not a skip.
    """
    api_key = os.environ["FEC_API_KEY"]  # precondition already checked this is set
    election = UPCOMING_ELECTIONS[0]
    election_year = int(str(election.election_date)[:4])
    pace = _pacer(1.1)

    ElectionSeeder(client, pace=pace).run()
    loader = CandidacyLoader(client, pace=pace)

    total_written = 0
    total_skipped = 0
    for state in ALL_STATES:
        for office in INGESTED_OFFICES:
            try:
                pace()
                records = fetch_candidates(
                    election_year=election_year, office=office, state=state, api_key=api_key
                )
            except CandidacySourceError as exc:
                logger.error("load_candidacies: could not fetch %s/%s: %s", state, office, exc)
                total_skipped += 1
                continue

            rows, skipped = candidacies_from_results(records, election=election)
            total_skipped += len(skipped)
            report = loader.load(rows)
            total_written += report.row_count
            total_skipped += len(report.skipped)

    logger.info(
        "load_candidacies: %d candidacy row(s) written, %d skipped", total_written, total_skipped
    )


# --- story 5.1 / 5.6: load_house_rollcalls ----------------------------------


def run_load_house_rollcalls(client: ZeroDBClient) -> None:
    """Hourly House roll-call sync, exactly like `scripts/load_house_rollcalls.py`
    but against the runner's own injected client and `CONGRESS_GOV_API_KEY`,
    at the cadence story 5.6 specifies rather than the CLI script's manual
    default.
    """
    api_key = os.environ["CONGRESS_GOV_API_KEY"]  # precondition already checked
    pace = _pacer(1.1)

    try:
        numbers = fetch_roll_call_numbers(
            congress=HOUSE_ROLLCALLS_CONGRESS, session=HOUSE_ROLLCALLS_SESSION, api_key=api_key
        )
        sample = sorted(numbers, reverse=True)[:HOUSE_ROLLCALLS_SAMPLE_SIZE]
        envelopes = []
        for roll_number in sample:
            pace()
            envelopes.append(
                fetch_roll_call_item(
                    congress=HOUSE_ROLLCALLS_CONGRESS,
                    session=HOUSE_ROLLCALLS_SESSION,
                    roll_call_number=roll_number,
                    api_key=api_key,
                )
            )
        rows, skipped = transform_roll_calls(envelopes)
    except HouseRollCallSourceError as exc:
        # A source-level failure (unreachable API, unparseable response) is a
        # real failure for this job, not a skip — unlike one state/office in
        # load_candidacies, there is only one source here and it either
        # worked or it didn't.
        raise ZeroDBError(f"could not load the House roll-call source: {exc}") from exc

    report = HouseRollCallLoader(client, pace=_pacer(1.1)).load(rows)
    logger.info(
        "load_house_rollcalls: %d roll call(s) written, %d skipped",
        report.row_count,
        len(skipped),
    )


# --- story 16.2: run_nightly_integrity_checks -------------------------------


def run_nightly_integrity_checks(client: ZeroDBClient) -> None:
    """Run all five datamodel.md integrity checks; raise if any found a violation.

    Delegates entirely to `app.integrity.checks.run_all_checks`, which never
    raises on a check's own findings (see that module's docstring: one check's
    violations must not stop the others from running, matching
    `ScheduledJobRunner`'s own principle). This function is where a violation
    becomes a job FAILURE rather than a quietly-logged report: raising here is
    what makes `ScheduledJobRunner` record this job as `FAILED` in its
    `RunReport` — visible in CI/ops output today, and the hook a real
    paging/ticketing integration attaches to tomorrow. See
    `app.integrity.checks`'s module docstring for why no such integration
    exists yet, and why faking one would be worse than the honest gap.

    A genuine `ZeroDBError` from a check's own reads (table unreachable, not a
    violation) propagates unchanged — that is already a job failure by the
    runner's own rules, with no need for this function to add to it.
    """
    report = run_all_checks(client)
    for check_report in report.reports:
        logger.info("integrity check %s", check_report.summary())
    if not report.ok:
        violation_lines = "; ".join(str(v) for v in report.violations)
        raise ZeroDBError(
            f"nightly integrity checks found {len(report.violations)} violation(s): "
            f"{violation_lines}"
        )


# --- story 16.4: repair_member_votes_denormalization ------------------------


def run_repair_member_votes_drift(client: ZeroDBClient) -> None:
    """Re-copy every drifted denormalized field on `member_votes`.

    Delegates entirely to `app.integrity.repair.repair_member_votes_drift`,
    which is idempotent (see that module's docstring) and raises `ZeroDBError`
    on a genuine write failure — exactly the behaviour the registry needs to
    decide RAN vs FAILED, the same contract `run_sweep_correction_emails`
    already follows for its own periodic repair.
    """
    result = repair_member_votes_drift(client)
    logger.info("repair_member_votes_denormalization: %s", result.summary())


# --- the registry itself ----------------------------------------------------


def default_jobs() -> list[JobRegistration]:
    """Every periodic job this codebase knows about, with its own cadence.

    This is the one list a new periodic story adds itself to. Nothing outside
    this function needs to change for a new job to be picked up by
    `scripts/run_scheduled_jobs.py` — the CLI, the GitHub Actions workflow and
    the runner itself are all job-agnostic by construction (see
    `app.scheduling.registry`'s module docstring).
    """
    return [
        JobRegistration(
            name="sweep_correction_emails",
            interval=timedelta(days=1),
            run=run_sweep_correction_emails,
        ),
        JobRegistration(
            name="load_candidacies",
            interval=timedelta(days=7),
            run=run_load_candidacies,
            precondition=_require_env("FEC_API_KEY"),
        ),
        JobRegistration(
            name="load_house_rollcalls",
            interval=timedelta(hours=1),
            run=run_load_house_rollcalls,
            precondition=_require_env("CONGRESS_GOV_API_KEY"),
        ),
        JobRegistration(
            name="run_nightly_integrity_checks",
            interval=timedelta(days=1),
            run=run_nightly_integrity_checks,
        ),
        JobRegistration(
            name="repair_member_votes_denormalization",
            interval=timedelta(days=1),
            run=run_repair_member_votes_drift,
        ),
    ]


#: `EARLIEST_CONGRESS` is re-exported only so a future story 5.6 implementer
#: extending `run_load_house_rollcalls` has it in reach from this module
#: without a second import of `app.ingest.house_rollcalls`; this module itself
#: does not use it (the hourly sync always targets the current congress/session,
#: never the earliest one).
__all__ = [
    "EARLIEST_CONGRESS",
    "HOUSE_ROLLCALLS_CONGRESS",
    "HOUSE_ROLLCALLS_SAMPLE_SIZE",
    "HOUSE_ROLLCALLS_SESSION",
    "default_jobs",
    "run_load_candidacies",
    "run_load_house_rollcalls",
    "run_nightly_integrity_checks",
    "run_repair_member_votes_drift",
    "run_sweep_correction_emails",
]
