# Observability: logs, metrics and ingest lag

Story: `16.5-monitoring-tracing` (Epic 16, 5 pts, Should, Iteration 6).

> As an operator, I want logs, metrics and traces across API, agents and
> ZeroDB calls, so that I can find problems fast.

Acceptance: Dashboards for ingest lag, error rate and p95 latency; alerts on
ingest lag over 6 hours.

This doc does two things: it documents what this story actually built, and it
specifies exactly what an operator should build in a real monitoring stack
once one is deployed. See "What this story does not do" below for why the
second half is a specification rather than a working dashboard.

## What exists today

### 1. Structured logging (`app.observability.logging_config`)

Every logger in the process goes through one JSON formatter once
`configure_logging()` runs (called from the app's entrypoint — see
`backend/app/main.py` and `backend/scripts/run_scheduled_jobs.py`). One line
per log record, with fixed keys (`timestamp`, `level`, `logger`, `message`)
plus whatever `extra={...}` a call site adds. Two conventions worth using in
new code:

- `extra={"operation": "load_house_rollcalls", "duration_ms": 340}` — makes
  "which operation, how long" a queryable field rather than free text buried
  in `message`.
- `extra={"story": "16.2"}` — tags which story's code produced a line, useful
  when triaging a log stream that spans many features.

Neither is required; most of this codebase's existing log lines are fine
without them.

**The plaintext guard.** `PlaintextLeakGuard`, attached to the root logger's
handler, redacts anything secret-shaped (an `Authorization:`/`api_key=`
header value, or a bare run of 32+ base64/hex characters) before a line is
ever rendered — independent of knowing the specific secret in advance. This
is belt-and-suspenders on top of the existing, more important discipline:
voter interests and free-text input are simply never passed to a logger
anywhere in this codebase (see `app.profiles.service`'s log lines — a version
number and a key reference, nothing from the decrypted profile). The
`PlaintextLeakGuard.contains_forbidden` helper is what
`tests/features/monitoring_and_tracing.feature`'s "no plaintext voter data
appears in any log line across a full request cycle" scenario uses to prove
that discipline holds through a real `POST /v1/profiles` → `GET /v1/profiles`
HTTP cycle, not just at the service layer story 14.1 originally checked.

**Extending the guard.** A new feature that logs something sensitive-shaped
should add its own known values to that same BDD scenario's forbidden list,
the same way this story generalised 14.1's check — this is the repo-wide
convention the acceptance criterion asked for, not a one-story fixture.

### 2. Request-level metrics (`app.observability.metrics`, `.middleware`)

`GET /metrics` returns Prometheus text exposition format
(`text/plain; version=0.0.4`), hand-rolled rather than via `prometheus-client`
— see `app/observability/metrics.py`'s module docstring for the full
reasoning (no multiprocess story needed yet; the format is small enough to
own, the same judgment this codebase already made for its rate limiter over
`slowapi`).

Two series, always present:

```
# TYPE http_requests_total counter
http_requests_total{method="GET",route="/v1/topics",status="200"} 42

# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{method="GET",route="/v1/topics",le="0.1"} 40
http_request_duration_seconds_bucket{method="GET",route="/v1/topics",le="+Inf"} 42
http_request_duration_seconds_sum{method="GET",route="/v1/topics"} 1.84
http_request_duration_seconds_count{method="GET",route="/v1/topics"} 42
```

