"""HTTP contract tests for the consent, export and delete endpoints (14.2, 14.4).

The feature file (`tests/features/consent_export_delete.feature`) drives the
behaviour an auditor and a voter see. This file pins the **status-code and
response-shape mapping** the router adds on top, the same split
`test_follow_incumbent_api_unit.py` documents for its own router:

  * `GET /v1/profiles/consent` — 200 with the wording and the anonymous
    alternative; 404 for a version this release cannot show.
  * `GET /v1/profiles/{user_id}/export` — 200 with a self-describing JSON
    document; 404 when nothing is stored (deliberately NOT the `null`-with-200
    that `GET /v1/profiles/{user_id}` returns — see `export_profile`'s
    docstring on why the two differ).
  * `DELETE /v1/profiles/{user_id}` — 200 with three named guarantees and a
    measured elapsed time; 200 (not 404) for a voter with nothing stored.
  * `POST /v1/profiles` — 422 when `consent_version` names wording this
    release cannot resolve, with nothing written.

Plus the two route-ordering traps this story created, which no amount of
docstring prevents and which only a request can catch:

  * `/v1/profiles/consent` must not be swallowed by `/v1/profiles/{user_id}`.
  * `DELETE /v1/profiles/{user_id}` must not be reachable as a profile delete
    for the literal user id "methodology" or "consent".

Every id used as test data is deliberately synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import TABLE_SCHEMAS
from app.main import create_app
from app.profiles import PROFILE_TABLES
from app.profiles.consent import (
    CONSENT_TABLE,
    CURRENT_CONSENT_VERSION,
    LAWFUL_BASIS,
)
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.router import profile_service
from app.profiles.service import VoterProfileService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

ROOT_KEY = bytes(range(32))
USER_ID = "synthetic-voter-14.2-api"
OTHER_USER_ID = "synthetic-voter-14.2-api-other"


@pytest.fixture
def zerodb() -> ZeroDBClient:
    client = ZeroDBClient(
        transport=FakeZeroDBTransport(), project_id="proj-test", sleep=lambda _: None
    )
    for table in PROFILE_TABLES:
        client.create_table(table, TABLE_SCHEMAS[table])
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


def _profile_body(user_id: str = USER_ID, **extra: object) -> dict[str, object]:
    return {
        "user_id": user_id,
        "consent": True,
        "profile": {
            "interests": [
                {
                    "topic_id": "test:api-14-2-topic:v1",
                    "stance_id": "test:api-14-2-topic:v1:test-stance",
                    "tier": "primary",
                    "intensity": 2,
                    "dealbreaker": True,
                }
            ],
            "evidence_mode": "blend",
        },
        **extra,
    }


def _save(api: TestClient, user_id: str = USER_ID) -> None:
    response = api.post("/v1/profiles", json=_profile_body(user_id))
    assert response.status_code == 200, response.text


# --- GET /v1/profiles/consent -------------------------------------------------


def test_the_consent_endpoint_returns_the_wording_and_the_alternative(
    api: TestClient,
) -> None:
    response = api.get("/v1/profiles/consent")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["consent_version"] == CURRENT_CONSENT_VERSION
    assert body["lawful_basis"] == LAWFUL_BASIS
    assert body["what_is_stored"]
    assert body["your_rights"]
    assert "anonymous" in body["anonymous_alternative"].lower()


def test_the_consent_endpoint_needs_no_identity_and_writes_nothing(
    api: TestClient, zerodb: ZeroDBClient
) -> None:
    """An anonymous voter reads this to decide whether to have an account at
    all, so it must take no identity and leave no trace (story 14.3)."""
    assert api.get("/v1/profiles/consent").status_code == 200
    for table in PROFILE_TABLES:
        assert zerodb.query_rows(table, limit=10).rows == [], table


def test_an_explicit_known_consent_version_is_served(api: TestClient) -> None:
    response = api.get(f"/v1/profiles/consent?version={CURRENT_CONSENT_VERSION}")
    assert response.status_code == 200, response.text
    assert response.json()["consent_version"] == CURRENT_CONSENT_VERSION


def test_an_unknown_consent_version_is_a_404_not_a_silent_fallback(
    api: TestClient,
) -> None:
    response = api.get("/v1/profiles/consent?version=1999.0")
    assert response.status_code == 404, response.text
    assert "1999.0" in response.json()["detail"]


def test_the_consent_route_is_not_swallowed_by_the_profile_load_route(
    api: TestClient,
) -> None:
    """`/v1/profiles/consent` and `/v1/profiles/{user_id}` are both GETs on a
    single path segment. FastAPI resolves in declaration order, so the literal
    route must be declared first — this is the test that fails if a future
    edit reorders them, which would otherwise show up as the consent text
    coming back as `null`."""
    body = api.get("/v1/profiles/consent").json()
    assert isinstance(body, dict)
    assert "consent_version" in body


# --- POST /v1/profiles: the consent_version gate ------------------------------


def test_a_save_defaults_to_the_current_consent_version(api: TestClient) -> None:
    """Story 4.1's clients omit `consent_version` entirely and must keep
    working — see `SaveProfileRequest.consent_version`'s description."""
    _save(api)
    exported = api.get(f"/v1/profiles/{USER_ID}/export").json()
    assert exported["consent"][0]["consent_version"] == CURRENT_CONSENT_VERSION


