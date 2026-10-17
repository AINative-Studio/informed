"""FastAPI application factory for the Informed API.

Routes so far:

    GET /health                       — liveness and readiness (story 1.1)
    GET /v1/ballot                    — districts and sitting officeholders for an address (1.5)
    GET /v1/topics                    — the issue taxonomy as a tree (4.1)
    GET /v1/topics/{id}/stances       — stance cards for one subtopic, randomized (4.1)
    POST /v1/profiles                 — save a tiered interest profile (4.1, building on 14.1)
    /v1/panel/*                       — the advisory panel's wording review workflow (3.3)
    /v1/methodology[/*]               — the published methodology and its changelog (12.2)
    POST /v1/corrections              — report an error on any claim, no account (12.4)
    GET  /v1/corrections/{token}      — the reporter checks their own status (12.4)
    GET  /v1/candidates/{id}/summary-card — shareable, candidate-data-only card (14.6)
    POST /v1/analytics/events         — record a coarse, non-identifying usage event (14.7)
    GET  /v1/analytics/aggregate      — k-anonymous aggregate count, k >= 50 (14.7)

The remaining endpoints in prd.md's Core API table (`/v1/candidates/{id}/
alignment`, `/v1/candidates/{id}/promise-record`, `/v1/agent/ask`) each mount
their own router here behind their own failing tests first.

`zerodb_client` is re-exported from `app.ballot.router` so tests and future
routers have one import path for the dependency they override:

    app = create_app()
    app.dependency_overrides[zerodb_client] = lambda: fake_client

The three story-4.1 routers each declare their own `zerodb_client_for_*`
dependency rather than sharing this one — see `app.topics.router`'s docstring
for why: a test overriding the ballot router's client must not have to know
every other router shares it.
"""

import os
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import __version__
from app.analytics import router as analytics_router
from app.ballot import router as ballot_router
from app.ballot.router import zerodb_client
from app.corrections import router as corrections_router
from app.methodology import router as methodology_router
from app.panel_review import router as panel_review_router
from app.profiles import router as profiles_router
from app.ratelimit import RateLimitMiddleware
from app.stances import router as stances_router
from app.summary_card import router as summary_card_router
from app.topics import router as topics_router

SERVICE_NAME = "informed-api"

#: Browser origins allowed to call the API. The Vite dev server and its preview
#: build, which is what the frontend BDD suite serves. Deployed origins are
#: configured per environment (docs/environments.md, `CORS_ALLOWED_ORIGINS`);
#: this list is the local default, never a wildcard — the API will carry voter
#: profile endpoints (epic 3), and `*` plus credentials is how those leak.
LOCAL_DEV_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
)


class HealthResponse(BaseModel):
    """What the health endpoint returns.

    Deliberately free of anything voter-specific: this payload is logged by
    uptime checks, and voter data must never reach logs (prd.md, privacy first).
    """

    status: str
    service: str
    version: str


def trust_proxy_headers() -> bool:
    """Whether to read the client address from `X-Forwarded-For` (story 14.5).

    Off unless `TRUST_PROXY_HEADERS` is set, because any client can send that
    header: honouring it on a directly-exposed deployment gives every scraper
    an unlimited supply of identities, which is worse than no limiter at all.
    Set it only where exactly one known proxy overwrites the header — see
    `docs/environments.md` and `app.ratelimit.policy.client_key`.
    """
    return os.getenv("TRUST_PROXY_HEADERS", "").strip().lower() in {"1", "true", "yes"}


def create_app(
    *,
    rate_limit: bool = True,
    now: Callable[[], float] | None = None,
    trust_forwarded_for: bool | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    A factory, not a module-level singleton, so tests get a fresh app per case
    and later stories can inject per-environment settings (story 1.6).

    Args:
        rate_limit: Mount story 14.5's limiter. On by default, including in
            tests — an endpoint's tests should exercise the stack it is
            actually served behind. Passing ``False`` is for the one test that
            compares a limited response against an unlimited one byte for
            byte, and for a future story that needs to isolate a behaviour
            from the limiter; it is not a production switch.
        now: Clock for the limiter, injected so its tests advance time by hand
            rather than sleeping. Defaults to `time.monotonic`.
        trust_forwarded_for: Read the client address from `X-Forwarded-For`.
            Defaults to `trust_proxy_headers()`, i.e. to the environment.
            Overridden explicitly by story 14.5's tests, which need many
            simulated source addresses from one TestClient — Starlette
            reports every test request as coming from the same peer, so
            without this a scenario cannot tell "fifty voters" from "one
            scraper", which is the distinction the whole story turns on.
    """
    app = FastAPI(
        title="Informed API",
        version=__version__,
        description="Interest-aligned voter intelligence. Evidence over opinion.",
    )

    # --- middleware order matters here, in one specific way ---------------
    #
    # Starlette applies `add_middleware` in reverse, so the LAST call is the
    # OUTERMOST layer. The limiter is added first and CORS second, which puts
    # CORS outside the limiter. That is deliberate and it is the opposite of
    # what "reject abuse as early as possible" would suggest:
    #
    #   * A 429 produced *outside* CORS carries no `Access-Control-Allow-
    #     Origin`, so a browser cannot read it. The PWA would see an opaque
    #     network error instead of "slow down, retry in 40 seconds" — the
    #     `Retry-After` the limiter went to the trouble of computing would be
    #     invisible to the one client we control.
    #   * The cost of putting CORS first is one header pass on a refused
    #     request. The limiter still short-circuits before routing, request
    #     validation, every dependency and every ZeroDB call, which is where
    #     the actual expense of a request is.
    #
    # Pinned by `test_a_refusal_is_readable_by_a_browser` and
    # `test_a_refused_request_never_reaches_the_endpoint`.
    if rate_limit:
        app.add_middleware(
            RateLimitMiddleware,
            now=now,
            trust_forwarded_for=(
                trust_proxy_headers() if trust_forwarded_for is None else trust_forwarded_for
            ),
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(LOCAL_DEV_ORIGINS),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["content-type"],
        expose_headers=[
            # So the PWA can pace itself against its own allowance rather
            # than discovering the limit by being refused. A cross-origin
            # caller cannot read a response header that is not exposed.
            "retry-after",
            "x-ratelimit-limit",
            "x-ratelimit-remaining",
            "x-ratelimit-policy",
        ],
    )

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        """Liveness and readiness probe for CI and the deploy pipeline."""
        return HealthResponse(status="ok", service=SERVICE_NAME, version=__version__)

    app.include_router(ballot_router)
    app.include_router(topics_router)
    app.include_router(stances_router)
    app.include_router(profiles_router)
    app.include_router(panel_review_router)
    app.include_router(methodology_router)
    app.include_router(corrections_router)
    app.include_router(summary_card_router)
    app.include_router(analytics_router)

    return app


app = create_app()

__all__ = [
    "LOCAL_DEV_ORIGINS",
    "SERVICE_NAME",
    "HealthResponse",
    "app",
    "create_app",
    "zerodb_client",
]
