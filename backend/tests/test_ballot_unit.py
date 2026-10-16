"""Unit tests for the ballot endpoint and its pieces (story 1.5).

The feature file proves the voter's journey; these pin the behaviour at each
seam the journey walks through, so a regression names the broken piece rather
than just "the ballot is wrong".
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ballot.service import BallotService
from app.districts.resolver import AddressResolutionError, StubAddressResolver
from app.main import create_app, zerodb_client
from app.repositories import Repository
from app.repositories.models import District, Person
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

HOUSE_DISTRICT = StubAddressResolver.DISTRICT_ID
SENATE_DISTRICT = "ocd-division/country:us/state:ca"
ADDRESS = "1 Synthetic Way, Example, CA 90000"


@pytest.fixture
def client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    zdb = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    for table in ("districts", "people"):
        zdb.create_table(table, {"id": "string"})
    return zdb


def seed_district(client: ZeroDBClient, district_id: str = HOUSE_DISTRICT) -> None:
    Repository(client=client, model=District).upsert(
        {
            "id": district_id,
            "body_id": "us-house",
            "name": "Synthetic District 19",
            "state": "CA",
            "valid_from": "2023-01-03",
        }
    )


def person(
    person_id: str,
    body_id: str,
    district_id: str,
    *,
    name: str = "Synthetic Member",
    end: str | None = "2027-01-03",
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
                "start": "2023-01-03",
                "end": end,
            }
        ],
    }


def seed_full_delegation(client: ZeroDBClient) -> None:
    seed_district(client)
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert(person("person:ZZ000001", "us-house", HOUSE_DISTRICT, name="Rep. Synthetic"))
    people.upsert(person("person:ZZ000002", "us-senate", SENATE_DISTRICT, name="Sen. One"))
    people.upsert(person("person:ZZ000003", "us-senate", SENATE_DISTRICT, name="Sen. Two"))


@pytest.fixture
def api(client: ZeroDBClient) -> Any:
    app = create_app()
    app.dependency_overrides[zerodb_client] = lambda: client
    with TestClient(app) as test_client:
        yield test_client


# --- the stub resolver ---------------------------------------------------


def test_stub_resolver_returns_a_well_formed_ocd_id() -> None:
    """An OCD-ID, because that is `districts`' id pattern (datamodel.md).

    Story 2.3 swaps the Census Geocoder in behind this interface. If the stub
    returned anything but a real OCD-ID shape, that swap would change the
    contract rather than just the implementation.
    """
    resolved = StubAddressResolver().resolve(ADDRESS)
    assert resolved.district_id.startswith("ocd-division/country:us/state:")
    assert resolved.state == "CA"


def test_stub_resolver_is_the_same_answer_for_every_address() -> None:
    """Named honestly: it does not geocode, and the test says so.

    A stub that varied its answer would look like a geocoder and be trusted as
    one. This asserts the opposite, so nobody mistakes it for story 2.3's work.
    """
    first = StubAddressResolver().resolve("1 A St, Example, CA")
    second = StubAddressResolver().resolve("99 Z Blvd, Other, NY")
    assert first == second


def test_stub_resolver_rejects_a_blank_address() -> None:
    with pytest.raises(AddressResolutionError):
        StubAddressResolver().resolve("   ")


def test_stub_resolver_derives_the_senate_district_from_the_state() -> None:
    """Senators represent the state, not a House district (datamodel.md).

    The senate OCD-ID is the House one with the `/cd:*` segment dropped, which
    is how the service finds two senators for one address.
    """
    resolved = StubAddressResolver().resolve(ADDRESS)
    assert resolved.senate_district_id == "ocd-division/country:us/state:ca"
    assert "/cd:" not in resolved.senate_district_id


# --- the service ---------------------------------------------------------


def test_service_returns_one_house_member_and_two_senators(client: ZeroDBClient) -> None:
    seed_full_delegation(client)
    ballot = BallotService(client=client).ballot_for(ADDRESS)

    house = [o for o in ballot.officeholders if o.body_id == "us-house"]
    senate = [o for o in ballot.officeholders if o.body_id == "us-senate"]
    assert len(house) == 1
    assert len(senate) == 2
    assert ballot.district.id == HOUSE_DISTRICT


def test_service_ignores_a_person_from_another_district(client: ZeroDBClient) -> None:
    """The whole point of filtering on `terms[]` rather than returning the table.

    With the live server-side filter unusable (story 1.4), the match happens in
    Python; a bug there would return every member in the country, which is
    exactly the kind of thing that looks fine on a one-row fixture.
    """
    seed_full_delegation(client)
    Repository(client=client, model=Person).upsert(
        person(
            "person:ZZ000009",
            "us-house",
            "ocd-division/country:us/state:tx/cd:1",
            name="Rep. Elsewhere",
        )
    )
    ballot = BallotService(client=client).ballot_for(ADDRESS)
    assert [o.name for o in ballot.officeholders if o.body_id == "us-house"] == ["Rep. Synthetic"]


def test_service_ignores_a_senator_from_another_state(client: ZeroDBClient) -> None:
    seed_full_delegation(client)
    Repository(client=client, model=Person).upsert(
        person(
            "person:ZZ000010",
            "us-senate",
            "ocd-division/country:us/state:ny",
            name="Sen. Elsewhere",
        )
    )
    ballot = BallotService(client=client).ballot_for(ADDRESS)
    names = {o.name for o in ballot.officeholders if o.body_id == "us-senate"}
    assert names == {"Sen. One", "Sen. Two"}


def test_service_returns_an_empty_delegation_rather_than_failing(client: ZeroDBClient) -> None:
    """A district whose members have not been ingested yet is not an error.

    Story 2.2 ingests the real Congress. Until then, and for any future
    jurisdiction added before its legislators, an empty list is the honest
    answer; a 500 would make the skeleton look broken when it is merely empty.
    """
    seed_district(client)
    ballot = BallotService(client=client).ballot_for(ADDRESS)
    assert ballot.officeholders == []
    assert ballot.district.id == HOUSE_DISTRICT


def test_service_raises_when_the_resolved_district_is_not_in_zerodb(client: ZeroDBClient) -> None:
    """An unknown district is a data gap, not a voter error.

    Surfaced as its own exception so the handler can answer 404 rather than
    inventing a district row the voter would then see named on screen.
    """
    with pytest.raises(AddressResolutionError):
        BallotService(client=client).ballot_for(ADDRESS)


def test_service_never_puts_the_address_in_its_result(client: ZeroDBClient) -> None:
    """prd.md, privacy first: the address is not stored and not echoed."""
    seed_full_delegation(client)
    ballot = BallotService(client=client).ballot_for("500 Private Lane, Example, CA")
    assert "Private Lane" not in str(ballot.model_dump(mode="json"))


def test_service_pages_through_people_rather_than_reading_one_page(
    client: ZeroDBClient,
) -> None:
    """The delegation must not be limited to `people`'s first page.

    The members are seeded *after* a page's worth of unrelated rows, so a
    service that read one page would find the district but none of its
    representatives — the same one-page bug story 1.4 found live in the client's
    filter fallback, which looked fine against a small fixture.
    """
    from app.ballot.service import PEOPLE_PAGE_SIZE

    seed_district(client)
    people: Repository[Person] = Repository(client=client, model=Person)
    for index in range(PEOPLE_PAGE_SIZE + 5):
        people.upsert(
            person(
                f"person:ZZFILL{index:04d}",
                "us-house",
                "ocd-division/country:us/state:tx/cd:1",
                name=f"Rep. Filler {index}",
            )
        )
    people.upsert(person("person:ZZ000001", "us-house", HOUSE_DISTRICT, name="Rep. Synthetic"))
    people.upsert(person("person:ZZ000002", "us-senate", SENATE_DISTRICT, name="Sen. One"))
    people.upsert(person("person:ZZ000003", "us-senate", SENATE_DISTRICT, name="Sen. Two"))

    ballot = BallotService(client=client).ballot_for(ADDRESS)
    assert [o.name for o in ballot.officeholders] == ["Rep. Synthetic", "Sen. One", "Sen. Two"]


def test_service_logs_an_error_rather_than_silently_truncating(
    client: ZeroDBClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Hitting the scan cap must be loud.

    A voter quietly missing a senator is indistinguishable from a voter who has
    none, so the cap logs an error naming the read model that should replace the
    scan. The cap is lowered here rather than seeding 5,000 rows.
    """
    import app.ballot.service as service_module

    monkeypatch.setattr(service_module, "MAX_PEOPLE_SCANNED", service_module.PEOPLE_PAGE_SIZE)
    seed_district(client)
    people: Repository[Person] = Repository(client=client, model=Person)
    for index in range(service_module.PEOPLE_PAGE_SIZE):
        people.upsert(
            person(
                f"person:ZZFILL{index:04d}",
                "us-house",
                "ocd-division/country:us/state:tx/cd:1",
            )
        )

    with caplog.at_level("ERROR"):
        BallotService(client=client).ballot_for(ADDRESS)

    assert "stopped scanning" in caplog.text
    assert "candidate_cards" in caplog.text


