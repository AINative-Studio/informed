"""The real job registry: every periodic task this codebase needs, named once.

Eight jobs today, one per story that flagged the scheduling gap:

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
  * `load_senate_rollcalls` — story 8.1's roll-call ingest, at the hourly
    cadence its own acceptance criterion names directly ("hourly sync while
    in session"). **No precondition, unlike `load_house_rollcalls`**: the
    senate.gov LIS feed is public and keyless, confirmed live during this
    story (see `app.ingest.senate_rollcalls`'s module docstring) — there is
    no credential to block on, so this job runs for real from the moment
    this registry is deployed, the same "always actually runs" posture the
    two integrity jobs and the memory sweep already have.
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
  * `run_panel_gate_check` — story 12.5's release gate
    (`app.panel_review.gate`). Nightly, beside the integrity checks it
    mirrors in shape: fails the job if any live `issue_topics`/`stances` row
    lacks an in-force `approved`-or-`grandfathered` panel decision at its
    current version. No precondition: like the integrity checks, it needs
    only the ZeroDB credentials this runner already has. This is the LIVE
    half of story 12.5 — the half `.github/workflows/ci.yml` cannot run
    because it has no ZeroDB credentials; see
    `scripts/check_panel_gate.py`'s module docstring for the CI-safe half
    and for why `grandfathered` passes deliberately rather than by omission.
  * `sweep_qa_session_memory` — story 13.5's 1-day Q&A memory retention. Every
    6 hours, not daily: a daily sweep of a 1-day window lets a turn survive up
    to 48 hours, which would make datamodel.md's "retention: 1 day" false of
    the database for an extra day. Registered from day one rather than left to
    a human with a script — the gap 12.4 documented and `ScheduledJobRunner`
    closed. No precondition: the memory API needs no credential beyond the
    project's own, so this job and the two integrity jobs are the three that
    always actually run.

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

from app.agent_memory import sweep_expired_session_memory
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
from app.ingest.senate_rollcalls import (
    SenateRollCallLoader,
    SenateRollCallSourceError,
    fetch_lis_to_bioguide_map,
    fetch_roll_call_xml,
    fetch_vote_numbers,
    roll_call_item_url,
    transform_member_votes_for,
)
from app.ingest.senate_rollcalls import (
    transform_roll_calls as transform_senate_roll_calls,
)
from app.integrity.checks import run_all_checks
from app.integrity.repair import repair_member_votes_drift
from app.panel_review.gate import check_panel_gate
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

#: Mirrors HOUSE_ROLLCALLS_SAMPLE_SIZE/CONGRESS/SESSION's own reasoning for
#: story 8.1's hourly sync: a small, bounded recent window, hard-coded to the
#: current congress/session rather than computed (no live source to derive
#: "current" from; an out-of-range congress/session 404s from senate.gov
#: itself rather than silently returning nothing).
SENATE_ROLLCALLS_SAMPLE_SIZE = 20
SENATE_ROLLCALLS_CONGRESS = 119
SENATE_ROLLCALLS_SESSION = 2


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


# --- story 8.1: load_senate_rollcalls ---------------------------------------


def run_load_senate_rollcalls(client: ZeroDBClient) -> None:
    """Hourly Senate roll-call sync, at the cadence story 8.1's own
    acceptance criterion names ("hourly sync while in session").

    Unlike `run_load_house_rollcalls`, this job has NO precondition: the
    senate.gov LIS feed needs no API key (confirmed live during this story),
    so there is no credential to check for before running. Fetches the
    `SENATE_ROLLCALLS_SAMPLE_SIZE` most recently listed votes, the matching
    `lis_member_id -> bioguide` crosswalk, and writes both `roll_calls` and
    `member_votes` — this story's wider scope than 5.1's `roll_calls`-only
    (see `app.ingest.senate_rollcalls`'s module docstring's "Member
    positions" section for why).

    A `SenateRollCallSourceError` from the vote-list or item fetch is a real
    failure for this job (only one source, unlike `load_candidacies`'s
    several states/offices) and propagates as a `ZeroDBError`, mirroring
    `run_load_house_rollcalls`'s own posture exactly.
    """
    pace = _pacer(1.1)

    try:
        numbers = fetch_vote_numbers(
            congress=SENATE_ROLLCALLS_CONGRESS, session=SENATE_ROLLCALLS_SESSION
        )
        sample = sorted(numbers, reverse=True)[:SENATE_ROLLCALLS_SAMPLE_SIZE]
        documents = []
        for vote_number in sample:
            pace()
            xml = fetch_roll_call_xml(
                congress=SENATE_ROLLCALLS_CONGRESS,
                session=SENATE_ROLLCALLS_SESSION,
                vote_number=vote_number,
            )
            source_url = roll_call_item_url(
                congress=SENATE_ROLLCALLS_CONGRESS,
                session=SENATE_ROLLCALLS_SESSION,
                vote_number=vote_number,
                base_url="https://www.senate.gov/legislative/LIS",
            )
            documents.append((xml, source_url))
        rows, skipped = transform_senate_roll_calls(documents)

        pace()
        bioguide_by_lis_id = fetch_lis_to_bioguide_map()

        member_vote_rows_by_roll_call: dict[str, list[dict]] = {}
        member_votes_skipped_total = 0
        xml_by_source_url = {source_url: xml for xml, source_url in documents}
        for row in rows:
            xml = xml_by_source_url.get(row["source_url"])
            if xml is None:
                continue
            member_rows, member_skipped = transform_member_votes_for(
                row, xml, bioguide_by_lis_id=bioguide_by_lis_id
            )
            member_vote_rows_by_roll_call[row["id"]] = member_rows
            member_votes_skipped_total += len(member_skipped)
    except SenateRollCallSourceError as exc:
        raise ZeroDBError(f"could not load the Senate roll-call source: {exc}") from exc

    report = SenateRollCallLoader(client, pace=_pacer(1.1)).load(
        rows, member_vote_rows_by_roll_call=member_vote_rows_by_roll_call
    )
    logger.info(
        "load_senate_rollcalls: %d roll call(s) written, %d skipped, "
        "%d member_vote(s) written, %d member_vote(s) skipped",
        report.row_count,
        len(skipped),
        len(report.member_votes_written),
        member_votes_skipped_total,
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


# --- story 12.5: run_panel_gate_check ---------------------------------------


def run_panel_gate_check(client: ZeroDBClient) -> None:
    """Fail this job if any live issue_topics/stances row lacks a panel decision.

    Delegates entirely to `app.panel_review.gate.check_panel_gate`, which
    never raises on its own findings (same contract as `run_all_checks`
    above) — this function is where a violation becomes a job FAILURE rather
    than a quietly-logged report, exactly as `run_nightly_integrity_checks`
    does for datamodel.md's checks.

    This is the LIVE half of story 12.5's release gate — the half that needs
    real ZeroDB credentials and so cannot run in `.github/workflows/ci.yml`
    today (see `scripts/check_panel_gate.py`'s module docstring). Registering
    it here means the day a `prod` GitHub Environment with ZeroDB credentials
    exists, this job starts protecting the real corpus with no further code
    change — the same "flip a flag once credentials exist" posture
    `load_candidacies`/`load_house_rollcalls` already document for their own
    missing API keys.

    Grandfathered rows do not fail this job — see
    `app.panel_review.gate`'s module docstring for why that is a deliberate
    tolerance of a known, visible debt rather than a gap in this check.
    """
    report = check_panel_gate(client)
    logger.info("panel gate check: %s", report.summary())
    if not report.ok:
        violation_lines = "; ".join(str(v) for v in report.violations)
        raise ZeroDBError(
            f"panel gate check found {len(report.violations)} unapproved row(s): {violation_lines}"
        )


# --- story 13.5: sweep_qa_session_memory ------------------------------------


def run_sweep_qa_session_memory(client: ZeroDBClient) -> None:
    """Delete Q&A session turns past their one-day retention window.

    Delegates entirely to `app.agent_memory.sweep_expired_session_memory`,
    which is idempotent and raises `ZeroDBError` on a genuine failure — the
    same contract `run_sweep_correction_emails` follows, and exactly what the
    registry needs to decide RAN vs FAILED.

    **Registered from day one, unlike story 12.4's email sweep.** 12.4 shipped
    with its retention promise "kept by a human running a script" because no
    scheduler existed; `ScheduledJobRunner` now does, so story 13.5 has no
    excuse to reopen that gap. The log line carries counts only — never a
    question, never a session id (see `app.agent_memory.retention`).
    """
    result = sweep_expired_session_memory(client)
    logger.info("sweep_qa_session_memory: %s", result.summary())


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
            # Story 8.1: no precondition -- the senate.gov LIS feed is
            # public and keyless (confirmed live), so unlike
            # load_house_rollcalls there is no credential to block on. Runs
            # for real from day one, the same posture the integrity jobs and
            # the memory sweep already have.
            name="load_senate_rollcalls",
            interval=timedelta(hours=1),
            run=run_load_senate_rollcalls,
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
        JobRegistration(
            # Story 12.5: unapproved taxonomy/stance wording blocked from
            # release. Daily, like the integrity checks it sits beside — this
            # is a standing-debt monitor (did anything slip past the review
            # workflow since yesterday), not a request-path gate, so a day's
            # latency on catching a bypass is the same trade
            # run_nightly_integrity_checks already makes.
            name="run_panel_gate_check",
            interval=timedelta(days=1),
            run=run_panel_gate_check,
        ),
        JobRegistration(
            # Every 6 hours rather than daily. A daily sweep of a 1-day window
            # means a turn can survive up to 48 hours — its own day, plus up to
            # a day waiting for the sweep — which would make datamodel.md's
            # "retention: 1 day" false of the database for a full extra day.
            # Six hours bounds the overshoot at a quarter-day, and
            # `SessionMemoryService.recall`'s read guard means the overshoot is
            # never voter-visible at any point. No precondition: the memory API
            # needs no credential beyond the project's own.
            name="sweep_qa_session_memory",
            interval=timedelta(hours=6),
            run=run_sweep_qa_session_memory,
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
    "SENATE_ROLLCALLS_CONGRESS",
    "SENATE_ROLLCALLS_SAMPLE_SIZE",
    "SENATE_ROLLCALLS_SESSION",
    "default_jobs",
    "run_load_candidacies",
    "run_load_house_rollcalls",
    "run_load_senate_rollcalls",
    "run_nightly_integrity_checks",
    "run_panel_gate_check",
    "run_repair_member_votes_drift",
    "run_sweep_correction_emails",
    "run_sweep_qa_session_memory",
]