`route` is the matched FastAPI template, never a raw concrete path — an
unmatched path (a 404 probe, a scraper) is folded to the single label
`unmatched` so a probe storm cannot grow the registry's cardinality (see
`app.observability.middleware`'s "Cardinality" section). `MetricsMiddleware`
is mounted as the outermost layer of the stack (added last in
`create_app`'s `add_middleware` calls — Starlette applies them in reverse),
so it also counts and times a rate-limiter refusal or a CORS preflight, not
just requests that reached a handler.

`/metrics` itself is in `app.ratelimit.policy.UNLIMITED_PATHS`, the same list
`/health` is in and for the same reason: throttling the thing an operator
reads during an incident is exactly backwards.

**Known limitation, stated plainly (matching the rate limiter's own
disclosure):** the registry is one process's in-memory dict. Multiple
instances or workers each report their own counters; a restart resets them to
zero. The conversation to reopen if this app ever runs more than one process
is the same one `app.ratelimit.middleware` names for its own counters: a
shared store behind the same seam (`MetricsRegistry`).

### 3. Ingest lag per job (`app.observability.ingest_lag`)

For every job `app.scheduling.jobs.default_jobs()` registers, `/metrics` also
exposes, read live from the same `scheduled_jobs` table
`ScheduledJobRunner` already maintains:

```
# TYPE job_last_success_timestamp_seconds gauge
job_last_success_timestamp_seconds{job="load_house_rollcalls"} 1760702400.0

# TYPE job_ingest_lag_over_threshold gauge
job_ingest_lag_over_threshold{job="load_house_rollcalls"} 0
job_ingest_lag_over_threshold{job="load_candidacies"} 1
```

`job_ingest_lag_over_threshold` is 1 whenever that job's lag exceeds
`INGEST_LAG_ALERT_THRESHOLD_SECONDS` (21,600 — the acceptance criterion's own
"6 hours", applied uniformly to every job rather than per-job intervals; see
that constant's own docstring for why). A job that has **never** run emits no
`job_last_success_timestamp_seconds` line at all (there is no timestamp to
report) but does emit `job_ingest_lag_over_threshold{job="..."} 0` — "never
run" is deliberately a different, more visible condition on a dashboard (a
gap in the plotted line) than "ran once, then stalled" (a flat line past the
threshold).

This computation degrades gracefully: if ZeroDB is unreachable, or no
`ZERODB_*` credential is configured in this environment at all, `/metrics`
still returns the request-level series above and simply omits the ingest-lag
gauges, logging a warning rather than 500ing the whole scrape.

## What a real dashboard should build, once real infra exists

Story 1.5/1.6 found no deployed server, no staging environment, and no
provisioned monitoring stack (`docs/environments.md`, "Deployment targets:
none provisioned yet"). The acceptance criterion's "dashboards" cannot be a
real Grafana/Datadog deployment until that infrastructure exists — this
section is the specification an operator follows the day it does, written
precisely enough that building the dashboard is a configuration task, not a
design one.

### Panel: error rate

```promql
sum(rate(http_requests_total{status=~"5.."}[5m]))
  /
sum(rate(http_requests_total[5m]))
```

Per-route breakdown: add `by (route)` to both sums. Suggested alert: fire
when this exceeds 5% sustained for 5 minutes — a starting number, not a
measured one; revisit once real traffic volume exists to base a threshold on.

### Panel: p95 (and p99) latency

```promql
histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, route))
```

Swap `0.95` for `0.99` for the second line on the same panel. Break out by
`route` so a single slow endpoint is visible rather than averaged away by
every fast one.

### Panel: ingest lag per job

Plot `job_last_success_timestamp_seconds{job="..."}` directly — a dashboard
tool computes "how long ago" from a timestamp series itself (Grafana's
`time() - <series>`), which is why the gauge is a timestamp and not a
pre-computed duration: a duration gauge only updates when scraped, so its
value is wrong between scrapes; a timestamp gauge is always correct read
against "now". One line per job, so a stalled job is visually a flat line
while the others keep climbing with wall-clock time.

### Alert: ingest lag over 6 hours

```promql
job_ingest_lag_over_threshold == 1
```

This is the metric the acceptance criterion's alert fires on. **No alerting
pipeline exists to evaluate this query today** — no Alertmanager, no
PagerDuty, no on-call rotation. That is the same honest gap every other
Epic 16 story already names for its own paging/ticketing dependency (see
`app.integrity.checks`'s module docstring). What this story built is the
metric a real alert rule would key on; wiring the rule itself is
infrastructure work for whoever stands up the monitoring stack, not an
application-code change.

### Panel: ZeroDB call health

Not built in this story: there is no per-ZeroDB-call metric today (only the
per-HTTP-request one). A follow-up could wrap `ZeroDBClient`'s own request
method with the same `MetricsRegistry.record_request`-shaped call, labelled
by ZeroDB operation (`query_rows`, `upsert_row`, ...) instead of by HTTP
route, which would make this project's actual external dependency visible on
its own panel rather than folded into whichever endpoint happened to call it.

## Tracing

The acceptance criterion also names traces "across API, agents and ZeroDB
calls." No distributed tracing (OpenTelemetry spans, a trace exporter) exists
in this codebase, and none was added by this story: there is exactly one
service in this architecture today (the FastAPI backend calls ZeroDB
directly; the AINative Agent Swarm referenced in `prd.md`'s architecture
section is not yet built per the backlog), so a distributed trace has nothing
to connect across process boundaries that structured logging's
request-scoped context does not already give for free. Revisit once the
agent swarm is a real, separately-deployed caller — at that point a trace id
propagated through `app.observability.logging_config`'s `extra` fields (a
`trace_id` key, the same mechanism `story`/`operation` already use) is the
natural next step, correlated across both services' logs without needing a
tracing backend before one is provisioned.

## Verification against the live "informed" ZeroDB project

Run during this story, using `local.env` credentials sourced into the shell
and never printed or committed:

```bash
cd backend
source ../local.env   # ZERODB_WORKSPACE_ID / ZERODB_PROJECT_ID / ZERODB_API_KEY
.venv/bin/uvicorn app.main:app --port 8000 &
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/v1/topics > /dev/null   # generates a real request-metrics sample
curl -s http://127.0.0.1:8000/metrics
```

See this story's PR for the actual captured output: real
`http_requests_total`/`http_request_duration_seconds` lines reflecting the
requests just made, and real `job_last_success_timestamp_seconds` /
`job_ingest_lag_over_threshold` lines read live from the "informed" project's
own `scheduled_jobs` table.
