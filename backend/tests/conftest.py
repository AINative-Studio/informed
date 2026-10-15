"""Shared pytest fixtures for the Informed backend test suite.

Story 1.7 replaces this with the shared BDD step library. For story 1.1 it only
needs to hand tests an HTTP client bound to the FastAPI app so the walking
skeleton can be exercised without a live server.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """An HTTP client that talks to the app in-process."""
    with TestClient(create_app()) as test_client:
        yield test_client
