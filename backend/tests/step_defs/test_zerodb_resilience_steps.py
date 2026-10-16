"""Step definitions for backend/tests/features/zerodb_resilience.feature (story 1.2).

Retries, backoff and error mapping, all against the in-memory fake. Sleeps are
captured rather than performed, so the suite stays fast.
"""

import inspect
from typing import Any

import httpx
import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.zerodb import ZeroDBClient
from app.zerodb import errors as zerodb_errors
from app.zerodb.errors import ZeroDBError
from app.zerodb.testing import FakeZeroDBTransport
from app.zerodb.transport import ZeroDBTransport

scenarios("../features/zerodb_resilience.feature")


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(fake: FakeZeroDBTransport, sleeps: list[float]) -> ZeroDBClient:
    return ZeroDBClient(
        transport=fake,
        project_id="proj-test",
        max_attempts=4,
        backoff_base=0.01,
        sleep=sleeps.append,
    )


@pytest.fixture
def outcome() -> dict[str, Any]:
    return {}


@given("a ZeroDB client backed by an in-memory fake")
def client_with_fake(client: ZeroDBClient) -> ZeroDBClient:
    return client


@given(parsers.parse("the next {count:d} requests fail with status {status:d}"))
def next_requests_fail_status(fake: FakeZeroDBTransport, count: int, status: int) -> None:
    fake.fail_next(count, status=status)


@given(parsers.parse("the next {count:d} requests fail with a connection error"))
def next_requests_fail_connection(fake: FakeZeroDBTransport, count: int) -> None:
    fake.fail_next(count, exception=httpx.ConnectError("connection refused"))


@given(parsers.parse("the next {count:d} requests fail with a timeout"))
def next_requests_fail_timeout(fake: FakeZeroDBTransport, count: int) -> None:
    fake.fail_next(count, exception=httpx.ReadTimeout("read timed out"))


@given(parsers.parse("every request fails with status {status:d}"))
def every_request_fails_status(fake: FakeZeroDBTransport, status: int) -> None:
    fake.always_fail(status=status)


@given("every request fails with a connection error")
def every_request_fails_connection(fake: FakeZeroDBTransport) -> None:
    fake.always_fail(exception=httpx.ConnectError("connection refused"))


@given("every request returns a body that is not JSON")
def every_request_returns_garbage(fake: FakeZeroDBTransport) -> None:
    fake.always_return_non_json()


@when(parsers.parse('I create the table "{table}" with fields "{spec}"'))
def create_table(client: ZeroDBClient, outcome: dict[str, Any], table: str, spec: str) -> None:
    fields = {name: "string" for name in spec.split(",")}
    try:
        outcome["value"] = client.create_table(table, fields)
    except BaseException as exc:  # noqa: BLE001 - the assertion is about what was raised
        outcome["error"] = exc


@then("the call succeeds")
def call_succeeds(outcome: dict[str, Any]) -> None:
    assert "error" not in outcome, f"unexpected error: {outcome.get('error')!r}"
    assert outcome["value"] is not None


@then(parsers.parse("the fake saw {count:d} request attempt"))
@then(parsers.parse("the fake saw {count:d} request attempts"))
def fake_saw_n_attempts(fake: FakeZeroDBTransport, count: int) -> None:
    assert len(fake.calls) == count


@then(parsers.parse('the call raises "{exception_name}"'))
def call_raises(outcome: dict[str, Any], exception_name: str) -> None:
    error = outcome.get("error")
    assert error is not None, "expected an error but the call succeeded"
    expected = getattr(zerodb_errors, exception_name)
    assert isinstance(error, expected), f"expected {exception_name}, got {error!r}"


@then("the recorded sleeps grow exponentially")
def sleeps_grow_exponentially(sleeps: list[float]) -> None:
    assert len(sleeps) >= 2, f"expected at least 2 sleeps, got {sleeps}"
    for earlier, later in zip(sleeps, sleeps[1:], strict=False):
        assert later > earlier, f"backoff did not grow: {sleeps}"


@then("the raised error message mentions the ZeroDB detail")
def error_mentions_detail(outcome: dict[str, Any]) -> None:
    assert FakeZeroDBTransport.FAILURE_DETAIL in str(outcome["error"])


@then(parsers.parse('every ZeroDB domain error descends from "{base_name}"'))
def errors_descend_from_base(base_name: str) -> None:
    base = getattr(zerodb_errors, base_name)
    domain_errors = [
        obj
        for name, obj in vars(zerodb_errors).items()
        if inspect.isclass(obj) and name.startswith("ZeroDB") and issubclass(obj, Exception)
    ]
    assert len(domain_errors) > 1
    assert all(issubclass(err, base) for err in domain_errors)


@then("the raised error is not an httpx exception")
def error_is_not_httpx(outcome: dict[str, Any]) -> None:
    error = outcome["error"]
    assert not isinstance(error, httpx.HTTPError)
    assert isinstance(error, ZeroDBError)


@then("the request carried an Authorization bearer header")
def request_had_bearer(fake: FakeZeroDBTransport) -> None:
    assert fake.calls
    assert fake.calls[0].headers.get("Authorization", "").startswith("Bearer ")


@then("no request body or header is written to the log")
def nothing_secret_logged(caplog: pytest.LogCaptureFixture) -> None:
    # Privacy first (prd.md): the client must not log payloads or credentials.
    assert FakeZeroDBTransport.API_KEY not in caplog.text


@then("the client accepts any transport implementing the ZeroDB transport protocol")
def transport_is_a_protocol() -> None:
    # The seam that makes swapping REST for MCP a one-file change (story 1.2 note).
    assert isinstance(FakeZeroDBTransport(), ZeroDBTransport)
