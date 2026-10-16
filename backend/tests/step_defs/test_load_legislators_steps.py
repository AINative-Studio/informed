"""Step definitions for tests/features/load_legislators.feature (story 2.2).

Runs entirely against story 1.2's in-memory fake transport and the fixture
roster at tests/fixtures/legislators_current_sample.yaml -- never the live
network, so CI needs neither credentials nor GitHub to be reachable
(CLAUDE.md). The live confirmation of the same behaviour lives in
`tests/test_legislators_live_shapes.py` and
`scripts/verify_legislators_live.py`.

Like `test_seed_reference_data_steps.py`, the fake has
`break_server_side_filter()` applied, because that is what the live API does
and this loader's idempotency claim rests on the client's paging fallback
working under it -- a fake with a working filter would make the "exactly 1
row" scenarios pass trivially.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.legislators import (
    LegislatorLoader,
    LoadReport,
    parse_legislators_yaml,
    transform_legislators,
)
from app.repositories import Repository
from app.repositories.models import Person
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/load_legislators.feature")

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "legislators_current_sample.yaml"
)


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
def loader(zerodb: ZeroDBClient) -> LegislatorLoader:
    return LegislatorLoader(zerodb, pace=lambda: None)


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


def _fixture_records() -> list[dict[str, Any]]:
    return parse_legislators_yaml(FIXTURE_PATH.read_text())


# --- background -------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="legislators_client")
def legislators_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables and seeded bodies exist")
def reference_tables_and_bodies(zerodb: ZeroDBClient) -> None:
    """`people` has no declared references, but the real bodies exist too.

    `Bootstrapper.run()` both creates every table (including `people`) and
    seeds `us-house`/`us-senate` (story 2.1), so calling it once here gives
    this feature the same environment a real bootstrapped project has,
    without duplicating story 2.1's seed logic in this file.
    """
    Bootstrapper(zerodb, pace=lambda: None).run()


# --- loading -----------------------------------------------------------------


def _load_fixture(loader: LegislatorLoader, result: dict[str, Any]) -> None:
    rows, skipped = transform_legislators(_fixture_records())
    result["skipped"] = skipped
    result["fixture_row_count"] = len(rows)
    report: LoadReport = loader.load(rows)
    result["report"] = report


@when("I load legislators from the fixture roster")
def load_fixture_roster(loader: LegislatorLoader, result: dict[str, Any]) -> None:
    _load_fixture(loader, result)


@when("I load legislators from the fixture roster again")
def load_fixture_roster_again(loader: LegislatorLoader, result: dict[str, Any]) -> None:
    _load_fixture(loader, result)


@given("I have already loaded legislators from the fixture roster once")
def already_loaded_once(
    loader: LegislatorLoader, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    _load_fixture(loader, result)
    fake.calls.clear()


@when('I load a refreshed roster where "person:C000127" was redistricted to a new party label')
def load_refreshed_roster(loader: LegislatorLoader, result: dict[str, Any]) -> None:
    """Simulates the source file being refreshed with a changed field.

    Mutates the *existing* current term rather than appending a new one,
    because a term outside the 118th/119th window is deliberately dropped by
    the transform (see test_legislators_unit.py's coverage-filter tests) --
    appending one here would prove nothing about re-ingest updating a row,
    only that the filter still works. Changing `party` on a kept term is a
    realistic refresh (a source correction, a party switch) that must flow
    through to the stored row on the second load.
    """
    records = _fixture_records()
    for record in records:
        if record.get("id", {}).get("bioguide") == "C000127":
            for term in record["terms"]:
                if term.get("start") == "2025-01-03":
                    term["party"] = "Democrat-Farmer-Labor"
    rows, skipped = transform_legislators(records)
    result["skipped"] = skipped
    report: LoadReport = loader.load(rows)
    result["report"] = report


# --- reading the loaded rows back -------------------------------------------


def _person(zerodb: ZeroDBClient, person_id: str) -> Person:
    repo: Repository[Person] = Repository(client=zerodb, model=Person)
    row = repo.get(person_id)
    assert row is not None, f"person {person_id!r} is not stored"
    return row


@then(parsers.parse('every loaded person has an id of the form "person:{{bioguide}}"'))
def every_person_has_bioguide_id(result: dict[str, Any]) -> None:
    report: LoadReport = result["report"]
    assert report.people_written, "no people were written"
    for person_id in report.people_written:
        assert person_id.startswith("person:"), person_id
        bioguide = person_id.removeprefix("person:")
        assert bioguide, f"{person_id!r} has an empty bioguide suffix"


@then("the loader reports as many people written as the fixture roster holds")
def report_matches_fixture_count(result: dict[str, Any]) -> None:
    report: LoadReport = result["report"]
    assert report.row_count == result["fixture_row_count"]
    assert not result["skipped"], f"unexpected skips: {result['skipped']}"


@then(parsers.parse('the person "{person_id}" has external id "{key}" "{value}"'))
def person_external_id(zerodb: ZeroDBClient, person_id: str, key: str, value: str) -> None:
    assert _person(zerodb, person_id).external_ids.get(key) == value


@then(parsers.parse('the person "{person_id}" has no photo_url'))
def person_no_photo_url(zerodb: ZeroDBClient, person_id: str) -> None:
    assert _person(zerodb, person_id).photo_url is None


@then(
    parsers.parse(
        'the person "{person_id}" has a term in body "{body_id}" '
        'starting "{start}" and ending "{end}"'
    )
)
def person_has_term(
    zerodb: ZeroDBClient, person_id: str, body_id: str, start: str, end: str
) -> None:
    person = _person(zerodb, person_id)
    matches = [
        t for t in person.terms if t.body_id == body_id and t.start == start and t.end == end
    ]
    assert matches, f"{person_id} has no {body_id} term {start}..{end}; terms={person.terms!r}"


@then(
    parsers.parse(
        'the person "{person_id}" has a term in body "{body_id}" '
        'starting "{start}" and ending "{end}" with party "{party}"'
    )
)
def person_has_term_with_party(
    zerodb: ZeroDBClient, person_id: str, body_id: str, start: str, end: str, party: str
) -> None:
    person = _person(zerodb, person_id)
    matches = [
        t
        for t in person.terms
        if t.body_id == body_id and t.start == start and t.end == end and t.party == party
    ]
    assert matches, (
        f"{person_id} has no {body_id} term {start}..{end} with party {party!r}; "
        f"terms={person.terms!r}"
    )


@then('every term of every loaded person has a body id of "us-house" or "us-senate"')
def every_term_has_known_body_id(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    report: LoadReport = result["report"]
    for person_id in report.people_written:
        person = _person(zerodb, person_id)
        for term in person.terms:
            assert term.body_id in {"us-house", "us-senate"}, term


@then("every term of every loaded person has a start date")
def every_term_has_start(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    report: LoadReport = result["report"]
    for person_id in report.people_written:
        person = _person(zerodb, person_id)
        for term in person.terms:
            assert term.start, term


@then("every term's party is a known party or is absent")
def every_term_party_known(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    known = {"Democrat", "Republican", "Independent"}
    report: LoadReport = result["report"]
    for person_id in report.people_written:
        person = _person(zerodb, person_id)
        for term in person.terms:
            assert term.party is None or term.party in known, term


# --- idempotency -------------------------------------------------------------


def _rows_with_id(fake: FakeZeroDBTransport, table: str, row_id: str) -> int:
    stored = fake.rows.get(table, {})
    return sum(1 for row in stored.values() if row["row_data"].get("id") == row_id)


@then(parsers.parse('exactly {count:d} row carries the person id "{person_id}"'))
def one_person_row(fake: FakeZeroDBTransport, count: int, person_id: str) -> None:
    actual = _rows_with_id(fake, "people", person_id)
    assert actual == count, f"{person_id} has {actual} row(s), expected {count}"
