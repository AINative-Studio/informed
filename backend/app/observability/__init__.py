"""Monitoring and tracing infrastructure (story 16.5).

"As an operator, I want logs, metrics and traces across API, agents and
ZeroDB calls, so that I can find problems fast." The acceptance criterion
names dashboards (ingest lag, error rate, p95 latency) and an alert on ingest
lag over 6 hours. This codebase has no deployed server and no staging
environment (story 1.5/1.6's own findings; docs/environments.md: "deployment
targets: none provisioned yet"), so a literal Grafana/alerting deployment is
not a thing application code can honestly claim to have built. What IS built,
and what the acceptance criterion's substance actually is at this project's
current infra reality:

  * `logging_config` — one JSON structured-log formatter and configurator for
    the whole app, with a `PlaintextLeakGuard` filter that is the automated,
    repo-wide generalisation of story 14.1's "plaintext is never logged"
    feature test. Every logger in this codebase goes through it once
    `configure_logging()` runs.
  * `metrics` — an in-process Prometheus text-exposition registry, hand-rolled
    (no new dependency; see that module's docstring for why) with one counter
    (requests by method/route/status) and one histogram (request duration).
  * `middleware` — the ASGI middleware that records those two series on every
    request, in the same pure-ASGI style as `app.ratelimit.middleware`.
  * `ingest_lag` — "seconds since last success" per registered scheduling job,
    read from the same `scheduled_jobs` table `app.scheduling.registry`
    already maintains, exposed as a gauge and flagged against the 6-hour
    threshold the acceptance criterion names.

See `docs/observability.md` for the dashboard panels an operator should build
against these series once real infra exists, and for exactly what is and is
not covered by "alerts on ingest lag" at this story's current scope.
"""

from app.observability.ingest_lag import (
    INGEST_LAG_ALERT_THRESHOLD_SECONDS,
    IngestLagReport,
    compute_ingest_lag,
    render_ingest_lag_prometheus_text,
)
from app.observability.logging_config import PlaintextLeakGuard, configure_logging
from app.observability.metrics import MetricsRegistry, render_prometheus_text
from app.observability.middleware import MetricsMiddleware

__all__ = [
    "INGEST_LAG_ALERT_THRESHOLD_SECONDS",
    "IngestLagReport",
    "MetricsMiddleware",
    "MetricsRegistry",
    "PlaintextLeakGuard",
    "compute_ingest_lag",
    "configure_logging",
    "render_ingest_lag_prometheus_text",
    "render_prometheus_text",
]
