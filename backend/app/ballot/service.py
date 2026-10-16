"""Building a ballot from an address (stories 1.5, 2.3, 2.4).

The chain one lookup walks:

    address -> CensusAddressResolver -> district id + the geocoder's own name
            -> `districts` row, created on first resolution (story 2.3)
            -> `people` rows with a **current** term in that district
            -> races, each with its candidates and an incumbent flag (2.4)
            -> BallotResponse

Story 1.5 built the chain against a stub resolver; story 2.3 made the resolver
real and fixed the at-large-state collision; story 2.4 adds the two things its
own acceptance criteria name — the race grouping and the latency budget.

"Current" is load-bearing, not a nicety: `people.terms[]` is a list because it
holds term history (story 2.2 ingests every term a member has served), so
matching on any term would hand a voter their former representative alongside
their sitting one. See `_is_current`.

Why the delegation lookup reads the table and matches in Python
===============================================================

  1. **The match is on an embedded list.** datamodel.md rule 3 embeds `terms[]`
     inside `people`, so "who represents this district" is a predicate over an
     array of objects. `zerodb_query_rows`' `filter` is flat equality; there is
     no spelling of it that reaches `terms[].district_id`.
  2. **The server-side filter does not work anyway.** Story 1.4 measured it
     live: a filter that should match returned `total=0` while an unfiltered
     read showed the row plainly. The client's client-side fallback covers flat
     filters; nothing covers a nested one.

That makes this a read of the whole `people` table, which is correct and cheap
today (539 rows live) and will not be correct forever. The honest fix is a read
model — datamodel.md rule 4 already names `candidate_cards`, rebuilt from
events — and the ballot view moves onto it once there is one. Until then the
read is bounded by `MAX_PEOPLE_SCANNED` and overflow is logged loudly rather
than silently truncating a voter's delegation.

The 800 ms budget, and what was actually slow (story 2.4)
=========================================================

Story 2.4's second acceptance criterion is a p95 under 800 ms. Measured against
the live project before any of this was written, the endpoint was **5.5x over**:

    geocoder (one external call)        median  491 ms   p95   600 ms
    `people` read, 6 pages of 100       median  653 ms   p95  1006 ms
    `districts` get                     median  136 ms
    whole endpoint                      median 1276 ms   p95  4454 ms

Three things follow from those numbers, and they are why this module looks the
way it does.

**The geocode is irreducible and cannot be cached.** It is one external HTTP
call to census.gov, it is 60-75% of the whole budget on its own, and the only
cache key that would hit is the address — which prd.md's privacy rule and story
2.3's "never stored" criterion both forbid, and which `CensusAddressResolver`'s
docstring already rules out by name. So everything else has to fit in the ~200-300
ms the geocode leaves.

**Paging `people` 100 rows at a time was most of the rest.** Six sequential
round trips cost ~1200 ms for 539 rows; one request for the same rows costs
~215 ms. Measured live: the project honours `limit` up to at least 10,000,
echoes an accurate `total`, and sets `has_more=False` — so `PEOPLE_PAGE_SIZE`
is 2,000 and the loop below normally runs exactly once. The loop is kept
because a page is still a page and the next Congress is not a promise.

    scan page_size=100   median 1202 ms    page_size=500   median 315 ms
    scan page_size=250   median  536 ms    page_size=1000  median 215 ms

**One read per request was still too slow.** Even with a single-request read,
geocode + read + district get is a median 846 ms and a p95 of 978 ms, because
the three are sequential and each is a network round trip. Two measured
alternatives:

  * *parallelise the geocode and the read.* They are genuinely independent —
    the read does not need the district id. Median improved to 594 ms, but p95
    went to **2162 ms**: the extra concurrent load produced a worse tail than
    the thing it fixed. Rejected on the measurement.
  * *cache the `people` read.* `PeopleSnapshot`, below. Median 456 ms, p95
    **656 ms**, max 699 ms — inside budget with headroom. Chosen.

What is cached is the **table**, never the address (see `PeopleSnapshot`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import date

from app.ballot.models import (
    BallotCandidate,
    BallotResponse,
    DataState,
    DistrictSummary,
    Officeholder,
    Race,
)
from app.districts.resolver import (
    AddressResolver,
    CensusAddressResolver,
    ResolvedAddress,
)
from app.districts.store import DistrictStore
from app.repositories import Repository
from app.repositories.models import Person, Term
from app.zerodb import ZeroDBClient

logger = logging.getLogger(__name__)

#: Rows of `people` read per request. Large on purpose: measured live, 539 rows
#: in one request cost ~215 ms against ~1200 ms for six requests of 100, and the
#: live project honours a `limit` of at least 10,000 while still reporting an
#: accurate `total`. 2,000 leaves room for the whole of a future Congress plus
#: the term history story 2.2 loads, so the paging loop in `_read_people`
#: normally runs once. See the module docstring for the measurements.
PEOPLE_PAGE_SIZE = 2_000

#: Hard cap on the read. 535 voting members of Congress plus delegates and
#: historical members leaves generous headroom; crossing it means `people` has
#: grown past what a full read should serve, and the read model (datamodel.md
#: rule 4) is overdue. Logged as an error because the symptom otherwise is a
#: voter silently missing a representative.
MAX_PEOPLE_SCANNED = 20_000

#: How long a `PeopleSnapshot` serves the rows it read before reading again.
#: Ten minutes: long enough that a burst of voters shares one read (which is
#: what brings the p95 inside budget), short enough that a swearing-in or a
#: story 2.2 refresh reaches voters within the cadence at which `people`
#: actually changes. A longer TTL would be a correctness bug dressed as a
#: performance win — see `PeopleSnapshot`.
PEOPLE_SNAPSHOT_TTL_SECONDS = 600.0

#: datamodel.md: `bodies` ids for the two federal chambers.
HOUSE_BODY_ID = "us-house"
SENATE_BODY_ID = "us-senate"

#: Display order for a delegation and for races. Chamber first, then name — a
#: stable, explainable order, never a ranked one (prd.md: no recommendations).
_BODY_ORDER = {HOUSE_BODY_ID: 0, SENATE_BODY_ID: 1}

#: The office each chamber elects to, for `Race.office_name`. These are the
#: `bodies.name` values story 2.1 seeds; written here rather than read from the
#: table because reading them would cost a second round trip inside an 800 ms
#: budget to display a string that has not changed since 1789. A chamber absent
#: from this map still produces a race, named by its id, rather than no race.
_OFFICE_NAMES = {
    HOUSE_BODY_ID: "U.S. House of Representatives",
    SENATE_BODY_ID: "U.S. Senate",
}


class PeopleSnapshot:
    """A short-lived, process-local cache of the `people` table.

    This is what brings story 2.4's p95 inside 800 ms. The geocode is ~500 ms of
    the budget and cannot be cached; the `people` read it runs alongside has to
    be close to free, and it can be, because `people` is **public reference data
    about legislators** that changes when somebody is sworn in.

    **What is cached, and what is emphatically not.** The key is nothing at all:
    the snapshot holds one list of rows for the whole table. In particular it is
    not keyed on the address, which would retain it for the process's lifetime
    and break prd.md's privacy rule and story 2.3's "never stored" criterion —
    `CensusAddressResolver`'s docstring rules that out by name, and this class is
    the "district-keyed read" it points at instead. There is no parameter here an
    address could arrive in, so the guarantee is structural rather than a matter
    of care.

    **Why it expires.** Without a TTL the API would serve whatever it read at
    boot until the process restarted, so a newly sworn-in member would be
    invisible to every voter in their district for as long as that took — a
    correctness bug that would look like a performance feature. Ten minutes
    bounds the staleness well inside the cadence at which `people` changes.

    **Why a lock.** Under concurrent first requests several threads would
    otherwise each read the table; the lock makes it one read and the others
    wait for it. Held across the read deliberately: a stampede of full-table
    reads is precisely the tail latency this class exists to remove.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = PEOPLE_SNAPSHOT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: list[Person] | None = None
        self._read_at: float = 0.0

    def rows(self, read: Callable[[], list[Person]]) -> list[Person]:
        """The cached rows, reading them through `read` if they are stale.

        `read` is passed in rather than held, so the snapshot knows nothing
        about repositories or ZeroDB and a test can drive it with a list.
        """
        with self._lock:
            now = self._clock()
            if self._rows is None or now - self._read_at >= self.ttl_seconds:
                self._rows = read()
                self._read_at = now
            return self._rows

    def invalidate(self) -> None:
        """Drop the cached rows, so the next call reads again.

        For an ingest job that has just refreshed `people` in this process, and
        for tests.
        """
        with self._lock:
            self._rows = None
            self._read_at = 0.0


