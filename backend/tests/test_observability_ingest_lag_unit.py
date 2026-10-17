"""Unit tests for `app.observability.ingest_lag` (story 16.5).

The BDD feature exercises the headline scenarios through pytest-bdd; these
pin edge cases that are awkward to phrase as Gherkin: exact-boundary timing,
multiple jobs in one call, and the Prometheus rendering shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.observability.ingest_lag import (
    INGEST_LAG_ALERT_THRESHOLD_SECONDS,
    compute_ingest_lag,
    render_ingest_lag_prometheus_text,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def client() -> ZeroDBClient:
    zdb = ZeroDBClient(transport=FakeZeroDBTransport(), project_id="proj-test")
    zdb.create_table("scheduled_jobs", {"id": "string", "last_run_at": "string"})
    return zdb


def _seed(client: ZeroDBClient, job_name: str, last_run: datetime) -> None:
    last_run_at = last_run.isoformat().replace("+00:00", "Z")
    client.upsert_row("scheduled_jobs", job_name, {"id": job_name, "last_run_at": last_run_at})


def test_a_job_exactly_at_the_threshold_is_not_over(client: ZeroDBClient) -> None:
    _seed(client, "job-a", NOW - timedelta(seconds=INGEST_LAG_ALERT_THRESHOLD_SECONDS))
    [report] = compute_ingest_lag(client, ["job-a"], now=NOW)
    assert report.lag_seconds == INGEST_LAG_ALERT_THRESHOLD_SECONDS
    assert report.over_threshold is False  # strictly greater-than, not >=


def test_a_job_one_second_past_the_threshold_is_over(client: ZeroDBClient) -> None:
    _seed(client, "job-a", NOW - timedelta(seconds=INGEST_LAG_ALERT_THRESHOLD_SECONDS + 1))
    [report] = compute_ingest_lag(client, ["job-a"], now=NOW)
    assert report.over_threshold is True


def test_a_job_that_just_succeeded_has_near_zero_lag(client: ZeroDBClient) -> None:
    _seed(client, "job-a", NOW)
    [report] = compute_ingest_lag(client, ["job-a"], now=NOW)
    assert report.lag_seconds == 0
    assert report.over_threshold is False


def test_multiple_jobs_are_each_reported_independently_in_order(client: ZeroDBClient) -> None:
    _seed(client, "job-a", NOW - timedelta(hours=1))
    _seed(client, "job-b", NOW - timedelta(hours=10))
    reports = compute_ingest_lag(client, ["job-a", "job-b"], now=NOW)
    assert [r.job_name for r in reports] == ["job-a", "job-b"]
    assert reports[0].over_threshold is False
    assert reports[1].over_threshold is True


def test_defaults_to_the_real_clock_when_now_is_not_given(client: ZeroDBClient) -> None:
    five_minutes_ago = datetime.now(UTC) - timedelta(minutes=5)
    _seed(client, "job-a", five_minutes_ago)
    [report] = compute_ingest_lag(client, ["job-a"])
    assert report.lag_seconds is not None
    # Approximately 5 minutes (300s), with generous slack for test overhead —
    # proves "now" defaulted to the real clock rather than, say, epoch zero.
    assert 290 <= report.lag_seconds < 310


# --- Prometheus rendering ----------------------------------------------------


def test_rendered_gauges_include_both_series_with_job_labels(client: ZeroDBClient) -> None:
    _seed(client, "job-a", NOW - timedelta(hours=1))
    reports = compute_ingest_lag(client, ["job-a", "never-run"], now=NOW)

    body = render_ingest_lag_prometheus_text(reports)

    assert "# TYPE job_last_success_timestamp_seconds gauge" in body
    assert 'job_last_success_timestamp_seconds{job="job-a"}' in body
    # A job that has never run has no timestamp line at all.
    assert 'job_last_success_timestamp_seconds{job="never-run"}' not in body
    assert "# TYPE job_ingest_lag_over_threshold gauge" in body
    assert 'job_ingest_lag_over_threshold{job="job-a"} 0' in body
    assert 'job_ingest_lag_over_threshold{job="never-run"} 0' in body


def test_an_over_threshold_job_renders_a_1(client: ZeroDBClient) -> None:
    _seed(client, "stuck-job", NOW - timedelta(hours=7))
    reports = compute_ingest_lag(client, ["stuck-job"], now=NOW)
    body = render_ingest_lag_prometheus_text(reports)
    assert 'job_ingest_lag_over_threshold{job="stuck-job"} 1' in body
