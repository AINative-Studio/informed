"""Unit tests for the race-grouped ballot and its latency budget (story 2.4).

Story 2.4's two acceptance criteria are "response lists races with incumbents
flagged" and "p95 latency under 800 ms", and these tests pin each at the seam
it lives at. `test_ballot_unit.py` keeps story 1.5/2.3's tests of the
delegation itself (who is returned); this file is about the shape it is
returned in and the cost of returning it.

What is deliberately *not* here: a wall-clock assertion. A millisecond number
measured against the in-memory fake would measure this machine, and one
measured against the live geocoder would make CI depend on census.gov
(CLAUDE.md forbids it). So the budget is defended here by the two things that
actually consume it — the number of geocodes and the number of reads of
`people` — and the millisecond p95 is measured by
`scripts/verify_ballot_live.py` against the live project.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ballot.models import BallotResponse, DataState
from app.ballot.router import ballot_service
from app.ballot.service import (
    HOUSE_BODY_ID,
    PEOPLE_PAGE_SIZE,
    SENATE_BODY_ID,
    BallotService,
    PeopleSnapshot,
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

TODAY = date.today()
TERM_START = (TODAY - timedelta(days=365)).isoformat()
TERM_END = (TODAY + timedelta(days=365)).isoformat()


@pytest.fixture
def transport() -> FakeZeroDBTransport:
    fake = FakeZeroDBTransport()
    fake.break_server_side_filter()
    return fake


@pytest.fixture
def client(transport: FakeZeroDBTransport) -> ZeroDBClient:
    zdb = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    for table in (*REFERENCE_TABLES, "districts", "people"):
        zdb.create_table(table, {"id": "string"})
    seed_reference_data(zdb)
    return zdb


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


def seed_district(
    client: ZeroDBClient, district_id: str = HOUSE_DISTRICT, *, name: str = "D"
) -> None:
    state = district_id.split("state:")[1].split("/")[0]
    Repository(client=client, model=District).upsert(
        {
            "id": district_id,
            "body_id": "us-house",
            "name": name,
            "state": state.upper(),
            "valid_from": "2023-01-03",
        }
    )


def seed_full_delegation(client: ZeroDBClient) -> None:
    seed_district(client, name="Congressional District 19")
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert(person("person:ZZ000001", "us-house", HOUSE_DISTRICT, name="Rep. Synthetic"))
    people.upsert(person("person:ZZ000002", "us-senate", SENATE_DISTRICT, name="Sen. One"))
    people.upsert(person("person:ZZ000003", "us-senate", SENATE_DISTRICT, name="Sen. Two"))


def service_for(client: ZeroDBClient, **kwargs: Any) -> BallotService:
    kwargs.setdefault("resolver", FixedDistrictResolver())
    kwargs.setdefault("snapshot", PeopleSnapshot())
    return BallotService(client=client, **kwargs)


def races_by_body(ballot: BallotResponse) -> dict[str, Any]:
    return {race.body_id: race for race in ballot.races}


# --- the race grouping ---------------------------------------------------


def test_the_ballot_groups_the_delegation_into_one_race_per_seat(client: ZeroDBClient) -> None:
    """The story's first acceptance criterion: a response that lists races.

    1.5 and 2.3 returned a flat `officeholders` list, which answers "who
    represents me" but not "what is on my ballot" — ui-prd.md 2.3 lays the
    ballot view out per race, so the API has to carry that grouping rather than
    leave the client to infer it from `body_id`.
    """
    seed_full_delegation(client)
    ballot = service_for(client).ballot_for(ADDRESS)

    by_body = races_by_body(ballot)
    assert set(by_body) == {HOUSE_BODY_ID, SENATE_BODY_ID}
    assert [c.name for c in by_body[HOUSE_BODY_ID].candidates] == ["Rep. Synthetic"]
    assert [c.name for c in by_body[SENATE_BODY_ID].candidates] == ["Sen. One", "Sen. Two"]


def test_every_candidate_is_flagged_as_an_incumbent(client: ZeroDBClient) -> None:
    """The flag is true because every candidate here IS a sitting member.

    No `elections` or `candidacies` rows exist yet (story 2.6 ingests them), so
    everyone this endpoint can name is somebody with a current term. Flagging
    them as incumbents is a fact about the data, not an assumption — and when
    2.6 adds challengers the flag becomes the discriminator rather than a
    constant.
    """
    seed_full_delegation(client)
    ballot = service_for(client).ballot_for(ADDRESS)

    candidates = [c for race in ballot.races for c in race.candidates]
    assert len(candidates) == 3
    assert all(candidate.is_incumbent for candidate in candidates)


def test_the_response_declares_that_only_incumbents_are_listed(client: ZeroDBClient) -> None:
    """An incomplete ballot must say it is incomplete.

    ui-prd.md 2.1 requires "no upcoming election / no candidates found" to be
    *explained* rather than rendered as a silently empty ballot. A client that
    cannot tell "this race has only an incumbent because challengers are not
    loaded" from "this race has one candidate" would show the second, which is
    a false claim about the race.
    """
    seed_full_delegation(client)
    ballot = service_for(client).ballot_for(ADDRESS)
    assert ballot.data_state is DataState.INCUMBENTS_ONLY


def test_a_race_carries_the_office_name_and_the_district_it_contests(
    client: ZeroDBClient,
) -> None:
    """ui-prd.md 2.3: each race shows "race/office name, district"."""
    seed_full_delegation(client)
    by_body = races_by_body(service_for(client).ballot_for(ADDRESS))

    house = by_body[HOUSE_BODY_ID]
    senate = by_body[SENATE_BODY_ID]
    assert house.district_id == HOUSE_DISTRICT
    assert house.district_name == "Congressional District 19"
    assert "House" in house.office_name
    assert senate.district_id == SENATE_DISTRICT
    assert "Senate" in senate.office_name
    assert senate.district_name == "CA"


def test_a_race_id_is_the_seat_not_an_invented_election(client: ZeroDBClient) -> None:
    """The id names the seat, because the election is not known yet.

    datamodel.md's `candidacies` id is `{election_id}:{district_id}:{person_id}`
    and no `elections` row exists, so any id this story emitted with an election
    in it would be fabricated. `{body_id}:{district_id}` is the seat's own
    identity, is stable across elections, and does not claim to be a candidacy
    id — so when story 2.6 adds real candidacies the response gains their ids
    rather than having to correct these.
    """
    seed_full_delegation(client)
    by_body = races_by_body(service_for(client).ballot_for(ADDRESS))

    assert by_body[HOUSE_BODY_ID].id == f"{HOUSE_BODY_ID}:{HOUSE_DISTRICT}"
    assert by_body[SENATE_BODY_ID].id == f"{SENATE_BODY_ID}:{SENATE_DISTRICT}"
    for race in by_body.values():
        assert "election" not in race.id
        assert race.election_id is None


def test_a_candidate_carries_no_candidacy_id_while_none_exists(client: ZeroDBClient) -> None:
    """`candidacy_id` is null, not guessed.

    prd.md's evidence-over-opinion rule applies to ids as much as to verdicts: a
    plausible-looking id for a row that does not exist is the kind of thing a
    later story joins against and gets nothing back from.
    """
    seed_full_delegation(client)
    ballot = service_for(client).ballot_for(ADDRESS)
    for race in ballot.races:
        for candidate in race.candidates:
            assert candidate.candidacy_id is None


def test_the_at_large_state_gets_two_races_not_one(client: ZeroDBClient) -> None:
    """Story 2.3's bug, restated for the grouped shape.

    In WY, VT, DE, AK, ND and SD the House seat is at-large, so the House and
    Senate OCD-IDs are the **same string**. A grouping keyed on the division
    alone merges the chambers into one race and loses a seat — exactly the
    collision 2.3 fixed in the flat list, which the new grouping could
    reintroduce. Races are therefore keyed on (chamber, division).
    """
    at_large = "ocd-division/country:us/state:wy"
    seed_district(client, at_large, name="Congressional District (at Large)")
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert(person("person:ZZATLARGE", "us-house", at_large, name="Rep. At Large"))
    people.upsert(person("person:ZZWYSEN1", "us-senate", at_large, name="Sen. Wyo One"))
    people.upsert(person("person:ZZWYSEN2", "us-senate", at_large, name="Sen. Wyo Two"))

    resolver = FixedDistrictResolver(at_large, name="Congressional District (at Large)")
    ballot = service_for(client, resolver=resolver).ballot_for(ADDRESS)

    by_body = races_by_body(ballot)
    assert set(by_body) == {HOUSE_BODY_ID, SENATE_BODY_ID}
    assert [c.name for c in by_body[HOUSE_BODY_ID].candidates] == ["Rep. At Large"]
    assert [c.name for c in by_body[SENATE_BODY_ID].candidates] == [
        "Sen. Wyo One",
        "Sen. Wyo Two",
    ]
    # The two races share a district_id and must still be two races.
    assert by_body[HOUSE_BODY_ID].district_id == by_body[SENATE_BODY_ID].district_id
    assert by_body[HOUSE_BODY_ID].id != by_body[SENATE_BODY_ID].id


def test_a_delegate_district_gets_no_empty_senate_race(client: ZeroDBClient) -> None:
    """DC and the five territories elect no senators, so there is no Senate race.

    An empty Senate race would tell a DC voter they have a Senate contest they
    do not have — a factual error, and the opposite of the empty *House* race a
    district with uningested members legitimately shows.
    """
    dc = "ocd-division/country:us/state:dc"
    seed_district(client, dc, name="Delegate District (at Large)")
    Repository(client=client, model=Person).upsert(
        person("person:ZZDELEGATE", "us-house", dc, name="Del. Synthetic")
    )

    resolver = FixedDistrictResolver(dc, name="Delegate District (at Large)")
    ballot = service_for(client, resolver=resolver).ballot_for(ADDRESS)

    assert [race.body_id for race in ballot.races] == [HOUSE_BODY_ID]


def test_a_house_race_with_no_members_yet_is_listed_empty(client: ZeroDBClient) -> None:
    """A seat whose holder is not ingested is an empty race, not a missing one.

    The House seat exists for every district Informed can resolve — that is what
    the `districts` row means — so dropping the race entirely would tell the
    voter they have no House contest. An empty candidate list says "we don't
    have the candidates yet", which is true.
    """
    seed_district(client, name="Congressional District 19")
    ballot = service_for(client).ballot_for(ADDRESS)

    assert [race.body_id for race in ballot.races] == [HOUSE_BODY_ID]
    assert ballot.races[0].candidates == []


def test_races_come_back_in_a_stable_explainable_order(client: ZeroDBClient) -> None:
    """prd.md: never a ranked order.

    ui-prd.md 2.3 asks for ballot order and forbids sorting by score by default.
    Chamber then name is the stable order this story can honestly produce; what
    matters for neutrality is that it is derived from the data and not from any
    judgement about the candidates.
    """
    seed_district(client, name="Congressional District 19")
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert(person("person:ZZB", "us-senate", SENATE_DISTRICT, name="Sen. Bravo"))
    people.upsert(person("person:ZZA", "us-senate", SENATE_DISTRICT, name="Sen. Alpha"))
    people.upsert(person("person:ZZH", "us-house", HOUSE_DISTRICT, name="Rep. Hotel"))

    ballot = service_for(client).ballot_for(ADDRESS)
    assert [race.body_id for race in ballot.races] == [HOUSE_BODY_ID, SENATE_BODY_ID]
    assert [c.name for c in ballot.races[1].candidates] == ["Sen. Alpha", "Sen. Bravo"]


def test_the_flat_officeholders_list_still_answers(client: ZeroDBClient) -> None:
    """`officeholders` is not removed, because epic 7 reaches the incumbent by it.

    `models.py` promised story 1.5's callers that races would arrive as an
    *addition* to this payload rather than a reshape of it, and the Promise vs.
    Record view (epic 7) starts from an officeholder rather than a candidacy. So
    both views of the same three people are served, and a test pins that they
    agree.
    """
    seed_full_delegation(client)
    ballot = service_for(client).ballot_for(ADDRESS)

    assert {o.person_id for o in ballot.officeholders} == {
        candidate.person_id for race in ballot.races for candidate in race.candidates
    }


def test_the_races_never_carry_the_address(client: ZeroDBClient) -> None:
    """prd.md, privacy first: the new fields are no place to put it either."""
    seed_full_delegation(client)
    ballot = service_for(client).ballot_for("500 Private Lane, Example, CA")
    assert "Private Lane" not in str(ballot.model_dump(mode="json"))


# --- the latency budget --------------------------------------------------


def people_reads(transport: FakeZeroDBTransport) -> int:
    """How many times `people` was queried, counted from the fake's call log.

    Counted as a *delta* by every caller rather than as a total, because seeding
    a fixture upserts rows and an upsert reads before it writes — so the total
    includes the fixture's own reads and would make this assertion measure the
    test setup.
    """
    return len([c for c in transport.calls if c.path.endswith("/tables/people/query")])


def test_one_lookup_reads_people_once(client: ZeroDBClient, transport: FakeZeroDBTransport) -> None:
    """The budget's dominant controllable cost: how many round trips the read takes.

    Measured against the live project at 539 `people` rows: six pages of 100
    cost roughly 1,200 ms on their own, against a whole-request budget of 800,
    while one read of the same rows costs roughly 215 ms. The page size is
    therefore the fix, and this test pins the request count rather than a
    duration so it means the same thing on any machine.
    """
    seed_full_delegation(client)
    before = people_reads(transport)
    service_for(client).ballot_for(ADDRESS)

    reads = people_reads(transport) - before
    assert reads == 1, f"{reads} reads of `people` for one lookup"


def test_the_people_page_is_big_enough_for_the_whole_congress() -> None:
    """One page must hold every sitting member, with headroom.

    535 voting members plus six delegates plus the handful of historical rows
    story 2.2 loads is 539 live today. A page size below that silently
    reintroduces paging and the latency it costs, which is the kind of
    regression a functional test cannot see.
    """
    assert PEOPLE_PAGE_SIZE >= 1000


def test_a_second_lookup_does_not_re_read_people(
    client: ZeroDBClient, transport: FakeZeroDBTransport
) -> None:
    """The snapshot is what brings the live p95 inside 800 ms.

    Measured live: with a warm snapshot the whole lookup is a median 453 ms and
    a p95 of 651 ms, against 978 ms p95 for the same code reading `people`
    every time. The geocode alone is ~500 ms and cannot be cached (its key
    would be the address), so the read it runs alongside has to be nearly free.
    """
    seed_full_delegation(client)
    service = service_for(client)
    service.ballot_for(ADDRESS)
    before = people_reads(transport)
    second = service.ballot_for(ADDRESS)

    assert people_reads(transport) == before, "the second lookup re-read `people`"
    assert [c.name for race in second.races for c in race.candidates] == [
        "Rep. Synthetic",
        "Sen. One",
        "Sen. Two",
    ]


def test_the_snapshot_is_keyed_on_nothing_at_all(client: ZeroDBClient) -> None:
    """prd.md, privacy first: what is cached is the table, never the address.

    `CensusAddressResolver`'s docstring states the rule this implements — the
    p95 budget is met by caching the district-keyed read and never the
    address-keyed one — because a memo on the address would retain it for the
    process's lifetime, which is exactly what story 2.3's "never stored"
    criterion forbids. The snapshot holds `people` rows, which are public
    reference data about legislators, and has no address-shaped key to hold.
    """
    seed_full_delegation(client)
    snapshot = PeopleSnapshot()
    service_for(client, snapshot=snapshot).ballot_for("500 Private Lane, Example, CA")

    assert "Private Lane" not in repr(snapshot.__dict__)
    assert "500" not in str(getattr(snapshot, "_key", ""))


def test_an_expired_snapshot_is_re_read(
    client: ZeroDBClient, transport: FakeZeroDBTransport
) -> None:
    """A snapshot must go stale, or a swearing-in never reaches a voter.

    story 2.2's loader refreshes `people`; without a TTL the API would serve the
    delegation it read at boot until it restarted, so a new member would be
    invisible for as long as the process lived. The clock is injected rather
    than slept on.
    """
    seed_full_delegation(client)
    now = [0.0]
    snapshot = PeopleSnapshot(ttl_seconds=60.0, clock=lambda: now[0])
    service = service_for(client, snapshot=snapshot)

    service.ballot_for(ADDRESS)
    before = people_reads(transport)
    now[0] = 61.0
    service.ballot_for(ADDRESS)

    assert people_reads(transport) == before + 1, "the snapshot never expired"


def test_a_refreshed_snapshot_picks_up_a_newly_sworn_in_member(client: ZeroDBClient) -> None:
    """The point of the TTL, asserted on the answer rather than the call count."""
    seed_district(client, name="Congressional District 19")
    now = [0.0]
    snapshot = PeopleSnapshot(ttl_seconds=60.0, clock=lambda: now[0])
    service = service_for(client, snapshot=snapshot)
    assert service.ballot_for(ADDRESS).races[0].candidates == []

    Repository(client=client, model=Person).upsert(
        person("person:ZZNEW", "us-house", HOUSE_DISTRICT, name="Rep. Newly Sworn")
    )
    now[0] = 61.0
    assert [c.name for c in service.ballot_for(ADDRESS).races[0].candidates] == ["Rep. Newly Sworn"]


def test_the_default_snapshot_ttl_is_short_enough_to_be_honest() -> None:
    """A long TTL is a correctness bug dressed as a performance win.

    Ten minutes bounds how stale a delegation can be, which is well inside the
    cadence at which `people` actually changes (a swearing-in, not a minute),
    and is long enough that a burst of voters shares one read.
    """
    assert 60.0 <= PeopleSnapshot().ttl_seconds <= 900.0


# --- the HTTP contract ---------------------------------------------------


@pytest.fixture
def api(client: ZeroDBClient) -> Any:
    app = create_app()
    app.dependency_overrides[zerodb_client] = lambda: client
    app.dependency_overrides[ballot_service] = lambda: service_for(client)
    with TestClient(app) as test_client:
        yield test_client


def test_the_endpoint_returns_races_with_the_incumbent_flag(
    api: TestClient, client: ZeroDBClient
) -> None:
    seed_full_delegation(client)
    body = api.get("/v1/ballot", params={"address": ADDRESS}).json()

    assert body["data_state"] == "incumbents_only"
    house = next(race for race in body["races"] if race["body_id"] == "us-house")
    assert house["candidates"][0]["is_incumbent"] is True
    assert house["candidates"][0]["person_id"] == "person:ZZ000001"


def test_the_endpoint_documents_the_latency_budget(api: TestClient) -> None:
    """The budget is part of the contract the frontend plans a loading state for.

    ui-prd.md 2.1 designs an address-entry loading state around "target p95
    under 800 ms", so the number belongs in the schema the client reads rather
    than only in the backlog.
    """
    operation = api.get("/openapi.json").json()["paths"]["/v1/ballot"]["get"]
    assert "800" in f"{operation.get('summary', '')} {operation.get('description', '')}"


def test_the_response_schema_names_a_race_and_a_candidate(api: TestClient) -> None:
    """The frontend contract: these component names are what the client types against."""
    schemas = api.get("/openapi.json").json()["components"]["schemas"]
    assert "Race" in schemas
    assert "BallotCandidate" in schemas
    assert "is_incumbent" in schemas["BallotCandidate"]["properties"]


def test_the_router_hands_every_request_the_same_snapshot(
    client: ZeroDBClient, transport: FakeZeroDBTransport
) -> None:
    """The wiring the latency fix depends on, and the half a service test cannot see.

    A `PeopleSnapshot` per request would cache nothing: each request would read
    `people` once and discard the rows, which is the 978 ms p95 this story
    replaces. So `app.ballot.router` holds one for the process and
    `ballot_service` puts it into every service it builds. A test that
    constructs its own service supplies its own snapshot and so can never
    notice if that wiring is wrong.

    Called directly rather than over HTTP because the production factory
    defaults the resolver to the real geocoder, and CI makes no network call
    (CLAUDE.md). What is under test is the snapshot, not the resolver.
    """
    from app.ballot.router import ballot_service as build_service
    from app.ballot.router import people_snapshot

    # The dependency hands out one object, which is what "shared" means.
    shared = people_snapshot()
    assert people_snapshot() is shared
    shared.invalidate()

    seed_full_delegation(client)
    first = build_service(client=client, snapshot=people_snapshot())
    second = build_service(client=client, snapshot=people_snapshot())
    assert first._snapshot is second._snapshot is shared

    # Two services, one read: the cache outlives the request that filled it.
    before = people_reads(transport)
    first._snapshot.rows(lambda: list(Repository(client=client, model=Person).query(limit=100)))
    second._snapshot.rows(lambda: pytest.fail("the second service re-read `people`"))
    assert people_reads(transport) == before + 1

    # Left clean for any later test in this process.
    shared.invalidate()


def test_invalidating_a_snapshot_forces_a_re_read(
    client: ZeroDBClient, transport: FakeZeroDBTransport
) -> None:
    """An ingest job that has just refreshed `people` must be able to say so.

    Story 2.2's loader runs in-process in some deployments, and waiting out a
    ten-minute TTL after a deliberate refresh would serve a delegation the
    process knows is stale.
    """
    seed_full_delegation(client)
    snapshot = PeopleSnapshot()
    service = service_for(client, snapshot=snapshot)
    service.ballot_for(ADDRESS)
    before = people_reads(transport)

    snapshot.invalidate()
    service.ballot_for(ADDRESS)
    assert people_reads(transport) == before + 1


def test_a_chamber_the_resolver_did_not_ask_for_produces_no_race(
    client: ZeroDBClient,
) -> None:
    """The races are exactly the seats the resolver resolved, and no others.

    A state-legislature term in the same division must not appear as a federal
    race. This pins the scope deliberately rather than by omission: a loop
    appending "any other chamber in the delegation" was written in
    `_races_from` and deleted as unreachable, and this test is what says so —
    `_delegation_for` matches exactly the two `(division, chamber)` pairs the
    resolver produces, so a third chamber never reaches the grouping. When
    prd.md's `JurisdictionAdapter` brings one, the resolver changes first and
    this test is what fails to say the grouping must follow.
    """
    seed_district(client, name="Congressional District 19")
    Repository(client=client, model=Person).upsert(
        person("person:ZZSTATE", "us-state-assembly", HOUSE_DISTRICT, name="Asm. Synthetic")
    )

    ballot = service_for(client).ballot_for(ADDRESS)
    assert [race.body_id for race in ballot.races] == [HOUSE_BODY_ID]
    assert ballot.races[0].candidates == []
    assert ballot.officeholders == []
