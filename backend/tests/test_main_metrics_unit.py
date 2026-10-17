"""Unit tests for `GET /metrics`'s own wiring in `app.main` (story 16.5).

The BDD feature covers the happy path (a configured, reachable ZeroDB client)
through the real app. These pin the two degraded paths `/metrics`'s own
docstring promises: no ZeroDB configured at all, and a ZeroDB client that
raises while being read. Neither must ever turn a scrape into a 500 — see
`app.main.zerodb_client_for_metrics` and the `/metrics` handler's own
docstrings for why.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app, zerodb_client_for_metrics
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError
from app.zerodb.testing import FakeZeroDBTransport


def test_zerodb_client_for_metrics_returns_none_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("ZERODB_API_KEY", "ZERODB_PROJECT_ID"):
        monkeypatch.delenv(var, raising=False)
    assert zerodb_client_for_metrics() is None


def test_metrics_still_returns_200_with_request_series_when_zerodb_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("ZERODB_API_KEY", "ZERODB_PROJECT_ID"):
        monkeypatch.delenv(var, raising=False)
    app = create_app(rate_limit=False)
    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "http_requests_total" in response.text
    # No ingest-lag gauges at all when there is no client to read them from —
    # distinct from "a job that never ran", which still gets a 0 line.
    assert "job_ingest_lag_over_threshold" not in response.text


def test_metrics_still_returns_200_when_ingest_lag_itself_raises() -> None:
    transport = FakeZeroDBTransport()
    transport.always_fail(status=503, exception=ZeroDBError("simulated outage", status_code=503))
    broken_client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )

    app = create_app(rate_limit=False)
    app.dependency_overrides[zerodb_client_for_metrics] = lambda: broken_client

    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "http_requests_total" in response.text
    assert "job_ingest_lag_over_threshold" not in response.text
