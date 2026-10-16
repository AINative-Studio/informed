"""Reading and writing `districts` rows, in space and in time (2.3, 2.7).

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

===============================================================================
REDISTRICTING: ONE DISTRICT, SEVERAL MAPS (STORY 2.7)
===============================================================================

A congressional district is a geography with a *history*. `WA-7` in 2016 and
`WA-7` in 2026 are the same name over different ground, so "who did this
district elect" and "who is on this district's ballot" have different answers
depending on which map was in force. datamodel.md anticipated this by giving
`districts` a `valid_from` and a `valid_to`, and story 2.3 was already writing
both. Story 2.7 is what makes them mean something to a reader: a lookup can
name a date and get the district that was valid on it.

What this story does NOT do, and why
=====================================

It does **not** load historical boundaries. Informed has exactly one source of
district geography, the Census Geocoder, and it answers for exactly one vintage
(`Current_Current` — see `app.districts.resolver`). There is no free, keyless
source of historical congressional boundaries this repo could pull, and
back-dating rows from nothing would be invented evidence about which map a 2016
election was held under — precisely what prd.md's evidence-over-opinion rule
forbids. A real historical map needs a dedicated geographic dataset and a story
of its own.

So **today every district has exactly one version**: `valid_from` is the day
the 119th Congress convened, `valid_to` is null, and a lookup for any date from
that day on returns it. The work here is that the *storage and the query are
correct when a second version arrives* — which is what makes the next
redistricting an ingest change rather than a schema migration under live
traffic. The multi-version behaviour is tested against a synthetic
two-version district, and the tests say so.

Where a version lives: the current one keeps the plain OCD-ID
=============================================================

datamodel.md keys `districts` by the OCD-ID and documents the table's query as
"by id after address geocode". Two versions of one district cannot both hold
that id, so something had to give. Three designs were considered:

**Re-key the table on `{ocd_id}@{valid_from}`.** Rejected. It contradicts
datamodel.md's documented id pattern, and it breaks every existing reader at
once: the seven live rows, story 2.4's ballot lookup, `DistrictCache`, story
2.6's candidacy loader. All of them would have to learn a date to ask a
question none of them is asking ("who represents me *today*"), and the live
rows would need rewriting under traffic.

**Store versions in an embedded list on one row.** Rejected. datamodel.md rule
3 embeds "what is always read together", and versions are the opposite: the
current map is read on every ballot lookup and a superseded one is read
essentially never. Embedding would make the hot read carry the cold history,
and would put a growing array behind the one id every reader already holds.

**The current version keeps the plain OCD-ID; a superseded one gets a
date-suffixed id.** Chosen, and it is `version_id_for` below. The invariant is:

    valid_to is None  ->  id is the bare OCD-ID        (the current map)
    valid_to is set   ->  id is {ocd_id}@{valid_to}    (a superseded map)

so the row every existing reader holds is always the one in force, no reader
changes, the live rows are already correct under it, and history accumulates
beside them under ids nothing else looks up. The suffix is the day the map
*stopped* applying rather than the day it started, because that is the field
that distinguishes a superseded version from the current one, which keeps the
id derivable from the row's own fields alone (datamodel.md rule 1: a
deterministic id from a natural key — re-ingesting one superseded version
upserts it rather than minting a second row).

`@` as the separator, not `:` or `/`: both of those already carry meaning
inside an OCD-ID, and a suffix built from them would be ambiguous to split.
`@` appears nowhere in an OCD-ID or in a date.

The lookup, and why the dateless path is untouched
===================================================

`get(ocd_id)` is still exactly what story 2.3 wrote: one `get_row` by id. That
matters for story 2.4's 800 ms budget — the geocode already takes ~500 ms of
it, and the `districts` read was itself measured at 160-290 ms in two round
trips before `DistrictCache` removed it. Making every "who represents me"
lookup scan a district's version history would spend that budget answering a
question nobody asked, so the fast path stays a single keyed read and a test
pins the round-trip count.

`get(ocd_id, as_of=some_date)` is the new capability. It reads the district's
versions and returns the one whose inclusive `[valid_from, valid_to]` interval
contains the date, or None. None rather than the nearest version, deliberately:
if no map was in force on a date, the honest answer is that Informed does not
know what the district looked like then — handing back a neighbouring era's map
would be a confident wrong answer, which for this product is worse than no
answer (`app.districts.resolver` makes the same call about an ambiguous
address).

Why the scan is a filtered read and not a prefix scan of the table
===================================================================

Versions are found by reading the table and matching on the row's own `id`
against the OCD-ID and its `@` suffix. Two details are load-bearing:

  * **the separator is part of the match.** `.../cd:1` is a plain-string prefix
    of `.../cd:19`, so matching by `startswith(ocd_id)` alone would hand
    district 1's voters district 19's history. A version id must be the OCD-ID
    exactly, or the OCD-ID followed by `@`.
  * **the server-side filter cannot express it.** ZeroDB's filter is flat
    equality (and story 1.4 measured it returning nothing even for an exact
    match), so there is no filter spelling for "ids starting with this". The
    scan is therefore a paged read, bounded by `MAX_DISTRICTS_SCANNED` — which
    is affordable precisely because it is NOT on the hot path: only a
    date-qualified lookup pays it, and `districts` is a table of ~450 rows plus
    however many superseded versions accumulate (today: zero).

Recording a redistricting
=========================

`redistrict(ocd_id, name=..., valid_from=...)` is the write a future ingest
calls when a new map takes effect. It closes the current version on the day
before the new one begins and writes the new one under the plain OCD-ID, which
means:

  * **no gap and no overlap.** Every date resolves to exactly one version
    across the boundary, which is the whole point of the two fields.
  * **history is never overwritten.** The superseded version moves to its own
    date-suffixed id and keeps its own name and dates. This is the spirit of
    datamodel.md rule 6 (append-only where history matters) applied to a table
    the rule does not formally name: `districts` is not a verdict, a position
    or the provenance log, so it has no `is_current` flag — the null `valid_to`
    *is* the flag, and it is how `verdicts` would read if its version key were
    a date rather than a number.

Overlapping ranges are refused rather than stored (`OverlappingValidityError`).
An overlap makes "which district was valid on election day" a question with two
answers, and a reader would then have to pick one arbitrarily — the kind of
silent, order-dependent wrongness datamodel.md rule 5 puts in code to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from app.districts.resolver import DistrictNotFoundError, ResolvedAddress, state_code_from
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

#: Separates a superseded version's OCD-ID from the day it stopped applying.
#: `@` because both `:` and `/` already carry meaning inside an OCD-ID, so a
#: suffix built from either would be ambiguous to split; `@` appears in neither
#: an OCD-ID nor an ISO date. See the module docstring.
VERSION_SEPARATOR = "@"

#: Rows of `districts` read per page when scanning a district's versions. The
#: whole table is ~450 current districts plus whatever superseded versions have
#: accumulated (today: zero), so one page normally covers it.
DISTRICTS_PAGE_SIZE = 1_000

#: Hard cap on a version scan. Crossing it means `districts` has grown past
#: what a full read should serve and the scan needs an index or a read model
#: (datamodel.md rule 4); logged as an error because the symptom otherwise is a
#: silently missing version.
MAX_DISTRICTS_SCANNED = 20_000


class OverlappingValidityError(RepositoryError):
    """Two versions of one district would both claim the same date.

    Refused rather than stored, because an overlap makes "which district was
    valid on election day" a question with two answers and leaves the reader to
    pick one arbitrarily — an order-dependent wrongness that would never
    surface as an error. datamodel.md rule 5: integrity lives in code.

    A `RepositoryError`, so a caller that already handles write failures for
    this table (story 2.3's `ensure`, story 2.6's candidacy loader) catches it
    without being changed.
    """

    def __init__(self, message: str, *, table: str = "districts") -> None:
        super().__init__(message)
        self.message = message
        self.table = table


@dataclass(frozen=True)
class Redistricting:
    """What recording a redistricting changed.

    Attributes:
        current: the new version, open-ended and holding the plain OCD-ID.
        superseded: the version it replaced, now closed and holding a
            date-suffixed id — or None when there was nothing to replace,
            which is the first version of a district rather than an error.
    """

    current: District
    superseded: District | None


def version_id_for(district_id: str, *, valid_to: str | None) -> str:
    """The `districts` row id for one version of a district.

        (".../cd:7", valid_to=None)         -> ".../cd:7"
        (".../cd:7", valid_to="2022-12-31") -> ".../cd:7@2022-12-31"

    The current version keeps the bare OCD-ID, which is what datamodel.md
    documents and what every existing reader already holds; a superseded one is
    suffixed with the day it stopped applying. Derived from the row's own
    fields, so re-ingesting one version upserts it rather than minting a second
    row (datamodel.md rule 1). See the module docstring for why the suffix is
    `valid_to` and not `valid_from`.
    """
    if valid_to is None:
        return district_id
    return f"{district_id}{VERSION_SEPARATOR}{valid_to}"


def base_id_of(row_id: str) -> str:
    """The OCD-ID a version id belongs to.

    `".../cd:7@2022-12-31"` -> `".../cd:7"`; a bare OCD-ID is returned
    unchanged. The inverse of `version_id_for`, so the two cannot drift.
    """
    return row_id.split(VERSION_SEPARATOR, 1)[0]


def contains(district: District, as_of: date) -> bool:
    """Whether this version of a district was in force on `as_of`.

    **Both bounds inclusive**: `valid_from` is the first day the map applies
    and `valid_to` the last. Excluding either would leave the boundary day
    belonging to no district at all, so an election held on it would map to
    nothing.

    A bound Informed cannot parse makes the version match **no** date. The
    opposite choice — treating an unreadable bound as open — would let one bad
    row answer for every date and shadow the good rows for the same district,
    turning a fixable data error into a wrong answer served confidently. A
    version Informed cannot place in time is a version it cannot vouch for.

    That branch is **not** reachable through the repository, and the test suite
    says so: `District`'s validator refuses such a row on write, and
    `Repository._parse_row` refuses to *read* one — loudly, by its own
    documented choice, because a silently skipped row would make a page look
    complete. So this guard is for a caller holding a `District` built some
    other way (`model_construct`, or a future reader that does not go through
    the repository), and the behaviour is stated here rather than left to
    whichever branch happened to be written first.
    """
    start = _as_date(district.valid_from)
    if start is None or as_of < start:
        return False
    if district.valid_to is None:
        return True
    end = _as_date(district.valid_to)
    return end is not None and as_of <= end


class DistrictStore:
    """Reads a `districts` row, writing it first if it does not exist yet.

    Separate from `CensusAddressResolver` because they answer different
    questions and fail differently: the resolver's job is "which district is
    this address in", which can fail in ways the voter can fix, and this one's
    is "does Informed have a row for that district", which cannot. Keeping them
    apart is also what lets the resolver be tested with no ZeroDB at all.

    As of story 2.7 it also answers the question in time — `get(..., as_of=d)`,
    `versions`, `record_version` and `redistrict`. The dateless `get` is
    unchanged and deliberately so: it is the hot path story 2.4 budgeted for.
    """

    def __init__(
        self,
        *,
        client: ZeroDBClient,
        page_size: int = DISTRICTS_PAGE_SIZE,
        max_rows_scanned: int = MAX_DISTRICTS_SCANNED,
    ) -> None:
        self._districts: Repository[District] = Repository(client=client, model=District)
        #: Injected so the scan's paging and its cap can be driven by a test
        #: without writing 20,000 rows. Production constructs this with no
        #: arguments and gets the module constants.
        self._page_size = page_size
        self._max_rows_scanned = max_rows_scanned

    def get(self, district_id: str, *, as_of: date | None = None) -> District | None:
        """The district valid on `as_of`, or the current one when no date is given.

        With no `as_of` this is one keyed read of the bare OCD-ID, exactly as
        story 2.3 wrote it — the current version holds that id, so the hot
        "who represents me today" path costs a single round trip and does not
        scan version history (see the module docstring on story 2.4's budget).

        With an `as_of` it returns the version whose inclusive
        `[valid_from, valid_to]` interval contains that date, or **None** if no
        version does. None rather than the nearest version: if Informed holds
        no map for a date, the honest answer is that it does not know what the
        district looked like then, and a neighbouring era's map would be a
        confident wrong answer.

        Args:
            district_id: the district's OCD-ID — the bare one, never a
                version-suffixed id. A caller asks for a district and a date,
                not for a version.
            as_of: the date the answer should be true for. An election date,
                for a ballot; a roll call's date, for a vote.
        """
        if as_of is None:
            return self._districts.get(district_id)
        for version in self.versions(district_id):
            if contains(version, as_of):
                return version
        return None

    def versions(self, district_id: str) -> list[District]:
        """Every stored version of one district, oldest `valid_from` first.

        Found by scanning rather than filtering: the match is "this row's id is
        the OCD-ID, or the OCD-ID followed by the version separator", which
        ZeroDB's flat-equality filter cannot express (and which story 1.4
        measured returning nothing even for an exact match). Affordable because
        it is off the hot path — only a date-qualified lookup gets here.

        The match is `base_id_of(row.id) == district_id` rather than a
        `startswith`, and that is load-bearing rather than tidy: `.../cd:1` is
        a plain-string prefix of `.../cd:19`, so a prefix match would mix two
        districts' histories together and hand one district's voters another's
        map. Going through `base_id_of` also makes the match the exact inverse
        of `version_id_for`, so the two cannot drift apart.
        """
        found = [row for row in self._all_districts() if base_id_of(row.id or "") == district_id]
        # Sorted on the parsed date, not the string, so a bound stored as a
        # timestamp orders with the bare dates rather than after all of them.
        # An unparseable `valid_from` sorts first; `contains` already refuses
        # to match it for any date, so its position cannot change an answer.
        found.sort(key=lambda row: _as_date(row.valid_from) or date.min)
        return found

    def record_version(
        self,
        district_id: str,
        *,
        body_id: str,
        name: str,
        state: str,
        valid_from: str,
        valid_to: str | None = None,
    ) -> District:
        """Write one version of a district, refusing an overlap with another.

        The id is derived from `valid_to` (`version_id_for`), so the current
        version holds the bare OCD-ID and a superseded one is suffixed — which
        means re-stating the same version is an idempotent upsert and not a
        second row (datamodel.md rule 1).

        Raises:
            OverlappingValidityError: another version already claims a date
                this one would also claim. Refused rather than stored: an
                overlap makes "which district was valid on this date" a
                question with two answers.
            RepositoryError: the row could not be written — in practice because
                `body_id` names a chamber that was never seeded.
        """
        row_id = version_id_for(district_id, valid_to=valid_to)
        self._refuse_overlap(district_id, row_id=row_id, valid_from=valid_from, valid_to=valid_to)
        return self._districts.upsert(
            {
                "id": row_id,
                "body_id": body_id,
                "name": name,
                "state": state,
                "valid_from": valid_from,
                "valid_to": valid_to,
            }
        )

    def redistrict(self, district_id: str, *, name: str, valid_from: str) -> Redistricting:
        """Record that a new map takes effect for this district on `valid_from`.

        The write a future boundary ingest calls. It closes the current version
        on the **day before** the new one begins and writes the new one under
        the plain OCD-ID, so across the boundary every date resolves to exactly
        one version — no gap, no overlap. The superseded version keeps its own
        row, its own name and its own dates under a date-suffixed id, so
        history is never overwritten.

        Idempotent: running the same redistricting twice leaves two versions,
        because the second run finds the already-closed version at the id it
        would write and the already-current one at the OCD-ID.

        The new version inherits `body_id` and `state` from the version it
        replaces rather than taking them as arguments: a redistricting changes
        a district's boundaries, never which chamber it elects to or which
        state it is in, and a caller that could pass a different chamber could
        silently move a district between them.

        Args:
            district_id: the district's OCD-ID.
            name: the new map's name for the district. Supplied rather than
                inherited because this is exactly what a redistricting can
                change ("Congressional District 7" is a label on a geography).
            valid_from: the first day the new map applies.

        Raises:
            OverlappingValidityError: the new map would begin on or before the
                day the current one did, so the current version cannot be
                closed without describing an empty or backwards interval.
            RepositoryError: a row could not be written.
        """
        effective = _as_date(valid_from)
        if effective is None:
            raise OverlappingValidityError(
                f"redistricting date {valid_from!r} for {district_id} is not an ISO-8601 date",
                table=District.table_name,
            )

        current = self._districts.get(district_id)
        if current is None:
            # Nothing to supersede is the first version of a district, not a
            # failure: a redistricting ingest should be able to write a map for
            # a district nobody has looked up yet.
            logger.info("recording the first stored map for %s from %s", district_id, valid_from)
            return Redistricting(
                current=self.record_version(
                    district_id,
                    body_id=HOUSE_BODY_ID,
                    name=name,
                    state=_state_of(district_id),
                    valid_from=valid_from,
                ),
                superseded=None,
            )

        if current.valid_from == valid_from and current.name == name:
            # This exact redistricting has already been recorded: the new map
            # is already the current one. Returning it is what makes a
            # re-running ingest idempotent (datamodel.md rule 1) — without this
            # the second run would see the new map as the thing to supersede
            # and refuse the write as an overlap with itself. The superseded
            # version is re-read rather than assumed, so the answer describes
            # what is stored and not what this call did.
            logger.info("redistricting %s from %s was already recorded", district_id, valid_from)
            return Redistricting(
                current=current,
                superseded=self._previous_version_before(district_id, effective),
            )

        current_start = _as_date(current.valid_from)
        if current_start is not None and effective <= current_start:
            raise OverlappingValidityError(
                f"a new map for {district_id} cannot begin on {valid_from} because the "
                f"current one began on {current.valid_from}",
                table=District.table_name,
            )

        closed_on = (effective - timedelta(days=1)).isoformat()
        # The superseded version is written FIRST, then the new current one
        # replaces it at the plain OCD-ID. Of the two torn states a crash
        # between the writes can leave, this one is recoverable — the old map
        # is stored twice, under its own id and still at the OCD-ID, and
        # re-running the redistricting fixes it. Writing the new map first
        # would instead lose the old one entirely, which is history this table
        # exists to keep. The same ordering reasoning as
        # `Repository.append_version`; see that method on why neither write is
        # atomic.
        superseded = self._districts.upsert(
            {
                "id": version_id_for(district_id, valid_to=closed_on),
                "body_id": current.body_id,
                "name": current.name,
                "state": current.state,
                "valid_from": current.valid_from,
                "valid_to": closed_on,
            }
        )
        logger.info(
            "redistricting %s: closed the previous map on %s, new map from %s",
            district_id,
            closed_on,
            valid_from,
        )
        new_current = self._districts.upsert(
            {
                "id": district_id,
                "body_id": current.body_id,
                "name": name,
                "state": current.state,
                "valid_from": valid_from,
                "valid_to": None,
            }
        )
        return Redistricting(current=new_current, superseded=superseded)

    def _previous_version_before(self, district_id: str, effective: date) -> District | None:
        """The stored version that ended the day before `effective`, if any.

        Used only to describe an already-recorded redistricting. Matched on the
        closing date rather than "the newest closed version", so the answer is
        the version this particular redistricting superseded and not whichever
        happens to be latest.
        """
        closed_on = (effective - timedelta(days=1)).isoformat()
        for version in self.versions(district_id):
            if version.valid_to == closed_on:
                return version
        return None

    def _refuse_overlap(
        self, district_id: str, *, row_id: str, valid_from: str, valid_to: str | None
    ) -> None:
        """Raise if another version of this district claims a date this one would.

        The version being written is excluded by its own id, so re-stating one
        version stays an upsert rather than overlapping itself.
        """
        start = _as_date(valid_from)
        if start is None:
            # The model's own validator will reject this with a better message
            # than an overlap check can; let the write produce it.
            return
        end = _as_date(valid_to) if valid_to is not None else None
        for other in self.versions(district_id):
            if other.id == row_id:
                continue
            # Every stored version's bounds parse: `District`'s validator
            # refuses an unreadable one on write and `Repository._parse_row`
            # refuses to read one back, so a branch for "this neighbour has no
            # date" would be dead code advertising a tolerance this layer does
            # not have. `date.min` is unreachable in practice and keeps the
            # comparison total rather than optional.
            other_start = _as_date(other.valid_from) or date.min
            other_end = _as_date(other.valid_to) if other.valid_to is not None else None
            # Two closed-inclusive intervals overlap unless one ends strictly
            # before the other begins. An open end (`valid_to` null) runs
            # forever, so it is compared as "never ends".
            if (end is not None and end < other_start) or (
                other_end is not None and other_end < start
            ):
                continue
            raise OverlappingValidityError(
                f"a version of {district_id} valid {valid_from}..{valid_to or 'open'} overlaps "
                f"the stored version valid {other.valid_from}..{other.valid_to or 'open'}",
                table=District.table_name,
            )

    def _all_districts(self) -> list[District]:
        """Every row of `districts`, paged.

        Normally one request: `DISTRICTS_PAGE_SIZE` is larger than the whole
        table. The loop is kept because a page is still a page, and the cap is
        logged loudly rather than silently truncating a district's history.
        """
        rows: list[District] = []
        skip = 0
        while skip < self._max_rows_scanned:
            page = self._districts.query(limit=self._page_size, skip=skip)
            rows.extend(page)
            if len(page) < self._page_size:
                return rows
            skip += self._page_size

        logger.error(
            "stopped reading `districts` at %d rows while listing a district's versions; "
            "a version may be missing. `districts` has outgrown a full scan.",
            self._max_rows_scanned,
        )
        return rows

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


def _as_date(value: str | None) -> date | None:
    """An ISO-8601 date from a stored validity bound, or None if unreadable.

    Accepts a bare `YYYY-MM-DD` and a full timestamp, because which of the two
    a source emits is its choice. None rather than a raise: every caller here
    already has a considered answer for an unreadable bound — `contains`
    matches no date, `versions` sorts it first, `_refuse_overlap` lets the
    model's validator produce the better message.
    """
    if not value:
        return None
    text = value.strip().removesuffix("Z")
    try:
        return date.fromisoformat(text[:10] if "T" in text else text)
    except ValueError:
        logger.warning("unparseable district validity bound %r; treating it as unknown", value)
        return None


def _state_of(district_id: str) -> str:
    """The two-letter state code in an OCD-ID, upper case, or an empty string.

    Only used when `redistrict` writes the *first* version of a district and so
    has no existing row to inherit `state` from. Empty rather than a raise for
    a division with no state segment: `District.state` is a plain string, and
    the model is the place that decides what it will accept.
    """
    return state_code_from(district_id) or ""


__all__ = [
    "DEFAULT_VALID_FROM",
    "DISTRICTS_PAGE_SIZE",
    "HOUSE_BODY_ID",
    "MAX_DISTRICTS_SCANNED",
    "VERSION_SEPARATOR",
    "DistrictStore",
    "OverlappingValidityError",
    "Redistricting",
    "base_id_of",
    "contains",
    "version_id_for",
]