class BallotService:
    """Resolves an address to its district, its races and the members in them.

    The resolver is injected so a test drives a captured Census response rather
    than the network, and so prd.md's `JurisdictionAdapter` can supply a
    different one per country later. It defaults to `CensusAddressResolver`
    (story 2.3).

    The snapshot is injected for the same reason and one more: it must be
    **shared across requests** to be worth anything, so the router holds one per
    process and hands it to every service it builds (see `app.ballot.router`). A
    service constructed without one gets a private snapshot, which is correct
    but caches nothing useful — fine for a one-shot script, wrong for the API.
    """

    def __init__(
        self,
        *,
        client: ZeroDBClient,
        resolver: AddressResolver | None = None,
        today: Callable[[], date] = date.today,
        snapshot: PeopleSnapshot | None = None,
    ) -> None:
        self._resolver = resolver or CensusAddressResolver()
        self._districts = DistrictStore(client=client)
        self._people: Repository[Person] = Repository(client=client, model=Person)
        #: Injected so a test can pin "now" without freezing the process clock,
        #: and so a future "what did my delegation look like on date X" read has
        #: a seam to use rather than a second code path.
        self._today = today
        self._snapshot = snapshot if snapshot is not None else PeopleSnapshot()

    def ballot_for(self, address: str) -> BallotResponse:
        """The district, races and sitting delegation for one address.

        The address is used to resolve a district and then dropped: it is not
        stored, not logged, and has nowhere to go in the response (prd.md,
        privacy first; story 2.3's acceptance criteria).

        Raises:
            AddressResolutionError: the address is blank or unusable, the
                geocoder could not find it, it matched more than one district,
                or the geocoder was unreachable. Each subclass carries a
                message the voter can act on.
            DistrictNotFoundError: it resolved to a district Informed could
                neither read nor create a row for.

        No message ever contains the address: all are returned to the voter and
        logged (prd.md, privacy first).
        """
        resolution = self._resolver.resolve_district(address)
        resolved = resolution.districts
        # Created on first resolution rather than pre-seeded — see
        # `app.districts.store` for why, and why the geocoder's own label is
        # the right name for the row.
        district = self._districts.ensure(resolved, name=resolution.name)

        officeholders = self._delegation_for(resolved)
        return BallotResponse(
            district=DistrictSummary(
                id=resolved.district_id,
                name=district.name,
                state=district.state,
            ),
            # Only incumbents are nameable until story 2.6 loads candidacies.
            # Declared rather than inferred: a race with one candidate must not
            # be mistaken for an uncontested one (ui-prd.md 2.1).
            data_state=DataState.INCUMBENTS_ONLY,
            races=_races_from(
                officeholders,
                resolved=resolved,
                district_name=district.name,
                state=district.state,
            ),
            officeholders=officeholders,
        )

    def _delegation_for(self, resolved: ResolvedAddress) -> list[Officeholder]:
        """Every member with a current term in these divisions.

        An empty list is a legitimate answer, not an error: a district whose
        legislators have not been ingested yet has no delegation, and a 500
        would make the endpoint look broken when it is merely empty.
        """
        # A **set of (division, chamber) pairs**, not a dict keyed by division.
        # For an at-large state or a delegate's territory the geocoder has no
        # district number, so `district_id` and `senate_district_id` are the
        # same string — a dict keyed on the division would hold one entry
        # instead of two, keep whichever chamber was written last, and silently
        # drop the other. With the senators written second it is the
        # *representative* who vanishes: a Wyoming voter is shown two senators
        # and told they have no representative, and a DC voter is shown nothing
        # at all. Measured against the live project, that is every voter in WY,
        # VT, DE, AK, ND, SD, DC and the five territories.
        wanted = {
            (resolved.district_id, HOUSE_BODY_ID),
            (resolved.senate_district_id, SENATE_BODY_ID),
        }
        # Read the clock once per request, so a delegation cannot be assembled
        # from two different "today"s if the read straddles midnight.
        today = self._today()
        officeholders = [
            officeholder
            for person in self._all_people()
            for officeholder in _officeholders_from(person, wanted, today=today)
        ]
        officeholders.sort(key=lambda o: (_BODY_ORDER.get(o.body_id, 99), o.name))
        return officeholders

    def _all_people(self) -> list[Person]:
        """Every row of `people`, from the snapshot if it is still fresh.

        See `PeopleSnapshot` for why this is cached and why the cache key is
        nothing, and the module docstring for the measurements that made it
        necessary.
        """
        return self._snapshot.rows(self._read_people)

    def _read_people(self) -> list[Person]:
        """Every row of `people`, read from ZeroDB.

        Normally one request: `PEOPLE_PAGE_SIZE` is larger than the table and
        the live project honours it. The loop is kept because a page is still a
        page — nothing in the API promises that a `limit` will not be capped
        server-side later, and a silently truncated delegation is the failure
        this whole function is careful about.
        """
        people: list[Person] = []
        skip = 0
        while skip < MAX_PEOPLE_SCANNED:
            page = self._people.query(limit=PEOPLE_PAGE_SIZE, skip=skip)
            people.extend(page)
            if len(page) < PEOPLE_PAGE_SIZE:
                return people
            skip += PEOPLE_PAGE_SIZE

        logger.error(
            "stopped reading `people` at %d rows while building a ballot; a "
            "delegation may be incomplete. Build the candidate_cards read model "
            "(datamodel.md rule 4) instead of reading the whole table.",
            MAX_PEOPLE_SCANNED,
        )
        return people


