"""Steps for the walking-skeleton feature (story 1.5).

Runs the real FastAPI app against the in-memory ZeroDB fake from story 1.2, so
the whole path under test — handler, repository, client, transport — is the
production code. Only the wire is faked, which is why no live credentials are
needed for the committed suite.

The address resolver is stubbed in production too (story 2.3 owns the Census
Geocoder), so these steps do not stub it: they exercise the same stub the app
ships with, and assert on the district it returns.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.ballot.models import BallotResponse
from app.ballot.service import BallotService
from app.districts.resolver import StubAddressResolver
from app.main import create_app, zerodb_client
from app.repositories import Repository
from app.repositories.models import District, Person
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/walking_skeleton.feature")

#: The district the stub resolver returns for any address (see the resolver's
#: docstring). Matching it here rather than hard-coding a second copy means a
#: change to the stub fails this feature instead of silently diverging.
TEST_DISTRICT_ID = StubAddressResolver.DISTRICT_ID

#: An address no voter owns. prd.md: never put real voter data in a fixture.
SYNTHETIC_ADDRESS = "1600 Pennsylvania Ave, Washington, DC"


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
    for table in ("districts", "people"):
        client.create_table(table, {"id": "string"})
    return client


@pytest.fixture
def districts(zdb: ZeroDBClient) -> Repository[District]:
    return Repository(client=zdb, model=District)


@pytest.fixture
def people(zdb: ZeroDBClient) -> Repository[Person]:
    return Repository(client=zdb, model=Person)


@pytest.fixture
def api(zdb: ZeroDBClient) -> Any:
    """The real app, wired to the fake ZeroDB through the app's own override."""
    app = create_app()
    app.dependency_overrides[zerodb_client] = lambda: zdb
    with TestClient(app) as test_client:
        yield test_client


# --- Given ---------------------------------------------------------------


@given(parsers.parse('the district "{district_id}" exists in ZeroDB'))
def district_exists(districts: Repository[District], district_id: str) -> None:
    state = district_id.split("state:")[1].split("/")[0]
    districts.upsert(
        {
            "id": district_id,
            "body_id": "us-house",
            "name": f"Synthetic district {district_id.rsplit('/', 1)[-1]}",
            "state": state.upper(),
            "valid_from": "2023-01-03",
        }
    )


@given("its representative and California's two senators are stored in ZeroDB")
def officeholders_exist(people: Repository[Person]) -> None:
    for person in synthetic_officeholders():
        people.upsert(person)


#: Term bounds relative to today, so no scenario starts failing on a calendar
#: date. A hard-coded future year would quietly become a past term.
TODAY = date.today()
TERM_START = (TODAY - timedelta(days=365)).isoformat()
TERM_END = (TODAY + timedelta(days=365)).isoformat()

#: The name of the former representative used by the term-history scenario.
FORMER_REPRESENTATIVE = "Former Rep. Synthetic Predecessor"


@given(
    "the district's previous representative is also stored in ZeroDB, with a term that has ended"
)
def former_representative_exists(people: Repository[Person]) -> None:
    """Term history, which is how story 2.2's real data will look.

    `people.terms[]` is a list because it holds every term a member has served,
    so a district's past representative sits in the same table as its current
    one. The ballot must tell them apart.
    """
    people.upsert(
        {
            "id": "person:ZZ000099",
            "canonical_name": FORMER_REPRESENTATIVE,
            "external_ids": {"bioguide": "ZZ000099"},
            "terms": [
                {
                    "body_id": "us-house",
                    "district_id": TEST_DISTRICT_ID,
                    "party": "Independent",
                    "start": (TODAY - timedelta(days=365 * 6)).isoformat(),
                    "end": (TODAY - timedelta(days=365 * 4)).isoformat(),
                }
            ],
        }
    )


def synthetic_officeholders() -> list[dict[str, Any]]:
    """One House member and two senators, shaped exactly as datamodel.md says.

    Synthetic names and bioguide-shaped ids, not the real sitting members:
    story 2.2 ingests those for real. `ZZ` prefixes make a stray row obvious.
    """
    return [
        {
            "id": "person:ZZ000001",
            "canonical_name": "Rep. Synthetic Example",
            "external_ids": {"bioguide": "ZZ000001"},
            "terms": [
                {
                    "body_id": "us-house",
                    "district_id": TEST_DISTRICT_ID,
                    "party": "Independent",
                    "start": TERM_START,
                    "end": TERM_END,
                }
            ],
        },
        {
            "id": "person:ZZ000002",
            "canonical_name": "Sen. Synthetic One",
            "external_ids": {"bioguide": "ZZ000002"},
            "terms": [
                {
                    "body_id": "us-senate",
                    "district_id": "ocd-division/country:us/state:ca",
                    "party": "Independent",
                    "start": TERM_START,
                    "end": TERM_END,
                }
            ],
        },
        {
            "id": "person:ZZ000003",
            "canonical_name": "Sen. Synthetic Two",
            "external_ids": {"bioguide": "ZZ000003"},
            "terms": [
                {
                    "body_id": "us-senate",
                    "district_id": "ocd-division/country:us/state:ca",
                    "party": "Independent",
                    "start": TERM_START,
                    "end": TERM_END,
                }
            ],
        },
    ]


