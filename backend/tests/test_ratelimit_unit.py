"""Unit tests for the rate limiter's internals (story 14.5).

The feature file drives the middleware over the real app. These tests pin the
parts that a request-level test cannot see clearly: the window arithmetic at
its boundaries, the policy table's own invariants, the client-address
resolution rules, and the memory behaviour that decides whether this limiter
is safe to leave running for a week.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import LOCAL_DEV_ORIGINS, create_app
from app.ratelimit import (
    PUBLIC_READ,
    RateLimitMiddleware,
    RateLimitPolicy,
    account_key,
    client_key,
    policy_for,
)
from app.ratelimit.policy import POLICIES, UNLIMITED_PATHS, resolve_template


class Clock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def limiter(clock: Clock) -> RateLimitMiddleware:
    return RateLimitMiddleware(app=None, now=clock)


# --- window arithmetic ----------------------------------------------------


def test_requests_under_the_limit_are_all_allowed() -> None:
    clock = Clock()
    middleware = limiter(clock)
    policy = RateLimitPolicy(name="test", limit=5, window_seconds=60)
    for _ in range(5):
        assert middleware.check(key="a", policy=policy).allowed


def test_the_request_after_the_limit_is_refused() -> None:
    clock = Clock()
    middleware = limiter(clock)
    policy = RateLimitPolicy(name="test", limit=3, window_seconds=60)
    for _ in range(3):
        assert middleware.check(key="a", policy=policy).allowed
    decision = middleware.check(key="a", policy=policy)
    assert not decision.allowed
    assert decision.retry_after_seconds >= 1


def test_a_refused_request_does_not_extend_the_window() -> None:
    """A sliding window must not punish a client for being refused.

    Some naive limiters record the rejected attempt too, which means a client
    hammering the endpoint never gets back in — the window keeps sliding
    forward on its own rejections. A well-behaved client that retried once a
    second would be locked out indefinitely. This pins the opposite.
    """
    clock = Clock()
    middleware = limiter(clock)
    policy = RateLimitPolicy(name="test", limit=2, window_seconds=10)
    middleware.check(key="a", policy=policy)
    middleware.check(key="a", policy=policy)
    for _ in range(50):
        clock.advance(0.1)
        assert not middleware.check(key="a", policy=policy).allowed
    clock.advance(5.1)
    assert middleware.check(key="a", policy=policy).allowed


def test_the_window_slides_rather_than_resetting_on_a_fixed_boundary() -> None:
    """A fixed-bucket limiter allows a 2x burst across the boundary.

    With limit=2/10s and fixed buckets, a client can spend both at t=9.9 and
    both again at t=10.1 — four requests in 0.2s. A sliding window refuses
    the third until the first has actually aged out.
    """
    clock = Clock()
    middleware = limiter(clock)
    policy = RateLimitPolicy(name="test", limit=2, window_seconds=10)
    clock.advance(9.9)
    assert middleware.check(key="a", policy=policy).allowed
    assert middleware.check(key="a", policy=policy).allowed
    clock.advance(0.2)
    assert not middleware.check(key="a", policy=policy).allowed


def test_retry_after_is_when_the_oldest_request_ages_out() -> None:
    clock = Clock()
    middleware = limiter(clock)
    policy = RateLimitPolicy(name="test", limit=1, window_seconds=60)
    middleware.check(key="a", policy=policy)
    clock.advance(20.0)
    decision = middleware.check(key="a", policy=policy)
    assert not decision.allowed
    # 40s remain on the oldest timestamp; Retry-After rounds up to whole
    # seconds so an honest client that waits exactly that long gets in.
    assert decision.retry_after_seconds == 40


def test_retry_after_is_never_zero() -> None:
    """A `Retry-After: 0` tells a client to retry immediately, which is wrong.

    It happens whenever the oldest timestamp is a fraction of a second from
    ageing out, and it turns a limiter into a busy-loop invitation.
    """
    clock = Clock()
    middleware = limiter(clock)
    policy = RateLimitPolicy(name="test", limit=1, window_seconds=60)
    middleware.check(key="a", policy=policy)
    clock.advance(59.99)
    decision = middleware.check(key="a", policy=policy)
    assert decision.retry_after_seconds >= 1


def test_keys_do_not_share_an_allowance() -> None:
    clock = Clock()
    middleware = limiter(clock)
    policy = RateLimitPolicy(name="test", limit=1, window_seconds=60)
    assert middleware.check(key="a", policy=policy).allowed
    assert middleware.check(key="b", policy=policy).allowed
    assert not middleware.check(key="a", policy=policy).allowed


def test_policies_do_not_share_an_allowance_for_one_key() -> None:
    """Exhausting the write allowance must not close the read endpoints.

    One bucket per (key, policy) rather than per key: a voter who submits
    their allowance of corrections can still read the ballot.
    """
    clock = Clock()
    middleware = limiter(clock)
    reads = RateLimitPolicy(name="reads", limit=1, window_seconds=60)
    writes = RateLimitPolicy(name="writes", limit=1, window_seconds=60)
    assert middleware.check(key="a", policy=writes).allowed
    assert not middleware.check(key="a", policy=writes).allowed
    assert middleware.check(key="a", policy=reads).allowed


def test_remaining_counts_down_and_floors_at_zero() -> None:
    clock = Clock()
    middleware = limiter(clock)
    policy = RateLimitPolicy(name="test", limit=2, window_seconds=60)
    assert middleware.check(key="a", policy=policy).remaining == 1
    assert middleware.check(key="a", policy=policy).remaining == 0
    assert middleware.check(key="a", policy=policy).remaining == 0


# --- memory behaviour -----------------------------------------------------


def test_idle_buckets_are_reclaimed_rather_than_leaking() -> None:
    """An in-process limiter that never forgets is a memory leak.

    One dict entry per source address, held forever, is how a long-running
    single process falls over after a week of bot traffic. Buckets whose
    timestamps have all aged out are dropped on the next sweep.
    """
    clock = Clock()
    middleware = limiter(clock)
    policy = RateLimitPolicy(name="test", limit=5, window_seconds=60)
    for index in range(500):
        middleware.check(key=f"ip-{index}", policy=policy)
    assert middleware.tracked_keys() == 500
    clock.advance(policy.window_seconds * 2)
    middleware.check(key="someone-new", policy=policy)
    assert middleware.tracked_keys() <= 2, (
        f"{middleware.tracked_keys()} buckets still held after every one aged out"
    )


def test_a_bucket_holds_at_most_the_limit_in_timestamps() -> None:
    """Refused attempts must not grow the bucket without bound.

    A scraper sending 100k requests a minute against a 60/minute limit must
    cost 60 floats, not 100k. This is the memory side of the same property
    `test_a_refused_request_does_not_extend_the_window` pins behaviourally.
    """
    clock = Clock()
    middleware = limiter(clock)
    policy = RateLimitPolicy(name="test", limit=10, window_seconds=60)
    for _ in range(5_000):
        middleware.check(key="flood", policy=policy)
    assert middleware.tracked_hits("flood", policy) <= policy.limit


# --- the policy table's invariants ---------------------------------------


def test_every_policy_has_a_positive_limit_and_window() -> None:
    for policy in POLICIES:
        assert policy.limit >= 1, policy
        assert policy.window_seconds >= 1, policy


def test_policy_names_are_unique() -> None:
    names = [policy.name for policy in POLICIES]
    assert len(names) == len(set(names)), f"duplicate policy names in {names}"


def test_every_write_policy_is_stricter_than_the_public_read_policy() -> None:
    """prd.md's abuse concern is scraping; a write is worse than a read.

    Asserted over the table rather than per endpoint so a policy added by a
    later story cannot quietly give a write a reader's allowance.
    """
    for policy in POLICIES:
        if "POST" in policy.methods or "DELETE" in policy.methods:
            assert policy.per_minute() <= PUBLIC_READ.per_minute(), (
                f"{policy.name} allows {policy.per_minute()}/min, which is not stricter "
                f"than the public read allowance of {PUBLIC_READ.per_minute()}/min"
            )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/topics"),
        ("GET", "/v1/topics/{topic_id}/stances"),
        ("GET", "/v1/ballot"),
        ("GET", "/v1/methodology"),
        ("GET", "/v1/methodology/versions"),
        ("GET", "/v1/methodology/{version}"),
        ("GET", "/v1/corrections/{tracking_token}"),
        ("GET", "/v1/profiles/{user_id}"),
        ("GET", "/v1/profiles/methodology"),
        ("POST", "/v1/topics/suggest"),
        ("POST", "/v1/profiles"),
        ("POST", "/v1/corrections"),
        ("POST", "/v1/profiles/{user_id}/follow/{person_id}"),
        ("DELETE", "/v1/profiles/{user_id}/follow/{person_id}"),
    ],
)
def test_each_public_route_resolves_to_a_policy(method: str, path: str) -> None:
    assert policy_for(method, path) is not None, f"{method} {path} is unlimited"


def test_health_is_explicitly_unlimited() -> None:
    """Rate-limiting the liveness probe would take the service down itself.

    A load balancer or uptime checker probes from one address far more often
    than any human, and a 429 to a health check reads as an outage.
    """
    assert "/health" in UNLIMITED_PATHS
    assert policy_for("GET", "/health") is None


def test_the_concrete_path_wins_over_the_parameterised_one() -> None:
    """`/v1/profiles/methodology` is a read; `/v1/profiles` is a write.

    Resolution is longest-prefix, most-specific-first, so the published tier
    weights are not accidentally served under the profile-write allowance.
    """
    weights = policy_for("GET", "/v1/profiles/methodology")
    writes = policy_for("POST", "/v1/profiles")
    assert weights is not None and writes is not None
    assert weights.per_minute() > writes.per_minute()


def test_an_unknown_path_falls_back_to_a_policy_rather_than_to_unlimited() -> None:
    """A route added by a later story is limited by default, not by accident.

    Fail-closed: an endpoint whose author forgot the policy table still gets
    the default public-read allowance instead of silently being open.
    """
    assert policy_for("GET", "/v1/candidates/abc/alignment") is not None
    assert policy_for("POST", "/v1/agent/ask") is not None


# --- identity resolution --------------------------------------------------


def _scope(client: tuple[str, int] | None, headers: dict[str, str] | None = None) -> dict:
    return {
        "type": "http",
        "client": client,
        "headers": [
            (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
        ],
    }


def test_the_key_is_the_peer_address_by_default() -> None:
    assert client_key(_scope(("192.0.2.5", 51234)), trust_forwarded_for=False) == "192.0.2.5"


def test_a_forwarded_for_header_is_ignored_unless_the_proxy_is_trusted() -> None:
    """Trusting `X-Forwarded-For` unconditionally makes the limiter useless.

    Any client can send it, so an untrusted-proxy deployment that honours it
    hands every scraper an unlimited supply of identities.
    """
    scope = _scope(("192.0.2.5", 51234), {"x-forwarded-for": "198.51.100.9"})
    assert client_key(scope, trust_forwarded_for=False) == "192.0.2.5"
    assert client_key(scope, trust_forwarded_for=True) == "198.51.100.9"


def test_the_leftmost_forwarded_address_is_the_client() -> None:
    scope = _scope(
        ("10.0.0.1", 1),
        {"x-forwarded-for": "198.51.100.9, 203.0.113.1, 10.0.0.1"},
    )
    assert client_key(scope, trust_forwarded_for=True) == "198.51.100.9"


def test_a_missing_peer_address_still_yields_a_usable_key() -> None:
    """An ASGI server may report no client (a unix socket, some test harnesses).

    Returning `None` would mean "unlimited" for those requests, so they share
    one explicitly-named bucket instead.
    """
    key = client_key(_scope(None), trust_forwarded_for=False)
    assert key and key != "None"


def test_a_blank_forwarded_header_falls_back_to_the_peer() -> None:
    scope = _scope(("192.0.2.5", 1), {"x-forwarded-for": "   "})
    assert client_key(scope, trust_forwarded_for=True) == "192.0.2.5"


# --- per-account: the deferral, asserted ---------------------------------


def test_the_account_hook_identifies_nobody_until_auth_exists() -> None:
    """Story 14.5's "per-account limits", honestly deferred.

    There is no authentication in this codebase: every endpoint takes a bare
    `user_id` the caller chooses. Keying an account limit on that would be a
    limit any abuser lifts by incrementing an integer, and would read in the
    code as though per-account limiting were done. So the hook exists, returns
    `None`, and this test is what fails the day someone wires it to an
    unauthenticated field.
    """
    assert account_key(_scope(("192.0.2.5", 1))) is None
    assert account_key(_scope(("192.0.2.5", 1), {"authorization": "Bearer whatever"})) is None


def test_the_composite_key_is_the_address_while_no_account_is_identified() -> None:
    middleware = limiter(Clock())
    scope = _scope(("192.0.2.5", 1), {"authorization": "Bearer whatever"})
    assert middleware.identity(scope) == "ip:192.0.2.5"


# --- template resolution, the bug that cost this story the most time -----


@pytest.mark.parametrize(
    ("concrete", "template"),
    [
        ("/v1/profiles/voter-1", "/v1/profiles/{user_id}"),
        ("/v1/topics/prescription-drug-pricing/stances", "/v1/topics/{topic_id}/stances"),
        ("/v1/methodology/2026.1", "/v1/methodology/{version}"),
        ("/v1/corrections/abcdefghijklmnop", "/v1/corrections/{tracking_token}"),
        (
            "/v1/profiles/voter-1/follow/us-house:119-2:452",
            "/v1/profiles/{user_id}/follow/{person_id}",
        ),
        # Literals are already templates and must come back untouched.
        ("/v1/topics", "/v1/topics"),
        ("/v1/corrections", "/v1/corrections"),
        ("/v1/profiles", "/v1/profiles"),
        ("/v1/profiles/methodology", "/v1/profiles/methodology"),
    ],
)
def test_a_concrete_path_resolves_to_the_template_it_was_declared_under(
    concrete: str, template: str
) -> None:
    """The limiter runs before routing, so it must do this itself.

    Without it, an allowance is per *URL* rather than per endpoint, and a
    scraper walking `/v1/topics/1/stances`, `/v1/topics/2/stances` … gets a
    fresh 60 requests per topic — the exact scraping prd.md names. The first
    implementation prefix-matched instead and silently gave every
    parameterised route the lenient default allowance.
    """
    assert resolve_template(concrete) == template


def test_a_concrete_profile_path_gets_the_private_read_policy_not_the_default() -> None:
    """The regression test for that bug, stated as a policy outcome.

    `/v1/profiles/voter-1` must be limited at 30/minute (`private-read`),
    not at the 60/minute default a failed template match would hand it.
    """
    policy = policy_for("GET", resolve_template("/v1/profiles/voter-1"))
    assert policy is not None
    assert policy.name == "private-read"


def test_a_specific_literal_beats_a_template_that_would_also_match() -> None:
    """`/v1/profiles/methodology` is published reference data, not a profile.

    Both `/v1/profiles/methodology` (literal) and `/v1/profiles/{user_id}`
    (template) match that path. The literal has to win, or the public
    methodology read is throttled as though it were a per-voter lookup.
    """
    assert resolve_template("/v1/profiles/methodology") == "/v1/profiles/methodology"
    policy = policy_for("GET", "/v1/profiles/methodology")
    assert policy is not None and policy.name == "public-read"


def test_template_matching_does_not_treat_dots_as_wildcards() -> None:
    """A literal `.` in a declared path must be escaped, not a regex wildcard.

    `/v1/methodology/{version}` is fine either way, but a template gaining a
    literal dotted segment later would start matching paths it should not.
    """
    assert resolve_template("/v1/methodology/2026X1") == "/v1/methodology/{version}"
    assert resolve_template("/v1/methodology") == "/v1/methodology"


def test_a_template_does_not_match_across_a_slash() -> None:
    """One `{param}` is one path segment, as Starlette treats it.

    If `{user_id}` matched greedily across slashes,
    `/v1/profiles/a/follow/b` would resolve to `/v1/profiles/{user_id}` and
    the follow *write* would be limited as a read.
    """
    assert (
        resolve_template("/v1/profiles/a/follow/b") == "/v1/profiles/{user_id}/follow/{person_id}"
    )
    policy = policy_for("POST", resolve_template("/v1/profiles/a/follow/b"))
    assert policy is not None and policy.name == "public-write"


# --- the 429 has to be usable by the one client we control ---------------
#
# These drive the real app over story 1.2's in-memory fake. The endpoint
# chosen is `/v1/methodology`, because it is the simplest public read and
# because what is under test is the limiter's own response, not the payload.


def _app_with_fake_zerodb() -> Any:
    """The real app and the real limiter, over a fake ZeroDB."""
    from app.methodology.router import zerodb_client_for_methodology
    from app.zerodb import ZeroDBClient
    from app.zerodb.testing import FakeZeroDBTransport

    transport = FakeZeroDBTransport()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table("methodology_versions", {"id": "string"})
    app = create_app()

    # A zero-argument closure, never `lambda c=client: c`: FastAPI reads an
    # override's signature as request parameters, so a default argument
    # becomes a query parameter and the override receives `None`.
    def override() -> ZeroDBClient:
        return client

    app.dependency_overrides[zerodb_client_for_methodology] = override
    return app


def _exhaust(api: TestClient, path: str, origin: str) -> Any:
    """Request ``path`` until it is refused, returning the refusal."""
    policy = policy_for("GET", resolve_template(path))
    assert policy is not None
    last = None
    for _ in range(policy.limit + 1):
        last = api.get(path, headers={"origin": origin})
    assert last is not None and last.status_code == 429, (
        f"never refused: {getattr(last, 'status_code', None)}"
    )
    return last


def test_a_refusal_is_readable_by_a_browser() -> None:
    """The 429 must carry CORS headers, or the PWA cannot read it.

    This is what fixes the middleware *order*. A limiter mounted outside
    `CORSMiddleware` short-circuits before CORS runs, so its 429 has no
    `Access-Control-Allow-Origin`; the browser then gives the app an opaque
    network error and the `Retry-After` the limiter computed is invisible to
    the one client we control. Paying one header pass on a refused request is
    the right trade.
    """
    origin = LOCAL_DEV_ORIGINS[0]
    with TestClient(_app_with_fake_zerodb()) as api:
        refusal = _exhaust(api, "/v1/methodology", origin)
    assert refusal.headers.get("access-control-allow-origin") == origin, (
        "the 429 carries no CORS header, so a browser cannot read it — the "
        "limiter has been mounted outside CORSMiddleware"
    )
    assert refusal.headers.get("retry-after") is not None


def test_the_allowance_headers_are_exposed_to_a_cross_origin_caller() -> None:
    """An honest client can only pace itself if it can read the headers.

    A cross-origin response's headers are invisible to JavaScript unless
    named in `Access-Control-Expose-Headers`.
    """
    origin = LOCAL_DEV_ORIGINS[0]
    with TestClient(_app_with_fake_zerodb()) as api:
        response = api.get("/v1/methodology", headers={"origin": origin})
    exposed = response.headers.get("access-control-expose-headers", "").lower()
    for header in ("retry-after", "x-ratelimit-remaining"):
        assert header in exposed, f"{header} is not exposed to a browser"


def test_an_allowed_response_reports_what_is_left() -> None:
    with TestClient(_app_with_fake_zerodb()) as api:
        first = api.get("/v1/methodology")
        second = api.get("/v1/methodology")
    assert first.headers["x-ratelimit-policy"] == "public-read"
    assert int(first.headers["x-ratelimit-remaining"]) > int(
        second.headers["x-ratelimit-remaining"]
    )


def test_a_refusal_is_not_cached_by_a_shared_cache() -> None:
    """One client's 429 must never be served to everybody else.

    The limit is per address, so a shared cache storing this response would
    turn one scraper's lockout into an outage for every other reader.
    """
    with TestClient(_app_with_fake_zerodb()) as api:
        refusal = _exhaust(api, "/v1/methodology", LOCAL_DEV_ORIGINS[0])
    assert "no-store" in refusal.headers.get("cache-control", "")


def test_the_health_probe_survives_far_more_requests_than_any_limit() -> None:
    with TestClient(_app_with_fake_zerodb()) as api:
        statuses = {api.get("/health").status_code for _ in range(200)}
    assert statuses == {200}, f"the liveness probe was rate-limited: {statuses}"
