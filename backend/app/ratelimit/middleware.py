"""The sliding-window rate-limit middleware (story 14.5).

## Why this is hand-rolled rather than `slowapi`

`slowapi` is the usual FastAPI answer and was the first thing considered. It
was not taken, for three reasons specific to this codebase:

1. **Its storage default is in-process too.** `slowapi` wraps `limits`, whose
   default backend is an in-memory dict. Taking the dependency would buy the
   *option* of a shared Redis or Memcached store — and there is no Redis, no
   Memcached and no shared cache of any kind in this project. (Data lives in
   ZeroDB, reached over HTTP; see CLAUDE.md.) So the dependency would not
   actually remove the single-process limitation documented below; it would
   only move it behind a library.

2. **Its decorator API is invasive.** `@limiter.limit("60/minute")` requires
   every decorated handler to take a `request: Request` parameter whether it
   needs one or not, and a module-level `app.state.limiter` singleton. That
   means touching all 23 existing handlers across seven routers, and a
   24th endpoint added later is unlimited unless its author remembers the
   decorator. A middleware plus the policy table in `policy.py` is
   fail-closed by construction: a new route is limited by default.

3. **The policy is the interesting part, and it is ours either way.** Which
   endpoints get which allowance, and why, is the actual content of this
   story (and the thing a reviewer should read). That table has to be written
   whichever limiter runs it.

What `slowapi` would genuinely add is a shared store. The moment this app is
deployed to more than one instance, that is the right conversation to reopen —
with real numbers, against real infrastructure. It is noted in
`docs/environments.md` rather than pre-built here.

## The limitation, stated plainly

**This limiter counts requests inside one Python process.** Two consequences,
neither of them a surprise at runtime:

* **Multiple instances or workers multiply every limit.** Four uvicorn
  workers behind one address means the effective allowance is 4x the number
  in the table, because each worker holds its own counters. The deployment
  that story 1.6 describes is a single instance; `docs/environments.md` now
  says so explicitly and says what to do about it when that changes
  (a shared store — Redis, or a ZeroDB-backed counter — behind the same
  `check()` seam, which is why the arithmetic lives in one method).
* **Counters reset on restart.** A deploy hands every client a fresh
  allowance. For a scraping defence measured in hours that is immaterial;
  for a security control it would not be, which is part of why this is
  described as abuse mitigation and not as authorization.

Neither is a silent gap: both are asserted by tests
(`test_idle_buckets_are_reclaimed_rather_than_leaking` pins the memory
behaviour that makes a long-lived single process safe) and both are in the
environments doc.

## Why a sliding window

A fixed-bucket counter ("60 per clock minute") lets a client spend its whole
allowance at 11:59:59.9 and its whole allowance again at 12:00:00.1 — a 2x
burst exactly at the boundary, which is what an unsophisticated scraper
produces by accident and a sophisticated one produces on purpose. A sliding
window holds the last N timestamps per bucket and refuses until the oldest
ages out. At these limits the cost is at most 60 floats per address (5 for
the correction policy), bounded by `limit` and never by the request rate —
a refused attempt is not recorded, which is both a memory property and a
fairness one: a client that keeps retrying must not thereby extend its own
lockout.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.ratelimit.policy import (
    RateLimitPolicy,
    account_key,
    client_key,
    policy_for,
    resolve_template,
)

#: How often idle buckets are swept, as a multiple of the longest window seen.
#: Sweeping on every request would make the limiter O(buckets) per call; never
#: sweeping would leak one dict entry per source address for the life of the
#: process. A sweep every 60 seconds of clock time costs a single pass over a
#: dict that holds, at worst, one entry per address seen in the last window.
SWEEP_INTERVAL_SECONDS = 60.0

#: Cap on the concrete-path to route-template memo. The route table is fixed,
#: so the useful size of this cache is the number of endpoints; the cap is
#: what stops a scraper walking a million distinct ids from turning the cache
#: itself into the leak the buckets are careful not to be.
ROUTE_CACHE_MAX = 2_048

#: The body returned with a 429. No request data is echoed and nothing
#: voter-derived appears, because this response is logged by whatever is
#: making the requests and prd.md's privacy rule applies to refusals too.
REFUSAL_DETAIL = (
    "Too many requests. This endpoint is rate-limited to protect published "
    "data from bulk collection. Wait for the period given in the Retry-After "
    "header and try again."
)


@dataclass(frozen=True)
class RateLimitDecision:
    """The outcome of one limit check.

    Attributes:
        allowed: Whether the request may proceed.
        remaining: Requests left in the window after this one. Floors at 0.
        retry_after_seconds: Whole seconds until the allowance frees up. At
            least 1 whenever ``allowed`` is false — a ``Retry-After: 0``
            invites the busy-loop the limiter exists to stop.
        policy: The policy that decided, for the response headers.
    """

    allowed: bool
    remaining: int
    retry_after_seconds: int
    policy: RateLimitPolicy


class RateLimitMiddleware:
    """Pure-ASGI sliding-window limiter over the policy table.

    Pure ASGI rather than `BaseHTTPMiddleware` on purpose: `BaseHTTPMiddleware`
    wraps every request in an anyio task group and buffers the response, which
    adds measurable per-request cost to *every* call — including the 99.99%
    that are nowhere near a limit. This implementation touches one dict and
    one deque on the allowed path and never reads the request body.

    ``app`` may be ``None``, which builds a limiter that can only be asked for
    decisions (`check`, `identity`). The tests use that to assert the window
    arithmetic and the sweep without standing up an application.
    """

    def __init__(
        self,
        app: ASGIApp | None,
        *,
        now: Callable[[], float] | None = None,
        trust_forwarded_for: bool = False,
    ) -> None:
        """
        Args:
            app: The ASGI app to wrap, or ``None`` for a decision-only limiter.
            now: Monotonic clock, injected so tests advance time by hand
                instead of sleeping. Defaults to `time.monotonic`, which is
                immune to the wall clock being stepped by NTP — a limiter on
                `time.time()` can be handed a free allowance by a clock jump.
            trust_forwarded_for: Honour `X-Forwarded-For` as the client
                address. Correct only behind exactly one proxy that overwrites
                the header; see `policy.client_key`.
        """
        self.app = app
        self._now = now or time.monotonic
        self._trust_forwarded_for = trust_forwarded_for
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._route_cache: dict[tuple[str, str], str] = {}
        self._last_sweep = self._now()

    # --- the decision ----------------------------------------------------

    def check(self, *, key: str, policy: RateLimitPolicy) -> RateLimitDecision:
        """Record and judge one request from ``key`` against ``policy``.

        The whole arithmetic of this story lives here, in one method, so that
        a future shared-store implementation replaces exactly this body.
        """
        now = self._now()
        bucket = self._hits.setdefault((key, policy.name), deque())
        cutoff = now - policy.window_seconds

        # Drop everything that has aged out. The deque is ordered by
        # construction, so this stops at the first live timestamp.
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= policy.limit:
            # Refused. Deliberately NOT recorded: recording rejections would
            # let a retrying client slide its own window forward forever, and
            # would make a bucket grow with the attack rate rather than with
            # the limit.
            oldest = bucket[0]
            retry_after = max(1, math.ceil(oldest + policy.window_seconds - now))
            return RateLimitDecision(
                allowed=False,
                remaining=0,
                retry_after_seconds=retry_after,
                policy=policy,
            )

        bucket.append(now)
        self._maybe_sweep(now)
        return RateLimitDecision(
            allowed=True,
            remaining=max(0, policy.limit - len(bucket)),
            retry_after_seconds=0,
            policy=policy,
        )

    def identity(self, scope: Scope) -> str:
        """The bucket identity for one request.

        Composed rather than bare so that the day `account_key` returns a
        verified subject, account traffic separates from address traffic with
        no change here. Today it is always ``ip:<address>``.
        """
        account = account_key(dict(scope))
        if account is not None:  # pragma: no cover - no auth exists yet
            return f"acct:{account}"
        return f"ip:{client_key(dict(scope), trust_forwarded_for=self._trust_forwarded_for)}"

    # --- housekeeping ----------------------------------------------------

    def _maybe_sweep(self, now: float) -> None:
        """Drop buckets whose timestamps have all aged out.

        Without this, one dict entry per source address is held for the life
        of the process: a week of bot traffic against a long-running instance
        is an unbounded dict, which is how a limiter becomes the outage.
        """
        if now - self._last_sweep < SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        windows = {policy.name: policy.window_seconds for policy in (*self._policies(),)}
        dead = [
            bucket_key
            for bucket_key, hits in self._hits.items()
            if not hits or hits[-1] <= now - windows.get(bucket_key[1], SWEEP_INTERVAL_SECONDS)
        ]
        for bucket_key in dead:
            del self._hits[bucket_key]

    @staticmethod
    def _policies() -> tuple[RateLimitPolicy, ...]:
        from app.ratelimit.policy import POLICIES

        return POLICIES

    def tracked_keys(self) -> int:
        """How many buckets are held. For the memory assertions."""
        return len(self._hits)

    def tracked_hits(self, key: str, policy: RateLimitPolicy) -> int:
        """How many timestamps one bucket holds. For the memory assertions."""
        return len(self._hits.get((key, policy.name), ()))

    # --- the ASGI surface -------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.app is None:
            assert self.app is not None, "a decision-only limiter cannot serve requests"
            await self.app(scope, receive, send)
            return

        policy = policy_for(scope.get("method", "GET"), self._route_path(scope))
        if policy is None:
            await self.app(scope, receive, send)
            return

        decision = self.check(key=self.identity(scope), policy=policy)
        if not decision.allowed:
            await self._refuse(decision, send)
            return

        await self.app(scope, receive, self._with_headers(send, decision))

    def _route_path(self, scope: Scope) -> str:
        """The route *template* for this request, e.g. `/v1/profiles/{user_id}`.

        Policies are declared against templates so that an allowance is per
        *endpoint* rather than per URL — keyed on concrete paths, a scraper
        walking `/v1/topics/1/stances`, `/v1/topics/2/stances` … would get a
        fresh allowance per topic, which is exactly the scraping prd.md names.

        Middleware runs **before** routing, so the template is not in the
        scope yet: the concrete path is all there is. Two approaches were
        tried and rejected before this one:

        * **Prefix-matching the concrete path against declared templates.** A
          template containing `{user_id}` is never a prefix of
          `/v1/profiles/voter-1`, so `GET /v1/profiles/{user_id}` silently
          fell through to the lenient `default-read` allowance instead of
          the stricter `private-read` one — with no error anywhere.
        * **Asking the app's own route table** via `route.matches(scope)`.
          Correct in principle, but Starlette constructs middleware lazily
          and puts the app on the scope from inside the router, which runs
          *within* this middleware — so the table is not reachable on the
          way in without threading a reference through `create_app`.

        What is here instead: each declared template is compiled once to a
        regex (`{anything}` → one non-slash segment), the same transformation
        Starlette applies, and the concrete path is matched against those.
        No app reference, no ordering hazard, and the result is memoised per
        path because the policy table is fixed for the life of the process.

        Pinned by `test_a_concrete_profile_path_resolves_to_the_private_read_
        template` and by the BDD scenario that walks the live route table.
        """
        concrete = scope.get("path", "/")
        cached = self._route_cache.get(concrete)
        if cached is not None:
            return cached

        resolved = resolve_template(concrete)
        # Bounded so a flood of distinct concrete paths cannot grow this
        # without limit — the same memory discipline as the hit buckets.
        if len(self._route_cache) < ROUTE_CACHE_MAX:
            self._route_cache[concrete] = resolved
        return resolved

    async def _refuse(self, decision: RateLimitDecision, send: Send) -> None:
        """Send the 429. prd.md's "challenge on abuse", at the protocol level."""
        import json

        body = json.dumps({"detail": REFUSAL_DETAIL}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(decision.retry_after_seconds).encode()),
                    (b"x-ratelimit-limit", str(decision.policy.limit).encode()),
                    (b"x-ratelimit-remaining", b"0"),
                    (b"x-ratelimit-policy", decision.policy.name.encode()),
                    # Signals to shared caches that this body is specific to
                    # the caller, so a CDN never serves one client's 429 to
                    # everybody else.
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _with_headers(send: Send, decision: RateLimitDecision) -> Send:
        """Add the allowance headers to an allowed response.

        An honest client — our own PWA included — can pace itself only if it
        can see how much is left, so `X-RateLimit-*` goes on successes too,
        not just on the refusal.
        """

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["x-ratelimit-limit"] = str(decision.policy.limit)
                headers["x-ratelimit-remaining"] = str(decision.remaining)
                headers["x-ratelimit-policy"] = decision.policy.name
            await send(message)

        return send_with_headers


__all__ = [
    "REFUSAL_DETAIL",
    "SWEEP_INTERVAL_SECONDS",
    "RateLimitDecision",
    "RateLimitMiddleware",
]
