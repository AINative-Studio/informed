"""Steps for the ballot lookup feature (story 2.4).

Runs the real FastAPI app against story 1.2's in-memory ZeroDB fake and a
counting address resolver, so the path under test is the production handler,
service, repository and client, and CI makes no network call.

Two kinds of assertion live here and they are deliberately different:

  * **shape** — races, candidates and the incumbent flag. These are the same
    claims against the fake as against the live project, so the fake proves
    them outright;
  * **cost** — how many geocodes and how many reads of `people` one lookup
    performs. The fake cannot tell you a number in milliseconds, and a timing
    assertion against it would measure this laptop rather than the budget. What
    the fake *can* prove deterministically is the shape of the work: one
    geocode, a bounded number of reads, and no re-read of `people` on a second
    lookup. The millisecond p95 is measured against the live project by
    `scripts/verify_ballot_live.py`, which is the only place it can honestly be
    claimed.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.ballot.models import BallotResponse
from app.ballot.router import ballot_service
from app.ballot.service import BallotService, DistrictCache, PeopleSnapshot
from app.districts.resolver import ResolvedDistrict
from app.main import create_app, zerodb_client
from app.repositories import Repository
from app.repositories.models import District, Person
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport
from tests.support.reference_data import REFERENCE_TABLES, seed_reference_data
from tests.support.resolvers import FixedDistrictResolver

scenarios("../features/ballot_lookup.feature")

#: The district the test resolver returns unless a step names another.
TEST_DISTRICT_ID = FixedDistrictResolver.DISTRICT_ID
SENATE_DIVISION_ID = "ocd-division/country:us/state:ca"

#: An address no voter owns. prd.md: never put real voter data in a fixture.
SYNTHETIC_ADDRESS = "1600 Pennsylvania Ave, Washington, DC"

#: Term bounds relative to today, so no scenario starts failing on a calendar
#: date.
TODAY = date.today()
TERM_START = (TODAY - timedelta(days=365)).isoformat()
TERM_END = (TODAY + timedelta(days=365)).isoformat()

FORMER_REPRESENTATIVE = "Former Rep. Synthetic Predecessor"


class CountingResolver:
    """A `FixedDistrictResolver` that records how often it was called.

    The geocode is the single most expensive thing one lookup does — measured
    live at roughly 500 ms, most of this story's 800 ms budget — so "exactly
    one geocode per lookup" is the cost claim that matters most, and it is the
    one a fake can prove exactly.
    """

    def __init__(self, district_id: str | None = None, *, name: str | None = None) -> None:
        self._inner = FixedDistrictResolver(district_id, name=name)
        self.calls = 0

    def resolve(self, address: str) -> Any:
        return self.resolve_district(address).districts

    def resolve_district(self, address: str) -> ResolvedDistrict:
        self.calls += 1
        return self._inner.resolve_district(address)


@pytest.fixture
def transport() -> FakeZeroDBTransport:
    """A fake with the live API's broken server-side filter (story 1.4)."""
    fake = FakeZeroDBTransport()
    fake.break_server_side_filter()
    return fake


