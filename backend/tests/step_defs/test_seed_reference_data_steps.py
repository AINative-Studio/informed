"""Step definitions for tests/features/seed_reference_data.feature (story 2.1).

Runs against story 1.2's in-memory fake transport, never live ZeroDB, so CI
needs no credentials — the rule every backend feature in this suite follows.
The live confirmation of the same behaviour lives in
`tests/test_seed_live_shapes.py` and `scripts/verify_seed_live.py`.

The fake used here has `break_server_side_filter()` applied, because that is
what the live API does (story 1.4's headline finding) and the seed's whole
idempotency claim rests on the client's find step working under it. A fake with
a working filter would make every "exactly 1 row" scenario below pass
trivially.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.repositories import MissingReferenceError, Repository
from app.repositories.models import Body, Jurisdiction
from app.seed import SeedReport
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/seed_reference_data.feature")


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    # Live ZeroDB's server-side /query filter matches nothing (story 1.4). The
    # seed's idempotency depends on the client's paging fallback, so the fake
    # must fail the same way or the idempotency scenarios are vacuous.
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )


@pytest.fixture
def bootstrapper(zerodb: ZeroDBClient) -> Bootstrapper:
    return Bootstrapper(zerodb, pace=lambda: None)


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


@given("a ZeroDB client backed by an in-memory fake", target_fixture="seed_client")
def seed_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


# --- running the bootstrap (which seeds) -----------------------------------


@when("I run the bootstrap")
def run_bootstrap(bootstrapper: Bootstrapper, result: dict[str, Any]) -> None:
    result["report"] = bootstrapper.run()


@when("I run the bootstrap again")
def run_bootstrap_again(bootstrapper: Bootstrapper, result: dict[str, Any]) -> None:
    result["report"] = bootstrapper.run()


@given("the bootstrap has already run once")
def bootstrap_already_ran(
    bootstrapper: Bootstrapper, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    result["first_report"] = bootstrapper.run()
    fake.calls.clear()


@given("the reference tables exist")
def reference_tables_exist(zerodb: ZeroDBClient) -> None:
    """Only the two tables this story writes, with no rows in them."""
    zerodb.create_table("jurisdictions", {"id": "string"})
    zerodb.create_table("bodies", {"id": "string"})


@given(parsers.parse('the jurisdiction "{row_id}" has been hand-edited to name "{name}"'))
def hand_edit_jurisdiction(zerodb: ZeroDBClient, row_id: str, name: str) -> None:
    """Simulate someone changing a seeded row out from under the seed.

    The seed is the single source of truth for these three rows, so a re-run
    must restore them rather than leave the edit in place — an `upsert` that
    quietly skipped an existing row would not.
    """
    zerodb.update_row("jurisdictions", row_id, {"name": name})


@when("I seed a chamber whose jurisdiction is missing")
def seed_orphan_chamber(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    bodies: Repository[Body] = Repository(client=zerodb, model=Body)
    try:
        bodies.upsert(
            {
                "id": "us-orphan",
                "jurisdiction_id": "no-such-jurisdiction",
                "name": "Orphan Chamber",
                "chamber": "lower",
                "seats": 1,
            }
        )
    except MissingReferenceError as exc:
        result["refusal"] = exc
    else:
        result["refusal"] = None


# --- reading the seeded rows back ------------------------------------------


def _jurisdiction(zerodb: ZeroDBClient, row_id: str) -> Jurisdiction:
    repo: Repository[Jurisdiction] = Repository(client=zerodb, model=Jurisdiction)
    row = repo.get(row_id)
    assert row is not None, f"jurisdiction {row_id!r} is not stored"
    return row


def _body(zerodb: ZeroDBClient, row_id: str) -> Body:
    repo: Repository[Body] = Repository(client=zerodb, model=Body)
    row = repo.get(row_id)
    assert row is not None, f"body {row_id!r} is not stored"
    return row


@then(parsers.parse('the jurisdiction "{row_id}" exists'))
def jurisdiction_exists(zerodb: ZeroDBClient, row_id: str) -> None:
    assert _jurisdiction(zerodb, row_id).id == row_id


@then(parsers.parse('the body "{row_id}" exists'))
def body_exists(zerodb: ZeroDBClient, row_id: str) -> None:
    assert _body(zerodb, row_id).id == row_id


@then(parsers.parse('the jurisdiction "{row_id}" has name "{name}"'))
def jurisdiction_name(zerodb: ZeroDBClient, row_id: str, name: str) -> None:
    assert _jurisdiction(zerodb, row_id).name == name


@then(parsers.parse('the jurisdiction "{row_id}" has country code "{code}"'))
def jurisdiction_country_code(zerodb: ZeroDBClient, row_id: str, code: str) -> None:
    assert _jurisdiction(zerodb, row_id).country_code == code


@then(parsers.parse('the jurisdiction "{row_id}" has level "{level}"'))
def jurisdiction_level(zerodb: ZeroDBClient, row_id: str, level: str) -> None:
    assert _jurisdiction(zerodb, row_id).level == level


@then(parsers.parse('the jurisdiction "{row_id}" has adapter name "{adapter}"'))
def jurisdiction_adapter(zerodb: ZeroDBClient, row_id: str, adapter: str) -> None:
    assert _jurisdiction(zerodb, row_id).adapter_name == adapter


@then(parsers.parse('the jurisdiction "{row_id}" is active'))
def jurisdiction_active(zerodb: ZeroDBClient, row_id: str) -> None:
    assert _jurisdiction(zerodb, row_id).active is True


@then(parsers.parse('the body "{row_id}" belongs to jurisdiction "{jurisdiction_id}"'))
def body_jurisdiction(zerodb: ZeroDBClient, row_id: str, jurisdiction_id: str) -> None:
    assert _body(zerodb, row_id).jurisdiction_id == jurisdiction_id


@then(parsers.parse('the body "{row_id}" has name "{name}"'))
def body_name(zerodb: ZeroDBClient, row_id: str, name: str) -> None:
    assert _body(zerodb, row_id).name == name


@then(parsers.parse('the body "{row_id}" has chamber "{chamber}"'))
def body_chamber(zerodb: ZeroDBClient, row_id: str, chamber: str) -> None:
    assert _body(zerodb, row_id).chamber == chamber


@then(parsers.parse('the body "{row_id}" has {seats:d} seats'))
def body_seats(zerodb: ZeroDBClient, row_id: str, seats: int) -> None:
    assert _body(zerodb, row_id).seats == seats


# --- the report -------------------------------------------------------------


@then(
    parsers.parse("the seed reports {jurisdictions:d} jurisdiction and {bodies:d} bodies written")
)
def seed_report_counts(result: dict[str, Any], jurisdictions: int, bodies: int) -> None:
    seed: SeedReport = result["report"].seed
    assert len(seed.jurisdictions_written) == jurisdictions
    assert len(seed.bodies_written) == bodies


# --- idempotency ------------------------------------------------------------


def _rows_with_id(fake: FakeZeroDBTransport, table: str, row_id: str) -> int:
    """How many rows the fake's storage holds under one natural-key id.

    Read from the fake's own storage rather than through the client, because
    `get_row` stops at the first match and so cannot distinguish one row from
    two — which is the entire question an idempotency scenario asks.
    """
    stored = fake.rows.get(table, {})
    return sum(1 for row in stored.values() if row["row_data"].get("id") == row_id)


@then(parsers.parse('exactly {count:d} row carries the jurisdiction id "{row_id}"'))
def one_jurisdiction_row(fake: FakeZeroDBTransport, count: int, row_id: str) -> None:
    actual = _rows_with_id(fake, "jurisdictions", row_id)
    assert actual == count, f"{row_id} has {actual} row(s), expected {count}"


@then(parsers.parse('exactly {count:d} row carries the body id "{row_id}"'))
def one_body_row(fake: FakeZeroDBTransport, count: int, row_id: str) -> None:
    actual = _rows_with_id(fake, "bodies", row_id)
    assert actual == count, f"{row_id} has {actual} row(s), expected {count}"


# --- ordering and reference integrity --------------------------------------


@then(parsers.parse('the jurisdiction "{jurisdiction_id}" was written before the body "{body_id}"'))
def jurisdiction_written_first(
    fake: FakeZeroDBTransport, jurisdiction_id: str, body_id: str
) -> None:
    """Order matters: the body's reference check reads the jurisdiction row.

    Asserted on the recorded calls rather than the final state, because the
    final state is identical either way — and seeding the body first would
    fail the repository's reference check against live ZeroDB while passing a
    naive state assertion.
    """
    writes = [
        call
        for call in fake.calls
        if call.method in {"POST", "PUT"} and "/rows" in call.path and call.json is not None
    ]
    jurisdiction_at = next(
        (
            index
            for index, call in enumerate(writes)
            if _written_table(call.path) == "jurisdictions"
            and _written_id(call.json) == jurisdiction_id
        ),
        None,
    )
    body_at = next(
        (
            index
            for index, call in enumerate(writes)
            if _written_table(call.path) == "bodies" and _written_id(call.json) == body_id
        ),
        None,
    )
    assert jurisdiction_at is not None, f"{jurisdiction_id} was never written"
    assert body_at is not None, f"{body_id} was never written"
    assert jurisdiction_at < body_at, (
        f"{body_id} was written at call {body_at}, before {jurisdiction_id} at {jurisdiction_at}"
    )


def _written_table(path: str) -> str | None:
    """The table a `.../tables/{table}/rows[/{uuid}]` write addressed."""
    segments = path.split("/")
    try:
        return segments[segments.index("tables") + 1]
    except (ValueError, IndexError):  # pragma: no cover - not a table path
        return None


def _written_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    row_data = payload.get("row_data")
    if isinstance(row_data, dict):
        return row_data.get("id")
    return payload.get("id")


@then("the write is refused for a missing reference")
def write_refused(result: dict[str, Any]) -> None:
    refusal = result["refusal"]
    assert isinstance(refusal, MissingReferenceError), (
        "a body referencing a jurisdiction that does not exist was accepted"
    )
    assert refusal.referenced_table == "jurisdictions"
    assert refusal.referenced_id == "no-such-jurisdiction"
