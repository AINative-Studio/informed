"""Step definitions for tests/features/provenance_enforcement.feature (16.3).

Runs entirely against story 1.2's in-memory fake transport, with
`break_server_side_filter()` applied because that is what the live API does and
the content-addressed check-then-insert rests on the client's paging fallback
working under it (story 1.4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.repositories import MissingProvenanceError, ProvenanceContext, Repository
from app.repositories.models import Person, RollCall
from app.repositories.provenance import (
    check_ingest_module,
    check_ingest_tree,
    models_requiring_provenance,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/provenance_enforcement.feature")

VIOLATION_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "provenance_violations"

ADAPTER = "test_adapter/v1"
SOURCE_URL = "https://api.congress.gov/v3/house-vote/119/2/452"


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


def _roll_call_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "us-house:119-2:452",
        "voted_at": "2026-03-11T14:22:00-04:00",
        "question": "On Motion to Suspend the Rules and Pass",
        "vote_type": "final_passage",
        "result": "Passed",
        "source_url": SOURCE_URL,
        "content_hash": "a" * 64,
    }
    row.update(overrides)
    return row


def _sourced_repo(zerodb: ZeroDBClient) -> Repository[RollCall]:
    return Repository(
        client=zerodb,
        model=RollCall,
        provenance=ProvenanceContext(adapter_version=ADAPTER),
    )


def _provenance_rows(fake: FakeZeroDBTransport, entity_id: str | None = None) -> list[dict]:
    rows = [row["row_data"] for row in fake.rows.get("provenance_log", {}).values()]
    if entity_id is None:
        return rows
    return [row for row in rows if row.get("entity_id") == entity_id]


# --- background --------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="provenance_client")
def provenance_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the provenance reference tables exist")
def provenance_tables(zerodb: ZeroDBClient) -> None:
    Bootstrapper(zerodb, pace=lambda: None).run()


# --- writing -----------------------------------------------------------------


@when(
    parsers.parse(
        'I write a roll_calls row with source_url "{source_url}" '
        "through a provenance-aware repository"
    )
)
def write_sourced_row(zerodb: ZeroDBClient, source_url: str) -> None:
    _sourced_repo(zerodb).upsert(_roll_call_row(source_url=source_url))


@given("I have already written that roll_calls row once")
def already_written_once(zerodb: ZeroDBClient) -> None:
    _sourced_repo(zerodb).upsert(_roll_call_row())


@when("I write the identical roll_calls row again")
def write_identical_again(zerodb: ZeroDBClient) -> None:
    _sourced_repo(zerodb).upsert(_roll_call_row())


@when("I write the same roll_calls row with changed source content")
def write_changed_content(zerodb: ZeroDBClient) -> None:
    """A correction at the source: the same roll call, a different result, and
    therefore a different content hash — so a SECOND provenance entry, keeping
    the first (datamodel.md's reproducibility goal).
    """
    _sourced_repo(zerodb).upsert(_roll_call_row(result="Agreed to", content_hash="b" * 64))


@when("I write a people row through a repository with no provenance context")
def write_unsourced_row(zerodb: ZeroDBClient) -> None:
    repo: Repository[Person] = Repository(client=zerodb, model=Person)
    repo.upsert({"id": "person:B000944", "canonical_name": "Test Person"})


@when(
    "I try to write a roll_calls row with a source_url "
    "through a repository with no provenance context"
)
def try_write_without_provenance(
    zerodb: ZeroDBClient, result: dict[str, Any]
) -> None:
    repo: Repository[RollCall] = Repository(client=zerodb, model=RollCall)
    try:
        repo.upsert(_roll_call_row())
    except MissingProvenanceError as exc:
        result["error"] = exc


@when(
    parsers.parse(
        "I write {count:d} roll_calls rows with distinct source_urls in one batch "
        "through a provenance-aware repository"
    )
)
def write_batch(zerodb: ZeroDBClient, count: int) -> None:
    rows = [
        _roll_call_row(
            id=f"us-house:119-2:{450 + index}",
            source_url=f"https://api.congress.gov/v3/house-vote/119/2/{450 + index}",
            content_hash=f"{index}" * 64,
        )
        for index in range(count)
    ]
    _sourced_repo(zerodb).upsert_many(rows)


# --- reading back ------------------------------------------------------------


@then(parsers.parse('the roll_calls row "{row_id}" is stored'))
def roll_call_stored(zerodb: ZeroDBClient, row_id: str) -> None:
    repo: Repository[RollCall] = Repository(client=zerodb, model=RollCall)
    assert repo.get(row_id) is not None, f"roll_calls row {row_id!r} is not stored"


@then(parsers.parse('no roll_calls row "{row_id}" is stored'))
def roll_call_not_stored(fake: FakeZeroDBTransport, row_id: str) -> None:
    stored = fake.rows.get("roll_calls", {})
    assert not [row for row in stored.values() if row["row_data"].get("id") == row_id]


@then(parsers.parse('the people row "{row_id}" is stored'))
def person_stored(zerodb: ZeroDBClient, row_id: str) -> None:
    repo: Repository[Person] = Repository(client=zerodb, model=Person)
    assert repo.get(row_id) is not None


@then(parsers.parse('a provenance_log entry exists for roll_calls "{entity_id}"'))
def provenance_exists(
    fake: FakeZeroDBTransport, entity_id: str, result: dict[str, Any]
) -> None:
    rows = _provenance_rows(fake, entity_id)
    assert rows, f"no provenance_log entry for roll_calls {entity_id!r}"
    result["provenance_row"] = rows[0]


@then(parsers.parse('that provenance entry has source_url "{source_url}"'))
def provenance_source_url(result: dict[str, Any], source_url: str) -> None:
    assert result["provenance_row"]["source_url"] == source_url


@then(parsers.parse('that provenance entry has adapter_version "{adapter_version}"'))
def provenance_adapter_version(result: dict[str, Any], adapter_version: str) -> None:
    assert result["provenance_row"]["adapter_version"] == adapter_version


@then("that provenance entry records when it was fetched")
def provenance_fetched_at(result: dict[str, Any]) -> None:
    fetched_at = result["provenance_row"]["fetched_at"]
    assert fetched_at, "fetched_at is empty"
    # An ISO 8601 instant, not a bare date: "when and how it was fetched" needs
    # the time of day to be auditable.
    assert "T" in fetched_at, fetched_at


@then(parsers.parse('exactly {count:d} row carries the roll_calls id "{row_id}"'))
@then(parsers.parse('exactly {count:d} rows carry the roll_calls id "{row_id}"'))
def exact_roll_call_rows(fake: FakeZeroDBTransport, count: int, row_id: str) -> None:
    stored = fake.rows.get("roll_calls", {})
    actual = sum(1 for row in stored.values() if row["row_data"].get("id") == row_id)
    assert actual == count, f"{row_id} has {actual} row(s), expected {count}"


@then(parsers.parse('exactly {count:d} provenance_log row exists for roll_calls "{entity_id}"'))
@then(parsers.parse('exactly {count:d} provenance_log rows exist for roll_calls "{entity_id}"'))
def exact_provenance_rows(fake: FakeZeroDBTransport, count: int, entity_id: str) -> None:
    rows = _provenance_rows(fake, entity_id)
    assert len(rows) == count, f"found {len(rows)} provenance_log row(s), expected {count}"


@then("no provenance_log rows exist at all")
def no_provenance_rows(fake: FakeZeroDBTransport) -> None:
    assert _provenance_rows(fake) == []


@then(parsers.parse("{count:d} roll_calls rows are stored"))
def n_roll_calls_stored(fake: FakeZeroDBTransport, count: int) -> None:
    assert len(fake.rows.get("roll_calls", {})) == count


@then(parsers.parse("{count:d} provenance_log rows exist"))
def n_provenance_rows(fake: FakeZeroDBTransport, count: int) -> None:
    assert len(_provenance_rows(fake)) == count


@then("the write is refused as untraceable evidence")
def write_refused(result: dict[str, Any]) -> None:
    error = result.get("error")
    assert isinstance(error, MissingProvenanceError), f"expected a refusal, got {error!r}"
    assert "provenance" in str(error).lower()


# --- the CI integrity check --------------------------------------------------


@when("I run the provenance integrity check over the repository models and ingest modules")
def run_integrity_check(result: dict[str, Any]) -> None:
    result["violations"] = check_ingest_tree()
    result["sourced_models"] = models_requiring_provenance()


@then("the integrity check reports no violations")
def no_violations(result: dict[str, Any]) -> None:
    violations = result["violations"]
    assert violations == [], "\n".join(str(violation) for violation in violations)


@then(parsers.parse('the integrity check covers the "{table}" table'))
def check_covers_table(result: dict[str, Any], table: str) -> None:
    tables = {model.table_name for model in result["sourced_models"]}
    assert table in tables, f"{table!r} not among the discovered sourced tables {sorted(tables)}"


@given("an ingest module that writes a source_url row through a repository with no provenance context")
def broken_ingest_module(result: dict[str, Any]) -> None:
    """A deliberately-broken module kept as a fixture, not generated: the
    negative control has to be a real file the real check parses, or it proves
    nothing about the check CI runs.
    """
    result["module_under_check"] = VIOLATION_FIXTURES / "forgets_provenance.py"


@when("I run the provenance integrity check over that module")
def run_check_over_module(result: dict[str, Any]) -> None:
    result["violations"] = check_ingest_module(result["module_under_check"])


@then("the integrity check reports a violation naming that module")
def violation_names_module(result: dict[str, Any]) -> None:
    violations = result["violations"]
    assert violations, "the check passed a module that forgets provenance"
    expected = result["module_under_check"].name
    assert any(expected in str(violation.module) for violation in violations), violations
