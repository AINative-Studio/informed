"""Building a ballot from an address (story 1.5).

The walking skeleton's one piece of business logic, and the whole of what the
`/v1/ballot` handler does beyond turning exceptions into status codes:

    address -> StubAddressResolver -> district id
            -> `districts` row (story 1.4 repository)
            -> `people` rows whose `terms[]` put them in that district
            -> BallotResponse

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

from app.ballot.models import BallotResponse, DistrictSummary, Officeholder
from app.districts.resolver import (
    AddressResolver,
    DistrictNotFoundError,
    ResolvedAddress,
    StubAddressResolver,
)
from app.repositories import Repository
from app.repositories.models import District, Person, Term
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

    The resolver is injected so story 2.3 swaps the Census Geocoder in without
    touching this class, and so a test can point the stub at a second district.
    """

    def __init__(
        self,
        *,
        client: ZeroDBClient,
        resolver: AddressResolver | None = None,
    ) -> None:
        self._resolver = resolver or StubAddressResolver()
        self._districts: Repository[District] = Repository(client=client, model=District)
        self._people: Repository[Person] = Repository(client=client, model=Person)

    def ballot_for(self, address: str) -> BallotResponse:
        """The district and sitting delegation for one address.

        The address is used to resolve a district and then dropped: it is not
        stored, not logged, and has nowhere to go in the response (prd.md,
        privacy first; story 2.3's acceptance criteria).

        Raises:
            AddressResolutionError: the address is blank or unusable.
            DistrictNotFoundError: it resolved to a district that is not in
                ZeroDB. A subclass of the above, so a caller that does not care
                about the distinction catches one exception and gets both.

        Neither message ever contains the address: both are returned to the
        voter and logged (prd.md, privacy first).
        """
        resolved = self._resolver.resolve(address)
        district = self._districts.get(resolved.district_id)
        if district is None:
            # A data gap, not a voter error: the geocoder (or stub) named a
            # district nobody has ingested. Naming the id and not the address
            # keeps the log line usable and the voter's address out of it.
            logger.warning("no districts row for %s", resolved.district_id)
            raise DistrictNotFoundError("We don't have district data for that address yet.")

        return BallotResponse(
            district=DistrictSummary(
                id=resolved.district_id,
                name=district.name,
                state=district.state,
            ),
            officeholders=self._delegation_for(resolved),
        )

    def _delegation_for(self, resolved: ResolvedAddress) -> list[Officeholder]:
        """Every sitting member whose terms put them in these divisions.

        An empty list is a legitimate answer, not an error: a district whose
        legislators have not been ingested yet (which is every district until
        story 2.2 runs) has no delegation, and a 500 would make the skeleton
        look broken when it is merely empty.
        """
        wanted = {
            resolved.district_id: HOUSE_BODY_ID,
            resolved.senate_district_id: SENATE_BODY_ID,
        }
        officeholders = [
            officeholder
            for person in self._scan_people()
            for officeholder in _officeholders_from(person, wanted)
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


def _officeholders_from(person: Person, wanted: dict[str, str]) -> list[Officeholder]:
    """The entries this person contributes to a delegation, usually zero or one.

    A person is matched by a term whose `district_id` is one of the wanted
    divisions **and** whose `body_id` is the chamber that division belongs to.
    Both halves are checked because the two are independent facts in the row:
    a senator's term carries the state division, a representative's carries the
    `/cd:` one, and a row where they disagree is bad data that must not be
    presented as a representative of a chamber they do not sit in.
    """
    seen: set[str] = set()
    found: list[Officeholder] = []
    for term in person.terms:
        body_id = wanted.get(term.district_id or "")
        if body_id is None or term.body_id != body_id or body_id in seen:
            continue
        seen.add(body_id)
        found.append(_officeholder(person, term, body_id))
    return found


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
