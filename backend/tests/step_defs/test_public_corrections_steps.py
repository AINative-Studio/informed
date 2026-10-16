"""Step definitions for tests/features/public_corrections.feature (story 12.4).

Runs against story 1.2's in-memory fake transport over a real FastAPI
`TestClient`, never live ZeroDB, so CI needs no credentials — the rule every
backend feature in this suite follows. The live confirmation lives in
`tests/test_public_corrections_live_shapes.py` and
`scripts/verify_public_corrections_live.py`.

`break_server_side_filter()` is applied for the reason stories 3.1, 3.2 and
12.2's steps apply it: live ZeroDB's server-side `/query` filter was measured
to match nothing (story 1.4's finding), so every read here must rest on the
client's own paging fallback. This matters more in this story than in most —
both the token lookup and the SLA sweep are filtered reads, and a fake with a
working filter would let them pass for a reason production does not have.

No request in this file carries an `Authorization` header, and that absence is
the subject of the "with no authentication" steps: the story is "as anyone",
and a step that quietly authenticated would prove nothing about it.

Every email address here is under `.invalid` (RFC 2606), so no scenario can
mail a real person, and no reporter in this file is a real one — prd.md forbids
real voter or reporter data in fixtures and test output.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import TABLE_SCHEMAS
from app.corrections.models import TABLE_NAME, CorrectionRow
from app.corrections.retention import sweep_expired_contact_emails
from app.corrections.router import zerodb_client_for_corrections
from app.corrections.service import (
    SLA_HOURS,
    CorrectionService,
    UnknownCorrectionError,
)
from app.main import create_app
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/public_corrections.feature")

#: Words whose presence in a public response would mean voter data had leaked.
#: Checked against the raw JSON a caller receives, not against the model —
#: the question is what bytes go over the wire. `stance` is excluded here (it
#: is in `corrections.target_id` by design, which is a public row id, not voter
#: data) but `profile`, `interest` and `payload` are not.
VOTER_WORDS = ("voter", "profile", "interest", "payload", "dealbreaker", "user_id")

#: A synthetic stance id of the shape story 3.2 writes. No real stance is
#: needed: the whole point of scenario "A correction about an unknown row is
#: accepted" is that this endpoint does not resolve its target.
STANCE_TARGET = "en-US:housing:v1:build-more-homes"

#: What a reporter actually writes in the form. Synthetic.
REPORT_BODY = "The cited roll call is from the wrong session of Congress."


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table(TABLE_NAME, TABLE_SCHEMAS[TABLE_NAME])
    return client


@pytest.fixture
def corrections_api(zerodb: ZeroDBClient) -> Iterator[TestClient]:
    """The real app with only the ZeroDB dependency swapped for the fake."""
    app = create_app()
    app.dependency_overrides[zerodb_client_for_corrections] = lambda: zerodb
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def service(zerodb: ZeroDBClient) -> CorrectionService:
    return CorrectionService(zerodb)


@pytest.fixture
def seen() -> dict[str, Any]:
    """Carries responses, tokens and ids between When and Then."""
    return {}


def _body(seen: dict[str, Any]) -> dict[str, Any]:
    response: Response = seen["response"]
    return response.json()


def _stored(zerodb: ZeroDBClient, seen: dict[str, Any]) -> CorrectionRow:
    row = zerodb.get_row(TABLE_NAME, seen["correction_id"])
    assert row is not None, f"no stored correction {seen['correction_id']!r}"
    return CorrectionRow.model_validate(row.row_data)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _submit(api: TestClient, seen: dict[str, Any], payload: dict[str, Any]) -> None:
    """POST a correction and remember the id and token it hands back."""
    seen["response"] = api.post("/v1/corrections", json=payload)
    assert "authorization" not in {key.lower() for key in seen["response"].request.headers}, (
        "this story is 'as anyone'; the submission must carry no credentials"
    )
    if seen["response"].status_code == 201:
        body = seen["response"].json()
        seen["correction_id"] = body["id"]
        seen["tracking_token"] = body["tracking_token"]


def _plant(
    service: CorrectionService,
    seen: dict[str, Any],
    *,
    created_hours_ago: float = 0.0,
    status: str = "received",
    resolved_days_ago: float | None = None,
    contact_email: str | None = None,
    body: str = REPORT_BODY,
    target_id: str = STANCE_TARGET,
    remember_as: str = "correction_id",
) -> str:
    """Write a correction directly, dated into the past.

    Backdating goes through the service's injectable clock rather than by
    patching the row afterwards, so the stored `sla_due_at` is the one the real
    creation path would have computed for that moment — which is the field two
    of these scenarios turn on.
    """
    created = datetime.now(UTC) - timedelta(hours=created_hours_ago)
    record = service.submit(
        target_table="stances",
        target_id=target_id,
        body=body,
        contact_email=contact_email,
        now=created,
    )
    seen[remember_as] = record.id
    seen.setdefault(f"token_for_{record.id}", record.tracking_token)
    seen["tracking_token"] = record.tracking_token
    if status != "received":
        resolved_at = (
            None
            if resolved_days_ago is None
            else datetime.now(UTC) - timedelta(days=resolved_days_ago)
        )
        service.set_status(record.id, status=status, now=resolved_at or datetime.now(UTC))
    return record.id


# --- Given ----------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _client_backed_by_fake(zerodb: ZeroDBClient) -> None:
    assert zerodb is not None


@given("the corrections table exists")
def _table_exists(zerodb: ZeroDBClient) -> None:
    tables = zerodb.list_tables()
    assert any(table.table_name == TABLE_NAME for table in tables)


#: Accepts both "1 hour ago" and "73 hours ago" so the Gherkin reads as
#: English. `parsers.parse`'s `{n:g}` does not match an optional plural, and a
#: scenario that had to say "1.0 hours" to satisfy a parser is a scenario
#: written for the test framework rather than for a reader.
@given(parsers.re(r"a correction submitted (?P<hours>[\d.]+) hours? ago and never triaged"))
def _submitted_hours_ago(service: CorrectionService, seen: dict[str, Any], hours: str) -> None:
    _plant(service, seen, created_hours_ago=float(hours))


@given(parsers.parse("a correction submitted {days:g} days ago and never triaged"))
def _submitted_days_ago(service: CorrectionService, seen: dict[str, Any], days: float) -> None:
    _plant(
        service,
        seen,
        created_hours_ago=days * 24,
        contact_email="stale@example.invalid",
    )


@given(parsers.parse('a correction resolved {days:g} days ago with contact email "{email}"'))
def _resolved_days_ago(
    service: CorrectionService, seen: dict[str, Any], days: float, email: str
) -> None:
    remember = "second_correction_id" if "correction_id" in seen else "correction_id"
    _plant(
        service,
        seen,
        created_hours_ago=days * 24 + SLA_HOURS,
        status="resolved",
        resolved_days_ago=days,
        contact_email=email,
        remember_as=remember,
    )
    # The first planted correction stays the subject of "that correction"; a
    # second one in the same scenario exists only to be left alone.
    if remember == "second_correction_id":
        seen["correction_id"] = seen["correction_id"]


@given(parsers.parse('a correction rejected {days:g} days ago with contact email "{email}"'))
def _rejected_days_ago(
    service: CorrectionService, seen: dict[str, Any], days: float, email: str
) -> None:
    _plant(
        service,
        seen,
        created_hours_ago=days * 24 + SLA_HOURS,
        status="rejected",
        resolved_days_ago=days,
        contact_email=email,
    )


@given("two corrections submitted by two different reporters")
def _two_reporters(service: CorrectionService, seen: dict[str, Any]) -> None:
    first = _plant(
        service,
        seen,
        contact_email="first@example.invalid",
        body="The first reporter's description of the error.",
        remember_as="first_correction_id",
    )
    second = _plant(
        service,
        seen,
        contact_email="second@example.invalid",
        body="SECOND-REPORTER-SECRET: the second reporter's private description.",
        target_id="en-US:housing:v1:protect-renters",
        remember_as="second_correction_id",
    )
    seen["first_token"] = seen[f"token_for_{first}"]
    seen["second_token"] = seen[f"token_for_{second}"]
    seen["correction_id"] = first


@given("the ops team triages that correction")
@when("the ops team triages that correction")
def _ops_triages(service: CorrectionService, seen: dict[str, Any]) -> None:
    service.set_status(seen["correction_id"], status="triaged")


@given("the ops team resolves that correction")
@when("the ops team resolves that correction")
def _ops_resolves(service: CorrectionService, seen: dict[str, Any]) -> None:
    service.set_status(seen["correction_id"], status="resolved")


@when("the ops team rejects that correction")
def _ops_rejects(service: CorrectionService, seen: dict[str, Any]) -> None:
    service.set_status(seen["correction_id"], status="rejected")


@when("the ops team moves that correction back to triaged")
def _ops_reopens(service: CorrectionService, seen: dict[str, Any]) -> None:
    service.set_status(seen["correction_id"], status="triaged")


@when(parsers.parse('the ops team tries to set the status to "{status}"'))
def _ops_sets_bad_status(service: CorrectionService, seen: dict[str, Any], status: str) -> None:
    try:
        service.set_status(seen["correction_id"], status=status)
    except (ValueError, UnknownCorrectionError) as exc:
        seen["refusal"] = exc
    else:
        seen["refusal"] = None


# --- When -----------------------------------------------------------------


@when(
    parsers.parse('anyone submits a correction about stance "{target_id}" with no authentication')
)
def _submit_about_stance(corrections_api: TestClient, seen: dict[str, Any], target_id: str) -> None:
    _submit(
        corrections_api,
        seen,
        {"target_table": "stances", "target_id": target_id, "body": REPORT_BODY},
    )


@when(parsers.parse('anyone submits a correction against target table "{table}"'))
def _submit_bad_table(corrections_api: TestClient, seen: dict[str, Any], table: str) -> None:
    _submit(
        corrections_api,
        seen,
        {"target_table": table, "target_id": STANCE_TARGET, "body": REPORT_BODY},
    )


@when("anyone submits a correction with an empty body")
def _submit_empty_body(corrections_api: TestClient, seen: dict[str, Any]) -> None:
    _submit(
        corrections_api,
        seen,
        {"target_table": "stances", "target_id": STANCE_TARGET, "body": "   "},
    )


@when("anyone submits a correction with no contact email")
def _submit_no_email(corrections_api: TestClient, seen: dict[str, Any]) -> None:
    _submit(
        corrections_api,
        seen,
        {"target_table": "stances", "target_id": STANCE_TARGET, "body": REPORT_BODY},
    )


@when(parsers.parse('anyone submits a correction with contact email "{email}"'))
def _submit_with_email(corrections_api: TestClient, seen: dict[str, Any], email: str) -> None:
    _submit(
        corrections_api,
        seen,
        {
            "target_table": "stances",
            "target_id": STANCE_TARGET,
            "body": REPORT_BODY,
            "contact_email": email,
        },
    )


@when("the reporter checks the status with their tracking token")
def _reporter_checks(corrections_api: TestClient, seen: dict[str, Any]) -> None:
    seen["response"] = corrections_api.get(f"/v1/corrections/{seen['tracking_token']}")


@when("the first reporter checks the status with their tracking token")
def _first_reporter_checks(corrections_api: TestClient, seen: dict[str, Any]) -> None:
    seen["response"] = corrections_api.get(f"/v1/corrections/{seen['first_token']}")


@when(parsers.parse('someone checks the status with tracking token "{token}"'))
def _someone_checks(corrections_api: TestClient, seen: dict[str, Any], token: str) -> None:
    seen["response"] = corrections_api.get(f"/v1/corrections/{token}")


@when("someone checks the status using the correction's row id instead of the token")
def _check_with_row_id(corrections_api: TestClient, seen: dict[str, Any]) -> None:
    seen["response"] = corrections_api.get(f"/v1/corrections/{seen['correction_id']}")


@when("the ops team lists corrections past their triage SLA")
def _list_past_sla(service: CorrectionService, seen: dict[str, Any]) -> None:
    seen["past_sla"] = [record.id for record in service.past_sla()]


@when("the retention sweep runs")
@when("the retention sweep runs again")
def _retention_sweep(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    seen["sweep"] = sweep_expired_contact_emails(zerodb)


# --- Then -----------------------------------------------------------------


@then(parsers.parse("the response is {status:d}"))
def _status_is(seen: dict[str, Any], status: int) -> None:
    assert seen["response"].status_code == status, seen["response"].text


@then(parsers.parse('the correction is stored with status "{status}"'))
@then(parsers.parse('the correction is stored with status "{status}"'))
def _stored_with_status(zerodb: ZeroDBClient, seen: dict[str, Any], status: str) -> None:
    assert _stored(zerodb, seen).status == status


@then(parsers.parse('the correction id starts with "{prefix}"'))
def _id_prefix(seen: dict[str, Any], prefix: str) -> None:
    assert seen["correction_id"].startswith(prefix), seen["correction_id"]


@then(parsers.parse('the stored correction targets table "{table}" and id "{target_id}"'))
def _stored_target(zerodb: ZeroDBClient, seen: dict[str, Any], table: str, target_id: str) -> None:
    stored = _stored(zerodb, seen)
    assert stored.target_table == table
    assert stored.target_id == target_id


@then("the stored correction carries the reporter's description of the error")
def _stored_body(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    assert _stored(zerodb, seen).body == REPORT_BODY


@then("no correction is stored")
def _nothing_stored(zerodb: ZeroDBClient) -> None:
    page = zerodb.query_rows(TABLE_NAME, limit=50)
    assert page.rows == [], f"{len(page.rows)} correction(s) were stored by a refused request"


@then("the stored correction has no contact email")
def _stored_no_email(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    assert _stored(zerodb, seen).contact_email is None


@then(parsers.parse('the stored correction has contact email "{email}"'))
def _stored_email(zerodb: ZeroDBClient, seen: dict[str, Any], email: str) -> None:
    assert _stored(zerodb, seen).contact_email == email


@then(parsers.parse("the stored correction is due for triage {hours:d} hours after it was created"))
def _sla_is_hours_after(zerodb: ZeroDBClient, seen: dict[str, Any], hours: int) -> None:
    stored = _stored(zerodb, seen)
    created = datetime.fromisoformat(stored.created_at.replace("Z", "+00:00"))
    due = datetime.fromisoformat(stored.sla_due_at.replace("Z", "+00:00"))
    assert due - created == timedelta(hours=hours), f"{created} -> {due}"


@then("that correction is listed")
def _is_listed(seen: dict[str, Any]) -> None:
    assert seen["correction_id"] in seen["past_sla"], seen["past_sla"]


@then("that correction is not listed")
def _is_not_listed(seen: dict[str, Any]) -> None:
    assert seen["correction_id"] not in seen["past_sla"], seen["past_sla"]


@then(parsers.parse('the reported status is "{status}"'))
def _reported_status(seen: dict[str, Any], status: str) -> None:
    assert _body(seen)["status"] == status


@then("the status response states the 72-hour triage deadline")
def _states_deadline(seen: dict[str, Any]) -> None:
    body = _body(seen)
    assert body["sla_hours"] == SLA_HOURS
    assert body["sla_due_at"]


@then("the response carries nothing from the second reporter's correction")
def _no_cross_leak(seen: dict[str, Any]) -> None:
    text = seen["response"].text
    assert "SECOND-REPORTER-SECRET" not in text
    assert "second@example.invalid" not in text
    assert seen["second_correction_id"] not in text
    assert seen["second_token"] not in text


@then("the response does not contain the contact email")
def _no_email_in_response(seen: dict[str, Any]) -> None:
    assert "reporter@example.invalid" not in seen["response"].text


@then("the response does not contain the reported description")
def _no_body_in_response(seen: dict[str, Any]) -> None:
    assert REPORT_BODY not in seen["response"].text


@then("the two tracking tokens share no guessable relationship")
def _tokens_unguessable(seen: dict[str, Any]) -> None:
    first, second = seen["first_token"], seen["second_token"]
    assert first != second
    # Not sequential: no shared long prefix, and neither is the other plus a
    # counter. A token a reader could derive from one they hold would make the
    # whole lookup enumerable.
    shared = 0
    for left, right in zip(first, second, strict=False):
        if left != right:
            break
        shared += 1
    assert shared <= 8, f"tokens share a {shared}-character prefix: {first} / {second}"
    assert len(first) >= 32, f"token is only {len(first)} characters"


@then("that correction has no contact email")
def _swept(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    assert _stored(zerodb, seen).contact_email is None


@then("the correction itself is still stored")
def _still_stored(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    assert zerodb.get_row(TABLE_NAME, seen["correction_id"]) is not None


@then("that correction still has its contact email")
def _not_swept(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    assert _stored(zerodb, seen).contact_email is not None


@then(parsers.parse("the sweep reports erasing exactly {count:d} contact email"))
@then(parsers.parse("the sweep reports erasing exactly {count:d} contact emails"))
def _sweep_count(seen: dict[str, Any], count: int) -> None:
    assert seen["sweep"].erased == count, seen["sweep"]


@then(parsers.parse('that correction is still status "{status}"'))
def _still_status(zerodb: ZeroDBClient, seen: dict[str, Any], status: str) -> None:
    assert _stored(zerodb, seen).status == status


@then("that correction still records when it was resolved")
def _still_resolved_at(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    assert _stored(zerodb, seen).resolved_at


@then("that correction still records what was reported")
def _still_has_body(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    assert _stored(zerodb, seen).body == REPORT_BODY


@then("the correction has no resolution time yet")
def _no_resolved_at(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    assert _stored(zerodb, seen).resolved_at is None


@then("the correction records when it was resolved")
def _has_resolved_at(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    assert _stored(zerodb, seen).resolved_at


@then("the status change is refused")
def _refused(seen: dict[str, Any]) -> None:
    assert seen["refusal"] is not None, "an unknown status was accepted"


@then("the response mentions no voter, profile or interest data")
def _no_voter_data(seen: dict[str, Any]) -> None:
    text = seen["response"].text.lower()
    leaked = [word for word in VOTER_WORDS if word in text]
    assert not leaked, f"public correction response mentions {leaked}"


@then("the stored correction carries only the fields datamodel.md documents")
def _only_documented_fields(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """datamodel.md's `corrections` row: target_table, target_id, body,
    contact_email, status, sla_due_at, resolved_at — plus the id every row
    has, and the two fields this story adds and argues for in
    `app/corrections/models.py` (`created_at`, `tracking_token_hash`).
    """
    row = zerodb.get_row(TABLE_NAME, seen["correction_id"])
    assert row is not None
    assert set(row.row_data) <= {
        "id",
        "target_table",
        "target_id",
        "body",
        "contact_email",
        "status",
        "sla_due_at",
        "resolved_at",
        "created_at",
        "tracking_token_hash",
    }, sorted(row.row_data)


def test_the_feature_file_pins_the_published_sla() -> None:
    """A guard on the Gherkin, not on the app.

    The feature file hard-codes 72 hours so a reader of the scenarios sees
    prd.md's actual commitment. If `SLA_HOURS` is ever changed, this fails
    here rather than letting the scenarios quietly assert a different promise
    than the one prd.md makes.
    """
    assert SLA_HOURS == 72


def test_the_retention_window_matches_the_data_model() -> None:
    """datamodel.md: `corrections.contact_email` retained "90 days after
    resolution". Pinned so the sweep cannot drift from the documented promise.
    """
    from app.corrections.retention import RETENTION_DAYS

    assert RETENTION_DAYS == 90