def _races_from(
    officeholders: list[Officeholder],
    *,
    resolved: ResolvedAddress,
    district_name: str,
    state: str,
) -> list[Race]:
    """The races on this ballot, one per seat the voter votes for.

    Two rules decide which races exist, and each is a factual claim rather than
    a convenience:

      * **The House race always exists.** A `districts` row means the district
        exists, so the seat exists, whether or not its holder has been ingested.
        Dropping the race would tell the voter they have no House contest; an
        empty candidate list says "we don't have the candidates yet", which is
        true (and is the case story 1.5's feature file already covers).
      * **The Senate race exists only if the state has senators.** DC, Puerto
        Rico and the four other territories elect a non-voting delegate and no
        senators at all, so an empty Senate race there would claim a contest
        that does not exist. Decided from the data — are there sitting senators
        for this division — rather than from a list of territories to maintain.

    Races are keyed on **(chamber, division)**, which is the same reason
    `_delegation_for` uses pairs: in an at-large state the House and Senate
    divisions are the same string, and keying on the division alone would merge
    the chambers into one race and lose a seat.

    Only the two federal chambers, deliberately. A loop appending "any other
    chamber found in the delegation" was written here and then deleted: it is
    unreachable, because `_delegation_for` matches exactly the two
    `(division, chamber)` pairs the resolver produces, so no third chamber can
    reach this function. Shipping it would have been dead code advertising a
    capability the endpoint does not have — when prd.md's `JurisdictionAdapter`
    brings a third chamber, the resolver and `_delegation_for` are what have to
    learn about it, and this function follows from there rather than pretending
    to be ready.
    """
    by_body: dict[str, list[Officeholder]] = {}
    for officeholder in officeholders:
        by_body.setdefault(officeholder.body_id, []).append(officeholder)

    races = [
        _race(
            body_id=HOUSE_BODY_ID,
            district_id=resolved.district_id,
            # The geocoder's own label for the district — "Congressional
            # District 7", "Delegate District (at Large)" — which is already
            # the honest name for the seat being contested.
            district_name=district_name,
            officeholders=by_body.get(HOUSE_BODY_ID, []),
        )
    ]
    senators = by_body.get(SENATE_BODY_ID, [])
    if senators:
        races.append(
            _race(
                body_id=SENATE_BODY_ID,
                district_id=resolved.senate_district_id,
                # Senators represent the state, so the state is the division's
                # name. The two-letter code rather than a full state name
                # because that is what `districts.state` holds and inventing a
                # lookup table of state names to display here would be a second
                # source of truth for a string.
                district_name=state,
                officeholders=senators,
            )
        )
    return races


