"""HTTP-level tests for the public corrections endpoints (story 12.4).

The feature file drives the happy paths as acceptance criteria. These cover the
wire contract: status codes, what the public responses do and do not carry, and
the shape of the OpenAPI surface — including the deliberate absence of any route
that could change a correction's status.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import TABLE_SCHEMAS
from app.corrections.models import MAX_BODY_LENGTH, TABLE_NAME
from app.corrections.router import zerodb_client_for_corrections
from app.corrections.service import SLA_HOURS, CorrectionService
from app.main import create_app
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

VALID = {
    "target_table": "stances",
    "target_id": "en-US:housing:v1:build-more-homes",
    "body": "The cited roll call is from the wrong session of Congress.",
}


@pytest.fixture
def zerodb() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table(TABLE_NAME, TABLE_SCHEMAS[TABLE_NAME])
    return client


@pytest.fixture
def api(zerodb: ZeroDBClient) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[zerodb_client_for_corrections] = lambda: zerodb
    with TestClient(app) as client:
        yield client


# --- the submission contract ----------------------------------------------


def test_a_submission_is_201_with_a_receipt(api: TestClient) -> None:
    response = api.post("/v1/corrections", json=VALID)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "status",
        "tracking_token",
        "status_path",
        "sla_due_at",
        "sla_hours",
    }
    assert body["status"] == "received"
    assert body["sla_hours"] == SLA_HOURS


def test_the_receipt_does_not_echo_the_report_or_the_email(api: TestClient) -> None:
    """This response reaches logs and screenshots; the reporter already has
    what they sent.
    """
    response = api.post(
        "/v1/corrections", json={**VALID, "contact_email": "reporter@example.invalid"}
    )
    assert VALID["body"] not in response.text
    assert "reporter@example.invalid" not in response.text


def test_the_status_path_in_the_receipt_actually_resolves(api: TestClient) -> None:
    receipt = api.post("/v1/corrections", json=VALID).json()
    followed = api.get(receipt["status_path"])
    assert followed.status_code == 200
    assert followed.json()["status"] == "received"


@pytest.mark.parametrize(
    "payload",
    [
        {**VALID, "target_table": "not_a_real_table"},
        {**VALID, "body": "   "},
        {**VALID, "body": ""},
        {**VALID, "contact_email": "not-an-email"},
        {**VALID, "body": "x" * (MAX_BODY_LENGTH + 1)},
        {"target_id": "x", "body": "Wrong."},
        {"target_table": "stances", "body": "Wrong."},
        {**VALID, "status": "resolved"},
        {**VALID, "resolved_at": "2020-01-01T00:00:00Z"},
        {**VALID, "tracking_token_hash": "x" * 64},
    ],
)
def test_a_bad_submission_is_422_and_stores_nothing(
    api: TestClient, zerodb: ZeroDBClient, payload: dict[str, object]
) -> None:
    assert api.post("/v1/corrections", json=payload).status_code == 422
    assert zerodb.query_rows(TABLE_NAME, limit=10).rows == []


def test_a_422_does_not_disclose_the_schema_or_the_store(api: TestClient) -> None:
    """The message names what the caller sent — never a table list, a project
    id, or anything about ZeroDB behind it.
    """
    text = api.post("/v1/corrections", json={**VALID, "target_table": "nope"}).text
    assert "nope" in text
    assert "proj-test" not in text
    assert "zerodb" not in text.lower()
    assert "stances" not in text


# --- the status lookup contract -------------------------------------------


def test_the_status_lookup_returns_status_only(api: TestClient) -> None:
    receipt = api.post(
        "/v1/corrections", json={**VALID, "contact_email": "reporter@example.invalid"}
    ).json()
    body = api.get(f"/v1/corrections/{receipt['tracking_token']}").json()
    assert set(body) == {
        "status",
        "submitted_at",
        "sla_due_at",
        "sla_hours",
        "past_sla",
        "resolved_at",
    }
    assert VALID["body"] not in str(body)
    assert "reporter@example.invalid" not in str(body)
    assert receipt["id"] not in str(body)


def test_an_unknown_token_is_404_with_one_message(api: TestClient) -> None:
    """One answer for every miss, so the endpoint is not an oracle for which
    ids exist.
    """
    first = api.get("/v1/corrections/" + "a" * 43)
    second = api.get("/v1/corrections/" + "b" * 43)
    assert first.status_code == second.status_code == 404
    assert first.json()["detail"] == second.json()["detail"]


def test_the_row_id_is_404_on_the_status_lookup(api: TestClient) -> None:
    receipt = api.post("/v1/corrections", json=VALID).json()
    assert api.get(f"/v1/corrections/{receipt['id']}").status_code == 404


@pytest.mark.parametrize("junk", ["short", "../../etc/passwd", "x" * 200])
def test_a_malformed_token_is_rejected_at_the_edge(api: TestClient, junk: str) -> None:
    """A token that cannot be one never reaches ZeroDB."""
    assert api.get(f"/v1/corrections/{junk}").status_code in {404, 422}


def test_one_reporters_token_does_not_open_anothers_report(api: TestClient) -> None:
    first = api.post(
        "/v1/corrections",
        json={**VALID, "body": "FIRST-REPORT", "contact_email": "first@example.invalid"},
    ).json()
    second = api.post(
        "/v1/corrections",
        json={**VALID, "body": "SECOND-REPORT", "contact_email": "second@example.invalid"},
    ).json()
    for token in (first["tracking_token"], second["tracking_token"]):
        text = api.get(f"/v1/corrections/{token}").text
        assert "FIRST-REPORT" not in text
        assert "SECOND-REPORT" not in text
        assert "@example.invalid" not in text


def test_the_reporter_sees_a_status_change_made_by_ops(
    api: TestClient, zerodb: ZeroDBClient
) -> None:
    """backlog.md 12.4: "status is visible to the reporter". End to end: ops
    moves it through the service, the reporter reads it over HTTP.
    """
    receipt = api.post("/v1/corrections", json=VALID).json()
    service = CorrectionService(zerodb)
    for status in ("triaged", "resolved"):
        service.set_status(receipt["id"], status=status)
        body = api.get(f"/v1/corrections/{receipt['tracking_token']}").json()
        assert body["status"] == status
    assert body["resolved_at"]


# --- no credentials, and no way to change a status over HTTP ---------------


def test_both_endpoints_answer_with_no_authorization_header(api: TestClient) -> None:
    """prd.md requires a public correction form. A test that quietly
    authenticated would prove nothing about it.
    """
    receipt = api.post("/v1/corrections", json=VALID)
    assert "authorization" not in {k.lower() for k in receipt.request.headers}
    assert receipt.status_code == 201
    lookup = api.get(f"/v1/corrections/{receipt.json()['tracking_token']}")
    assert "authorization" not in {k.lower() for k in lookup.request.headers}
    assert lookup.status_code == 200


def test_no_mounted_route_can_change_a_corrections_status(api: TestClient) -> None:
    """The deliberate omission, asserted.

    There is no auth in this codebase, and an unauthenticated status write
    would let anyone mark an inconvenient correction `rejected` — which is
    exactly how a correction gets buried. Triage is a service method reached
    by `scripts/triage_correction.py` until an auth story lands.
    """
    receipt = api.post("/v1/corrections", json=VALID).json()
    for method in ("PATCH", "PUT", "DELETE", "POST"):
        for path in (
            f"/v1/corrections/{receipt['id']}",
            f"/v1/corrections/{receipt['tracking_token']}",
        ):
            # `request` rather than the per-verb helpers: httpx's `delete()`
            # takes no `json` argument, and the point is to try the verb WITH
            # a status body — the shape a caller trying to bury a correction
            # would send.
            response = api.request(method, path, json={"status": "rejected"})
            assert response.status_code in {404, 405}, f"{method} {path}"
    # And on the collection path, where only POST (submission) is allowed.
    for method in ("PATCH", "PUT", "DELETE"):
        response = api.request(method, "/v1/corrections", json={"status": "rejected"})
        assert response.status_code in {404, 405}, method


def test_the_openapi_surface_is_one_post_and_one_get(api: TestClient) -> None:
    paths = api.get("/openapi.json").json()["paths"]
    assert set(paths["/v1/corrections"]) == {"post"}
    assert set(paths["/v1/corrections/{tracking_token}"]) == {"get"}


def test_neither_endpoint_declares_a_security_requirement(api: TestClient) -> None:
    """Public by design, and visible as such in the published schema."""
    paths = api.get("/openapi.json").json()["paths"]
    assert "security" not in paths["/v1/corrections"]["post"]
    assert "security" not in paths["/v1/corrections/{tracking_token}"]["get"]
