"""The rate-limit policy table and identity resolution (story 14.5).

prd.md, "Integrity and abuse": *"Rate limits and bot protection on public
endpoints to stop scraping of aggregate sentiment."*

## Why these numbers

The limits below are set by what a human using the app actually does, with
enough headroom that nobody real ever meets one, and then low enough that
bulk collection is slow and visible. Four bands:

* **Published reads — 60/minute** (`PUBLIC_READ`). A voter opening the app,
  expanding the topic tree, reading a handful of stance cards and looking up
  their ballot makes perhaps a dozen calls in their first minute, and the PWA
  caches (TanStack Query) rather than re-fetching. 60/minute is five times
  that and still means a scraper needs ten hours to walk a hundred thousand
  rows from one address instead of ten minutes. This is the band prd.md's
  "scraping of aggregate sentiment" is aimed at.
* **Reads keyed on a caller-held secret — 30/minute** (`PRIVATE_READ`). A
  voter's own profile, and a correction's status by tracking token. Stricter
  than the published reads because both are *enumeration* surfaces: the
  status lookup is the one place a guessed token gets confirmed, so this
  allowance is what bounds a guessing run.
* **Cheap uncredentialed writes — 30/minute** (`PUBLIC_WRITE`).
  `POST /v1/topics/suggest` (story 3.5) runs a semantic search per call, so
  it is a cost amplifier as well as a write. A voter typing free-text
  interests during onboarding submits a few; 30/minute is generous for that
  and caps the amplification. Profile saves and the follow/unfollow writes
  (story 15.1) sit here too.
* **The correction write — 5/HOUR** (`CORRECTION_WRITE`).
  `POST /v1/corrections` (story 12.4) is the endpoint whose own router
  docstring asked to be put behind a limiter first: it is unauthenticated, it
  writes a row per call, and the row deliberately holds nothing to identify
  on. A person reporting errors in good faith files one, occasionally a few.
  Five an hour is well above real use and turns table-filling from a script
  into a sustained, obvious campaign. A 1-hour window rather than a 1-minute
  one on purpose: a per-minute cap on a write this cheap to repeat still
  permits 7,200 rows a day.

The advisory panel's workflow (story 3.3) gets the read/write numbers under
its own policy names, and `DEFAULT_READ` / `DEFAULT_WRITE` are the fail-closed
fallback for a route a later story adds and forgets to list.

These are first-deployment numbers, not tuned ones — nothing has production
traffic to tune against yet. They live in one table so changing them is a
one-line diff with the reasoning next to it, and
`tests/test_ratelimit_unit.py` pins the invariants (every write stricter than
a read, every route covered) rather than the specific integers, so tuning
does not mean rewriting tests.

## Per-account limits are deferred, deliberately

backlog.md 14.5 asks for "per-IP **and per-account** limits". Per-IP is here.
Per-account is not, and is not faked: there is no authentication anywhere in
this codebase. Every endpoint built so far — `/v1/profiles/{user_id}`, the
follow endpoints from story 15.1 — takes a `user_id` the caller simply
states. An "account" limit keyed on that is a limit any abuser lifts by
incrementing an integer, while reading in the code as though per-account
limiting were done, which is worse than not having it.

So `account_key` exists, is called on every request, and returns `None`. The
day a real authentication story lands, it reads the verified subject from the
request and `RateLimitMiddleware.identity` starts composing
`acct:<subject>` keys with no other change. `test_the_account_hook_identifies
_nobody_until_auth_exists` is what fails if someone wires it to an
unauthenticated field in the meantime.

## Challenge on abuse

The same acceptance criterion says "challenge on abuse". What is here is a
correct `429 Too Many Requests` with `Retry-After`, which is the protocol's
own challenge: it tells an honest client exactly when to come back and makes
an abusive one pay in wall-clock time. An interactive bot challenge
(hCaptcha, Turnstile) is a separate feature with a frontend, a provider
dependency, a privacy review of a third-party script against prd.md's
"no third-party trackers", and a fallback for voters who cannot complete it.
That is not a sub-task of a 3-point story and is not stubbed here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Paths that are never rate-limited, with the reason.
#:
#: `/health` is probed by the deploy pipeline, the load balancer and any
#: uptime checker, all from a small number of addresses and far more often
#: than any human browses. A 429 to a health check reads as an outage, so
#: rate-limiting the liveness probe is how a limiter takes down the service it
#: was added to protect.
#:
#: `/metrics` (story 16.5) is the same argument: a monitoring scraper polls it
#: on a fixed interval from a small, known set of addresses, and throttling
#: the thing an operator reads to find out whether the service is healthy is
#: exactly backwards during an incident, which is the one time it is scraped
#: hardest and matters most.
UNLIMITED_PATHS: frozenset[str] = frozenset({"/health", "/metrics"})

#: OpenAPI and docs routes, excluded from policy resolution the same way.
_DOCS_PREFIXES = ("/openapi", "/docs", "/redoc")


@dataclass(frozen=True)
class RateLimitPolicy:
    """One allowance: ``limit`` requests per ``window_seconds``, per identity.

    Attributes:
        name: Stable identifier. Part of the bucket key, so two policies
            never share an allowance, and the string that appears in the
            `X-RateLimit-Policy` response header.
        limit: Requests permitted inside one window.
        window_seconds: Length of the sliding window.
        methods: HTTP methods this policy applies to. Empty means all.
        paths: Route paths this policy covers, as FastAPI declares them
            (`/v1/profiles/{user_id}`) or as a concrete prefix.
    """

    name: str
    limit: int
    window_seconds: int
    methods: frozenset[str] = field(default_factory=frozenset)
    paths: tuple[str, ...] = ()

    def per_minute(self) -> float:
        """The allowance normalised to requests per minute.

        Policies have different window lengths, so comparing `limit` across
        them is meaningless — 5/hour and 30/minute cannot be ordered by their
        integers alone. Every "stricter than" assertion goes through this.
        """
        return self.limit * 60.0 / self.window_seconds


#: Reads of published, aggregate reference data: the topic tree, stance cards,
#: the ballot lookup, the methodology and its changelog. prd.md's scraping
#: concern is aimed squarely at these.
PUBLIC_READ = RateLimitPolicy(
    name="public-read",
    limit=60,
    window_seconds=60,
    methods=frozenset({"GET"}),
    paths=(
        "/v1/topics",
        "/v1/topics/{topic_id}/stances",
        "/v1/ballot",
        # Story 5.4. A published read like the rest of this band: the bill ids
        # are public and the archived document is public record. The 60/minute
        # allowance also bounds how fast a scraper can mint presigned URLs,
        # which matters more here than on the other paths — each call produces
        # a working, if short-lived, download link to a stored object.
        "/v1/bills/{bill_id}/text",
        "/v1/methodology",
        "/v1/methodology/versions",
        "/v1/methodology/{version}",
        "/v1/profiles/methodology",
        "/v1/panel/published/topics",
        "/v1/panel/published/topics/{topic_id}/stances",
    ),
)

#: Reads keyed on something the caller holds — a voter's own profile, a
#: correction's tracking token. Stricter than the published reads because
#: both are also *enumeration* surfaces: the correction status lookup is the
#: one place a guessed token would be confirmed, so the allowance is what
#: bounds a guessing run. See `app/corrections/router.py` on why that
#: endpoint returns the status and nothing else.
PRIVATE_READ = RateLimitPolicy(
    name="private-read",
    limit=30,
    window_seconds=60,
    methods=frozenset({"GET"}),
    paths=(
        "/v1/profiles/{user_id}",
        "/v1/corrections/{tracking_token}",
    ),
)

#: Uncredentialed writes that cost real work per call: the free-text interest
#: mapping of story 3.5 (a semantic search per request), profile saves, and
#: the follow/unfollow writes of story 15.1.
PUBLIC_WRITE = RateLimitPolicy(
    name="public-write",
    limit=30,
    window_seconds=60,
    methods=frozenset({"POST", "PUT", "PATCH", "DELETE"}),
    paths=(
        "/v1/topics/suggest",
        "/v1/profiles",
        "/v1/profiles/{user_id}/follow/{person_id}",
    ),
)

#: The public correction write, on its own much stricter policy. See this
#: module's docstring for the 5/hour reasoning.
CORRECTION_WRITE = RateLimitPolicy(
    name="correction-write",
    limit=5,
    window_seconds=3_600,
    methods=frozenset({"POST"}),
    paths=("/v1/corrections",),
)

#: The advisory panel's workflow (story 3.3). Internal-facing but, like
#: everything else here, uncredentialed today, so it gets the write
#: allowance rather than nothing.
PANEL_WRITE = RateLimitPolicy(
    name="panel-write",
    limit=30,
    window_seconds=60,
    methods=frozenset({"POST", "PUT", "PATCH", "DELETE"}),
    paths=("/v1/panel",),
)

PANEL_READ = RateLimitPolicy(
    name="panel-read",
    limit=60,
    window_seconds=60,
    methods=frozenset({"GET"}),
    paths=("/v1/panel",),
)

#: Fail-closed defaults. A route added by a later story and not listed above
#: is still limited, because the alternative — resolving to `None`, which
#: means unlimited — turns a forgotten table entry into a silently open
#: endpoint. The write default is the strict one for the same reason.
DEFAULT_READ = RateLimitPolicy(
    name="default-read",
    limit=60,
    window_seconds=60,
    methods=frozenset({"GET"}),
)
DEFAULT_WRITE = RateLimitPolicy(
    name="default-write",
    limit=30,
    window_seconds=60,
    methods=frozenset({"POST", "PUT", "PATCH", "DELETE"}),
)

#: Every policy, most specific first. Resolution walks this in order, so a
#: concrete path listed before a prefix wins over it — which is what keeps
#: `GET /v1/profiles/methodology` (a published read) out of
#: `POST /v1/profiles`'s write allowance.
POLICIES: tuple[RateLimitPolicy, ...] = (
    CORRECTION_WRITE,
    PRIVATE_READ,
    PUBLIC_READ,
    PUBLIC_WRITE,
    PANEL_READ,
    PANEL_WRITE,
    DEFAULT_READ,
    DEFAULT_WRITE,
)


def _template_pattern(template: str) -> re.Pattern[str]:
    """Compile a route template to a regex, Starlette's way.

    `{user_id}` becomes one non-slash segment, so
    `/v1/profiles/{user_id}/follow/{person_id}` matches
    `/v1/profiles/voter-1/follow/us-house:...` and nothing shorter or longer.
    Literal segments are escaped, so a `.` in a path is a dot and not a
    wildcard.
    """
    parts = re.split(r"(\{[^/}]*\})", template)
    pattern = "".join(
        r"[^/]+" if part.startswith("{") and part.endswith("}") else re.escape(part)
        for part in parts
    )
    return re.compile(f"^{pattern}$")


#: Every path any policy declares, with its regex. Ordered so that the most
#: specific declaration wins: literals before templates at the same depth,
#: then more segments before fewer. That ordering is what keeps
#: `/v1/profiles/methodology` — published reference data, a `public-read` —
#: from resolving to `/v1/profiles/{user_id}` and being throttled as though
#: it were a per-voter lookup. `test_a_specific_literal_beats_a_template_
#: that_would_also_match` is the test.
_DECLARED_PATHS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (path, _template_pattern(path))
    for path in sorted(
        {path for policy in POLICIES for path in policy.paths},
        key=lambda value: (-value.count("/"), value.count("{"), -len(value), value),
    )
)


def resolve_template(concrete_path: str) -> str:
    """The declared path a concrete request path belongs to, or the path itself.

    Used by the middleware, which runs before routing and therefore sees only
    the concrete URL. Returning the path unchanged when nothing matches is
    correct: it then falls through to a default policy rather than to
    unlimited.
    """
    for path, pattern in _DECLARED_PATHS:
        if pattern.match(concrete_path):
            return path
    return concrete_path


def declared_route_templates() -> frozenset[str]:
    """Every path template any rate-limit policy declares.

    A public, read-only view over `_DECLARED_PATHS` (story 16.5): the
    metrics middleware needs to tell "a concrete path that resolved to a
    real declared template" apart from "a concrete path `resolve_template`
    could not match and returned unchanged", and the private tuple itself is
    not something another module should reach into directly.
    """
    return frozenset(path for path, _ in _DECLARED_PATHS)


def _matches(policy: RateLimitPolicy, method: str, path: str) -> bool:
    if policy.methods and method.upper() not in policy.methods:
        return False
    if not policy.paths:
        # A default policy: method-matched, path-agnostic.
        return True
    return any(path == declared or path.startswith(declared + "/") for declared in policy.paths)


def policy_for(method: str, path: str) -> RateLimitPolicy | None:
    """The policy covering ``method path``, or ``None`` if it is unlimited.

    ``path`` is the route path as FastAPI declares it — with
    ``{user_id}`` placeholders intact, not the concrete request path. Using
    the template is what makes the limit per *endpoint* rather than per URL:
    keyed on concrete paths, a scraper walking
    ``/v1/topics/1/stances``, ``/v1/topics/2/stances`` … would get a fresh
    allowance per topic, which is precisely the scraping prd.md names.

    Returns ``None`` only for `UNLIMITED_PATHS` and the docs routes;
    everything else falls through to a default, so a new endpoint is limited
    by default rather than open by default.
    """
    if path in UNLIMITED_PATHS or path.startswith(_DOCS_PREFIXES):
        return None
    for policy in POLICIES:
        if _matches(policy, method, path):
            return policy
    return None


def _header(scope: dict[str, Any], name: str) -> str | None:
    wanted = name.lower().encode()
    for key, value in scope.get("headers") or ():
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


def client_key(scope: dict[str, Any], *, trust_forwarded_for: bool) -> str:
    """The per-IP identity for one request.

    ``trust_forwarded_for`` must be true only when the app is known to sit
    behind exactly one proxy that overwrites the header. Any client can send
    ``X-Forwarded-For``, so honouring it on a directly-exposed deployment
    hands every scraper an unlimited supply of identities — which is why this
    is an explicit, off-by-default argument rather than something inferred
    from the header's presence. See `docs/environments.md`.

    A request whose ASGI scope reports no peer (a unix socket, some test
    harnesses) is given one shared, explicitly-named bucket rather than
    `None`: unidentifiable traffic must not mean unlimited traffic.
    """
    if trust_forwarded_for:
        forwarded = _header(scope, "x-forwarded-for")
        if forwarded:
            # Leftmost entry is the originating client; the rest are proxies.
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    peer = scope.get("client")
    if peer and peer[0]:
        return str(peer[0])
    return "unknown-peer"


def unlimited_routes(app: Any) -> list[str]:
    """Every `METHOD /path` the app serves that no policy covers.

    The answer to "is any public endpoint still unlimited", and the thing a
    later story's new router has to keep empty.

    **Read from the OpenAPI schema, NOT from `app.routes`.** This version of
    FastAPI wraps each `include_router` in an opaque `_IncludedRouter` object
    that exposes neither `path` nor `methods` nor a nested `routes` list, so
    walking `app.routes` finds `/health` and the docs routes and *nothing
    else*. A check written that way passes while 22 of the app's 23
    route/method pairs go uninspected — which is exactly what the first
    version of this story's coverage assertion did. The schema is the
    authoritative list of served paths and is already built from the real
    route table, so it cannot drift from what is mounted.

    `UNLIMITED_PATHS` and the docs routes are excluded: they are deliberately
    not limited, and the health endpoint has its own test saying so.
    """
    try:
        paths: dict[str, dict[str, Any]] = app.openapi().get("paths", {})
    except Exception:  # pragma: no cover - a non-FastAPI app
        return []

    uncovered: list[str] = []
    for path, operations in paths.items():
        if path in UNLIMITED_PATHS or path.startswith(_DOCS_PREFIXES):
            continue
        for method in operations:
            verb = method.upper()
            if verb in {"HEAD", "OPTIONS", "PARAMETERS"}:
                continue
            if policy_for(verb, path) is None:
                uncovered.append(f"{verb} {path}")
    return sorted(uncovered)


def served_routes(app: Any) -> list[tuple[str, str]]:
    """Every `(METHOD, path)` the app serves, from its OpenAPI schema.

    Same reasoning as `unlimited_routes` for why the schema and not
    `app.routes`. Includes the unlimited paths; callers filter.
    """
    try:
        paths: dict[str, dict[str, Any]] = app.openapi().get("paths", {})
    except Exception:  # pragma: no cover - a non-FastAPI app
        return []
    return sorted(
        (method.upper(), path)
        for path, operations in paths.items()
        for method in operations
        if method.upper() not in {"HEAD", "OPTIONS", "PARAMETERS"}
    )


def account_key(scope: dict[str, Any]) -> str | None:
    """The per-account identity for one request. Always ``None`` today.

    This is story 14.5's "per-account limits", and it is deliberately not
    implemented — see this module's docstring. There is no authentication in
    this codebase: every `user_id` is caller-stated, so keying an account
    limit on one would be a limit that an abuser lifts by changing a string.

    The day a real auth story lands, this reads the *verified* subject from
    the request (and only a verified one) and the composite key in
    `RateLimitMiddleware.identity` begins to separate accounts with no other
    change anywhere.
    """
    del scope  # No field on an unauthenticated request identifies an account.
    return None


__all__ = [
    "CORRECTION_WRITE",
    "DEFAULT_READ",
    "DEFAULT_WRITE",
    "PANEL_READ",
    "PANEL_WRITE",
    "POLICIES",
    "PRIVATE_READ",
    "PUBLIC_READ",
    "PUBLIC_WRITE",
    "UNLIMITED_PATHS",
    "RateLimitPolicy",
    "account_key",
    "client_key",
    "declared_route_templates",
    "policy_for",
    "served_routes",
    "unlimited_routes",
    "resolve_template",
]
