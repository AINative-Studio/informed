"""Shared pytest fixtures and the shared BDD step library for the backend suite.

``pytest_plugins`` registers tests/step_defs/common_steps.py globally, so every
step and fixture it defines is visible to every feature file without an import.
That is what "API and UI steps share fixtures" means on this side: a new feature
file writes only the steps unique to it.

See tests/step_defs/common_steps.py for how to add a feature and for the pending
(xfail) idiom used by not-yet-implemented steps.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

pytest_plugins = ["tests.step_defs.common_steps"]


@pytest.fixture
def client() -> Iterator[TestClient]:
    """An HTTP client that talks to the app in-process."""
    with TestClient(create_app()) as test_client:
        yield test_client
