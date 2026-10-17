"""Per-job ingest lag: "time since last successful run" (story 16.5).

"Dashboards for ingest lag" and "alerts on ingest lag over 6 hours" are this
story's acceptance criterion. `app.scheduling.registry.ScheduledJobRunner`
already keeps exactly the bookkeeping an ingest-lag metric needs — one row
per job in `scheduled_jobs`, holding `last_run_at`, updated only on success
(see that module's docstring). This module is the thin read-and-compute layer
on top of it: for every job this app knows about
(`app.scheduling.jobs.default_jobs`), how long ago did it last succeed, and
is that over the alert threshold.

## What this is not

There is no deployed alerting pipeline in this codebase — no PagerDuty, no
on-call rotation, no webhook that fires when a threshold is crossed. That is
the same honest gap every other Epic 16 story already names for its own
paging/ticketing dependency (see `app.integrity.checks`'s module docstring:
"the honest gap around paging/ticketing this does NOT do"). What this module
builds is the METRIC a real alerting system would evaluate once one exists:
`job_last_success_timestamp` as a gauge on `/metrics`, and `IngestLagReport`
as the same computation available to a script or a future alerting
integration without needing to scrape the app's own `/metrics` output to get
it. `docs/observability.md` names the panel and the alert rule this is for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.scheduling.registry import last_success_at
from app.zerodb.client import ZeroDBClient

#: "Alerts on ingest lag over 6 hours" — the acceptance criterion's own
#: number, not a guess. A job whose own interval is longer than 6 hours
#: (`load_candidacies`, weekly; `sweep_correction_emails`, daily) is still
#: measured against this one threshold rather than its own interval, because
#: the acceptance criterion names a single, fixed number — and because a
#: per-job threshold would need its own product decision (how much slack
#: past a weekly job's interval is "fine" vs "stuck") that is out of scope
#: here. `docs/observability.md` flags this as worth revisiting once a real
#: alerting system exists and per-job thresholds become cheap to configure.
INGEST_LAG_ALERT_THRESHOLD_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class IngestLagReport:
    """One job's ingest-lag reading.

    Attributes:
        job_name: the `JobRegistration.name` this reading is for.
        last_success_at: when the job last recorded a success, or `None` if
            it has never run — a brand-new job, or a project whose
            `scheduled_jobs` table has never been bootstrapped. `None` is
            deliberately NOT reported as "over threshold": a job that has
            literally never run is a different, and arguably more urgent,
            condition than one that ran and then stalled, and collapsing the
            two into the same alert would hide which one actually happened.
        lag_seconds: seconds between `last_success_at` and the reference
            "now" this report was computed against, or `None` to match
            `last_success_at`.
        over_threshold: `lag_seconds is not None and lag_seconds >
            INGEST_LAG_ALERT_THRESHOLD_SECONDS`.
    """

    job_name: str
    last_success_at: datetime | None
    lag_seconds: float | None
    over_threshold: bool


def compute_ingest_lag(
    client: ZeroDBClient, job_names: list[str], *, now: datetime | None = None
) -> list[IngestLagReport]:
    """One `IngestLagReport` per name in `job_names`, in the same order.

    `job_names` is passed explicitly rather than read from
    `app.scheduling.jobs.default_jobs()` internally, so a test can compute
    lag for one synthetic job name without needing every real job's
    preconditions (an FEC key, a Congress.gov key) to be satisfiable. The
    live `/metrics` wiring passes `[job.name for job in default_jobs()]`.
    """
    moment = now or datetime.now(UTC)
    reports: list[IngestLagReport] = []
    for job_name in job_names:
        last_success = last_success_at(client, job_name)
        if last_success is None:
            reports.append(
                IngestLagReport(
                    job_name=job_name,
                    last_success_at=None,
                    lag_seconds=None,
                    over_threshold=False,
                )
            )
            continue
        lag_seconds = (moment - last_success).total_seconds()
        reports.append(
            IngestLagReport(
                job_name=job_name,
                last_success_at=last_success,
                lag_seconds=lag_seconds,
                over_threshold=lag_seconds > INGEST_LAG_ALERT_THRESHOLD_SECONDS,
            )
        )
    return reports


def render_ingest_lag_prometheus_text(reports: list[IngestLagReport]) -> str:
    """`reports` as Prometheus gauge lines for `/metrics`.

    Two gauges, both labelled by `job`:

      * `job_last_success_timestamp_seconds` — Unix epoch seconds of the
        last success, the raw value a dashboard panel plots over time
        (`docs/observability.md`'s "ingest lag per job" panel).
      * `job_ingest_lag_over_threshold` — 1 if this job is over the 6-hour
        alert threshold, 0 otherwise; the value a real alerting rule would
        key on once one exists (`ALERT ingest_lag_too_high IF
        job_ingest_lag_over_threshold == 1`, in Prometheus's own alerting
        rule syntax).

    A job that has never run emits neither line for
    `job_last_success_timestamp_seconds` (there is no timestamp to report)
    but DOES emit `job_ingest_lag_over_threshold{job="..."} 0` — "never run"
    is reported as not-yet-over-threshold here (see `IngestLagReport`'s own
    docstring for why that case is deliberately distinguished rather than
    folded into "over threshold"), while still being visible on the
    dashboard as a job with no last-success point plotted at all, which is
    its own clear signal to an operator reading the panel.
    """
    lines = [
        "# HELP job_last_success_timestamp_seconds Unix timestamp of each job's last success.",
        "# TYPE job_last_success_timestamp_seconds gauge",
    ]
    for report in reports:
        if report.last_success_at is not None:
            epoch_seconds = report.last_success_at.timestamp()
            lines.append(
                f'job_last_success_timestamp_seconds{{job="{report.job_name}"}} {epoch_seconds}'
            )

    lines.append(
        "# HELP job_ingest_lag_over_threshold "
        f"1 if a job's ingest lag exceeds {INGEST_LAG_ALERT_THRESHOLD_SECONDS} seconds, else 0."
    )
    lines.append("# TYPE job_ingest_lag_over_threshold gauge")
    for report in reports:
        value = 1 if report.over_threshold else 0
        lines.append(f'job_ingest_lag_over_threshold{{job="{report.job_name}"}} {value}')

    return "\n".join(lines) + "\n"


__all__ = [
    "INGEST_LAG_ALERT_THRESHOLD_SECONDS",
    "IngestLagReport",
    "compute_ingest_lag",
    "render_ingest_lag_prometheus_text",
]
