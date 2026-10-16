"""Making sure a resolved district has a `districts` row (story 2.3).

Why districts are created on first resolution, not pre-seeded
==============================================================

datamodel.md does not say `districts` must be pre-seeded, and story 2.3's
acceptance criteria say nothing about it either — but the ballot lookup reads a
`districts` row by id after the geocode ("By id after address geocode", the
table's own "Main queries" entry), and that table was **empty** in the live
project before this story, so every ballot lookup 404'd. Something had to put
rows there. Two designs were possible:

**Pre-seed all ~442 districts.** Rejected. There is no free, keyless, canonical
list of current congressional districts *with their names* that this repo could
pull without taking on a second data source and a boundary dataset to keep
current. Worse, the list is not static: it changes with every redistricting
(mid-decade in several states recently), so a seeded list is a thing that goes
quietly stale and starts disagreeing with the geocoder that produced the id.
Deriving the roster from the 442 distinct `people.terms[].district_id` values
story 2.2 loaded was the tempting shortcut, and it is backwards: it would make
Informed's districts a function of who currently holds a seat, so a vacant
district would have no row and its voters would be told Informed has no data
for their address.

**Create the row on first resolution.** Chosen. The geocoder that resolved the
id is the same authority that names the district ("Congressional District 7",
"Delegate District (at Large)"), so the row is written from the one response
that produced it — no second source, nothing to keep in sync, and a
redistricting is picked up the first time somebody in the new district looks
themselves up. The cost is one extra write on a cold district, once.

This is reference data, like story 2.1's seed rows and not like stories
1.3-1.5's `zzz-live-check-*` debris: rows written here are production data and
are never cleaned up.

Idempotency
===========

`Repository.upsert` finds the row by its natural-key id (the OCD-ID) and
replaces it, or inserts when there is none, so resolving the same address twice
— or two voters in one district resolving at once — leaves exactly one row
(datamodel.md rule 1). Not "skips": re-resolving restates the district's name
from the geocoder's current answer, which is what makes a renamed or
redistricted district self-correct rather than keep its first-ever label.

When creation itself fails
==========================

`districts.body_id` references `bodies` (story 2.3 declared it; story 1.4's
`District` docstring deferred the call to here), so a district whose chamber was
never seeded cannot be written. That is the one failure left on this path, and
it is reported as `DistrictNotFoundError` rather than allowed to escape as a
`MissingReferenceError`:

  * It is a **data gap on Informed's side**, not an address the voter can
    retype — which is exactly the distinction `DistrictNotFoundError` was
    created to carry, and why the router answers it 404 rather than 500.
  * It keeps the router's documented 404 reachable. Before this story the 404
    meant "no `districts` row for your district", which lazy creation retires;
    without a replacement the branch would be dead code advertising a status
    the API could never return.
  * A `MissingReferenceError` reaching the handler would be a 500, telling the
    voter Informed is broken when the truth is that its reference data is
    incomplete — and it names the table and ids, which belongs in a log and not
    in a voter-facing body.

Privacy
=======

The row carries the district, the chamber, the state and the dates. It never
carries the address, and `ensure` is given a `ResolvedAddress` plus a name,
never the address string — so there is no parameter the address could arrive in
(prd.md, privacy first; this story's "the address is never stored"). The
not-found message names neither the address nor the internals of the failed
write, because it is returned to the voter as well as logged.
"""

from __future__ import annotations

import logging

from app.districts.resolver import DistrictNotFoundError, ResolvedAddress
from app.repositories import Repository
from app.repositories.errors import RepositoryError
from app.repositories.models import District
from app.zerodb import ZeroDBClient

logger = logging.getLogger(__name__)

#: datamodel.md: the `bodies` id for the chamber a congressional district
#: elects to. Story 2.1 seeded this row; a district written here points at it.
HOUSE_BODY_ID = "us-house"

#: `districts.valid_from` is required by the model and is a redistricting
#: boundary, not an ingest timestamp. The 119th Congress convened on this date
#: and is the first Congress whose boundaries Informed resolves against, so it
#: is the honest "valid from" for a row derived from today's geocoder vintage.
#: `valid_to` stays null: these boundaries are current, and datamodel.md's
#: append-only rule does not apply to `districts` (it is not a verdict, a
#: position or the provenance log).
DEFAULT_VALID_FROM = "2025-01-03"


class DistrictStore:
    """Reads a `districts` row, writing it first if it does not exist yet.

    Separate from `CensusAddressResolver` because they answer different
    questions and fail differently: the resolver's job is "which district is
    this address in", which can fail in ways the voter can fix, and this one's
    is "does Informed have a row for that district", which cannot. Keeping them
    apart is also what lets the resolver be tested with no ZeroDB at all.
    """

    def __init__(self, *, client: ZeroDBClient) -> None:
        self._districts: Repository[District] = Repository(client=client, model=District)

    def get(self, district_id: str) -> District | None:
        """The stored district, or None."""
        return self._districts.get(district_id)

    def ensure(self, resolved: ResolvedAddress, *, name: str) -> District:
        """The `districts` row for a resolved district, created if absent.

        Takes the resolved districts and the geocoder's own label for them —
        never the address, which has no parameter here to arrive in.

        Returns the stored row. The write is an idempotent upsert on the
        OCD-ID, so two voters in one cold district do not produce two rows.

        Raises:
            DistrictNotFoundError: the row does not exist and cannot be
                written — in practice, because the chamber it references was
                never seeded. See the module docstring for why that is a 404
                and not a 500.
        """
        existing = self._districts.get(resolved.district_id)
        if existing is not None:
            return existing

        # Logged with the district id and nothing else: the id is not personal
        # data (a district holds ~760,000 people), while the address is.
        logger.info("creating districts row for %s", resolved.district_id)
        try:
            return self._districts.upsert(
                {
                    "id": resolved.district_id,
                    "body_id": HOUSE_BODY_ID,
                    "name": name,
                    "state": resolved.state,
                    "valid_from": DEFAULT_VALID_FROM,
                }
            )
        except RepositoryError as exc:
            # The cause is logged in full — it names the table, field and
            # missing id, which is what an operator needs to fix the seed — and
            # deliberately not repeated to the voter, who can do nothing with
            # it. Neither the log line nor the message carries the address.
            logger.error("cannot record resolved district %s: %s", resolved.district_id, exc)
            raise DistrictNotFoundError(
                "We found your district but don't have data for it yet. Please try again later."
            ) from exc


__all__ = ["DEFAULT_VALID_FROM", "HOUSE_BODY_ID", "DistrictStore"]
