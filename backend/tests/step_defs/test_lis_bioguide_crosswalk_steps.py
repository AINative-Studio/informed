"""Step definitions for tests/features/lis_bioguide_crosswalk.feature (8.2).

Runs entirely against story 1.2's in-memory fake transport and the fixture at
tests/fixtures/lis_crosswalk_sample.yaml -- never the live network, so CI
needs no credential and does not depend on raw.githubusercontent.com being
reachable (CLAUDE.md). The real live run is
`backend/scripts/verify_lis_crosswalk_live.py`.

Like test_senate_rollcalls_steps.py, the fake has `break_server_side_filter()`
applied, for the same reason: this crosswalk's idempotency claim and its
"read every senator's people row back" claim both rest on the client's paging
fallback working under a broken server-side filter.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.lis_crosswalk import (
    LIS_EXTERNAL_ID_KEY,
    CoverageReport,
    LisCrosswalkWriter,
    UnmappedLisMemberIdError,
    check_coverage,
    load_crosswalk,
    render_coverage_prometheus_text,
    sitting_senators_from_roster,
)
from app.repositories import Repository
from app.repositories.models import Person
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/lis_bioguide_crosswalk.feature")

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "lis_crosswalk_sample.yaml"


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
def writer(zerodb: ZeroDBClient) -> LisCrosswalkWriter:
    return LisCrosswalkWriter(zerodb, pace=lambda: None)


@pytest.fixture
def people(zerodb: ZeroDBClient) -> Repository[Person]:
    return Repository(client=zerodb, model=Person)


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


def _roster() -> list[dict[str, Any]]:
    records = yaml.safe_load(FIXTURE_PATH.read_text())
    assert isinstance(records, list)
    return records


def _roster_without_lis_for(bioguide: str) -> list[dict[str, Any]]:
    """The sample roster with one senator's `id.lis` removed entirely -- the
    live file's own shape for a record that simply has no LIS id, not a blank
    string (confirmed live: the ~439 non-Senate records omit the key rather
    than carrying an empty one).
    """
    records = _roster()
    for record in records:
        if record.get("id", {}).get("bioguide") == bioguide:
            record["id"].pop("lis", None)
    return records


def _roster_without(bioguide: str) -> list[dict[str, Any]]:
    """The sample roster with one senator's whole record removed -- the shape
    of a roster that is simply behind the sitting Senate (a senator sworn in
    since the roster was last refreshed).
    """
    return [r for r in _roster() if r.get("id", {}).get("bioguide") != bioguide]


# --- background --------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="crosswalk_client")
def crosswalk_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def reference_tables(zerodb: ZeroDBClient) -> None:
    Bootstrapper(zerodb, pace=lambda: None).run()


@given("the sitting senators from the sample roster")
def senators_exist(people: Repository[Person]) -> None:
    """Story 2.2's `legislators.py` is what writes `people`, and it is already
    live. This crosswalk UPDATES those rows' `external_ids`, so the rows have
    to be there first -- exactly as the live project has them (542 `people`
    rows, confirmed live during this story).
    """
    for senator in sitting_senators_from_roster(_roster()):
        people.upsert(
            {
                "id": senator.person_id,
                "canonical_name": senator.canonical_name,
                "external_ids": {},
            }
        )


@given(parsers.parse('the people row "{person_id}" already carries external id "{key}" "{value}"'))
def person_has_external_id(
    people: Repository[Person], person_id: str, key: str, value: str
) -> None:
    existing = people.get(person_id)
    assert existing is not None
    external_ids = dict(existing.external_ids)
    external_ids[key] = value
    people.upsert(
        {
            "id": person_id,
            "canonical_name": existing.canonical_name,
            "external_ids": external_ids,
        }
    )


@given("I have already refreshed the LIS crosswalk from the sample roster once")
def already_refreshed(writer: LisCrosswalkWriter) -> None:
    writer.refresh(sitting_senators_from_roster(_roster()))


@given("the crosswalk has been refreshed from the sample roster")
def refreshed_from_sample(people: Repository[Person], writer: LisCrosswalkWriter) -> None:
    senators_exist(people)
    writer.refresh(sitting_senators_from_roster(_roster()))


@given(
    parsers.parse('the crosswalk has been refreshed from a roster missing senator "{person_id}"')
)
def refreshed_missing_one(
    people: Repository[Person], writer: LisCrosswalkWriter, person_id: str
) -> None:
    senators_exist(people)
    bioguide = person_id.removeprefix("person:")
    writer.refresh(sitting_senators_from_roster(_roster_without(bioguide)))


# --- when: refreshing --------------------------------------------------------


@when("I refresh the LIS crosswalk from the sample roster")
def refresh_from_sample(writer: LisCrosswalkWriter, result: dict[str, Any]) -> None:
    result["report"] = writer.refresh(sitting_senators_from_roster(_roster()))


@when("I refresh the LIS crosswalk from the sample roster again")
def refresh_again(writer: LisCrosswalkWriter, result: dict[str, Any]) -> None:
    result["report"] = writer.refresh(sitting_senators_from_roster(_roster()))


@when(
    parsers.parse(
        'I refresh the LIS crosswalk from a roster whose senator "{person_id}" has no LIS id'
    )
)
def refresh_missing_lis(writer: LisCrosswalkWriter, result: dict[str, Any], person_id: str) -> None:
    bioguide = person_id.removeprefix("person:")
    result["report"] = writer.refresh(
        sitting_senators_from_roster(_roster_without_lis_for(bioguide))
    )


# --- when: loading and resolving --------------------------------------------


@when("I load the crosswalk from ZeroDB")
def load_from_zerodb(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    result["crosswalk"] = load_crosswalk(zerodb)


@when(parsers.parse('I load the crosswalk from ZeroDB and resolve LIS id "{lis_id}" with alerting'))
def load_and_resolve_alerting(
    zerodb: ZeroDBClient, result: dict[str, Any], lis_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    crosswalk = load_crosswalk(zerodb)
    result["crosswalk"] = crosswalk
    with caplog.at_level(logging.ERROR, logger="app.ingest.lis_crosswalk"):
        crosswalk.resolve_bioguide_id(lis_id, alert=True)
    result["records"] = list(caplog.records)


@when(
    parsers.parse('I load the crosswalk from ZeroDB and resolve LIS ids "{lis_ids}" with alerting')
)
def load_and_resolve_many_alerting(
    zerodb: ZeroDBClient, result: dict[str, Any], lis_ids: str, caplog: pytest.LogCaptureFixture
) -> None:
    crosswalk = load_crosswalk(zerodb)
    result["crosswalk"] = crosswalk
    with caplog.at_level(logging.ERROR, logger="app.ingest.lis_crosswalk"):
        for lis_id in lis_ids.split(","):
            crosswalk.resolve_bioguide_id(lis_id.strip(), alert=True)
    result["records"] = list(caplog.records)


# --- when: coverage ----------------------------------------------------------


@when(
    parsers.parse(
        "I check the crosswalk covers the {count:d} sitting senators in the sample roster"
    )
)
def check_coverage_step(zerodb: ZeroDBClient, result: dict[str, Any], count: int) -> None:
    senators = sitting_senators_from_roster(_roster())
    assert len(senators) == count
    result["coverage"] = check_coverage(load_crosswalk(zerodb), senators)


@when(
    parsers.parse(
        "I render the crosswalk coverage metrics for the {count:d} sitting senators "
        "in the sample roster"
    )
)
def render_coverage_step(zerodb: ZeroDBClient, result: dict[str, Any], count: int) -> None:
    senators = sitting_senators_from_roster(_roster())
    assert len(senators) == count
    coverage = check_coverage(load_crosswalk(zerodb), senators)
    result["coverage"] = coverage
    result["metrics"] = render_coverage_prometheus_text(coverage)


# --- then: the written rows --------------------------------------------------


@then(parsers.parse('the people row "{person_id}" carries external id "{key}" "{value}"'))
def row_carries_external_id(
    people: Repository[Person], person_id: str, key: str, value: str
) -> None:
    person = people.get(person_id)
    assert person is not None, f"{person_id} is absent"
    assert person.external_ids.get(key) == value, person.external_ids


@then(parsers.parse('the people row "{person_id}" carries no external id "{key}"'))
def row_carries_no_external_id(people: Repository[Person], person_id: str, key: str) -> None:
    person = people.get(person_id)
    assert person is not None, f"{person_id} is absent"
    assert key not in person.external_ids, person.external_ids


@then(parsers.parse('exactly 1 row carries the people id "{person_id}"'))
def exactly_one_row(people: Repository[Person], person_id: str) -> None:
    matches = [p for p in people.query(limit=500) if p.id == person_id]
    assert len(matches) == 1, f"{len(matches)} rows carry {person_id}"


# --- then: the refresh report ------------------------------------------------


@then(parsers.parse("the crosswalk report says {mapped:d} of {total:d} sitting senators mapped"))
def report_says(result: dict[str, Any], mapped: int, total: int) -> None:
    report = result["report"]
    assert len(report.mapped) == mapped, report
    assert len(report.mapped) + len(report.without_lis_id) == total, report


@then(parsers.parse('the crosswalk report names "{person_id}" as a senator with no LIS id'))
def report_names_missing(result: dict[str, Any], person_id: str) -> None:
    assert person_id in result["report"].without_lis_id, result["report"]


# --- then: resolving ---------------------------------------------------------


@then(parsers.parse('resolving LIS id "{lis_id}" returns bioguide id "{bioguide}"'))
def resolves_to_bioguide(result: dict[str, Any], lis_id: str, bioguide: str) -> None:
    assert result["crosswalk"].resolve_bioguide_id(lis_id) == bioguide


@then(parsers.parse('resolving LIS id "{lis_id}" returns person id "{person_id}"'))
def resolves_to_person_id(result: dict[str, Any], lis_id: str, person_id: str) -> None:
    assert result["crosswalk"].resolve_person_id(lis_id) == person_id


@then(parsers.parse('resolving LIS id "{lis_id}" returns no bioguide id'))
def resolves_to_nothing(result: dict[str, Any], lis_id: str) -> None:
    assert result["crosswalk"].resolve_bioguide_id(lis_id) is None


@then(parsers.parse("the loaded crosswalk holds {count:d} mappings"))
def crosswalk_size(result: dict[str, Any], count: int) -> None:
    assert len(result["crosswalk"]) == count


@then(parsers.parse('resolving LIS id "{lis_id}" strictly raises an unmapped-id alert'))
def strict_resolve_raises(result: dict[str, Any], lis_id: str) -> None:
    with pytest.raises(UnmappedLisMemberIdError) as excinfo:
        result["crosswalk"].require_bioguide_id(lis_id)
    assert lis_id in str(excinfo.value)


# --- then: coverage ----------------------------------------------------------


@then(parsers.parse("the coverage check passes with {mapped:d} of {total:d} mapped"))
def coverage_passes(result: dict[str, Any], mapped: int, total: int) -> None:
    coverage: CoverageReport = result["coverage"]
    assert coverage.is_complete, coverage
    assert coverage.mapped_count == mapped, coverage
    assert coverage.sitting_count == total, coverage


@then(parsers.parse('the coverage check fails naming "{person_id}" as unmapped'))
def coverage_fails_naming(result: dict[str, Any], person_id: str) -> None:
    coverage: CoverageReport = result["coverage"]
    assert not coverage.is_complete, coverage
    assert person_id in coverage.unmapped_person_ids, coverage


@then(parsers.parse("the coverage check reports {mapped:d} of {total:d} mapped"))
def coverage_counts(result: dict[str, Any], mapped: int, total: int) -> None:
    coverage: CoverageReport = result["coverage"]
    assert coverage.mapped_count == mapped, coverage
    assert coverage.sitting_count == total, coverage


# --- then: alerting ----------------------------------------------------------


@then(parsers.parse('an alert was logged naming LIS id "{lis_id}"'))
def alert_logged(result: dict[str, Any], lis_id: str) -> None:
    records = result["records"]
    assert records, "no log records captured"
    assert any(lis_id in record.getMessage() for record in records), [
        r.getMessage() for r in records
    ]


@then(parsers.parse('that alert carries the story tag "{story}"'))
def alert_carries_story(result: dict[str, Any], story: str) -> None:
    records = result["records"]
    assert any(getattr(record, "story", None) == story for record in records), [
        getattr(r, "story", None) for r in records
    ]


@then(parsers.parse("the unmapped-id alert count is {count:d}"))
def alert_count(result: dict[str, Any], count: int) -> None:
    assert result["crosswalk"].unmapped_alert_count == count


@then(parsers.parse("{count:d} distinct unmapped LIS ids were alerted on"))
def distinct_alerts(result: dict[str, Any], count: int) -> None:
    assert len(result["crosswalk"].unmapped_lis_ids) == count


# --- then: rendered metrics --------------------------------------------------


@then(parsers.parse('the rendered metrics include "{line}"'))
def metrics_include(result: dict[str, Any], line: str) -> None:
    assert line in result["metrics"], result["metrics"]


def test_lis_external_id_key_is_the_one_legislators_already_writes() -> None:
    """`app.ingest.legislators` writes every source id under its own source
    key, so the crosswalk's key must be the same `lis` -- confirmed live: 100
    of the live project's 542 `people` rows already carry `external_ids.lis`.
    """
    assert LIS_EXTERNAL_ID_KEY == "lis"
