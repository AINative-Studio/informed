"""Unit tests for the ballot endpoint and its pieces (story 1.5).

The feature file proves the voter's journey; these pin the behaviour at each
seam the journey walks through, so a regression names the broken piece rather
than just "the ballot is wrong".
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ballot.service import HOUSE_BODY_ID, SENATE_BODY_ID, BallotService
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


#: Term bounds relative to today, so no test starts failing on a calendar date.
#: A hard-coded "2027-01-03" would have quietly become a *past* term in 2027 and
#: turned every delegation test red for a reason nobody would look for.
TODAY = date.today()
TERM_START = (TODAY - timedelta(days=365)).isoformat()
TERM_END = (TODAY + timedelta(days=365)).isoformat()
PAST_TERM_START = (TODAY - timedelta(days=365 * 6)).isoformat()
PAST_TERM_END = (TODAY - timedelta(days=365 * 4)).isoformat()


def person(
    person_id: str,
    body_id: str,
    district_id: str,
    *,
    name: str = "Synthetic Member",
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


def test_service_excludes_a_member_whose_term_has_ended(client: ZeroDBClient) -> None:
    """A former representative is not a representative.

    `people.terms[]` is a **list** precisely because it holds term history, and
    story 2.2 ingests real members with every term they have served. Matching on
    any term rather than a current one means a district whose past
    representative is in the table returns two House members, which breaks this
    story's own acceptance criterion ("1 House member and 2 senators") for every
    voter with term history — and presents a non-officeholder as their
    representative, which prd.md's "evidence over opinion" does not permit.
    """
    seed_full_delegation(client)
    Repository(client=client, model=Person).upsert(
        person(
            "person:ZZRETIRED",
            "us-house",
            HOUSE_DISTRICT,
            name="Former Rep. Retired",
            start=PAST_TERM_START,
            end=PAST_TERM_END,
        )
    )

    ballot = BallotService(client=client).ballot_for(ADDRESS)
    house = [o for o in ballot.officeholders if o.body_id == HOUSE_BODY_ID]
    assert [o.name for o in house] == ["Rep. Synthetic"]


def test_service_excludes_a_member_whose_term_has_not_started(client: ZeroDBClient) -> None:
    """A member-elect is not yet in office either.

    The mirror of the test above. Between an election and a swearing-in both the
    outgoing and incoming member are in `people` with terms for the same
    district, so a start check is as load-bearing as an end check — and that is
    exactly the window in which a voter is most likely to look.
    """
    seed_full_delegation(client)
    future_start = (TODAY + timedelta(days=30)).isoformat()
    Repository(client=client, model=Person).upsert(
        person(
            "person:ZZELECT",
            "us-house",
            HOUSE_DISTRICT,
            name="Rep.-elect Incoming",
            start=future_start,
            end=(TODAY + timedelta(days=760)).isoformat(),
        )
    )

    ballot = BallotService(client=client).ballot_for(ADDRESS)
    house = [o for o in ballot.officeholders if o.body_id == HOUSE_BODY_ID]
    assert [o.name for o in house] == ["Rep. Synthetic"]


def test_service_keeps_a_member_whose_term_has_no_end_date(client: ZeroDBClient) -> None:
    """An open-ended term is a current one, not an invalid one.

    `Term.end` is optional in the model, and an ingest source that does not
    publish an end date is common. Treating a missing end as "not current" would
    silently empty a delegation, which is a worse failure than the bug this
    group of tests fixes — so the absent case is pinned explicitly.
    """
    seed_district(client)
    Repository(client=client, model=Person).upsert(
        person("person:ZZOPEN", "us-house", HOUSE_DISTRICT, name="Rep. Open Term", end=None)
    )

    ballot = BallotService(client=client).ballot_for(ADDRESS)
    assert [o.name for o in ballot.officeholders] == ["Rep. Open Term"]


def test_service_keeps_a_member_whose_term_has_no_start_date(client: ZeroDBClient) -> None:
    """Likewise a missing start: unknown is not the same as "not yet"."""
    seed_district(client)
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert(
        {
            "id": "person:ZZNOSTART",
            "canonical_name": "Rep. No Start",
            "terms": [
                {
                    "body_id": "us-house",
                    "district_id": HOUSE_DISTRICT,
                    "party": "Independent",
                    "end": TERM_END,
                }
            ],
        }
    )

    ballot = BallotService(client=client).ballot_for(ADDRESS)
    assert [o.name for o in ballot.officeholders] == ["Rep. No Start"]


def test_service_picks_the_current_term_of_a_member_with_term_history(
    client: ZeroDBClient,
) -> None:
    """One person, several terms in the same seat, counted once.

    How story 2.2's data actually looks: a long-serving member carries one term
    per Congress for the same district. The delegation must list them once, from
    their current term, not once per term they have ever served.
    """
    seed_district(client)
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert(
        {
            "id": "person:ZZLONG",
            "canonical_name": "Rep. Long Serving",
            "terms": [
                {
                    "body_id": "us-house",
                    "district_id": HOUSE_DISTRICT,
                    "party": "Independent",
                    "start": PAST_TERM_START,
                    "end": PAST_TERM_END,
                },
                {
                    "body_id": "us-house",
                    "district_id": HOUSE_DISTRICT,
                    "party": "Independent",
                    "start": TERM_START,
                    "end": TERM_END,
                },
            ],
        }
    )

    ballot = BallotService(client=client).ballot_for(ADDRESS)
    assert len(ballot.officeholders) == 1
    assert ballot.officeholders[0].name == "Rep. Long Serving"


def test_service_lists_a_member_who_moved_from_the_house_to_the_senate_once(
    client: ZeroDBClient,
) -> None:
    """A real pattern: a representative elected to the Senate from the same state.

    Their House term has ended and their Senate term is current, so they belong
    in the Senate list and nowhere else. Listing them in both would overstate
    the delegation and show a chamber they no longer sit in.
    """
    seed_district(client)
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert(
        {
            "id": "person:ZZMOVED",
            "canonical_name": "Sen. Formerly House",
            "terms": [
                {
                    "body_id": "us-house",
                    "district_id": HOUSE_DISTRICT,
                    "party": "Independent",
                    "start": PAST_TERM_START,
                    "end": PAST_TERM_END,
                },
                {
                    "body_id": "us-senate",
                    "district_id": SENATE_DISTRICT,
                    "party": "Independent",
                    "start": TERM_START,
                    "end": TERM_END,
                },
            ],
        }
    )

    ballot = BallotService(client=client).ballot_for(ADDRESS)
    assert [(o.name, o.body_id) for o in ballot.officeholders] == [
        ("Sen. Formerly House", SENATE_BODY_ID)
    ]


def test_service_ignores_an_unparseable_term_date_rather_than_dropping_the_member(
    client: ZeroDBClient,
) -> None:
    """A malformed date must not silently remove someone from a delegation.

    `Term.start`/`end` are plain strings in the model, so an ingest adapter can
    put anything there. Dropping the member would make a bad date look like a
    vacant seat; keeping them surfaces the row so the data can be corrected.
    """
    seed_district(client)
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert(
        {
            "id": "person:ZZBADDATE",
            "canonical_name": "Rep. Bad Date",
            "terms": [
                {
                    "body_id": "us-house",
                    "district_id": HOUSE_DISTRICT,
                    "party": "Independent",
                    "start": "not-a-date",
                    "end": "also-not-a-date",
                }
            ],
        }
    )

    ballot = BallotService(client=client).ballot_for(ADDRESS)
    assert [o.name for o in ballot.officeholders] == ["Rep. Bad Date"]


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        # Both bounds are inclusive: a term's start is the swearing-in day and
        # its end the last day in office, so excluding either would leave a
        # one-day hole in the delegation at each boundary.
        ("2026-06-15", "2026-06-15", True),
        ("2026-06-15", "2026-12-31", True),
        ("2026-01-01", "2026-06-15", True),
        ("2026-06-16", "2026-12-31", False),
        ("2026-01-01", "2026-06-14", False),
        # A timestamped bound is a date, not an unknown: reading it as unknown
        # would make every term from such a source look current.
        ("2026-06-15T00:00:00Z", "2026-12-31T00:00:00Z", True),
        ("2026-01-01T00:00:00Z", "2026-06-14T23:59:59Z", False),
    ],
)
def test_term_currency_at_each_boundary(
    client: ZeroDBClient, start: str, end: str, expected: bool
) -> None:
    """Term bounds decided against an injected date, not the real clock."""
    seed_district(client)
    Repository(client=client, model=Person).upsert(
        person("person:ZZBOUND", "us-house", HOUSE_DISTRICT, start=start, end=end)
    )

    ballot = BallotService(client=client, today=lambda: date(2026, 6, 15)).ballot_for(ADDRESS)
    assert bool(ballot.officeholders) is expected


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
