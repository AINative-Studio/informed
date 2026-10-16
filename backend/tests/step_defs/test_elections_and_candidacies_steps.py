"""Step definitions for tests/features/elections_and_candidacies.feature (2.6).

Runs entirely against story 1.2's in-memory fake transport and the fixture at
tests/fixtures/fec_candidates_sample.json -- never the live network, so CI
needs no FEC API key (none exists in this environment; see
app/ingest/candidacies.py's module docstring and this story's PR).

Like test_load_legislators_steps.py and test_house_rollcalls_steps.py, the
fake has `break_server_side_filter()` applied, because that is what the live
API does and this loader's idempotency claim rests on the client's paging
fallback working under it -- a fake with a working filter would make the
"exactly 1 row" scenarios pass trivially.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.candidacies import (
    CandidacyLoader,
    LoadReport,
    candidacies_from_results,
)
from app.repositories import Repository
from app.repositories.models import Candidacy, Election, Person
from app.seed_elections import ElectionSeeder, UPCOMING_ELECTIONS
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/elections_and_candidacies.feature")

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "fec_candidates_sample.json"


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
def loader(zerodb: ZeroDBClient) -> CandidacyLoader:
    return CandidacyLoader(zerodb, pace=lambda: None)


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


def _fixture_results() -> list[dict[str, Any]]:
    """The fixture's `results` array, with fixture-only annotations removed.

    `_fixture_note` is documentation for a human reading the JSON; the loader
    must never see a field the live API does not send, or a test could pass
    because of a key that does not exist in production.
    """
    data = json.loads(FIXTURE_PATH.read_text())
    return [
        {key: value for key, value in record.items() if not key.startswith("_")}
        for record in data["results"]
    ]


# --- background ------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="candidacies_client")
def candidacies_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables and seeded bodies exist")
def reference_tables(zerodb: ZeroDBClient) -> None:
    """`Bootstrapper.run()` creates every table and seeds the reference tree.

    Both are needed: `elections.jurisdiction_id` references `jurisdictions`,
    so the `us-federal` row story 2.1 seeds has to be there before an election
    can be written at all.
    """
    Bootstrapper(zerodb, pace=lambda: None).run()


# --- seeding the election --------------------------------------------------


def _seed_elections(zerodb: ZeroDBClient) -> None:
    ElectionSeeder(zerodb, pace=lambda: None).run()


@when("I seed the upcoming federal elections")
def seed_elections(zerodb: ZeroDBClient) -> None:
    _seed_elections(zerodb)


@when("I seed the upcoming federal elections again")
def seed_elections_again(zerodb: ZeroDBClient) -> None:
    _seed_elections(zerodb)


@given("I have already seeded the upcoming federal elections once")
def already_seeded_once(zerodb: ZeroDBClient) -> None:
    _seed_elections(zerodb)


@given("the upcoming federal elections are seeded")
def elections_are_seeded(zerodb: ZeroDBClient) -> None:
    _seed_elections(zerodb)


@then(parsers.parse('exactly 1 row carries the election id "{election_id}"'))
def exactly_one_election(fake: FakeZeroDBTransport, election_id: str) -> None:
    rows = _rows_with_id(fake, "elections", election_id)
    assert len(rows) == 1, f"expected exactly 1 elections row {election_id!r}, found {len(rows)}"


@then(parsers.parse('the election "{election_id}" is held on "{election_date}"'))
def election_date(zerodb: ZeroDBClient, election_id: str, election_date: str) -> None:
    election = _elections(zerodb).get(election_id)
    assert election is not None, f"no elections row {election_id!r}"
    assert election.election_date == election_date


@then(parsers.parse('the election "{election_id}" is of kind "{kind}"'))
def election_kind(zerodb: ZeroDBClient, election_id: str, kind: str) -> None:
    election = _elections(zerodb).get(election_id)
    assert election is not None, f"no elections row {election_id!r}"
    assert election.kind == kind


@then(parsers.parse('the election "{election_id}" belongs to jurisdiction "{jurisdiction_id}"'))
def election_jurisdiction(zerodb: ZeroDBClient, election_id: str, jurisdiction_id: str) -> None:
    election = _elections(zerodb).get(election_id)
    assert election is not None, f"no elections row {election_id!r}"
    assert election.jurisdiction_id == jurisdiction_id


# --- loading candidacies ---------------------------------------------------


def _load(
    loader: CandidacyLoader,
    result: dict[str, Any],
    records: list[dict[str, Any]],
) -> LoadReport:
    rows, skipped = candidacies_from_results(records, election=UPCOMING_ELECTIONS[0])
    report = loader.load(rows)
    report.skipped.extend(skipped)
    result["report"] = report
    result["rows"] = rows
    return report


@when("I load candidacies from the fixture filings")
def load_fixture(loader: CandidacyLoader, result: dict[str, Any]) -> None:
    _load(loader, result, _fixture_results())


@when("I load candidacies from the fixture filings again")
def load_fixture_again(loader: CandidacyLoader, result: dict[str, Any]) -> None:
    _load(loader, result, _fixture_results())


@given("I have already loaded candidacies from the fixture filings once")
def already_loaded_once(
    loader: CandidacyLoader, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    _load(loader, result, _fixture_results())
    fake.calls.clear()


@when(parsers.parse('I load a refreshed pull where FEC candidate "{fec_id}" has withdrawn'))
def load_refreshed(loader: CandidacyLoader, result: dict[str, Any], fec_id: str) -> None:
    """A later weekly pull in which one filer's status changed.

    `candidate_status` 'P' is FEC's "prior candidate" -- what a withdrawal
    looks like in this feed within the same cycle.
    """
    records = copy.deepcopy(_fixture_results())
    for record in records:
        if record["candidate_id"] == fec_id:
            record["candidate_status"] = "P"
            record["candidate_inactive"] = True
    _load(loader, result, records)


@when("I load candidacies from the fixture filings with a recording pacer")
def load_with_pacer(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    calls: list[int] = []
    paced_loader = CandidacyLoader(zerodb, pace=lambda: calls.append(1))
    _load(paced_loader, result, _fixture_results())
    result["pace_calls"] = calls


# --- pre-existing sitting member ------------------------------------------


@given(
    parsers.parse('the sitting member "{person_id}" is already loaded with FEC id "{fec_id}"')
)
def sitting_member_loaded(zerodb: ZeroDBClient, person_id: str, fec_id: str) -> None:
    """A `people` row in exactly the shape story 2.2's loader writes.

    `external_ids["fec"]` is semicolon-joined there (a member can hold more
    than one FEC candidate id across House and Senate runs), and the term's
    `district_id` is the same unpadded OCD-ID scheme story 2.3 confirmed. Both
    details matter: the incumbent cross-reference reads them.
    """
    people: Repository[Person] = Repository(client=zerodb, model=Person)
    people.upsert(
        {
            "id": person_id,
            "canonical_name": "McBride, Sarah",
            "external_ids": {"bioguide": "M001234", "fec": f"H4DE99999;{fec_id}"},
            "photo_url": None,
            "terms": [
                {
                    "body_id": "us-house",
                    "district_id": "ocd-division/country:us/state:de",
                    "party": "Democrat",
                    "start": "2025-01-03",
                    "end": "2027-01-03",
                }
            ],
        }
    )


# --- assertions on candidacies --------------------------------------------


@then("the loader reports as many candidacies written as the fixture holds")
def report_count(result: dict[str, Any]) -> None:
    """Every fixture record the loader was *meant* to write, and no more.

    The fixture deliberately carries two records this loader must refuse (a
    2024-only filing and a presidential one), so the expected count is the
    fixture's length minus those -- asserted against the rows the transform
    produced rather than a hard-coded number, so adding a fixture record does
    not silently break this step.
    """
    report: LoadReport = result["report"]
    rows: list[dict[str, Any]] = result["rows"]
    assert len(rows) > 0, "the transform produced no rows at all"
    assert report.row_count == len(rows)


@then(parsers.parse('every loaded candidacy has an id of the form "{_pattern}"'))
def candidacy_id_shape(zerodb: ZeroDBClient, result: dict[str, Any], _pattern: str) -> None:
    report: LoadReport = result["report"]
    for candidacy_id in report.candidacies_written:
        candidacy = _candidacies(zerodb).get(candidacy_id)
        assert candidacy is not None, f"no candidacies row {candidacy_id!r}"
        expected = (
            f"{candidacy.election_id}:{candidacy.district_id}:{candidacy.person_id}"
        )
        assert candidacy_id == expected, (
            f"candidacy id {candidacy_id!r} is not "
            f"{{election_id}}:{{district_id}}:{{person_id}} ({expected!r})"
        )


@then(parsers.parse('every loaded candidacy references the election "{election_id}"'))
def candidacies_reference_election(
    zerodb: ZeroDBClient, result: dict[str, Any], election_id: str
) -> None:
    report: LoadReport = result["report"]
    assert report.candidacies_written, "no candidacies were written"
    for candidacy_id in report.candidacies_written:
        candidacy = _candidacies(zerodb).get(candidacy_id)
        assert candidacy is not None
        assert candidacy.election_id == election_id


@then(parsers.parse('the candidacy for FEC candidate "{fec_id}" is in district "{district_id}"'))
def candidacy_district(fake: FakeZeroDBTransport, zerodb: ZeroDBClient, fec_id: str, district_id: str) -> None:
    candidacy = _candidacy_for_fec(fake, zerodb, fec_id)
    assert candidacy.district_id == district_id


@then(parsers.parse('the candidacy for FEC candidate "{fec_id}" has party "{party}"'))
def candidacy_party(fake: FakeZeroDBTransport, zerodb: ZeroDBClient, fec_id: str, party: str) -> None:
    assert _candidacy_for_fec(fake, zerodb, fec_id).party == party


@then(parsers.parse('the candidacy for FEC candidate "{fec_id}" has person name "{name}"'))
def candidacy_person_name(fake: FakeZeroDBTransport, zerodb: ZeroDBClient, fec_id: str, name: str) -> None:
    assert _candidacy_for_fec(fake, zerodb, fec_id).person_name == name


@then(parsers.parse('the candidacy for FEC candidate "{fec_id}" has person id "{person_id}"'))
def candidacy_person_id(fake: FakeZeroDBTransport, zerodb: ZeroDBClient, fec_id: str, person_id: str) -> None:
    assert _candidacy_for_fec(fake, zerodb, fec_id).person_id == person_id


@then(parsers.parse('the candidacy for FEC candidate "{fec_id}" has status "{status}"'))
def candidacy_status(fake: FakeZeroDBTransport, zerodb: ZeroDBClient, fec_id: str, status: str) -> None:
    assert _candidacy_for_fec(fake, zerodb, fec_id).status == status


@then(parsers.parse('the candidacy for FEC candidate "{fec_id}" is flagged incumbent'))
def candidacy_is_incumbent(fake: FakeZeroDBTransport, zerodb: ZeroDBClient, fec_id: str) -> None:
    assert _candidacy_for_fec(fake, zerodb, fec_id).is_incumbent is True


@then(parsers.parse('the candidacy for FEC candidate "{fec_id}" is not flagged incumbent'))
def candidacy_not_incumbent(fake: FakeZeroDBTransport, zerodb: ZeroDBClient, fec_id: str) -> None:
    assert _candidacy_for_fec(fake, zerodb, fec_id).is_incumbent is False


@then(parsers.parse('no candidacy was written for FEC candidate "{fec_id}"'))
def no_candidacy_for(fake: FakeZeroDBTransport, fec_id: str) -> None:
    rows = _all_rows(fake, "candidacies")
    matches = [row for row in rows if fec_id in str(row.get("id", ""))]
    assert not matches, f"expected no candidacy for {fec_id!r}, found {matches!r}"


@then(parsers.parse('the loader reports a skip for FEC candidate "{fec_id}"'))
def reports_skip(result: dict[str, Any], fec_id: str) -> None:
    report: LoadReport = result["report"]
    skipped_ids = [skip.candidate_id for skip in report.skipped]
    assert fec_id in skipped_ids, f"expected a skip for {fec_id!r}, got {skipped_ids!r}"


@then(parsers.parse('exactly 1 row carries the candidacy id "{candidacy_id}"'))
def exactly_one_candidacy(fake: FakeZeroDBTransport, candidacy_id: str) -> None:
    rows = _rows_with_id(fake, "candidacies", candidacy_id)
    assert len(rows) == 1, f"expected exactly 1 candidacies row {candidacy_id!r}, got {len(rows)}"


# --- assertions on people created for challengers -------------------------


@then(parsers.parse('a person row exists with id "{person_id}"'))
def person_exists(zerodb: ZeroDBClient, person_id: str) -> None:
    assert _people(zerodb).get(person_id) is not None, f"no people row {person_id!r}"


@then(parsers.parse('no person row exists with id "{person_id}"'))
def person_absent(fake: FakeZeroDBTransport, person_id: str) -> None:
    rows = _rows_with_id(fake, "people", person_id)
    assert not rows, f"expected no people row {person_id!r}, found {len(rows)}"


@then(parsers.parse('the person "{person_id}" has canonical name "{name}"'))
def person_canonical_name(zerodb: ZeroDBClient, person_id: str, name: str) -> None:
    person = _people(zerodb).get(person_id)
    assert person is not None, f"no people row {person_id!r}"
    assert person.canonical_name == name


@then(parsers.parse('the person "{person_id}" has external id "{key}" "{value}"'))
def person_external_id(zerodb: ZeroDBClient, person_id: str, key: str, value: str) -> None:
    person = _people(zerodb).get(person_id)
    assert person is not None, f"no people row {person_id!r}"
    assert person.external_ids.get(key) == value


@then(parsers.parse('the person "{person_id}" has no terms'))
def person_has_no_terms(zerodb: ZeroDBClient, person_id: str) -> None:
    """A challenger has never held this seat, so there is no term to record.

    Writing an invented term would put a person in office who is not in
    office -- the one thing a voter-facing ballot must never do.
    """
    person = _people(zerodb).get(person_id)
    assert person is not None, f"no people row {person_id!r}"
    assert person.terms == []


@then(parsers.parse('exactly 1 row carries the person id "{person_id}"'))
def exactly_one_person(fake: FakeZeroDBTransport, person_id: str) -> None:
    rows = _rows_with_id(fake, "people", person_id)
    assert len(rows) == 1, f"expected exactly 1 people row {person_id!r}, got {len(rows)}"


# --- pacing ----------------------------------------------------------------


@then("the pacer was called at least once per candidacy written")
def pacer_called(result: dict[str, Any]) -> None:
    report: LoadReport = result["report"]
    calls: list[int] = result["pace_calls"]
    assert len(calls) >= report.row_count, (
        f"pacer called {len(calls)} times for {report.row_count} candidacies written"
    )


# --- helpers ---------------------------------------------------------------


def _elections(zerodb: ZeroDBClient) -> Repository[Election]:
    return Repository(client=zerodb, model=Election)


def _candidacies(zerodb: ZeroDBClient) -> Repository[Candidacy]:
    return Repository(client=zerodb, model=Candidacy)


def _people(zerodb: ZeroDBClient) -> Repository[Person]:
    return Repository(client=zerodb, model=Person)


def _candidacy_for_fec(fake: FakeZeroDBTransport, zerodb: ZeroDBClient, fec_id: str) -> Candidacy:
    """The one stored candidacy whose person id derives from this FEC id.

    Looked up by scanning rather than by rebuilding the id, deliberately: the
    id is composed from district and person id, and rebuilding it here would
    duplicate the loader's own logic and so could not catch it being wrong.
    """
    rows = _all_rows(fake, "candidacies")
    matches = [row for row in rows if fec_id in str(row.get("id", ""))]
    if not matches:
        candidacy = _candidacy_by_stored_person_fec(zerodb, fec_id, rows)
        assert candidacy is not None, (
            f"no candidacy found for FEC candidate {fec_id!r}; "
            f"stored ids: {[row.get('id') for row in rows]!r}"
        )
        return candidacy
    assert len(matches) == 1, f"expected 1 candidacy for {fec_id!r}, found {len(matches)}"
    return Candidacy.model_validate(matches[0])


def _candidacy_by_stored_person_fec(
    zerodb: ZeroDBClient, fec_id: str, rows: list[dict[str, Any]]
) -> Candidacy | None:
    """Fallback lookup for a candidacy whose person is a SITTING member.

    An incumbent's candidacy id carries their `person:{bioguide}` id, which
    does not contain the FEC id at all, so the id-substring scan above cannot
    find it. Resolved through the person's own `external_ids["fec"]` -- the
    same semicolon-joined field story 2.2 writes.
    """
    for row in rows:
        person_id = row.get("person_id")
        if not person_id:
            continue
        person = _people(zerodb).get(str(person_id))
        if person is None:
            continue
        fec_ids = str(person.external_ids.get("fec", "")).split(";")
        if fec_id in fec_ids:
            return Candidacy.model_validate(row)
    return None


def _all_rows(fake: FakeZeroDBTransport, table: str) -> list[dict[str, Any]]:
    """Every stored row of a table, straight off the fake's store.

    Read from the store rather than through `query_rows`, exactly as
    `test_load_legislators_steps.py` does: the "exactly 1 row" assertions are
    about genuine duplicate *storage*, and a read path that de-duplicates by
    id (or pages) could hide the very duplicate they exist to catch.
    """
    stored = fake.rows.get(table, {})
    return [row["row_data"] for row in stored.values()]


def _rows_with_id(fake: FakeZeroDBTransport, table: str, row_id: str) -> list[dict[str, Any]]:
    return [row for row in _all_rows(fake, table) if row.get("id") == row_id]