@pytest.fixture
def zdb(transport: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    for table in (*REFERENCE_TABLES, "districts", "people"):
        client.create_table(table, {"id": "string"})
    seed_reference_data(client)
    return client


@pytest.fixture
def districts(zdb: ZeroDBClient) -> Repository[District]:
    return Repository(client=zdb, model=District)


@pytest.fixture
def people(zdb: ZeroDBClient) -> Repository[Person]:
    return Repository(client=zdb, model=Person)


@pytest.fixture
def resolver() -> CountingResolver:
    return CountingResolver()


@pytest.fixture
def snapshot() -> PeopleSnapshot:
    """One snapshot shared by every lookup in a scenario.

    This is the production object, not a test double: `ballot_service` holds one
    per process so a second voter's lookup does not re-read `people`. A scenario
    that asserts "the people table was not read again" needs the same sharing,
    so it is constructed once here and handed to every service the scenario
    builds.
    """
    return PeopleSnapshot()


@pytest.fixture
def districts_cache() -> DistrictCache:
    """One districts cache shared by every lookup in a scenario, as in production.

    Same reason as `snapshot`: the router holds one per process, so a scenario
    asserting "the second lookup read nothing again" has to share it too.
    """
    return DistrictCache()


@pytest.fixture
def service(
    zdb: ZeroDBClient,
    resolver: CountingResolver,
    snapshot: PeopleSnapshot,
    districts_cache: DistrictCache,
) -> BallotService:
    return BallotService(
        client=zdb, resolver=resolver, snapshot=snapshot, districts=districts_cache
    )


@pytest.fixture
def api(zdb: ZeroDBClient, service: BallotService) -> Any:
    """The real app, wired to the fake ZeroDB and the counting resolver."""
    app = create_app()
    app.dependency_overrides[zerodb_client] = lambda: zdb
    app.dependency_overrides[ballot_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client


def person_row(
    person_id: str,
    body_id: str,
    district_id: str,
    *,
    name: str,
    start: str = TERM_START,
    end: str | None = TERM_END,
) -> dict[str, Any]:
    return {
        "id": person_id,
        "canonical_name": name,
        "external_ids": {"bioguide": person_id.removeprefix("person:")},
        "terms": [
            {
                "body_id": body_id,
                "district_id": district_id,
                "party": "Independent",
                "start": start,
                "end": end,
            }
        ],
    }


def write_district(districts: Repository[District], district_id: str, name: str) -> None:
    state = district_id.split("state:")[1].split("/")[0]
    districts.upsert(
        {
            "id": district_id,
            "body_id": "us-house",
            "name": name,
            "state": state.upper(),
            "valid_from": "2023-01-03",
        }
    )


# --- Given ---------------------------------------------------------------


@given(parsers.parse('the district "{district_id}" exists in ZeroDB'))
def district_exists(districts: Repository[District], district_id: str) -> None:
    write_district(districts, district_id, f"Synthetic district {district_id.rsplit('/', 1)[-1]}")


@given("its representative and California's two senators are stored in ZeroDB")
def delegation_exists(people: Repository[Person]) -> None:
    people.upsert(
        person_row("person:ZZ000001", "us-house", TEST_DISTRICT_ID, name="Rep. Synthetic")
    )
    people.upsert(person_row("person:ZZ000002", "us-senate", SENATE_DIVISION_ID, name="Sen. One"))
    people.upsert(person_row("person:ZZ000003", "us-senate", SENATE_DIVISION_ID, name="Sen. Two"))


@given(
    "the district's previous representative is also stored in ZeroDB, with a term that has ended"
)
def former_representative_exists(people: Repository[Person]) -> None:
    people.upsert(
        person_row(
            "person:ZZ000099",
            "us-house",
            TEST_DISTRICT_ID,
            name=FORMER_REPRESENTATIVE,
            start=(TODAY - timedelta(days=365 * 6)).isoformat(),
            end=(TODAY - timedelta(days=365 * 4)).isoformat(),
        )
    )


@given(parsers.parse('the at-large state "{district_id}" and its three members exist in ZeroDB'))
def at_large_state_exists(
    districts: Repository[District], people: Repository[Person], district_id: str
) -> None:
    """Wyoming: one at-large House seat and two senators on the *same* division.

    The collision story 2.3 fixed, restated for the race-grouped response: the
    House and Senate OCD-IDs are the same string here, so a grouping keyed on
    the division alone merges the two chambers into one race and loses a seat.
    """
    write_district(districts, district_id, "Congressional District (at Large)")
    people.upsert(person_row("person:ZZATLARGE", "us-house", district_id, name="Rep. At Large"))
    people.upsert(person_row("person:ZZWYSEN1", "us-senate", district_id, name="Sen. Wyo One"))
    people.upsert(person_row("person:ZZWYSEN2", "us-senate", district_id, name="Sen. Wyo Two"))


@given(parsers.parse('the delegate district "{district_id}" and its delegate exist in ZeroDB'))
def delegate_district_exists(
    districts: Repository[District], people: Repository[Person], district_id: str
) -> None:
    write_district(districts, district_id, "Delegate District (at Large)")
    people.upsert(person_row("person:ZZDELEGATE", "us-house", district_id, name="Del. Synthetic"))


# --- When ----------------------------------------------------------------


@when(parsers.parse('the voter submits the address "{address}"'))
def submit_address(
    api: TestClient, context: Any, transport: FakeZeroDBTransport, address: str
) -> None:
    # Recorded before the lookup, so the read counts a Then asserts on are the
    # lookups' own and not the fixture's: seeding upserts rows, and an upsert
    # reads before it writes.
    context.people_reads_before = people_reads(transport)
    context.district_reads_before = district_reads(transport)
    context.response = api.get("/v1/ballot", params={"address": address})


@when(parsers.parse('the voter submits the address "{address}" again'))
def submit_address_again(api: TestClient, context: Any, address: str) -> None:
    context.first_response = context.response
    context.response = api.get("/v1/ballot", params={"address": address})


@when("the voter submits an address in the at-large state")
def submit_at_large(
    context: Any,
    zdb: ZeroDBClient,
    snapshot: PeopleSnapshot,
    districts_cache: DistrictCache,
) -> None:
    context.response = BallotService(
        client=zdb,
        resolver=CountingResolver(
            "ocd-division/country:us/state:wy", name="Congressional District (at Large)"
        ),
        snapshot=snapshot,
        districts=districts_cache,
    ).ballot_for(SYNTHETIC_ADDRESS)


@when("the voter submits an address in the delegate district")
def submit_delegate(
    context: Any,
    zdb: ZeroDBClient,
    snapshot: PeopleSnapshot,
    districts_cache: DistrictCache,
) -> None:
    context.response = BallotService(
        client=zdb,
        resolver=CountingResolver(
            "ocd-division/country:us/state:dc", name="Delegate District (at Large)"
        ),
        snapshot=snapshot,
        districts=districts_cache,
    ).ballot_for(SYNTHETIC_ADDRESS)


@when("the voter submits an address in that empty district")
def submit_empty_district(
    context: Any,
    zdb: ZeroDBClient,
    snapshot: PeopleSnapshot,
    districts_cache: DistrictCache,
) -> None:
    context.response = BallotService(
        client=zdb,
        resolver=CountingResolver(
            "ocd-division/country:us/state:wy", name="Congressional District (at Large)"
        ),
        snapshot=snapshot,
        districts=districts_cache,
    ).ballot_for(SYNTHETIC_ADDRESS)


@when("the API's OpenAPI schema is fetched")
def fetch_openapi(api: TestClient, context: Any) -> None:
    context.response = api.get("/openapi.json")


# --- Then ----------------------------------------------------------------


def people_reads(transport: FakeZeroDBTransport) -> int:
    """How many times `people` was queried, counted from the fake's call log."""
    return len([call for call in transport.calls if call.path.endswith("/tables/people/query")])


def payload(context: Any) -> dict[str, Any]:
    """The ballot payload, whether the step called HTTP or the service."""
    response = context.response
    if isinstance(response, BallotResponse):
        return response.model_dump(mode="json")
    assert response.status_code == 200, response.text
    return response.json()


def races(context: Any) -> list[dict[str, Any]]:
    return payload(context)["races"]


def race_for(context: Any, body_id: str) -> dict[str, Any] | None:
    found = [race for race in races(context) if race["body_id"] == body_id]
    assert len(found) <= 1, f"{len(found)} races for {body_id}: {found}"
    return found[0] if found else None


@then("the ballot lists a House race for the voter's district")
def lists_a_house_race(context: Any) -> None:
    race = race_for(context, "us-house")
    assert race is not None, f"no House race in {races(context)}"
    assert race["district_id"] == payload(context)["district"]["id"]


@then("the ballot lists a Senate race for the voter's state")
def lists_a_senate_race(context: Any) -> None:
    race = race_for(context, "us-senate")
    assert race is not None, f"no Senate race in {races(context)}"
    assert "/cd:" not in race["district_id"]


@then("the ballot lists no Senate race")
def lists_no_senate_race(context: Any) -> None:
    assert race_for(context, "us-senate") is None, "an empty Senate race was listed"


@then(parsers.parse("the House race lists exactly {count:d} candidate"))
@then(parsers.parse("the House race lists exactly {count:d} candidates"))
def house_candidate_count(context: Any, count: int) -> None:
    race = race_for(context, "us-house")
    assert race is not None, "no House race at all"
    assert len(race["candidates"]) == count, f"got {race['candidates']}"


@then(parsers.parse("the Senate race lists exactly {count:d} candidates"))
def senate_candidate_count(context: Any, count: int) -> None:
    race = race_for(context, "us-senate")
    assert race is not None, "no Senate race at all"
    assert len(race["candidates"]) == count, f"got {race['candidates']}"


@then("every race names the office being contested")
def races_name_the_office(context: Any) -> None:
    for race in races(context):
        assert race["office_name"], f"a race has no office name: {race}"
        assert race["id"], f"a race has no id: {race}"


@then("every candidate in every race is flagged as an incumbent")
def candidates_are_incumbents(context: Any) -> None:
    candidates = [c for race in races(context) for c in race["candidates"]]
    assert candidates, "no candidates at all, so the flag proves nothing"
    for candidate in candidates:
        assert candidate["is_incumbent"] is True, f"not flagged: {candidate}"


@then("every candidate names the person it refers to")
def candidates_name_a_person(context: Any) -> None:
    for race in races(context):
        for candidate in race["candidates"]:
            assert candidate["person_id"], f"no person_id: {candidate}"
            assert candidate["name"], f"no name: {candidate}"


@then("the ballot declares that challengers are not loaded yet")
def declares_incumbents_only(context: Any) -> None:
    assert payload(context)["data_state"] == "incumbents_only"


@then("the ballot makes no recommendation about who to vote for")
def no_recommendation(context: Any) -> None:
    body = str(payload(context)).lower()
    for forbidden in ("recommend", "endorse", "vote for", "best match"):
        assert forbidden not in body, f"{forbidden!r} appears in the payload"


@then("the former representative is not among the candidates")
def former_representative_excluded(context: Any) -> None:
    names = [c["name"] for race in races(context) for c in race["candidates"]]
    assert FORMER_REPRESENTATIVE not in names, f"a former member was listed: {names}"


@then(parsers.parse("the geocoder was called exactly {count:d} time"))
@then(parsers.parse("the geocoder was called exactly {count:d} times"))
def geocoder_call_count(resolver: CountingResolver, count: int) -> None:
    assert resolver.calls == count, f"the geocoder was called {resolver.calls} times"


@then(parsers.parse("the people table was read at most {count:d} times"))
def people_read_count(transport: FakeZeroDBTransport, context: Any, count: int) -> None:
    """Reads attributable to the lookups, not to the fixture that seeded them."""
    reads = people_reads(transport) - context.people_reads_before
    assert reads <= count, (
        f"`people` was read {reads} times across the lookups; one lookup must "
        "not page the table and a second must not re-read it. See "
        "BallotService.PEOPLE_PAGE_SIZE and PeopleSnapshot."
    )


def district_reads(transport: FakeZeroDBTransport) -> int:
    """Every round trip the `districts` read takes, from the fake's call log.

    Both `/query` and `/rows` are counted, because the live read is **two**
    round trips: the filtered query returns nothing (story 1.4's broken
    server-side filter) and the client-side fallback then reads the table in
    full. Counting only the query would hide half of the ~160-290 ms this
    story's `DistrictCache` removes.
    """
    return len(
        [
            call
            for call in transport.calls
            if "/tables/districts/query" in call.path or "/tables/districts/rows" in call.path
        ]
    )


@then(parsers.parse("the districts table was read at most {count:d} times"))
def district_read_count(transport: FakeZeroDBTransport, context: Any, count: int) -> None:
    """Reads attributable to the lookups, not to the fixture that seeded them."""
    reads = district_reads(transport) - context.district_reads_before
    assert reads <= count, (
        f"`districts` was read {reads} times across the lookups. One lookup "
        "costs two round trips (filtered query, then the client-side fallback's "
        "full read); a second lookup in the same district must cost none. See "
        "DistrictCache."
    )


@then("both lookups returned the same races")
def both_lookups_agree(context: Any) -> None:
    assert context.first_response.json()["races"] == context.response.json()["races"]


@then("the ballot endpoint documents its 800 ms p95 budget")
def openapi_documents_the_budget(context: Any) -> None:
    schema = context.response.json()
    operation = schema["paths"]["/v1/ballot"]["get"]
    text = f"{operation.get('summary', '')} {operation.get('description', '')}"
    assert "800" in text, f"the 800 ms budget is not documented: {text!r}"


@then("every field of the ballot response carries a description")
def every_field_is_documented(context: Any) -> None:
    """The contract the frontend reads (ui-prd.md 2.1, 2.3).

    An undescribed field is one the UI has to guess at, and `is_incumbent` in
    particular carries a claim about a person that must not be guessed at.
    """
    schemas = context.response.json()["components"]["schemas"]
    undocumented: list[str] = []
    for name in ("BallotResponse", "DistrictSummary", "Race", "BallotCandidate", "Officeholder"):
        assert name in schemas, f"{name} is missing from the OpenAPI schema"
        for field_name, field_schema in schemas[name].get("properties", {}).items():
            if not _described(field_schema):
                undocumented.append(f"{name}.{field_name}")
    assert not undocumented, f"undocumented fields: {undocumented}"


def _described(field_schema: dict[str, Any]) -> bool:
    """Whether a property schema carries a description.

    Checked on the property and on the branches of an `anyOf`, because Pydantic
    renders an optional field as `anyOf: [..., {"type": "null"}]` and may hang
    the description on either level depending on how the field is declared.
    """
    if field_schema.get("description"):
        return True
    return any(branch.get("description") for branch in field_schema.get("anyOf", []))
