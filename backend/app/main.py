"""FastAPI application factory for the Informed API.

Routes so far:

    GET /health                       — liveness and readiness (story 1.1)
    GET /metrics                      — Prometheus-format request and ingest-lag metrics (16.5)
    GET /v1/ballot                    — districts and sitting officeholders for an address (1.5)
    GET /v1/topics                    — the issue taxonomy as a tree (4.1)
    GET /v1/topics/{id}/stances       — stance cards for one subtopic, randomized (4.1)
    POST /v1/profiles                 — save a tiered interest profile (4.1, building on 14.1)
    /v1/panel/*                       — the advisory panel's wording review workflow (3.3)
    /v1/methodology[/*]               — the published methodology and its changelog (12.2)
    POST /v1/corrections              — report an error on any claim, no account (12.4)
    GET  /v1/corrections/{token}      — the reporter checks their own status (12.4)
    GET  /v1/candidates/{id}/summary-card — shareable, candidate-data-only card (14.6)
    GET  /v1/candidates/{id}/alignment    — score, confidence, dealbreakers, reasons (6.4-6.7)
    POST /v1/analytics/events         — record a coarse, non-identifying usage event (14.7)
    GET  /v1/analytics/aggregate      — k-anonymous aggregate count, k >= 50 (14.7)

The remaining endpoints in prd.md's Core API table
(`/v1/candidates/{id}/promise-record`, `/v1/agent/ask`) each mount their own
router here behind their own failing tests first.

`zerodb_client` is re-exported from `app.ballot.router` so tests and future
routers have one import path for the dependency they override:

    app = create_app()
    app.dependency_overrides[zerodb_client] = lambda: fake_client

The three story-4.1 routers each declare their own `zerodb_client_for_*`
dependency rather than sharing this one — see `app.topics.router`'s docstring
for why: a test overriding the ballot router's client must not have to know
every other router shares it.
"""

import logging
import os
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import __version__
from app.analytics import router as analytics_router
from app.ballot import cards_router as ballot_cards_router
from app.ballot import router as ballot_router
from app.ballot.router import zerodb_client
from app.bills.router import router as bills_router
from app.corrections import router as corrections_router
from app.methodology import router as methodology_router
from app.observability import MetricsMiddleware, MetricsRegistry, render_prometheus_text
from app.observability.ingest_lag import compute_ingest_lag, render_ingest_lag_prometheus_text
from app.panel_review import router as panel_review_router
from app.profiles import router as profiles_router
from app.promises.router import router as promises_router
from app.ratelimit import RateLimitMiddleware
from app.scheduling.jobs import default_jobs
from app.scoring.router import router as alignment_router
from app.stances import router as stances_router
from app.summary_card import router as summary_card_router
from app.topics import router as topics_router
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)


def zerodb_client_for_metrics() -> ZeroDBClient | None:
    """The ZeroDB client `/metrics` reads ingest lag from, or `None`.

    A real `Depends`, not a bare `ZeroDBClient.from_env()` call inside the
    handler, for the same reason every other router in this file has its own
    `zerodb_client_for_*`: a test overriding this one dependency should not
    need live `ZERODB_*` credentials, and should not have to know any other
    router shares (or doesn't share) a client. See `app.topics.router`'s
    docstring for the fuller reasoning this pattern follows.

    Returns `None` rather than raising when `ZERODB_*` is not configured
    (`from_env()`'s own `pydantic.ValidationError`). That matters here
    specifically because a FastAPI dependency's exception is raised while
    FastAPI resolves it, BEFORE the `/metrics` handler's own body — and
    therefore before that handler's try/except around ingest lag ever runs.
    A deployment that has not set up ZeroDB yet must still be able to scrape
    request-level metrics (`http_requests_total`,
    `http_request_duration_seconds`), which need no ZeroDB client at all; the
    handler checks for `None` and simply skips the ingest-lag gauges rather
    than failing the whole scrape over a feature the base metrics do not need.
    """
    try:
        return ZeroDBClient.from_env()
    except Exception as exc:  # noqa: BLE001 - config/validation, not a ZeroDB call
        logger.warning("metrics: no ZeroDB client available for ingest lag: %s", exc)
        return None


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

    # One registry per app instance, not a module-level singleton: a test
    # that builds several apps (the whole test suite does, one per TestClient)
    # must not have one app's requests show up in another's /metrics, and a
    # module-level dict would leak exactly that way across tests.
    metrics_registry = MetricsRegistry()
    app.state.metrics_registry = metrics_registry

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

    # Added last, so Starlette's reverse-order application (see above) makes
    # this the OUTERMOST layer of all: it times and counts a request even if
    # the rate limiter refuses it or a CORS preflight short-circuits it. An
    # operator watching error rate needs refusals represented too — see
    # `app.observability.middleware.MetricsMiddleware`'s own docstring.
    app.add_middleware(MetricsMiddleware, registry=metrics_registry)

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        """Liveness and readiness probe for CI and the deploy pipeline."""
        return HealthResponse(status="ok", service=SERVICE_NAME, version=__version__)

    @app.get("/metrics", tags=["ops"])
    def metrics(
        client: Annotated[ZeroDBClient | None, Depends(zerodb_client_for_metrics)],
    ) -> Response:
        """Prometheus text-exposition metrics (story 16.5).

        Two series always present: `http_requests_total` and
        `http_request_duration_seconds`, from this app instance's own
        `MetricsRegistry` (reset per process — see that module's docstring
        for why no shared store exists yet). Plus, best-effort, per-job
        ingest-lag gauges computed live against `scheduled_jobs`.

        "Best-effort" because a scrape must not fail just because ZeroDB is
        unreachable or unconfigured in this environment: `client` is `None`
        when `zerodb_client_for_metrics` could not build one at all (no
        `ZERODB_*` configured), and any `ZeroDBError` the ingest-lag read
        itself raises (a transient outage, a missing table) is caught here —
        either way, the request-metrics half of the response is still
        returned, rather than turning a ZeroDB hiccup or a missing credential
        into a 500 that makes the scraper record THIS endpoint's own
        unavailability instead of what it came to read.
        """
        body = render_prometheus_text(metrics_registry)
        if client is not None:
            try:
                reports = compute_ingest_lag(client, [job.name for job in default_jobs()])
                body += render_ingest_lag_prometheus_text(reports)
            except Exception as exc:  # noqa: BLE001 - a scrape must never 500 on this half
                logger.warning("metrics: could not compute ingest lag: %s", exc)
        return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")

    app.include_router(ballot_router)
    app.include_router(ballot_cards_router)
    app.include_router(bills_router)
    app.include_router(topics_router)
    app.include_router(stances_router)
    app.include_router(profiles_router)
    app.include_router(panel_review_router)
    app.include_router(methodology_router)
    app.include_router(corrections_router)
    app.include_router(summary_card_router)
    app.include_router(analytics_router)
    app.include_router(alignment_router)
    app.include_router(promises_router)

    return app


app = create_app()

__all__ = [
    "LOCAL_DEV_ORIGINS",
    "SERVICE_NAME",
    "HealthResponse",
    "app",
    "create_app",
    "zerodb_client",
    "zerodb_client_for_metrics",
]
