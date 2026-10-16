"""Unit tests for the app factory and health endpoint (story 1.1)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app


def test_create_app_returns_a_fastapi_instance() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)
    assert app.title == "Informed API"


def test_health_payload_shape(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body == {"status": "ok", "service": "informed-api", "version": "0.1.0"}


def test_routes_are_only_health_and_ballot_so_far(client: TestClient) -> None:
    """Story 1.5 mounted `/v1/ballot`; the rest of prd.md's Core API has not.

    Replaces story 1.1's "health is the only route" assertion, whose own
    docstring named 1.5 as the story that would change it. The check still
    earns its place: it fails the moment an endpoint is mounted without its
    own tests, which is how a half-built route reaches main unnoticed.
    """
    # Read from the OpenAPI schema rather than walking `app.routes`: an
    # included router is wrapped rather than flattened, so its paths are not on
    # `app.routes` at all, and the schema is the surface clients actually see.
    paths = {path for path in client.get("/openapi.json").json()["paths"] if path.startswith("/v1")}
    assert paths == {"/v1/ballot"}

    # The later Core API endpoints are genuinely absent, not merely untested.
    for not_yet in ("/v1/profiles", "/v1/agent/ask", "/v1/corrections"):
        assert client.get(not_yet).status_code == 404