def test_service_skips_a_term_with_no_district(client: ZeroDBClient) -> None:
    """`Term.district_id` is optional in the model, so None must not match.

    A None district_id compared loosely against a None filter would make every
    term-less person a member of every district.
    """
    seed_district(client)
    Repository(client=client, model=Person).upsert(
        {
            "id": "person:ZZ000011",
            "canonical_name": "No District",
            "terms": [{"body_id": "us-house", "party": "Independent"}],
        }
    )
    assert BallotService(client=client).ballot_for(ADDRESS).officeholders == []


# --- the endpoint --------------------------------------------------------


def test_endpoint_returns_the_delegation(api: TestClient, client: ZeroDBClient) -> None:
    seed_full_delegation(client)
    response = api.get("/v1/ballot", params={"address": ADDRESS})

    assert response.status_code == 200
    body = response.json()
    assert body["district"]["id"] == HOUSE_DISTRICT
    assert len(body["officeholders"]) == 3
    assert {o["body_id"] for o in body["officeholders"]} == {"us-house", "us-senate"}


def test_endpoint_rejects_a_missing_address(api: TestClient) -> None:
    assert api.get("/v1/ballot").status_code == 422


def test_endpoint_rejects_a_blank_address(api: TestClient) -> None:
    response = api.get("/v1/ballot", params={"address": "   "})
    assert response.status_code == 422
    assert "address" in response.text.lower()


def test_endpoint_answers_404_for_an_unknown_district(api: TestClient) -> None:
    response = api.get("/v1/ballot", params={"address": ADDRESS})
    assert response.status_code == 404
    assert "district" in response.json()["detail"].lower()


def test_endpoint_does_not_echo_the_address(api: TestClient, client: ZeroDBClient) -> None:
    seed_full_delegation(client)
    response = api.get("/v1/ballot", params={"address": "77 Secret Road, Example, CA"})
    assert "Secret Road" not in response.text


def test_endpoint_makes_no_recommendation(api: TestClient, client: ZeroDBClient) -> None:
    """prd.md non-negotiable: the app never recommends who to vote for.

    Asserted on the payload, not the UI, because a field named `recommended`
    would be the first thing a client rendered as one.
    """
    seed_full_delegation(client)
    body = api.get("/v1/ballot", params={"address": ADDRESS}).text.lower()
    for forbidden in ("recommend", "endorse", "vote for", "best match"):
        assert forbidden not in body


def test_health_still_works(api: TestClient) -> None:
    """The story 1.1 endpoint keeps answering once the app grows a dependency."""
    assert api.get("/health").json()["status"] == "ok"