def _race(
    *,
    body_id: str,
    district_id: str,
    district_name: str,
    officeholders: list[Officeholder],
) -> Race:
    """One race, with its candidates in the order they were given.

    `election_id` is null and so is every `candidacy_id`: no `elections` or
    `candidacies` row exists yet (story 2.6), and an id for a row that does not
    exist is a fabrication a later story would join against and get nothing
    from. See `app.ballot.models`.
    """
    return Race(
        # The chamber is part of the id, not decoration: an at-large state's
        # two races share a `district_id`.
        id=f"{body_id}:{district_id}",
        body_id=body_id,
        office_name=_OFFICE_NAMES.get(body_id, body_id),
        district_id=district_id,
        district_name=district_name,
        election_id=None,
        candidates=[
            BallotCandidate(
                person_id=officeholder.person_id,
                name=officeholder.name,
                party=officeholder.party,
                # True as a fact about this row, not as a default: every person
                # in a race here holds a current term in the seat. Story 2.6
                # adds candidates for whom this is false.
                is_incumbent=True,
                candidacy_id=None,
            )
            for officeholder in officeholders
        ],
    )


def _officeholders_from(
    person: Person, wanted: set[tuple[str, str]], *, today: date
) -> list[Officeholder]:
    """The entries this person contributes to a delegation, usually zero or one.

    A term matches on two facts, both of which must hold:

      1. its `(district_id, body_id)` is one of the wanted pairs. Matched as a
         pair, not as a division with the chamber checked after, because for an
         at-large seat the House and Senate divisions are the *same string* —
         see `_delegation_for` for what keying on the division alone costs. The
         chamber is still part of the match rather than merely derived from it,
         so a row whose division and chamber disagree is not presented as
         membership of a chamber this person does not sit in;
      2. **it is current** — see `_is_current`.

    `people.terms[]` is a list because it holds term *history*: story 2.2
    ingests every term a member has served, and a long-serving representative
    carries one per Congress for the same seat. Without (2) a district whose
    past representative is in the table returns two House members, which breaks
    this story's own acceptance criterion and, worse, tells a voter that someone
    who left office years ago represents them.

    `seen` is keyed on the chamber, so one person contributes at most one seat
    per chamber however many terms they have served in it — and a member who
    moved from the House to the Senate is listed once, in the chamber whose term
    is current.
    """
    seen: set[str] = set()
    found: list[Officeholder] = []
    for term in person.terms:
        body_id = term.body_id
        if (term.district_id or "", body_id) not in wanted or body_id in seen:
            continue
        if not _is_current(term, today=today):
            continue
        seen.add(body_id)
        found.append(_officeholder(person, term, body_id))
    return found


