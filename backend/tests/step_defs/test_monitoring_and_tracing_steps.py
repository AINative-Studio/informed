"""Step definitions for tests/features/monitoring_and_tracing.feature (16.5).

Runs the real FastAPI app (`/metrics` included) against story 1.2's in-memory
ZeroDB fake, the same no-network-call pattern every backend feature in this
suite follows — see `test_quick_onboarding_steps.py` for the precedent this
file's `api` fixture is built from.

Every voter and interest here is synthetic (`test-topic-alpha`, matching
story 14.1's own feature). CLAUDE.md forbids real voter data in a fixture,
and this feature's whole point is proving that data never reaches a log line.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.main import create_app, zerodb_client_for_metrics
from app.observability.ingest_lag import INGEST_LAG_ALERT_THRESHOLD_SECONDS, compute_ingest_lag
from app.observability.logging_config import PlaintextLeakGuard
from app.profiles import PROFILE_TABLES
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.router import profile_service, zerodb_client_for_profiles
from app.profiles.service import VoterProfileService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/monitoring_and_tracing.feature")

#: A fixed "now" so "last succeeded N hours ago" is deterministic.
_NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)

#: A 32-byte test root key, the same obvious synthetic value
#: `test_quick_onboarding_steps.py` uses — never written to a tracked file.
TEST_ROOT_KEY = bytes(range(32))

SYNTHETIC_USER = "synthetic-16-5-voter"
PRIMARY_TOPIC_ID = "test:test-topic-alpha:v1"
PRIMARY_STANCE_ID = "test:test-topic-alpha:v1:test-stance"


@pytest.fixture
def transport() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def zdb(transport: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table("scheduled_jobs", {"id": "string", "last_run_at": "string"})
    for table in PROFILE_TABLES:
        client.create_table(table, {"id": "string"})
    return client


@pytest.fixture
def keyring(zdb: ZeroDBClient) -> ProfileKeyring:
    return ProfileKeyring(client=zdb, root_key=ProfileRootKey(TEST_ROOT_KEY))


@pytest.fixture
def api(zdb: ZeroDBClient, keyring: ProfileKeyring) -> Any:
    """The real app, `/metrics` included, wired to the fake ZeroDB client."""
    app = create_app()
    app.dependency_overrides[zerodb_client_for_profiles] = lambda: zdb
    app.dependency_overrides[profile_service] = lambda: VoterProfileService(
        client=zdb, keyring=keyring
    )
    app.dependency_overrides[zerodb_client_for_metrics] = lambda: zdb
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def result() -> dict[str, Any]:
    return {}


# --- GET /metrics exposes request latency and status counts ----------------


@given("a running instance of the API")
def running_api(api: TestClient) -> TestClient:
    return api


@when(parsers.parse('a request is made to "{verb} {path}"'))
def make_request(api: TestClient, result: dict[str, Any], verb: str, path: str) -> None:
    response = api.request(verb, path)
    result["response"] = response
    result.setdefault("responses", []).append(response)


@when(parsers.parse('a second request is made to "{verb} {path}"'))
def make_second_request(api: TestClient, result: dict[str, Any], verb: str, path: str) -> None:
    response = api.request(verb, path)
    result["response"] = response
    result.setdefault("responses", []).append(response)


@then("the metrics response is valid Prometheus text exposition format")
def metrics_is_valid_prometheus_text(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    # Every metric family in the exposition format opens with its own
    # "# HELP ..." / "# TYPE ..." pair before any sample line — the minimal,
    # format-level check that this is actually Prometheus text and not some
    # other 200 OK body.
    assert "# HELP" in body
    assert "# TYPE" in body


@then("it contains a counter for http requests by method, route and status")
def metrics_has_request_counter(result: dict[str, Any]) -> None:
    body = result["response"].text
    assert "# TYPE http_requests_total counter" in body
    assert "http_requests_total{" in body
    # The /health request this scenario made just before scraping /metrics
    # must itself be reflected — proving the counter is live, not a static
    # template with no real observations in it.
    assert 'method="GET"' in body
    assert 'route="/health"' in body


@then("it contains a histogram for http request duration in seconds")
def metrics_has_duration_histogram(result: dict[str, Any]) -> None:
    body = result["response"].text
    assert "# TYPE http_request_duration_seconds histogram" in body
    assert "http_request_duration_seconds_bucket{" in body
    assert "http_request_duration_seconds_sum{" in body
    assert "http_request_duration_seconds_count{" in body


# --- /metrics is never rate limited -----------------------------------------


@then(parsers.parse('"{verb} {path}" is not subject to any rate-limit policy'))
def not_rate_limited(api: TestClient, verb: str, path: str) -> None:
    from app.ratelimit import UNLIMITED_PATHS

    assert path in UNLIMITED_PATHS
    # Belt and suspenders: hammer it well past any real policy's limit and
    # confirm none of the calls is ever refused.
    for _ in range(200):
        response = api.request(verb, path)
        assert response.status_code != 429


# --- Ingest lag is computed per job -----------------------------------------


@given(parsers.parse('a scheduled job "{job_name}" last succeeded {hours:d} hours ago'))
def job_last_succeeded_hours_ago(zdb: ZeroDBClient, job_name: str, hours: int) -> None:
    last_run = _NOW - timedelta(hours=hours)
    last_run_at = last_run.isoformat().replace("+00:00", "Z")
    zdb.upsert_row("scheduled_jobs", job_name, {"id": job_name, "last_run_at": last_run_at})


@when("the ingest lag is computed for every registered job")
def compute_lag_for_every_job(zdb: ZeroDBClient, result: dict[str, Any]) -> None:
    job_names = ["load_house_rollcalls", "a-job-that-has-never-run"]
    reports = compute_ingest_lag(zdb, job_names, now=_NOW)
    result["lag_reports"] = {report.job_name: report for report in reports}


@then(parsers.parse('"{job_name}" reports a lag of approximately {hours:d} hours'))
def lag_is_approximately_hours(result: dict[str, Any], job_name: str, hours: int) -> None:
    report = result["lag_reports"][job_name]
    assert report.lag_seconds is not None
    expected_seconds = hours * 3600
    assert abs(report.lag_seconds - expected_seconds) < 5


@then("a job that has never run reports no last-success timestamp")
def never_run_job_has_no_timestamp(result: dict[str, Any]) -> None:
    report = result["lag_reports"]["a-job-that-has-never-run"]
    assert report.last_success_at is None
    assert report.lag_seconds is None
    assert report.over_threshold is False


@then(parsers.parse('"{job_name}" is flagged as over the 6 hour ingest-lag alert threshold'))
def job_is_over_threshold(result: dict[str, Any], job_name: str) -> None:
    report = result["lag_reports"][job_name]
    assert report.over_threshold is True
    assert report.lag_seconds is not None
    assert report.lag_seconds > INGEST_LAG_ALERT_THRESHOLD_SECONDS


# --- No plaintext voter data in any log line over a full request cycle -----


@given(parsers.parse('a synthetic voter profile with a primary interest "{topic}"'))
def synthetic_profile(result: dict[str, Any], topic: str) -> None:
    assert topic == "test-topic-alpha"
    result["profile_body"] = {
        "interests": [
            {
                "topic_id": PRIMARY_TOPIC_ID,
                "stance_id": PRIMARY_STANCE_ID,
                "tier": "primary",
                "intensity": 2,
                "dealbreaker": False,
            }
        ],
    }


@when("the voter saves their profile over HTTP while logs are captured at every level")
def save_over_http_capturing_logs(
    api: TestClient, result: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    body = {
        "user_id": SYNTHETIC_USER,
        "consent": True,
        "profile": result["profile_body"],
    }
    with caplog.at_level(logging.DEBUG):
        save_response = api.post("/v1/profiles", json=body)
        read_response = api.get(f"/v1/profiles/{SYNTHETIC_USER}")
    result["response"] = save_response
    result["read_response"] = read_response
    result["request_body_json"] = body
    # The same "name message" join `test_encrypted_profile_storage_steps.py`
    # uses, so this scenario's assertions read the identical shape of text
    # that story's own "Plaintext is never logged" scenario checks.
    result["log_text"] = "\n".join(
        f"{record.name} {record.getMessage()}" for record in caplog.records
    )


@then("no captured log line contains any interest topic id")
def logs_have_no_topic_id(result: dict[str, Any]) -> None:
    hits = PlaintextLeakGuard.contains_forbidden(result["log_text"], [PRIMARY_TOPIC_ID])
    assert hits == []


@then("no captured log line contains any stance id")
def logs_have_no_stance_id(result: dict[str, Any]) -> None:
    hits = PlaintextLeakGuard.contains_forbidden(result["log_text"], [PRIMARY_STANCE_ID])
    assert hits == []


@then("no captured log line contains any key material")
def logs_have_no_key_material(result: dict[str, Any]) -> None:
    import base64

    secrets = [
        TEST_ROOT_KEY.hex(),
        base64.b64encode(TEST_ROOT_KEY).decode(),
    ]
    hits = PlaintextLeakGuard.contains_forbidden(result["log_text"], secrets)
    assert hits == []


@then("no captured log line contains the raw request body")
def logs_have_no_raw_request_body(result: dict[str, Any]) -> None:
    import json

    # The exact JSON the client sent, serialized the same way httpx/FastAPI
    # would if some handler ever carelessly logged `await request.json()` or
    # `body` wholesale — proving this is absent is a stronger guarantee than
    # checking the topic/stance ids individually, because it also rules out
    # some OTHER field of the payload leaking that this scenario did not
    # think to name ahead of time.
    serialized = json.dumps(result["request_body_json"])
    assert serialized not in result["log_text"]


# --- The repo-wide guard rejects a line that would have leaked --------------


@given("a log record carrying a synthetic voter interest topic id in its message")
def log_record_with_leak(result: dict[str, Any]) -> None:
    logger = logging.getLogger("tests.monitoring_and_tracing.synthetic")
    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        0,
        f"voter saved interest {PRIMARY_TOPIC_ID}",
        (),
        None,
    )
    result["leaking_record"] = record


@when("the plaintext-in-logs guard inspects that record")
def guard_inspects_record(result: dict[str, Any]) -> None:
    record = result["leaking_record"]
    rendered = record.getMessage()
    result["guard_violations"] = PlaintextLeakGuard.contains_forbidden(rendered, [PRIMARY_TOPIC_ID])


@then("the guard reports it as a violation")
def guard_reports_violation(result: dict[str, Any]) -> None:
    assert result["guard_violations"] == [PRIMARY_TOPIC_ID]
