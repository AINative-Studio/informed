"""ASGI middleware recording per-request metrics (story 16.5).

Pure ASGI, not `BaseHTTPMiddleware`, for the identical reason
`app.ratelimit.middleware.RateLimitMiddleware` gives in its own module
docstring: `BaseHTTPMiddleware` wraps every request in an anyio task group and
buffers the response, adding measurable per-request cost to all traffic, not
just the requests this middleware cares about. This implementation touches a
dict and a few counters on every request and never buffers a body.

Route-template resolution is reused from `app.ratelimit.policy.resolve_template`
rather than reimplemented: it is the one function in this codebase that
already answers "which declared route does this concrete path belong to",
and recomputing that logic here would be a second place a new route's
template could silently disagree with the rate limiter's.

## Cardinality: the one new risk this middleware adds

`resolve_template` falls back to the concrete path when nothing matches a
declared route — correct for the rate limiter (an unmatched path just gets a
default policy) but wrong here: a scraper walking `/v1/nonexistent-1`,
`/v1/nonexistent-2`, ... would otherwise hand this middleware's metrics
registry one new label combination per probe, exactly the unbounded-growth
failure `MetricsRegistry`'s own `MAX_LABEL_COMBINATIONS` guards against in
the aggregate but which is cheaper to prevent at the source. So any path that
does not resolve to a declared template is folded to a single
`UNMATCHED_ROUTE_LABEL` here before it ever reaches the registry — a 404
storm shows up as one growing counter, not thousands of them.
"""

from __future__ import annotations

import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.metrics import MetricsRegistry
from app.ratelimit.policy import declared_route_templates, resolve_template

#: What an unresolved concrete path is folded to before being recorded. See
#: the module docstring's "Cardinality" section.
UNMATCHED_ROUTE_LABEL = "unmatched"

#: Ops endpoints: not declared in the rate-limit policy table (they are
#: deliberately unlimited — `app.ratelimit.policy.UNLIMITED_PATHS`), but
#: real, bounded, known routes that should be recorded under their own
#: label rather than folded into `UNMATCHED_ROUTE_LABEL`.
_KNOWN_UNLIMITED_ROUTES = frozenset({"/health", "/metrics"})


class MetricsMiddleware:
    """Records method/route/status/duration for every HTTP request.

    Mounted around everything else (including the rate limiter) so that even
    a refused (429) or unauthenticated request is counted — an operator
    watching error rate needs refusals to show up too, not just requests
    that reached a handler. See `app.main.create_app`'s middleware-ordering
    docstring for why this still composes correctly with CORS and the
    limiter: Starlette applies `add_middleware` in reverse, so mounting this
    one LAST in the `add_middleware` calls makes it the OUTERMOST layer,
    timing the whole stack including the limiter's own refusal path.
    """

    def __init__(self, app: ASGIApp, *, registry: MetricsRegistry, now: object = None) -> None:
        self.app = app
        self._registry = registry
        self._now = now or time.perf_counter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        route = _label_for(resolve_template(scope.get("path", "/")))

        start = self._now()
        status_holder = {"status": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = self._now() - start
            status = status_holder["status"] or 500
            self._registry.record_request(
                method=method, route=route, status=status, duration_seconds=duration
            )


def _label_for(resolved: str) -> str:
    """`resolved` if it is one of this app's declared routes, else the
    bounded `UNMATCHED_ROUTE_LABEL`. Declared routes come from
    `app.ratelimit.policy.declared_route_templates`, so this never drifts
    from what the rate limiter itself considers "a real route".
    """
    if resolved in declared_route_templates() or resolved in _KNOWN_UNLIMITED_ROUTES:
        return resolved
    return UNMATCHED_ROUTE_LABEL


__all__ = ["UNMATCHED_ROUTE_LABEL", "MetricsMiddleware"]
