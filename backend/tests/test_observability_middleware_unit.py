"""Unit tests for `app.observability.middleware.MetricsMiddleware` (16.5).

The BDD feature drives this through the real app; these pin the route-
resolution and cardinality-bounding behaviour at the ASGI layer directly,
the same split `test_ratelimit_unit.py` makes for `RateLimitMiddleware`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.observability.metrics import MetricsRegistry
from app.observability.middleware import UNMATCHED_ROUTE_LABEL, MetricsMiddleware
from app.stances.router import zerodb_client_for_stances
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport


def test_a_declared_route_is_recorded_under_its_own_template() -> None:
    registry = MetricsRegistry()
    app = create_app(rate_limit=False)
    app.add_middleware(MetricsMiddleware, registry=registry)
    # create_app() already added its own MetricsMiddleware instance around
    # `metrics_registry`; adding a second, outer one bound to `registry` lets
    # this test observe the ASGI behaviour directly without reaching into
    # app internals, at the cost of one extra (harmless) layer of timing.
    with TestClient(app) as client:
        client.get("/health")

    counters, _ = registry.snapshot()
    assert ("GET", "/health", "200") in counters


def test_a_404_on_an_unknown_path_is_folded_to_the_unmatched_label() -> None:
    registry = MetricsRegistry()
    app = create_app(rate_limit=False)
    app.add_middleware(MetricsMiddleware, registry=registry)
    with TestClient(app) as client:
        client.get("/this/path/does/not/exist")

    counters, _ = registry.snapshot()
    assert (
        "GET",
        UNMATCHED_ROUTE_LABEL,
        "404",
    ) in counters
    # Critically, the unbounded concrete path itself never becomes a label.
    assert not any(route == "/this/path/does/not/exist" for _, route, _ in counters)


def test_many_distinct_unknown_paths_collapse_to_one_counter() -> None:
    registry = MetricsRegistry()
    app = create_app(rate_limit=False)
    app.add_middleware(MetricsMiddleware, registry=registry)
    with TestClient(app) as client:
        for i in range(25):
            client.get(f"/scrape-probe-{i}")

    counters, _ = registry.snapshot()
    assert counters[("GET", UNMATCHED_ROUTE_LABEL, "404")] == 25


def test_a_parameterised_route_is_recorded_under_its_template_not_the_concrete_path() -> None:
    registry = MetricsRegistry()
    app = create_app(rate_limit=False)
    app.add_middleware(MetricsMiddleware, registry=registry)
    fake_client = ZeroDBClient(transport=FakeZeroDBTransport(), project_id="proj-test")
    fake_client.create_table("stances", {"id": "string"})
    app.dependency_overrides[zerodb_client_for_stances] = lambda: fake_client
    with TestClient(app) as client:
        client.get("/v1/topics/some-topic-id/stances")

    counters, _ = registry.snapshot()
    assert any(route == "/v1/topics/{topic_id}/stances" for _, route, _ in counters)
    assert not any(route == "/v1/topics/some-topic-id/stances" for _, route, _ in counters)


def test_lifespan_scopes_pass_through_untouched() -> None:
    registry = MetricsRegistry()
    app = create_app(rate_limit=False)
    app.add_middleware(MetricsMiddleware, registry=registry)
    # Starting/stopping the TestClient exercises the ASGI lifespan protocol;
    # it must not raise and must not be recorded as an HTTP request.
    with TestClient(app):
        pass
    counters, _ = registry.snapshot()
    assert counters == {}
