#!/usr/bin/env python3
"""Verify `/metrics` against the LIVE "informed" ZeroDB project (story 16.5).

Starts the real FastAPI app in-process (no network bind needed — a
`TestClient` is enough to drive real request/response cycles through the
real ASGI stack, including `MetricsMiddleware`), makes a few real HTTP
requests, then scrapes `/metrics` and prints it. Confirms two things that
cannot be proven by the in-memory-fake test suite alone:

  1. the request-metrics counters/histogram reflect requests actually just
     made, in a real running app instance;
  2. the ingest-lag gauges are computed from the REAL `scheduled_jobs` table
     in the live "informed" project — not a fixture — via a real
     `ZeroDBClient.from_env()`.

Nothing is written to ZeroDB by this script: `/metrics`'s ingest-lag read is
read-only (`get_row` against `scheduled_jobs`), so there is no cleanup step,
unlike `verify_integrity_checks_live.py`'s synthetic-row dance.

Run it with the live credentials sourced into the shell, never passed on the
command line or printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_monitoring_and_tracing_live.py

See this story's PR for the captured output.
"""

from __future__ import annotations

import contextlib

from fastapi.testclient import TestClient

from app.main import create_app
from app.scheduling.jobs import default_jobs
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError


def main() -> int:
    # Confirms ZERODB_* really is configured before anything else: a clearer
    # failure here than letting /metrics silently degrade to request-only.
    try:
        client = ZeroDBClient.from_env()
    except Exception as exc:  # noqa: BLE001 - config validation, report plainly
        print(f"FAIL: could not build a ZeroDBClient from the environment: {exc}")
        return 1

    print(f"Connected to ZeroDB project: {client.project_id!r}")

    app = create_app(rate_limit=False)
    with TestClient(app) as http:
        health_response = http.get("/health")
        print(f"GET /health -> {health_response.status_code}")

        topics_response = http.get("/v1/topics")
        print(f"GET /v1/topics -> {topics_response.status_code} (ZeroDB-backed read)")

        metrics_response = http.get("/metrics")
        print(f"GET /metrics -> {metrics_response.status_code}")

    if metrics_response.status_code != 200:
        print("FAIL: /metrics did not return 200")
        return 1

    body = metrics_response.text
    print("\n--- /metrics output (request-metrics portion) ---")
    for line in body.splitlines():
        if line.startswith(("http_", "# HELP http", "# TYPE http")):
            print(line)

    if 'route="/health"' not in body:
        print("FAIL: /metrics does not reflect the /health request just made")
        return 1
    print("\nPASS: /metrics reflects the real requests just made.")

    print("\n--- /metrics output (ingest-lag portion, read from the live scheduled_jobs table) ---")
    lag_lines = [
        line for line in body.splitlines() if line.startswith(("job_", "# HELP job", "# TYPE job"))
    ]
    for line in lag_lines:
        print(line)

    job_names = {job.name for job in default_jobs()}
    reported_jobs = {
        line.split('job="')[1].split('"')[0]
        for line in lag_lines
        if line.startswith("job_ingest_lag_over_threshold{")
    }
    if reported_jobs != job_names:
        print(
            f"FAIL: ingest-lag gauges cover {reported_jobs!r}, expected every "
            f"registered job {job_names!r}"
        )
        return 1
    print(
        f"\nPASS: ingest-lag gauges were computed live for all {len(job_names)} "
        "registered jobs against the real scheduled_jobs table."
    )

    with contextlib.suppress(ZeroDBError):
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
