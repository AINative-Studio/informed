"""Step definitions for backend/tests/features/health.feature (story 1.1)."""

import httpx
import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, scenarios, then, when

scenarios("../features/health.feature")


@pytest.fixture
def response_holder() -> dict[str, httpx.Response]:
    """Carries the response between the When and Then steps."""
    return {}


@given("the Informed API is running")
def api_is_running(client: TestClient) -> TestClient:
    return client


@when("I request the health endpoint")
def request_health(client: TestClient, response_holder: dict[str, httpx.Response]) -> None:
    response_holder["response"] = client.get("/health")


@when("I request an unknown path")
def request_unknown_path(
    client: TestClient, response_holder: dict[str, httpx.Response]
) -> None:
    response_holder["response"] = client.get("/no-such-path")


@then("the response status is 200")
def status_is_200(response_holder: dict[str, httpx.Response]) -> None:
    assert response_holder["response"].status_code == 200


@then("the response status is 404")
def status_is_404(response_holder: dict[str, httpx.Response]) -> None:
    assert response_holder["response"].status_code == 404


@then("the response says the service is healthy")
def body_says_healthy(response_holder: dict[str, httpx.Response]) -> None:
    body = response_holder["response"].json()
    assert body["status"] == "ok"
    assert body["service"] == "informed-api"
