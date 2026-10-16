"""Step definitions for tests/features/scheduled_jobs.feature (Epic 16 infra).

Runs entirely against the in-memory fake transport from story 1.2
(`FakeZeroDBTransport`), never live ZeroDB — the same rule every backend
feature in this suite follows. The live confirmation of the real job registry
(`app.scheduling.jobs.default_jobs`) against the real "informed" project is
reported in this story's PR, not asserted here: CI needs no credentials.

Demo jobs in this file ("demo-job", "broken-job", "healthy-job", "needs-a-key")
are synthetic `JobRegistration`s built per-scenario, not the real registry —
they exist to pin the runner's own mechanics (due/not-due, one failure not
blocking another, blocked-vs-failed) independently of which three jobs happen
to be registered today. The scenarios about the real registry
(`sweep_correction_emails`, `load_candidacies`, `load_house_rollcalls`) import
`app.scheduling.jobs.default_jobs` directly and assert against it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.scheduling.jobs import default_jobs
from app.scheduling.registry import (
    JobOutcome,
    JobRegistration,
    JobStatus,
    ScheduledJobRunner,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/scheduled_jobs.feature")

#: A fixed "now" so "last ran N days ago" is deterministic across the suite.
_NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(transport=fake, project_id="proj-test")


@given("a ZeroDB client backed by an in-memory fake")
def client_with_fake(client: ZeroDBClient) -> ZeroDBClient:
    # Models an already-bootstrapped project (`app.bootstrap` creates this
    # table) so the scenarios below exercise "no row for this job yet" rather
    # than "table does not exist" — both are tolerated by
    # `ScheduledJobRunner._last_run`, but only the first is the realistic
    # case for a project this story's runner would actually be pointed at.
    client.create_table("scheduled_jobs", {"id": "string", "last_run_at": "string"})
    return client


@pytest.fixture
def jobs() -> dict[str, JobRegistration]:
    """Registered jobs for this scenario, keyed by name. A dict (not a list)
    so a `Given` step can add to it without caring about order, and the `When`
    step passes `list(jobs.values())` to the runner.
    """
    return {}


@pytest.fixture
def calls() -> list[str]:
    """Names of jobs whose `run` callable actually executed, in order."""
    return []


@pytest.fixture
def report() -> dict[str, Any]:
    """Carries the `RunReport` (and anything else) between When/Then steps."""
    return {}


# --- Given: registering a demo job -----------------------------------------


@given(parsers.parse('a registered job "{name}" that runs every {count:d} day(s)'))
def register_daily_job(
    jobs: dict[str, JobRegistration], calls: list[str], name: str, count: int
) -> None:
    jobs[name] = JobRegistration(
        name=name, interval=timedelta(days=count), run=lambda c, n=name: calls.append(n)
    )


@given(parsers.parse('a registered job "{name}" that runs every {count:d} hour(s)'))
def register_hourly_job(
    jobs: dict[str, JobRegistration], calls: list[str], name: str, count: int
) -> None:
    jobs[name] = JobRegistration(
        name=name, interval=timedelta(hours=count), run=lambda c, n=name: calls.append(n)
    )


@given(parsers.parse('a registered job "{name}" that runs every {count:d} day(s) and always fails'))
def register_failing_job(jobs: dict[str, JobRegistration], name: str, count: int) -> None:
    def _always_fails(_client: ZeroDBClient) -> None:
        raise RuntimeError(f"{name} deliberately failed")

    jobs[name] = JobRegistration(name=name, interval=timedelta(days=count), run=_always_fails)


@given(
    parsers.parse(
        'a registered job "{name}" that runs every {count:d} day(s) '
        "and requires an unset environment variable"
    )
)
def register_blocked_job(
    jobs: dict[str, JobRegistration], calls: list[str], name: str, count: int
) -> None:
    jobs[name] = JobRegistration(
        name=name,
        interval=timedelta(days=count),
        run=lambda c, n=name: calls.append(n),
        precondition=lambda: "SOME_CREDENTIAL_THAT_IS_NOT_SET is not set",
    )


@given(parsers.parse("the job has never recorded a last-run"))
@given(parsers.parse("neither job has ever run"))
def job_never_ran() -> None:
    """A no-op: a job the fake has never seen a row for is "never run" by
    construction (see `ScheduledJobRunner._last_run`'s tolerance of a missing
    row). Kept as an explicit step for readability in the feature file.
    """


@given(parsers.parse('the job "{name}" last ran {count:d} day(s) ago'))
def job_last_ran_days_ago(client: ZeroDBClient, name: str, count: int) -> None:
    last_run = _NOW - timedelta(days=count)
    last_run_at = last_run.isoformat().replace("+00:00", "Z")
    client.upsert_row("scheduled_jobs", name, {"id": name, "last_run_at": last_run_at})


# --- When -------------------------------------------------------------------


@when("I run the scheduled job runner")
def run_the_runner(
    client: ZeroDBClient, jobs: dict[str, JobRegistration], report: dict[str, Any]
) -> None:
    runner = ScheduledJobRunner(client, list(jobs.values()))
    report["result"] = runner.run(now=_NOW)


# --- Then: individual job outcomes ------------------------------------------


def _outcome(report: dict[str, Any], name: str) -> JobOutcome:
    result = report["result"]
    matches = [o for o in result.outcomes if o.name == name]
    assert matches, f"no outcome recorded for job {name!r}; got {[o.name for o in result.outcomes]}"
    return matches[0]


@then(parsers.parse('the job "{name}" ran'))
def job_ran(report: dict[str, Any], calls: list[str], name: str) -> None:
    assert _outcome(report, name).status is JobStatus.RAN
    assert name in calls


@then(parsers.parse('the job "{name}" did not run'))
def job_did_not_run(calls: list[str], name: str) -> None:
    assert name not in calls


@then(parsers.parse('the job "{name}" has a recorded last-run timestamp'))
def job_has_last_run(client: ZeroDBClient, name: str) -> None:
    row = client.get_row("scheduled_jobs", name)
    assert row is not None
    assert row.row_data.get("last_run_at")


@then(parsers.parse('the job "{name}" has no recorded last-run timestamp'))
def job_has_no_last_run(client: ZeroDBClient, name: str) -> None:
    row = client.get_row("scheduled_jobs", name)
    assert row is None or not row.row_data.get("last_run_at")


@then(parsers.parse('the runner reports the job "{name}" as skipped, not due yet'))
def job_reported_skipped(report: dict[str, Any], name: str) -> None:
    assert _outcome(report, name).status is JobStatus.SKIPPED_NOT_DUE


@then(parsers.parse('the runner reports the job "{name}" as failed'))
def job_reported_failed(report: dict[str, Any], name: str) -> None:
    assert _outcome(report, name).status is JobStatus.FAILED


@then(parsers.parse('the runner reports the job "{name}" as blocked, missing credentials'))
def job_reported_blocked(report: dict[str, Any], name: str) -> None:
    outcome = _outcome(report, name)
    assert outcome.status is JobStatus.BLOCKED
    assert outcome.detail


@then("the runner's overall exit code is nonzero")
def runner_exit_code_nonzero(report: dict[str, Any]) -> None:
    result = report["result"]
    assert not result.ok
    assert result.failed


# --- Then: the real registry ------------------------------------------------


@then(parsers.parse('the real job registry includes a job named "{name}"'))
def real_registry_includes(report: dict[str, Any], name: str) -> None:
    jobs_by_name = {job.name: job for job in default_jobs()}
    assert name in jobs_by_name, f"{name!r} not in real registry: {sorted(jobs_by_name)}"
    report["matched_job"] = jobs_by_name[name]


@then(parsers.parse("that job's interval is {count:d} day(s)"))
def real_job_interval_days(report: dict[str, Any], count: int) -> None:
    assert report["matched_job"].interval == timedelta(days=count)


@then(parsers.parse("that job's interval is {count:d} hour(s)"))
def real_job_interval_hours(report: dict[str, Any], count: int) -> None:
    assert report["matched_job"].interval == timedelta(hours=count)
