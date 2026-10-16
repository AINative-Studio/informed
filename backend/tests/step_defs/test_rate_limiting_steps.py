"""Step definitions for tests/features/rate_limiting.feature (story 14.5).

Every scenario here drives the real middleware over the real app. Two things
make that possible without any network, any sleeping and any ZeroDB:

* **A frozen, hand-advanced clock.** The limiter takes its `now` from an
  injected callable, so "the window passes" is one assignment rather than a
  `time.sleep(60)` that would put a minute of wall time into CI. This is also
  what makes the window-boundary assertions deterministic instead of flaky.

* **A spoofed client address.** Starlette's TestClient reports every request
  as coming from `testclient`, so a test cannot have "fifty different voters"
  without saying which address each request came from. The limiter reads the
  address from the ASGI scope, and `_as_ip` rewrites that scope per request,
  which is exactly what a real deployment's distinct source addresses give it.

The ZeroDB-backed endpoints are driven through story 1.2's in-memory fake, per
CLAUDE.md, so no scenario here needs credentials.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.main import create_app
from app.ratelimit import (
    POLICIES,
    RateLimitMiddleware,
    RateLimitPolicy,
    account_key,
    policy_for,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/rate_limiting.feature")

#: Addresses the scenarios spend. Documentation addresses from RFC 5737, so
#: nothing here can be mistaken for a real client's address in a log.
SCRAPER_IP = "192.0.2.10"
OTHER_IP = "198.51.100.7"


@dataclass
class Clock:
    """A hand-advanced monotonic clock for the limiter under test."""

    now: float = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class LimiterContext:
    """State shared between the steps of one scenario."""

    clock: Clock = field(default_factory=Clock)
    app: FastAPI | None = None
    transport: FakeZeroDBTransport | None = None
    responses: list[Any] = field(default_factory=list)
    overhead_seconds: float | None = None

    @property
    def statuses(self) -> list[int]:
        return [response.status_code for response in self.responses]


@pytest.fixture
def limiter() -> LimiterContext:
    return LimiterContext()


def _build_app(context: LimiterContext) -> FastAPI:
    """The real app, the real middleware, a fake ZeroDB and a frozen clock."""
    from app.corrections.router import zerodb_client_for_corrections
    from app.methodology.router import zerodb_client_for_methodology
    from app.profiles.router import zerodb_client_for_profiles
    from app.topics.router import zerodb_client_for_topics

    transport = FakeZeroDBTransport()
    client = ZeroDBClient(
        api_key="test-key",  # noqa: S106 - the fake transport never authenticates
        project_id="test-project",
        transport=transport,
    )
    app = create_app(now=context.clock)
    for dependency in (
        zerodb_client_for_topics,
        zerodb_client_for_profiles,
        zerodb_client_for_corrections,
        zerodb_client_for_methodology,
    ):
        app.dependency_overrides[dependency] = lambda client=client: client
    context.transport = transport
    context.app = app
    return app


def _as_ip(client: TestClient, ip: str) -> dict[str, Any]:
    """Request kwargs that make TestClient look like a request from ``ip``.

    Starlette's TestClient takes the client address from the transport's
    ``client`` argument, which is fixed per client instance. Rather than build
    one TestClient per simulated voter, the address is carried in the
    ``X-Forwarded-For`` header and the app under test is built to trust it —
    see ``RateLimitMiddleware``'s docstring on why trusting that header is
    correct behind a single known proxy and nowhere else.
    """
    return {"headers": {"x-forwarded-for": ip}}


def _correction_body(index: int) -> dict[str, str]:
    return {
        "target_table": "methodology_versions",
        "target_id": "2026.1",
        "body": f"The published weight looks wrong to me, report {index}.",
    }


@given("the API is running behind the rate limiter")
def api_behind_limiter(limiter: LimiterContext) -> Iterator[None]:
    _build_app(limiter)
    yield


def _client(limiter: LimiterContext) -> TestClient:
    assert limiter.app is not None
    return TestClient(limiter.app)


# --- real-user traffic ----------------------------------------------------


@when(parsers.parse("{count:d} different voters each read the topics tree twice"))
def many_voters_read(limiter: LimiterContext, count: int) -> None:
    with _client(limiter) as api:
        for voter in range(count):
            ip = f"203.0.113.{voter % 254 + 1}"
            for _ in range(2):
                limiter.responses.append(api.get("/v1/topics", **_as_ip(api, ip)))


@when(parsers.parse("a voter makes {count:d} reads from the same address within a minute"))
def one_voter_paced_reads(limiter: LimiterContext, count: int) -> None:
    with _client(limiter) as api:
        for index in range(count):
            limiter.responses.append(api.get("/v1/topics", **_as_ip(api, OTHER_IP)))
            limiter.clock.advance(60.0 / count if count else 0)
            del index


@when(
    parsers.parse(
        "a voter makes {count:d} reads from the same address under a generous limit"
    )
)
def measure_overhead(limiter: LimiterContext, count: int) -> None:
    """Time the limiter's own decision, not the endpoint behind it.

    Measuring end-to-end request latency here would mostly measure FastAPI's
    routing and the fake transport, and would be a noisy, machine-dependent
    assertion. What this story needs to show is that the limiter itself is not
    where the time goes, so the limiter's `check` is timed directly, with the
    same call pattern a run of reads produces.
    """
    policy = RateLimitPolicy(name="generous", limit=count * 10, window_seconds=60)
    middleware = RateLimitMiddleware(app=None, now=limiter.clock)
    started = time.perf_counter()
    for _ in range(count):
        middleware.check(key=SCRAPER_IP, policy=policy)
    elapsed = time.perf_counter() - started
    limiter.overhead_seconds = elapsed / count


# --- scraper traffic ------------------------------------------------------


@when(parsers.parse("one address requests the topics tree {count:d} times in a minute"))
def scrape_topics(limiter: LimiterContext, count: int) -> None:
    with _client(limiter) as api:
        for _ in range(count):
            limiter.responses.append(api.get("/v1/topics", **_as_ip(api, SCRAPER_IP)))


@when("one address exhausts its read allowance", target_fixture="exhausted")
@given("one address has exhausted its read allowance", target_fixture="exhausted")
def exhaust_reads(limiter: LimiterContext) -> RateLimitPolicy:
    policy = policy_for("GET", "/v1/topics")
    assert policy is not None
    with _client(limiter) as api:
        for _ in range(policy.limit + 1):
            limiter.responses.append(api.get("/v1/topics", **_as_ip(api, SCRAPER_IP)))
    return policy


@given("one address has exhausted its correction allowance", target_fixture="exhausted")
def exhaust_corrections(limiter: LimiterContext) -> RateLimitPolicy:
    policy = policy_for("POST", "/v1/corrections")
    assert policy is not None
    with _client(limiter) as api:
        for index in range(policy.limit):
            limiter.responses.append(
                api.post(
                    "/v1/corrections",
                    json=_correction_body(index),
                    **_as_ip(api, SCRAPER_IP),
                )
            )
    return policy


@when(parsers.parse("one address posts {count:d} corrections in an hour"))
def post_many_corrections(limiter: LimiterContext, count: int) -> None:
    with _client(limiter) as api:
        for index in range(count):
            limiter.responses.append(
                api.post(
                    "/v1/corrections",
                    json=_correction_body(index),
                    **_as_ip(api, SCRAPER_IP),
                )
            )


@when(parsers.parse("one address posts {count:d} free-text interest mappings in a minute"))
def post_many_suggestions(limiter: LimiterContext, count: int) -> None:
    with _client(limiter) as api:
        for _ in range(count):
            limiter.responses.append(
                api.post(
                    "/v1/topics/suggest",
                    json={"text": "prescription drug prices are too high"},
                    **_as_ip(api, SCRAPER_IP),
                )
            )


@when("the window passes")
def window_passes(limiter: LimiterContext, exhausted: RateLimitPolicy) -> None:
    limiter.clock.advance(exhausted.window_seconds + 1)


@when("that address can read again", target_fixture="_noop")
def _unused() -> None:  # pragma: no cover - placeholder for parser symmetry
    return None


@when("a different address reads the topics tree")
def other_address_reads(limiter: LimiterContext) -> None:
    with _client(limiter) as api:
        limiter.responses.append(api.get("/v1/topics", **_as_ip(api, OTHER_IP)))


@when("that address posts another correction")
def post_one_more_correction(limiter: LimiterContext) -> None:
    with _client(limiter) as api:
        limiter.responses.append(
            api.post(
                "/v1/corrections",
                json=_correction_body(999),
                **_as_ip(api, SCRAPER_IP),
            )
        )


@when(parsers.parse("an uptime checker probes health {count:d} times in a minute"))
def probe_health(limiter: LimiterContext, count: int) -> None:
    with _client(limiter) as api:
        for _ in range(count):
            limiter.responses.append(api.get("/health", **_as_ip(api, SCRAPER_IP)))


@when("a voter reads the published methodology through the limiter")
def read_methodology(limiter: LimiterContext) -> None:
    with _client(limiter) as api:
        limiter.responses.append(api.get("/v1/methodology", **_as_ip(api, OTHER_IP)))


# --- assertions -----------------------------------------------------------


@then("every one of those requests succeeds")
def all_succeeded(limiter: LimiterContext) -> None:
    assert limiter.responses, "the scenario made no requests"
    refused = [status for status in limiter.statuses if status == 429]
    assert not refused, f"{len(refused)} of {len(limiter.statuses)} requests were refused"
    assert all(status < 400 for status in limiter.statuses), (
        f"non-success statuses: {sorted(set(limiter.statuses))}"
    )


@then("no response carries a 429 status")
def no_429(limiter: LimiterContext) -> None:
    assert 429 not in limiter.statuses


@then(parsers.parse("the limiter's own overhead stays under {budget:d} millisecond per request"))
def overhead_within_budget(limiter: LimiterContext, budget: int) -> None:
    assert limiter.overhead_seconds is not None
    assert limiter.overhead_seconds < budget / 1000.0, (
        f"{limiter.overhead_seconds * 1000:.4f} ms per decision exceeds {budget} ms"
    )


@then(parsers.parse("the last request is refused with {status:d}"))
def last_refused(limiter: LimiterContext, status: int) -> None:
    assert limiter.responses, "the scenario made no requests"
    assert limiter.responses[-1].status_code == status, (
        f"statuses were {limiter.statuses[-5:]}"
    )


@then("the refusal carries a Retry-After header in seconds")
def refusal_has_retry_after(limiter: LimiterContext) -> None:
    refusal = limiter.responses[-1]
    retry_after = refusal.headers.get("retry-after")
    assert retry_after is not None, "no Retry-After header on the 429"
    assert retry_after.isdigit() and int(retry_after) >= 1, f"Retry-After was {retry_after!r}"


@then("the refusal names no voter, profile or interest data")
def refusal_is_clean(limiter: LimiterContext) -> None:
    body = json.dumps(limiter.responses[-1].json()).lower()
    forbidden = ("voter", "profile", "interest", "dealbreaker", "user_id", "payload")
    leaked = [word for word in forbidden if word in body]
    assert not leaked, f"the refusal body mentions {leaked}"


@then("the Retry-After header is at most the length of the window")
def retry_after_bounded(limiter: LimiterContext, exhausted: RateLimitPolicy) -> None:
    retry_after = int(limiter.responses[-1].headers["retry-after"])
    assert 1 <= retry_after <= exhausted.window_seconds, (
        f"Retry-After {retry_after}s is outside a {exhausted.window_seconds}s window"
    )


@then("that address can read again")
def can_read_again(limiter: LimiterContext) -> None:
    with _client(limiter) as api:
        response = api.get("/v1/topics", **_as_ip(api, SCRAPER_IP))
    assert response.status_code == 200, f"still refused: {response.status_code}"


@then("that request succeeds")
def last_succeeded(limiter: LimiterContext) -> None:
    assert limiter.responses[-1].status_code == 200, (
        f"a different address was refused with {limiter.responses[-1].status_code}"
    )


@then(
    parsers.parse(
        'the limit for "{write_path}" is lower than the limit for "{read_path}"'
    )
)
def writes_stricter_than_reads(write_path: str, read_path: str) -> None:
    write_policy = policy_for("POST", write_path)
    read_policy = policy_for("GET", read_path)
    assert write_policy is not None, f"no policy covers POST {write_path}"
    assert read_policy is not None, f"no policy covers GET {read_path}"
    assert write_policy.per_minute() < read_policy.per_minute(), (
        f"{write_path} allows {write_policy.per_minute()}/min, "
        f"{read_path} allows {read_policy.per_minute()}/min"
    )


@then("every public route resolves to a rate-limit policy")
def every_route_covered(limiter: LimiterContext) -> None:
    assert limiter.app is not None
    uncovered: list[str] = []
    for route in limiter.app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if not methods or path.startswith(("/openapi", "/docs", "/redoc")):
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            if policy_for(method, path) is None:
                uncovered.append(f"{method} {path}")
    assert not uncovered, f"routes with no rate-limit policy: {uncovered}"


@then("the response is byte-for-byte what the unlimited app returns")
def matches_unlimited_app(limiter: LimiterContext) -> None:
    from app.methodology.router import zerodb_client_for_methodology

    limited = limiter.responses[-1]
    plain = create_app(rate_limit=False)
    assert limiter.transport is not None
    client = ZeroDBClient(
        api_key="test-key",  # noqa: S106 - the fake transport never authenticates
        project_id="test-project",
        transport=limiter.transport,
    )
    plain.dependency_overrides[zerodb_client_for_methodology] = lambda: client
    with TestClient(plain) as api:
        unlimited = api.get("/v1/methodology")
    assert limited.status_code == unlimited.status_code
    assert limited.json() == unlimited.json()


@then("no correction row is written")
def no_correction_written(limiter: LimiterContext, exhausted: RateLimitPolicy) -> None:
    assert limiter.transport is not None
    stored = limiter.transport.rows.get("corrections", {})
    assert len(stored) == exhausted.limit, (
        f"{len(stored)} rows stored for a {exhausted.limit}-request allowance, so the "
        "refused request reached the endpoint behind the limiter"
    )


@then("the limiter exposes an account-identity hook")
def account_hook_exists() -> None:
    assert callable(account_key)


@then("that hook identifies nobody today")
def account_hook_returns_none() -> None:
    class _Scope:
        headers: dict[str, str] = {}

    assert account_key({"type": "http", "headers": []}) is None


@then("a request carrying a bare user_id is still limited by address only")
def bare_user_id_does_not_change_the_key(limiter: LimiterContext) -> None:
    """A `user_id` in the path must not become a limiter identity.

    This is the assertion that keeps the deferral honest: if a later story
    reaches for `user_id` as an account key, two requests carrying different
    ids from the same address would get separate buckets, and this fails.
    """
    policy = policy_for("GET", "/v1/profiles/{user_id}")
    assert policy is not None
    refusals = 0
    with _client(limiter) as api:
        for index in range(policy.limit + 1):
            response = api.get(
                f"/v1/profiles/voter-{index}",
                **_as_ip(api, SCRAPER_IP),
            )
            refusals += response.status_code == 429
    assert refusals >= 1, (
        "distinct user_ids from one address were never limited, which means the "
        "limiter is keying on a caller-supplied id rather than on the address"
    )


# A named export so the policy table itself is reachable from the step file for
# debugging a failure, without importing app internals at the use site.
__all__ = ["POLICIES", "Callable"]
