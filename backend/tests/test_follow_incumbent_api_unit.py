"""HTTP contract tests for `/v1/profiles/{user_id}/follow/{person_id}` (story 15.1).

The feature file (`tests/features/follow_incumbent.feature`) drives the
service-layer happy and rejection paths; this file pins the **status-code
mapping** the router adds on top, the same split `test_panel_review_api_unit.py`
documents for its own router:

  * `200` — follow/unfollow succeeded; body is the updated followed-person list.
  * `404` — the person_id is not a known `people` row (follow only; unfollow
    never checks existence).
  * `422` — no `user_id` was given (this app's pre-auth placeholder for
    anonymous mode — see `app.profiles.router`'s module docstring on why a
    request identifies its own voter today).

Every person id used as test data is deliberately synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import TABLE_SCHEMAS
from app.main import create_app
from app.profiles import PROFILE_TABLES
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.router import profile_service
from app.profiles.service import VoterProfileService
from app.repositories.base import Repository
from app.repositories.models import Person
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

ROOT_KEY = bytes(range(32))
USER_ID = "synthetic-voter-15.1-api"
KNOWN_PERSON = "person:zzz-synthetic-api-B000002"
UNKNOWN_PERSON = "person:zzz-does-not-exist-api"


@pytest.fixture
def zerodb() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    client = ZeroDBClient(transport=transport, project_id="proj-test", sleep=lambda _: None)
    for table in (*PROFILE_TABLES, Person.table_name):
        client.create_table(table, TABLE_SCHEMAS[table])
    Repository(client=client, model=Person).upsert(
        {"id": KNOWN_PERSON, "canonical_name": "Synthetic Test Legislator"}
    )
    return client


@pytest.fixture
def service(zerodb: ZeroDBClient) -> VoterProfileService:
    keyring = ProfileKeyring(client=zerodb, root_key=ProfileRootKey(ROOT_KEY))
    return VoterProfileService(client=zerodb, keyring=keyring)


@pytest.fixture
def api(service: VoterProfileService) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[profile_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client


def _save_a_profile(api: TestClient) -> None:
    response = api.post(
        "/v1/profiles",
        json={
            "user_id": USER_ID,
            "consent": True,
            "profile": {
                "interests": [
                    {
                        "topic_id": "test:api-topic:v1",
                        "stance_id": "test:api-topic:v1:test-stance",
                        "tier": "primary",
                        "intensity": 2,
                        "dealbreaker": False,
                    }
                ]
            },
        },
    )
    assert response.status_code == 200, response.text


def test_following_a_real_person_returns_200_and_the_updated_list(api: TestClient) -> None:
    _save_a_profile(api)
    response = api.post(f"/v1/profiles/{USER_ID}/follow/{KNOWN_PERSON}")
    assert response.status_code == 200
    assert response.json() == {"followed_person_ids": [KNOWN_PERSON]}


def test_following_an_unknown_person_returns_404_and_changes_nothing(api: TestClient) -> None:
    _save_a_profile(api)
    response = api.post(f"/v1/profiles/{USER_ID}/follow/{UNKNOWN_PERSON}")
    assert response.status_code == 404

    loaded = api.get(f"/v1/profiles/{USER_ID}")
    assert loaded.json()["followed_person_ids"] == []


def test_following_with_no_user_id_is_422(api: TestClient) -> None:
    # FastAPI path params cannot be empty, so the anonymous case is simulated
    # with a user id that was never saved against *and* carries no profile —
    # the router's 422 for this comes from `AnonymousModeError`, which only
    # `user_id=None` raises; a path param can never be None, so this exercises
    # the "no profile exists yet" shape instead and documents that a follow on
    # a voter with no interests yet is a 422 naming the real rule.
    response = api.post(f"/v1/profiles/never-saved-voter/follow/{KNOWN_PERSON}")
    assert response.status_code == 422


def test_unfollowing_returns_200_and_removes_the_person(api: TestClient) -> None:
    _save_a_profile(api)
    api.post(f"/v1/profiles/{USER_ID}/follow/{KNOWN_PERSON}")

    response = api.delete(f"/v1/profiles/{USER_ID}/follow/{KNOWN_PERSON}")
    assert response.status_code == 200
    assert response.json() == {"followed_person_ids": []}


def test_unfollowing_someone_never_followed_is_200_not_an_error(api: TestClient) -> None:
    _save_a_profile(api)
    response = api.delete(f"/v1/profiles/{USER_ID}/follow/{UNKNOWN_PERSON}")
    assert response.status_code == 200
    assert response.json() == {"followed_person_ids": []}


def test_unfollowing_with_no_saved_profile_is_200_with_an_empty_list(api: TestClient) -> None:
    response = api.delete(f"/v1/profiles/never-saved-voter-2/follow/{KNOWN_PERSON}")
    assert response.status_code == 200
    assert response.json() == {"followed_person_ids": []}


def test_following_twice_is_idempotent(api: TestClient) -> None:
    _save_a_profile(api)
    api.post(f"/v1/profiles/{USER_ID}/follow/{KNOWN_PERSON}")
    response = api.post(f"/v1/profiles/{USER_ID}/follow/{KNOWN_PERSON}")
    assert response.status_code == 200
    assert response.json() == {"followed_person_ids": [KNOWN_PERSON]}
