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


def test_health_is_the_only_route_for_now(client: TestClient) -> None:
    """The walking skeleton exposes no feature endpoints yet (stories 1.5, 2.4)."""
    assert client.get("/v1/ballot").status_code == 404