# --- When ----------------------------------------------------------------


@when(parsers.parse('the voter submits the address "{address}"'))
def submit_address(api: TestClient, context: Any, address: str) -> None:
    context.response = api.get("/v1/ballot", params={"address": address})


@when("the voter submits an empty address")
def submit_empty_address(api: TestClient, context: Any) -> None:
    context.response = api.get("/v1/ballot", params={"address": "   "})


@when("the voter submits an address in that district")
def submit_address_in_district(api: TestClient, context: Any, zdb: ZeroDBClient) -> None:
    """Resolve to the Wyoming district instead of the stub's default one.

    The service takes its resolver by injection precisely so a caller can point
    it at a different district without a second HTTP surface; story 2.3 replaces
    the resolver with the real geocoder behind the same seam.
    """
    service = BallotService(
        client=zdb,
        resolver=StubAddressResolver(district_id="ocd-division/country:us/state:wy/cd:at-large"),
    )
    context.response = service.ballot_for(SYNTHETIC_ADDRESS)


# --- Then ----------------------------------------------------------------


def payload(context: Any) -> dict[str, Any]:
    """The ballot payload, whether the step called HTTP or the service.

    Dispatches on `status_code` rather than on `json`, because a Pydantic model
    still carries a (deprecated) `json` attribute and would be mistaken for an
    HTTP response.
    """
    response = context.response
    if isinstance(response, BallotResponse):
        return response.model_dump(mode="json")
    assert response.status_code == 200, response.text
    return response.json()


@then("the ballot response names the voter's House district")
def names_the_district(context: Any) -> None:
    body = payload(context)
    assert body["district"]["id"].startswith("ocd-division/country:us/")
    assert body["district"]["name"]


@then(parsers.parse("it lists exactly {count:d} House member"))
@then(parsers.parse("it lists exactly {count:d} House members"))
def lists_house_members(context: Any, count: int) -> None:
    house = [o for o in payload(context)["officeholders"] if o["body_id"] == "us-house"]
    assert len(house) == count, f"got {len(house)} House members: {house}"


@then(parsers.parse("it lists exactly {count:d} senators"))
def lists_senators(context: Any, count: int) -> None:
    senate = [o for o in payload(context)["officeholders"] if o["body_id"] == "us-senate"]
    assert len(senate) == count, f"got {len(senate)} senators: {senate}"


@then("the former representative is not among them")
def former_representative_excluded(context: Any) -> None:
    names = [o["name"] for o in payload(context)["officeholders"]]
    assert FORMER_REPRESENTATIVE not in names, f"a former member was listed: {names}"


@then("every officeholder carries the body they serve in")
def officeholders_carry_body(context: Any) -> None:
    for officeholder in payload(context)["officeholders"]:
        assert officeholder["body_id"] in {"us-house", "us-senate"}
        assert officeholder["person_id"]
        assert officeholder["name"]


@then("the request is rejected with a helpful message")
def rejected_with_message(context: Any) -> None:
    response = context.response
    assert response.status_code == 422, response.text
    assert "address" in response.text.lower()


@then("the response does not echo the address back")
def address_not_echoed(context: Any) -> None:
    assert SYNTHETIC_ADDRESS not in context.response.text
    assert "Pennsylvania" not in context.response.text


@then("no row written to ZeroDB contains the address")
def address_not_stored(transport: FakeZeroDBTransport, zdb: ZeroDBClient) -> None:
    """The ballot read is a read. Nothing about it may land in storage.

    prd.md, privacy first: the address is political-opinion-adjacent data and
    story 2.3's acceptance criterion says it is never stored. Asserting it here,
    in the skeleton, means the rule is in place before there is any code that
    could break it.
    """
    for table in ("districts", "people"):
        for row in zdb.query_rows(table, limit=100).rows:
            serialised = str(row.row_data)
            assert "Pennsylvania" not in serialised
            assert SYNTHETIC_ADDRESS not in serialised
