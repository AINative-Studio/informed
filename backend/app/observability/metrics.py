"""A minimal, hand-rolled Prometheus text-exposition registry (story 16.5).

## Why hand-rolled rather than `prometheus-client`

`prometheus-client` is the obvious dependency and was the first thing
considered. It was not taken, for the same shape of reason
`app.ratelimit.middleware`'s own docstring gives for not taking `slowapi`:

  * **The format is small enough to own.** Prometheus's text exposition
    format (https://prometheus.io/docs/instrumenting/exposition_formats/) is
    `# HELP`, `# TYPE`, then `name{label="value", ...} value` lines. A
    counter and a histogram — the two series this story needs — are perhaps
    sixty lines of code including the bucket arithmetic. Pulling in a
    dependency to avoid sixty lines, in a codebase that already hand-rolls
    its rate limiter for the identical reason, would be inconsistent with
    its own stated engineering taste.
  * **No multiprocess story needed.** `prometheus-client`'s main complexity
    is coordinating counters across multiple worker processes (its
    `multiprocess` module, env-var-configured shared directories). This app
    runs as a single process today (`app.ratelimit.middleware`'s own
    docstring: "the deployment ... describes is a single instance"), so that
    whole subsystem would be dead weight.
  * **The registry is one dict, like the rate limiter's one dict of
    buckets.** Consistent with this codebase's existing in-process-only,
    documented-as-such approach to shared state (see that same docstring's
    "The limitation, stated plainly" section) — the same caveat applies
    here and is documented in `docs/observability.md` rather than solved
    with new infrastructure this story does not need.

If this app ever runs more than one process or instance, the conversation to
reopen is the same one `app.ratelimit.middleware` already names for its own
counters: a shared store behind the same seam. `MetricsRegistry` is written
so that seam is this one class, not every call site.

## What is tracked

Two series, which is what "error rate and p95 latency" dashboards are built
from:

  * `http_requests_total{method, route, status}` — a counter. Error rate is
    `rate(http_requests_total{status=~"5.."}[5m]) /
    rate(http_requests_total[5m])` once this is scraped by something that
    can evaluate that query (docs/observability.md spells this out as a
    panel).
  * `http_request_duration_seconds{method, route}` — a histogram with fixed
    buckets. p95 latency is `histogram_quantile(0.95,
    rate(http_request_duration_seconds_bucket[5m]))`.

`route` is the matched FastAPI route template (`/v1/profiles/{user_id}`, not
`/v1/profiles/voter-1`) for the identical reason
`app.ratelimit.middleware._route_path` resolves templates rather than
concrete paths: an unbounded label value is how a metrics registry leaks
memory and how a scraper walking a million ids turns a dashboard into noise.
`MetricsMiddleware` reuses that exact resolution function rather than
reimplementing it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

#: Histogram bucket upper bounds, in seconds. Chosen to resolve the range this
#: app's own endpoints actually occupy: sub-10ms in-process health checks up
#: through a multi-second ZeroDB-backed read, with enough buckets near the
#: p95/p99 range (100ms-2s) that a quantile computed from them is meaningful
#: rather than landing on a bucket boundary that is too coarse to say
#: anything. `+Inf` is implicit, per the exposition format's own convention.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

#: Cap on distinct label combinations held by either series. The same memory
#: discipline as `app.ratelimit.middleware.ROUTE_CACHE_MAX`: the route table
#: is fixed for the life of the process, so this is sized generously above
#: "every real route times every real status code times every real method"
#: and exists only to stop an unbounded label value (which should not be
#: possible once `route` is always a resolved template, but costs nothing to
#: guard anyway) from growing the registry without bound.
MAX_LABEL_COMBINATIONS = 4_096


@dataclass
class _Histogram:
    buckets: tuple[float, ...]
    bucket_counts: list[int] = field(init=False)
    sum_seconds: float = 0.0
    count: int = 0

    def __post_init__(self) -> None:
        self.bucket_counts = [0] * len(self.buckets)

    def observe(self, value: float) -> None:
        self.sum_seconds += value
        self.count += 1
        for i, upper in enumerate(self.buckets):
            if value <= upper:
                self.bucket_counts[i] += 1


class MetricsRegistry:
    """One process's worth of request counters and duration histograms.

    Thread-safe (a `threading.Lock` around every mutation) rather than
    asyncio-safe-by-single-threadedness, because `uvicorn --workers N` or a
    sync endpoint running in Starlette's threadpool both call in from more
    than one OS thread even within "one process" — the rate limiter gets this
    for free from the GIL plus no cross-await mutation of its deque, but a
    dict-of-counters updated with `+=` is not atomic across threads without
    one.
    """

    def __init__(self, *, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self._buckets = buckets
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, str, str], int] = {}
        self._histograms: dict[tuple[str, str], _Histogram] = {}

    def record_request(
        self, *, method: str, route: str, status: int, duration_seconds: float
    ) -> None:
        """Record one completed request's outcome and latency."""
        counter_key = (method, route, str(status))
        histogram_key = (method, route)
        with self._lock:
            if counter_key not in self._counters and len(self._counters) >= MAX_LABEL_COMBINATIONS:
                return  # pragma: no cover - guard against unbounded growth
            self._counters[counter_key] = self._counters.get(counter_key, 0) + 1

            histogram = self._histograms.get(histogram_key)
            if histogram is None:
                if len(self._histograms) >= MAX_LABEL_COMBINATIONS:
                    return  # pragma: no cover - guard against unbounded growth
                histogram = _Histogram(buckets=self._buckets)
                self._histograms[histogram_key] = histogram
            histogram.observe(duration_seconds)

    def snapshot(self) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str], _Histogram]]:
        """A consistent read of both series, for rendering. Copies rather
        than returning live references, so a render in progress cannot race
        a concurrent `record_request`.
        """
        with self._lock:
            counters = dict(self._counters)
            histograms = {key: _copy_histogram(h) for key, h in self._histograms.items()}
        return counters, histograms


