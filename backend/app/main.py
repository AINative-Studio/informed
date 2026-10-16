"""FastAPI application factory for the Informed API.

Story 1.1 scope: the app serves a health check only. The typed ZeroDB client
(story 1.2), bootstrap (1.3), repository layer (1.4) and the ballot endpoint
(2.4) each add their own wiring here behind their own failing tests first.
"""

from fastapi import FastAPI
from pydantic import BaseModel

from app import __version__

SERVICE_NAME = "informed-api"


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

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        """Liveness and readiness probe for CI and the deploy pipeline."""
        return HealthResponse(status="ok", service=SERVICE_NAME, version=__version__)

    return app


app = create_app()
