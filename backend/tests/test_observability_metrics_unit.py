"""Unit tests for `app.observability.metrics` (story 16.5).

The BDD feature exercises `/metrics` through the real app; these pin the
registry and renderer's own arithmetic and format details that a request-
level test cannot see clearly — bucket boundaries, label escaping, the
cardinality guard.
"""

from __future__ import annotations

from app.observability.metrics import (
    DEFAULT_BUCKETS,
    MAX_LABEL_COMBINATIONS,
    MetricsRegistry,
    render_prometheus_text,
)


def test_a_single_request_is_reflected_in_both_series() -> None:
    registry = MetricsRegistry()
    registry.record_request(method="GET", route="/health", status=200, duration_seconds=0.01)

    body = render_prometheus_text(registry)

    assert 'http_requests_total{method="GET",route="/health",status="200"} 1' in body
    assert "http_request_duration_seconds_count" in body
    assert 'method="GET",route="/health"' in body


def test_counts_accumulate_across_multiple_requests_to_the_same_label_set() -> None:
    registry = MetricsRegistry()
    for _ in range(3):
        registry.record_request(method="GET", route="/health", status=200, duration_seconds=0.01)

    body = render_prometheus_text(registry)
    assert 'http_requests_total{method="GET",route="/health",status="200"} 3' in body


def test_different_statuses_on_the_same_route_are_separate_series() -> None:
    registry = MetricsRegistry()
    registry.record_request(method="GET", route="/v1/topics", status=200, duration_seconds=0.01)
    registry.record_request(method="GET", route="/v1/topics", status=500, duration_seconds=0.01)

    body = render_prometheus_text(registry)
    assert 'status="200"' in body
    assert 'status="500"' in body


def test_a_value_at_a_bucket_boundary_is_counted_in_that_bucket() -> None:
    registry = MetricsRegistry(buckets=(0.1, 0.5))
    registry.record_request(method="GET", route="/x", status=200, duration_seconds=0.1)

    counters, histograms = registry.snapshot()
    histogram = histograms[("GET", "/x")]
    assert histogram.bucket_counts == [1, 1]  # le=0.1 AND le=0.5 both include it
    assert histogram.count == 1
    assert histogram.sum_seconds == 0.1


def test_the_plus_inf_bucket_is_rendered_and_equals_the_total_count() -> None:
    registry = MetricsRegistry(buckets=(0.01,))
    registry.record_request(method="GET", route="/x", status=200, duration_seconds=99.0)

    body = render_prometheus_text(registry)
    assert 'le="+Inf"} 1' in body
    # The observation exceeded every finite bucket.
    assert 'le="0.01"} 0' in body


def test_default_buckets_are_sorted_ascending() -> None:
    assert list(DEFAULT_BUCKETS) == sorted(DEFAULT_BUCKETS)


def test_a_label_value_containing_a_quote_is_escaped() -> None:
    registry = MetricsRegistry()
    registry.record_request(method="GET", route='/weird"route', status=200, duration_seconds=0.01)

    body = render_prometheus_text(registry)
    assert '/weird\\"route' in body
    # The raw, unescaped quote must never appear bare inside a label value.
    assert '"/weird"route"' not in body


def test_an_empty_registry_still_renders_valid_help_and_type_lines() -> None:
    registry = MetricsRegistry()
    body = render_prometheus_text(registry)
    assert "# HELP http_requests_total" in body
    assert "# TYPE http_requests_total counter" in body
    assert "# HELP http_request_duration_seconds" in body
    assert "# TYPE http_request_duration_seconds histogram" in body


def test_label_combinations_are_capped_so_a_scrape_storm_cannot_grow_without_bound() -> None:
    registry = MetricsRegistry()
    # One more than the cap: distinct status codes on the same route/method
    # each create a new counter label combination.
    for status in range(200, 200 + MAX_LABEL_COMBINATIONS + 5):
        registry.record_request(method="GET", route="/x", status=status, duration_seconds=0.001)

    counters, _ = registry.snapshot()
    assert len(counters) <= MAX_LABEL_COMBINATIONS


def test_snapshot_is_a_copy_not_a_live_reference() -> None:
    registry = MetricsRegistry()
    registry.record_request(method="GET", route="/x", status=200, duration_seconds=0.01)
    counters, histograms = registry.snapshot()

    registry.record_request(method="GET", route="/x", status=200, duration_seconds=0.01)

    # The earlier snapshot must not have been mutated by the later call.
    assert counters[("GET", "/x", "200")] == 1
    assert histograms[("GET", "/x")].count == 1
