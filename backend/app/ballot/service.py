"""Building a ballot from an address (stories 1.5, 2.3).

The walking skeleton's one piece of business logic, and the whole of what the
`/v1/ballot` handler does beyond turning exceptions into status codes:

    address -> CensusAddressResolver -> district id + the geocoder's own name
            -> `districts` row, created on first resolution (story 2.3)
            -> `people` rows with a **current** term in that district
            -> BallotResponse

Story 2.3 changed two things in that chain and nothing else. The resolver is
real (story 1.5's stub is gone), and the `districts` read became a
read-or-create: the table was empty in the live project, so against real
geocoded ids every lookup would otherwise 404. See `app.districts.store` for
why the row is created from the geocoder's answer rather than pre-seeded from a
roster.

"Current" is load-bearing, not a nicety: `people.terms[]` is a list because it
holds term history (story 2.2 ingests every term a member has served), so
matching on any term would hand a voter their former representative alongside
their sitting one. See `_is_current`.

Why the delegation lookup pages the table and matches in Python rather than
asking ZeroDB to filter:

  1. **The match is on an embedded list.** datamodel.md rule 3 embeds `terms[]`
     inside `people`, so "who represents this district" is a predicate over an
     array of objects. `zerodb_query_rows`' `filter` is flat equality; there is
     no spelling of it that reaches `terms[].district_id`.
  2. **The server-side filter does not work anyway.** Story 1.4 measured it
     live: a filter that should match returned `total=0` while an unfiltered
     read showed the row plainly. The client's client-side fallback covers flat
     filters; nothing covers a nested one.

That makes this a full table scan of `people`, which is correct and cheap today
(`people` holds 535 federal legislators once story 2.2 has run, and is empty
right now) and will not be correct forever. The honest fix is a read model —
datamodel.md rule 4 already names `candidate_cards`, rebuilt from events — and
epic 2 builds the ballot view on it. Until then the scan is bounded by
`MAX_PEOPLE_SCANNED` and overflow is logged loudly rather than silently
truncating a voter's delegation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from app.ballot.models import BallotResponse, DistrictSummary, Officeholder
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

#: Rows of `people` read per page when scanning for a delegation.
PEOPLE_PAGE_SIZE = 100

#: Hard cap on the scan. 535 voting members of Congress plus delegates and
#: historical members leaves generous headroom; crossing it means `people` has
#: grown past what a scan should serve, and the read model (datamodel.md rule 4)
#: is overdue. Logged as an error because the symptom otherwise is a voter
#: silently missing a representative.
MAX_PEOPLE_SCANNED = 5_000

#: datamodel.md: `bodies` ids for the two federal chambers.
HOUSE_BODY_ID = "us-house"
SENATE_BODY_ID = "us-senate"

#: Display order for a delegation. Chamber first, then name — a stable,
#: explainable order, never a ranked one (prd.md: no recommendations).
_BODY_ORDER = {HOUSE_BODY_ID: 0, SENATE_BODY_ID: 1}


class BallotService:
    """Resolves an address to its district and the members who represent it.

    The resolver is injected so a test drives a captured Census response rather
    than the network, and so prd.md's `JurisdictionAdapter` can supply a
    different one per country later. It defaults to `CensusAddressResolver`
    (story 2.3), which replaced story 1.5's stub behind the same protocol.
    """

    def __init__(
        self,
        *,
        client: ZeroDBClient,
        resolver: AddressResolver | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._resolver = resolver or CensusAddressResolver()
        self._districts = DistrictStore(client=client)
        self._people: Repository[Person] = Repository(client=client, model=Person)
        #: Injected so a test can pin "now" without freezing the process clock,
        #: and so a future "what did my delegation look like on date X" read has
        #: a seam to use rather than a second code path.
        self._today = today

    def ballot_for(self, address: str) -> BallotResponse:
        """The district and sitting delegation for one address.

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

        return BallotResponse(
            district=DistrictSummary(
                id=resolved.district_id,
                name=district.name,
                state=district.state,
            ),
            officeholders=self._delegation_for(resolved),
        )

    def _delegation_for(self, resolved: ResolvedAddress) -> list[Officeholder]:
        """Every member with a current term in these divisions.

        An empty list is a legitimate answer, not an error: a district whose
        legislators have not been ingested yet (which is every district until
        story 2.2 runs) has no delegation, and a 500 would make the skeleton
        look broken when it is merely empty.
        """
        wanted = {
            resolved.district_id: HOUSE_BODY_ID,
            resolved.senate_district_id: SENATE_BODY_ID,
        }
        # Read the clock once per request, so a delegation cannot be assembled
        # from two different "today"s if the scan straddles midnight.
        today = self._today()
        officeholders = [
            officeholder
            for person in self._scan_people()
            for officeholder in _officeholders_from(person, wanted, today=today)
        ]
        officeholders.sort(key=lambda o: (_BODY_ORDER.get(o.body_id, 99), o.name))
        return officeholders

    def _scan_people(self) -> list[Person]:
        """Every row of `people`, read a page at a time.

        See the module docstring for why this is a scan. The cap is a guard, not
        a budget: hitting it means the scan has outlived its usefulness.
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
            "stopped scanning `people` at %d rows while building a ballot; a "
            "delegation may be incomplete. Build the candidate_cards read model "
            "(datamodel.md rule 4) instead of scanning.",
            MAX_PEOPLE_SCANNED,
        )
        return people


def _officeholders_from(
    person: Person, wanted: dict[str, str], *, today: date
) -> list[Officeholder]:
    """The entries this person contributes to a delegation, usually zero or one.

    A term matches on three independent facts, all of which must hold:

      1. its `district_id` is one of the wanted divisions;
      2. its `body_id` is the chamber that division belongs to — checked
         separately because a senator's term carries the state division and a
         representative's carries the `/cd:` one, and a row where the two
         disagree is bad data that must not be presented as membership of a
         chamber this person does not sit in;
      3. **it is current** — see `_is_current`.

    `people.terms[]` is a list because it holds term *history*: story 2.2
    ingests every term a member has served, and a long-serving representative
    carries one per Congress for the same seat. Without (3) a district whose
    past representative is in the table returns two House members, which breaks
    this story's own acceptance criterion and, worse, tells a voter that someone
    who left office years ago represents them.
    """
    seen: set[str] = set()
    found: list[Officeholder] = []
    for term in person.terms:
        body_id = wanted.get(term.district_id or "")
        if body_id is None or term.body_id != body_id or body_id in seen:
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
    "SENATE_BODY_ID",
    "BallotService",
]
