"""Unit tests for the ballot endpoint and its pieces (story 1.5, 2.3).

The feature file proves the voter's journey; these pin the behaviour at each
seam the journey walks through, so a regression names the broken piece rather
than just "the ballot is wrong".

The resolver these tests inject is `tests.support.resolvers.FixedDistrictResolver`,
not a production class. Story 1.5 shipped `StubAddressResolver` in `app/` because
the app really did resolve addresses with it; story 2.3 replaced it with
`CensusAddressResolver`, so a fixed-district resolver is now only a *test* need
and lives in `tests/support`. These tests inject it deliberately rather than
letting `BallotService` default: the default is now the real geocoder, and the
service's own behaviour — current-term matching, paging, privacy — is what is
under test here, not geocoding. `test_districts_resolver_unit.py` and
`features/address_to_district.feature` cover the geocoder itself.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ballot.router import ballot_service
from app.ballot.service import HOUSE_BODY_ID, SENATE_BODY_ID, BallotService
from app.districts.resolver import (
    AddressResolutionError,
    CensusAddressResolver,
    DistrictNotFoundError,
)
from app.main import create_app, zerodb_client
from app.repositories import Repository
from app.repositories.models import District, Person
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport
from tests.support.reference_data import REFERENCE_TABLES, seed_reference_data
from tests.support.resolvers import FixedDistrictResolver

HOUSE_DISTRICT = FixedDistrictResolver.DISTRICT_ID
SENATE_DISTRICT = "ocd-division/country:us/state:ca"
ADDRESS = "1 Synthetic Way, Example, CA 90000"


@pytest.fixture
def client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    zdb = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    for table in (*REFERENCE_TABLES, "districts", "people"):
        zdb.create_table(table, {"id": "string"})
    return zdb


def seed_body(client: ZeroDBClient) -> None:
    """Story 2.1's reference data: `us-federal`, `us-house`, `us-senate`.

    Needed by every test that writes a `districts` row, because story 2.3
    declared `districts.body_id` a reference to `bodies` — the call story 1.4's
    `District` docstring explicitly deferred to this story. Shared with the
    feature step files through `tests/support`, and built from `app.seed`'s own
    constants, so no fixture can claim a chamber the seed does not write.
    """
    seed_reference_data(client)


def seed_district(client: ZeroDBClient, district_id: str = HOUSE_DISTRICT) -> None:
    seed_body(client)
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


def service_for(client: ZeroDBClient, **kwargs: Any) -> BallotService:
    """A `BallotService` on the fake ZeroDB with a determinate resolver.

    The resolver is injected rather than defaulted because the default is now
    the real Census Geocoder (story 2.3), and these tests must make no network
    call. Every test below goes through here, so there is one place that knows
    which resolver the service suite uses.
    """
    return BallotService(client=client, resolver=FixedDistrictResolver(), **kwargs)


@pytest.fixture
def api(client: ZeroDBClient) -> Any:
    """The real app, with its ZeroDB client and its resolver overridden.

    Both overrides go through the app's own dependency seams, so the handler,
    service, repository and client under test are the production ones. The
    resolver override is what keeps the endpoint tests off the network now that
    `ballot_service` defaults to `CensusAddressResolver`.
    """
    app = create_app()
    app.dependency_overrides[zerodb_client] = lambda: client
    app.dependency_overrides[ballot_service] = lambda: service_for(client)
    with TestClient(app) as test_client:
        yield test_client


# --- the resolver the service gets by default ----------------------------
#
# Story 1.5 had four tests here for `StubAddressResolver`, two of which this
# story deleted rather than ported, because they asserted the *absence* of
# geocoding:
#
#   * `test_stub_resolver_returns_a_well_formed_ocd_id` — that the stub's
#     fixed answer was at least OCD-ID-shaped, so swapping a real geocoder in
#     behind the protocol would change the implementation and not the contract.
#     Now asserted against a captured Census response in
#     `test_districts_resolver_unit.py::test_resolver_returns_a_well_formed_ocd_id`,
#     where it tests the code that actually builds the id.
#   * `test_stub_resolver_is_the_same_answer_for_every_address` — that the stub
#     answered *identically* for every address, so nobody could mistake it for
#     a geocoder. That was the right guarantee while the app shipped without
#     one; it is now exactly the wrong one, and its replacement in
#     `test_districts_resolver_unit.py` asserts the opposite —
#     `test_resolver_gives_different_districts_for_different_addresses`.
#
# The other two (blank address refused, senate division derived from the state)
# are real behaviour of any resolver, so they survive below against the test
# double, and against the real resolver in `test_districts_resolver_unit.py`.
#
# What belongs here instead is the one thing those tests could not cover while
# the stub was the default: that the service and the endpoint are now wired to
# the *real* resolver.


def test_the_service_defaults_to_the_real_census_resolver() -> None:
    """The one line story 2.3 was supposed to change (prd.md's data source).

    Pinned because the whole story is observable from the outside as "the
    default stopped being a stub". A `BallotService()` built with no resolver —
    which is what `ballot_service` in the router does — must geocode for real,
    and nothing in `app/` should be able to hand a voter a fixed district.
    """
    service = BallotService(client=None)  # type: ignore[arg-type]
    assert isinstance(service._resolver, CensusAddressResolver)


def test_no_fixed_district_resolver_ships_in_the_app_package() -> None:
    """Story 1.5's stub is gone from production code, not merely unused.

    While it lived in `app/districts/`, any caller could construct it and serve
    confident, wrong districts. The test double now lives in `tests/support`,
    so that is no longer reachable from shipped code.
    """
    import app.districts as districts
    import app.districts.resolver as resolver_module

    assert not hasattr(resolver_module, "StubAddressResolver")
    assert not hasattr(districts, "StubAddressResolver")


def test_the_resolver_refuses_a_blank_address() -> None:
    """Kept from story 1.5: a blank address is a 422, never a confident answer."""
    with pytest.raises(AddressResolutionError):
        FixedDistrictResolver().resolve("   ")


def test_the_resolver_derives_the_senate_district_from_the_state() -> None:
    """Senators represent the state, not a House district (datamodel.md).

    Kept from story 1.5. The senate OCD-ID is the House one with the `/cd:*`
    segment dropped, which is how the service finds two senators for one
    address; the real resolver is held to the same rule in
    `test_districts_resolver_unit.py`.
    """
    resolved = FixedDistrictResolver().resolve(ADDRESS)
    assert resolved.senate_district_id == "ocd-division/country:us/state:ca"
    assert "/cd:" not in resolved.senate_district_id


# --- the service ---------------------------------------------------------


def test_service_returns_one_house_member_and_two_senators(client: ZeroDBClient) -> None:
    seed_full_delegation(client)
    ballot = service_for(client).ballot_for(ADDRESS)

    house = [o for o in ballot.officeholders if o.body_id == "us-house"]
    senate = [o for o in ballot.officeholders if o.body_id == "us-senate"]
    assert len(house) == 1
    assert len(senate) == 2
    assert ballot.district.id == HOUSE_DISTRICT


def test_service_returns_the_representative_of_an_at_large_state(
    client: ZeroDBClient,
) -> None:
    """An at-large state's one representative must not be lost to its senators.

    The case story 1.5 could not reach and story 2.3 creates. In Wyoming,
    Vermont, Delaware, Alaska and the two Dakotas the House seat is at-large, so
    the geocoder codes it `00` and `ocd_id_for` returns the **state-only**
    division — which means `district_id` and `senate_district_id` are the same
    string. Any lookup keyed on the district id alone silently keeps whichever
    chamber it saw last and drops the other, and with the senators written
    second it is the representative who disappears: a Wyoming voter is shown two
    senators and told they have no representative.

    Not a hypothetical. Confirmed against the live project, where every voter in
    those six states plus DC and the five territories — roughly five million
    people — got an empty House seat.
    """
    seed_body(client)
    at_large = "ocd-division/country:us/state:wy"
    Repository(client=client, model=District).upsert(
        {
            "id": at_large,
            "body_id": "us-house",
            "name": "Congressional District (at Large)",
            "state": "WY",
            "valid_from": "2023-01-03",
        }
    )
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert(person("person:ZZATLARGE", "us-house", at_large, name="Rep. At Large"))
    people.upsert(person("person:ZZWYSEN1", "us-senate", at_large, name="Sen. Wyo One"))
    people.upsert(person("person:ZZWYSEN2", "us-senate", at_large, name="Sen. Wyo Two"))

    resolver = FixedDistrictResolver(at_large, name="Congressional District (at Large)")
    ballot = BallotService(client=client, resolver=resolver).ballot_for(ADDRESS)

    assert [o.name for o in ballot.officeholders if o.body_id == HOUSE_BODY_ID] == ["Rep. At Large"]
    assert [o.name for o in ballot.officeholders if o.body_id == SENATE_BODY_ID] == [
        "Sen. Wyo One",
        "Sen. Wyo Two",
    ]


def test_service_returns_the_delegate_of_a_district_with_no_senators(
    client: ZeroDBClient,
) -> None:
    """DC's delegate is the same collision with no senators to hide it.

    DC, Puerto Rico and the four other territories have a non-voting delegate or
    resident commissioner (geocoder code `98`) and no senators at all, so the
    state-only division carries a House term and nothing else. The delegate is a
    real sitting member of Congress that story 2.2 loaded, and a DC voter asking
    who represents them must be told — an empty answer would read as "Informed
    has no data for you".
    """
    seed_body(client)
    dc = "ocd-division/country:us/state:dc"
    Repository(client=client, model=District).upsert(
        {
            "id": dc,
            "body_id": "us-house",
            "name": "Delegate District (at Large)",
            "state": "DC",
            "valid_from": "2023-01-03",
        }
    )
    Repository(client=client, model=Person).upsert(
        person("person:ZZDELEGATE", "us-house", dc, name="Del. Synthetic")
    )

    resolver = FixedDistrictResolver(dc, name="Delegate District (at Large)")
    ballot = BallotService(client=client, resolver=resolver).ballot_for(ADDRESS)

    assert [(o.name, o.body_id) for o in ballot.officeholders] == [
        ("Del. Synthetic", HOUSE_BODY_ID)
    ]


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
    ballot = service_for(client).ballot_for(ADDRESS)
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
    ballot = service_for(client).ballot_for(ADDRESS)
    names = {o.name for o in ballot.officeholders if o.body_id == "us-senate"}
    assert names == {"Sen. One", "Sen. Two"}


def test_service_returns_an_empty_delegation_rather_than_failing(client: ZeroDBClient) -> None:
    """A district whose members have not been ingested yet is not an error.

    Story 2.2 ingests the real Congress. Until then, and for any future
    jurisdiction added before its legislators, an empty list is the honest
    answer; a 500 would make the skeleton look broken when it is merely empty.
    """
    seed_district(client)
    ballot = service_for(client).ballot_for(ADDRESS)
    assert ballot.officeholders == []
    assert ballot.district.id == HOUSE_DISTRICT


def test_service_creates_the_district_row_on_first_resolution(client: ZeroDBClient) -> None:
    """A district nobody has looked up before is created, not 404'd.

    Story 1.5 raised `DistrictNotFoundError` here, because its stub resolved to
    a district only a fixture could have seeded. Story 2.3 resolves *real*
    districts, and the `districts` table was empty in the live project — so
    keeping that behaviour would 404 every real voter in the country. The row is
    written from the geocoder's own answer instead; see `app.districts.store`
    for why that beats pre-seeding a roster.
    """
    seed_body(client)
    ballot = service_for(client).ballot_for(ADDRESS)

    assert ballot.district.id == HOUSE_DISTRICT
    assert ballot.district.name == FixedDistrictResolver.DEFAULT_NAME
    stored = Repository(client=client, model=District).get(HOUSE_DISTRICT)
    assert stored is not None
    assert stored.body_id == "us-house"


def test_service_creating_a_district_twice_leaves_one_row(client: ZeroDBClient) -> None:
    """Two voters in one cold district must not produce two rows.

    datamodel.md rule 1: a deterministic natural-key id makes the write an
    idempotent upsert. Asserted because the lazy-creation design makes a
    concurrent first lookup the normal case, not an edge one.
    """
    seed_body(client)
    service_for(client).ballot_for(ADDRESS)
    service_for(client).ballot_for(ADDRESS)

    districts = Repository(client=client, model=District).query(limit=100)
    assert [d.id for d in districts] == [HOUSE_DISTRICT]


def test_service_raises_not_found_when_the_district_row_cannot_be_created(
    client: ZeroDBClient,
) -> None:
    """The one remaining 404: a resolved district Informed cannot record.

    `districts.body_id` references `bodies` (declared by this story, as story
    1.4's `District` docstring deferred to it), so a chamber story 2.1 never
    seeded makes the row unwritable. That is Informed's data gap and not
    anything the voter can retype, which is exactly the distinction
    `DistrictNotFoundError` exists to carry — so the handler still has a real
    404 to answer rather than a branch that can no longer be reached.
    """
    with pytest.raises(DistrictNotFoundError) as excinfo:
        service_for(client).ballot_for(ADDRESS)

    assert isinstance(excinfo.value, AddressResolutionError)
    assert "Private" not in str(excinfo.value)


def test_the_not_found_error_never_names_the_address(client: ZeroDBClient) -> None:
    """prd.md, privacy first: the failure path leaks nothing either.

    The 404's message is both returned to the voter and logged, so it is held to
    the same rule as every other message in this flow.
    """
    with pytest.raises(DistrictNotFoundError) as excinfo:
        service_for(client).ballot_for("500 Private Lane, Example, CA")
    assert "Private Lane" not in str(excinfo.value)


def test_service_never_puts_the_address_in_its_result(client: ZeroDBClient) -> None:
    """prd.md, privacy first: the address is not stored and not echoed."""
    seed_full_delegation(client)
    ballot = service_for(client).ballot_for("500 Private Lane, Example, CA")
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

    ballot = service_for(client).ballot_for(ADDRESS)
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

    ballot = service_for(client).ballot_for(ADDRESS)
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

    ballot = service_for(client).ballot_for(ADDRESS)
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

    ballot = service_for(client).ballot_for(ADDRESS)
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

    ballot = service_for(client).ballot_for(ADDRESS)
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

    ballot = service_for(client).ballot_for(ADDRESS)
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

    ballot = service_for(client).ballot_for(ADDRESS)
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

    ballot = service_for(client, today=lambda: date(2026, 6, 15)).ballot_for(ADDRESS)
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

    ballot = service_for(client).ballot_for(ADDRESS)
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
        service_for(client).ballot_for(ADDRESS)

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
    assert service_for(client).ballot_for(ADDRESS).officeholders == []


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


def test_endpoint_answers_200_for_a_district_it_has_not_seen_before(
    api: TestClient, client: ZeroDBClient
) -> None:
    """A real district nobody has looked up yet is a 200, not a 404.

    This is the externally visible consequence of story 2.3's lazy creation.
    Story 1.5 answered 404 here, which was right while the only districts were
    fixtures; against the real geocoder and an empty `districts` table it would
    have 404'd every voter in the country.
    """
    seed_body(client)
    response = api.get("/v1/ballot", params={"address": ADDRESS})

    assert response.status_code == 200
    assert response.json()["district"]["id"] == HOUSE_DISTRICT
    assert response.json()["officeholders"] == []


def test_endpoint_answers_404_when_the_district_row_cannot_be_created(
    api: TestClient,
) -> None:
    """The 404 the router documents, still reachable.

    With no `bodies` row for the resolved district's chamber, Informed cannot
    record the district it just resolved. The voter gets a 404 rather than a 500
    because it is a data gap on Informed's side and not a server fault, and
    nothing they retype will change it.
    """
    response = api.get("/v1/ballot", params={"address": ADDRESS})

    assert response.status_code == 404
    assert "district" in response.json()["detail"].lower()


def test_endpoint_does_not_echo_the_address_in_a_404(api: TestClient) -> None:
    """The 404 body is voter-facing, so privacy first applies to it too."""
    response = api.get("/v1/ballot", params={"address": "77 Secret Road, Example, CA"})
    assert response.status_code == 404
    assert "Secret Road" not in response.text


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
