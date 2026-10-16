"""FastAPI application factory for the Informed API.

Routes so far:

    GET /health                       — liveness and readiness (story 1.1)
    GET /v1/ballot                    — districts and sitting officeholders for an address (1.5)
    GET /v1/topics                    — the issue taxonomy as a tree (4.1)
    GET /v1/topics/{id}/stances       — stance cards for one subtopic, randomized (4.1)
    POST /v1/profiles                 — save a tiered interest profile (4.1, building on 14.1)
    /v1/panel/*                       — the advisory panel's wording review workflow (3.3)

The remaining endpoints in prd.md's Core API table (`/v1/candidates/{id}/
alignment`, `/v1/candidates/{id}/promise-record`, `/v1/agent/ask`,
`/v1/corrections`) each mount their own router here behind their own failing
tests first.

`zerodb_client` is re-exported from `app.ballot.router` so tests and future
routers have one import path for the dependency they override:

    app = create_app()
    app.dependency_overrides[zerodb_client] = lambda: fake_client

The three story-4.1 routers each declare their own `zerodb_client_for_*`
dependency rather than sharing this one — see `app.topics.router`'s docstring
for why: a test overriding the ballot router's client must not have to know
every other router shares it.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import __version__
from app.ballot import router as ballot_router
from app.ballot.router import zerodb_client
from app.panel_review import router as panel_review_router
from app.profiles import router as profiles_router
from app.stances import router as stances_router
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


def create_app() -> FastAPI:
    """Build the FastAPI app.

    A factory, not a module-level singleton, so tests get a fresh app per case
    and later stories can inject per-environment settings (story 1.6).
    """
    app = FastAPI(
        title="Informed API",
        version=__version__,
        description="Interest-aligned voter intelligence. Evidence over opinion.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(LOCAL_DEV_ORIGINS),
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
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