def _is_current(term: Term, *, today: date) -> bool:
    """Whether this term is in effect today.

    Both bounds are **inclusive**, because a term's `start` is the swearing-in
    day and its `end` the last day in office; excluding them would leave a
    one-day vacancy in a delegation at each boundary.

    An absent or unparseable bound is treated as **not disqualifying**, which is
    the deliberate choice here. `Term.start`/`end` are optional plain strings in
    the model, so an ingest source that publishes no end date (common) or a
    malformed one (possible) would otherwise silently empty a delegation — and a
    voter shown no representative cannot tell that from a genuinely vacant seat,
    whereas a voter shown one with a bad date surfaces a row that can be fixed.
    Erring toward including is the lesser failure; the nightly audit, not this
    read path, is where bad dates should be caught.
    """
    start = _as_date(term.start)
    if start is not None and start > today:
        return False
    end = _as_date(term.end)
    return not (end is not None and end < today)


def _as_date(value: str | None) -> date | None:
    """An ISO-8601 date from a term bound, or None if absent or unparseable.

    A bound published as a full timestamp (`2023-01-03T00:00:00Z`) is accepted
    as well as a bare date, because which of the two an ingest source emits is
    its choice, and reading a timestamp as "unknown" would quietly make every
    term from that source current.
    """
    if not value:
        return None
    text = value.strip().removesuffix("Z")
    try:
        return date.fromisoformat(text[:10] if "T" in text else text)
    except ValueError:
        logger.warning("unparseable term bound %r; treating it as unknown", value)
        return None


def _officeholder(person: Person, term: Term, body_id: str) -> Officeholder:
    """One delegation entry, carrying nothing the voter did not ask for."""
    return Officeholder(
        person_id=person.id or "",
        name=person.canonical_name,
        body_id=body_id,
        party=term.party,
        district_id=term.district_id,
    )


__all__ = [
    "HOUSE_BODY_ID",
    "MAX_PEOPLE_SCANNED",
    "PEOPLE_PAGE_SIZE",
    "PEOPLE_SNAPSHOT_TTL_SECONDS",
    "SENATE_BODY_ID",
    "BallotService",
    "PeopleSnapshot",
]