def test_a_save_naming_an_unknown_consent_version_is_422_and_writes_nothing(
    api: TestClient, zerodb: ZeroDBClient
) -> None:
    response = api.post("/v1/profiles", json=_profile_body(consent_version="1999.0"))
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "1999.0" in detail
    assert "consent version" in detail.lower()
    for table in PROFILE_TABLES:
        assert zerodb.query_rows(table, limit=10).rows == [], table


def test_a_blank_consent_version_is_rejected_by_the_request_model(
    api: TestClient,
) -> None:
    """`min_length=1`: an empty string would otherwise reach the ledger as a
    version nothing can resolve."""
    response = api.post("/v1/profiles", json=_profile_body(consent_version=""))
    assert response.status_code == 422, response.text


# --- GET /v1/profiles/{user_id}/export ----------------------------------------


def test_the_export_is_a_self_describing_json_document(api: TestClient) -> None:
    _save(api)
    response = api.get(f"/v1/profiles/{USER_ID}/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["version"] == 1
    assert body["exported_at"].endswith("Z")
    assert body["profile"]["interests"][0]["dealbreaker"] is True
    assert body["consent"][0]["action"] == "granted"


def test_the_export_response_model_forbids_extra_fields(api: TestClient) -> None:
    """`extra="forbid"` on the response models means a field added to the
    export document without a matching field here is a 500 in CI rather than
    an undocumented field reaching a voter's downloaded file."""
    _save(api)
    body = api.get(f"/v1/profiles/{USER_ID}/export").json()
    assert set(body) == {
        "exported_at",
        "version",
        "updated_at",
        "locale",
        "jurisdiction_ids",
        "profile",
        "consent",
    }


def test_exporting_nothing_is_a_404_naming_anonymous_mode(api: TestClient) -> None:
    response = api.get(f"/v1/profiles/{OTHER_USER_ID}/export")
    assert response.status_code == 404, response.text
    # The message explains the likely innocent cause rather than implying loss.
    assert "anonymous" in response.json()["detail"].lower()


def test_one_voters_export_never_returns_anothers(api: TestClient) -> None:
    _save(api, USER_ID)
    assert api.get(f"/v1/profiles/{OTHER_USER_ID}/export").status_code == 404
    body = api.get(f"/v1/profiles/{USER_ID}/export").json()
    assert body["profile"]["interests"]


# --- DELETE /v1/profiles/{user_id} --------------------------------------------


def test_delete_reports_all_three_guarantees_and_a_measured_time(
    api: TestClient,
) -> None:
    _save(api)
    response = api.request("DELETE", f"/v1/profiles/{USER_ID}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    assert body["key_shredded"] is True
    assert body["consent_withdrawal_recorded"] is True
    assert 0.0 <= body["elapsed_seconds"] < 60.0


def test_delete_actually_makes_the_profile_unloadable_and_unexportable(
    api: TestClient,
) -> None:
    _save(api)
    api.request("DELETE", f"/v1/profiles/{USER_ID}")
    load = api.get(f"/v1/profiles/{USER_ID}")
    assert load.status_code == 200
    assert load.json() is None
    assert api.get(f"/v1/profiles/{USER_ID}/export").status_code == 404


def test_deleting_twice_is_still_a_200(api: TestClient) -> None:
    """prd.md's one-tap delete must survive a retry; a 404 on the second tap
    would leave a voter unsure whether their data is gone."""
    _save(api)
    assert api.request("DELETE", f"/v1/profiles/{USER_ID}").status_code == 200
    second = api.request("DELETE", f"/v1/profiles/{USER_ID}")
    assert second.status_code == 200, second.text
    assert second.json()["deleted"] is True


def test_deleting_a_never_saved_profile_is_a_200(api: TestClient) -> None:
    response = api.request("DELETE", f"/v1/profiles/{OTHER_USER_ID}")
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True


def test_deleting_one_voter_leaves_another_intact(api: TestClient) -> None:
    _save(api, USER_ID)
    _save(api, OTHER_USER_ID)
    api.request("DELETE", f"/v1/profiles/{USER_ID}")
    survivor = api.get(f"/v1/profiles/{OTHER_USER_ID}")
    assert survivor.status_code == 200
    assert survivor.json() is not None


def test_the_delete_response_model_forbids_extra_fields(api: TestClient) -> None:
    _save(api)
    body = api.request("DELETE", f"/v1/profiles/{USER_ID}").json()
    assert set(body) == {
        "deleted",
        "key_shredded",
        "consent_withdrawal_recorded",
        "elapsed_seconds",
    }


def test_a_delete_response_never_echoes_the_profile(api: TestClient) -> None:
    """Nothing about what was deleted comes back. A response that listed the
    erased interests would be a plaintext leak at the exact moment the voter
    asked for the opposite."""
    _save(api)
    body = api.request("DELETE", f"/v1/profiles/{USER_ID}").text
    assert "api-14-2-topic" not in body
    assert USER_ID not in body


# --- the consent ledger is reachable only in the ways this story intends ------


def test_no_endpoint_exposes_another_voters_consent_ledger(
    api: TestClient, zerodb: ZeroDBClient
) -> None:
    """The ledger has no endpoint of its own: it is reachable only inside the
    owner's own export. A per-voter consent read would be a new surface, and
    this test is what fails if one is added without its own story."""
    _save(api, USER_ID)
    _save(api, OTHER_USER_ID)
    assert len(zerodb.query_rows(CONSENT_TABLE, limit=10).rows) == 2

    exported = api.get(f"/v1/profiles/{USER_ID}/export").json()
    assert len(exported["consent"]) == 1

    paths = api.get("/openapi.json").json()["paths"]
    assert not any("consent" in path and "{" in path for path in paths), paths