def _copy_histogram(histogram: _Histogram) -> _Histogram:
    copy = _Histogram(buckets=histogram.buckets)
    copy.bucket_counts = list(histogram.bucket_counts)
    copy.sum_seconds = histogram.sum_seconds
    copy.count = histogram.count
    return copy


def _escape_label_value(value: str) -> str:
    """Escape a label value per the exposition format's own rules: backslash,
    double-quote and newline are escaped; everything else is passed through.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus_text(registry: MetricsRegistry) -> str:
    """Render `registry`'s current state as Prometheus text exposition
    format (https://prometheus.io/docs/instrumenting/exposition_formats/).

    This is the body `GET /metrics` returns verbatim, with the
    `text/plain; version=0.0.4` content type a scraper expects.
    """
    counters, histograms = registry.snapshot()
    lines: list[str] = []

    lines.append("# HELP http_requests_total Total HTTP requests by method, route and status.")
    lines.append("# TYPE http_requests_total counter")
    for (method, route, status), count in sorted(counters.items()):
        labels = (
            f'method="{_escape_label_value(method)}",'
            f'route="{_escape_label_value(route)}",'
            f'status="{status}"'
        )
        lines.append(f"http_requests_total{{{labels}}} {count}")

    lines.append("# HELP http_request_duration_seconds HTTP request duration in seconds.")
    lines.append("# TYPE http_request_duration_seconds histogram")
    for (method, route), histogram in sorted(histograms.items()):
        base_labels = f'method="{_escape_label_value(method)}",route="{_escape_label_value(route)}"'
        # `histogram.bucket_counts[i]` is already the CUMULATIVE count for
        # that bucket — `_Histogram.observe` increments every bucket a value
        # falls under, which for an ascending bucket list means "this many
        # observations were <= this bucket's upper bound" directly. The
        # exposition format's own `le` buckets are cumulative by definition
        # (https://prometheus.io/docs/instrumenting/exposition_formats/), so
        # the stored counts are rendered as-is rather than re-accumulated.
        for upper, bucket_count in zip(histogram.buckets, histogram.bucket_counts, strict=True):
            lines.append(
                f'http_request_duration_seconds_bucket{{{base_labels},le="{upper}"}} {bucket_count}'
            )
        lines.append(
            f'http_request_duration_seconds_bucket{{{base_labels},le="+Inf"}} {histogram.count}'
        )
        lines.append(f"http_request_duration_seconds_sum{{{base_labels}}} {histogram.sum_seconds}")
        lines.append(f"http_request_duration_seconds_count{{{base_labels}}} {histogram.count}")

    return "\n".join(lines) + "\n"


__all__ = [
    "DEFAULT_BUCKETS",
    "MAX_LABEL_COMBINATIONS",
    "MetricsRegistry",
    "render_prometheus_text",
]
