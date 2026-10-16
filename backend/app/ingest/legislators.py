"""Load Congress's sitting members from congress-legislators (story 2.2).

What this is, and why it exists
================================

Story 1.5's walking skeleton proved address -> district -> officeholder works
end to end, but `people` was empty: `BallotService._delegation_for` scans a
table nothing has ever written to. This module is what writes it, from the
real, canonical source prd.md names:

    https://github.com/unitedstates/congress-legislators

specifically `legislators-current.yaml` — "every current legislator, with
their most recent terms" (the project's own description). It is maintained by
congress.gov/GovTrack contributors, CC0-licensed, and updated within days of
any membership change (sworn-in, resigned, died, expelled), which is why
prd.md names it rather than scraping congress.gov directly.

**Why YAML and not the published `.json`**: the project publishes
`legislators-current.yaml` at the repo root; there is no sibling `.json` file
there (confirmed live — `legislators-current.json` 404s against both `main`
and every recent tag). YAML it is, parsed with PyYAML's `safe_load`.

Why `legislators-current.yaml` alone, not `legislators-historical.yaml` too
=============================================================================

Story 2.2's acceptance criterion is "all 535 sitting members are present
... `terms[]` covers the 118th and 119th Congress" — about the terms of
*today's* members, not a full historical backfill of everyone who has ever
left office. `legislators-current.yaml` already carries each sitting member's
complete term history, which is enough to cover both Congresses for every
member who served in either while also sitting today. A member who served in
the 118th and then lost re-election is `legislators-historical.yaml`'s
concern, not this story's; pulling that file in would silently turn a
5-point story into a much larger one. If a later story needs the full
roster who served *either* Congress regardless of whether they are still
seated, it should say so and extend this loader rather than have this story
guess at that scope.

**On the "535" in the acceptance criterion**: 535 is the full statutory size
of Congress (435 House + 100 Senate), not a count this loader enforces or
pads to. The live source, fetched during this story (2026-10-16), carried 539
current records: 433 voting representatives (2 fewer than 435 — real,
currently-vacant seats) + 100 senators + 6 non-voting delegates (DC, PR, GU,
VI, AS, MP), who are legitimate current members of Congress the source itself
lists alongside voting members. This loader ingests every record the source
publishes rather than filtering to exactly 535 or inventing rows for vacant
seats — a fixed count would drift out of date the moment a seat changes hands,
which is precisely what a live, actively-maintained source is for. See the
story's PR for the live count at ingestion time.

Shape of one source record (confirmed live, 2026-10-16)
=========================================================

    id:
      bioguide: C000127          # always present, the only id this loader requires
      govtrack: 300018           # most ids are ints; some (bioguide, lis, thomas) are strings
      fec: [S8WA00194, ...]      # a LIST, uniquely among the id fields
      ...
    name:
      official_full: Maria Cantwell   # falls back to "first [middle] last" if absent
    terms:
      - type: sen | rep                # which chamber this term was in
        start: '2019-01-03'            # ISO date, always present
        end: '2025-01-03'              # ISO date, always present
        state: WA                      # two-letter USPS code
        district: 1                    # only on `rep` terms; 0 means at-large/territory
        party: Democrat | Republican | Independent | ...   # free text, not a fixed enum
        class: 1                       # only on `sen` terms (Senate election class); unused here
        caucus: Democrat               # only for an Independent who caucuses; unused here

Transform, field by field
==========================

  * **id** -- `person:{bioguide}`, datamodel.md's documented pattern. Every
    current member's record carries a bioguide id (confirmed live: 539/539),
    so this loader never falls back to the `person:{uuid}` alternative
    datamodel.md also allows.
  * **canonical_name** -- `name.official_full` when present (it almost always
    is), else assembled from `name.first`/`middle`/`last`.
  * **external_ids** -- every id under the source's `id:` map, stringified.
    `fec` is a list in the source (a member can have run under more than one
    FEC committee id across House and Senate runs); joined with `;` because
    `Person.external_ids` is `dict[str, str]`, not `dict[str, list[str]]`, and
    joining is lossless (no FEC id contains `;`) while dropping every id but
    one would not be.
  * **photo_url** -- always `None`. congress-legislators does not bundle a
    photo URL in this file (there is a separate, unofficial `legislator-photos`
    companion repo keyed by bioguide id, but it is a different project with its
    own license and update cadence, not something datamodel.md or prd.md names
    as a source) -- inventing a URL pattern against a repo this loader does not
    verify would risk a broken image for every voter, so the field is left null
    per this story's own instruction rather than guessed at.
  * **terms[]** -- built by `_terms_for`, below.

Which terms are kept, and why
==============================

datamodel.md's acceptance shape is `terms[]` of `(body_id, district_id, party,
start, end)`, and story 2.2's criterion is specifically that terms *cover* the
118th (2023-01-03 to 2025-01-03) and 119th (2025-01-03 to 2027-01-03)
Congress. Rather than keep a member's entire chamber history (which can run
to 20+ terms for a long-serving member and is out of this story's scope), this
loader keeps exactly the terms that overlap either Congress's date range --
see `_overlaps_either_congress`. A term with no `end` is never produced by
this source (every term carries both bounds), but the overlap check tolerates
a missing bound defensively rather than crashing an entire member's load over
one malformed row.

A member who has served in neither the 118th nor the 119th cannot appear in
`legislators-current.yaml` at all (anyone currently sitting was sworn in on or
before today, which is inside the 119th), so in practice every member keeps at
least one term; the filter only trims terms from *earlier* Congresses that
would otherwise bloat the row for no purpose this story needs.

body_id and district_id
========================

  * **body_id** -- `us-house` for a `rep` term, `us-senate` for a `sen` term,
    matching story 2.1's seeded `bodies` ids exactly (`BallotService` compares
    against these two literals). A term of any other `type` is skipped with a
    warning rather than guessed at -- the source has none today, but a future
    schema addition (a resident commissioner's distinct type, say) must not
    silently misfile as a House seat.
  * **district_id** -- a **provisional** OCD-ID, built by `_provisional_district_id`
    from the term's `state` and `district`, in exactly the scheme
    `app.districts.resolver` already uses and the one `BallotService` queries
    against:

        rep, state=CA, district=19  -> ocd-division/country:us/state:ca/cd:19
        rep, state=DC, district=0   -> ocd-division/country:us/state:dc  (at-large/territory)
        sen, state=WA               -> ocd-division/country:us/state:wa

    This is **provisional**, not datamodel.md's final word on district ids:
    story 2.3 is what ingests real congressional district boundaries and
    decides their canonical ids (redistricting-aware, per datamodel.md's
    `valid_from`/`valid_to`). Built this way rather than left null because (a)
    it is the real, documented OCD-ID scheme, not an invented one, so
    `BallotService`'s matching logic -- which filters on exactly this id --
    works against this data the moment story 2.3 seeds matching `districts`
    rows, and (b) datamodel.md's worked example for `districts` is this exact
    scheme. If story 2.3 ultimately needs a different a id for a boundary that
    does not coincide with state + district number (a redistricted boundary,
    say), that story updates these `people.terms[].district_id` values when it
    runs -- the same re-ingest this loader already supports idempotently.
  * A district numbered `0` is the source's convention for an at-large House
    seat (currently only a few states) or a non-voting delegate's
    territory/DC seat -- both map to the state-only OCD-ID with no `/cd:`
    segment, matching `senate_division_for`'s own handling of a statewide
    division.

Idempotency
===========

Delegated entirely to `Repository.upsert_many`, exactly as `app.seed` delegates
to `Repository.upsert`: finding the row by its natural-key id (`person:{bioguide}`)
and replacing it, or inserting when there is none (datamodel.md rule 1). A
second run of the same roster produces the same 535-odd rows, not
duplicates; a refreshed roster (a new Congress sworn in, a resignation) updates
the existing row to the new `terms[]` rather than appending a stale copy
alongside it.

Fetching
========

`fetch_legislators_yaml` is the one function that reaches the network, kept
separate from `transform_legislators` and `LegislatorLoader.load` so every
other piece of this module is testable without a live HTTP call -- the CI
suite drives the transform and load functions directly from
`tests/fixtures/legislators_current_sample.yaml`, a small fixture in the
source's exact shape, and never touches the network (CLAUDE.md: CI must not
depend on GitHub being reachable). Only `scripts/load_legislators.py` and the
live verification script call `fetch_legislators_yaml` for real.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import yaml

from app.repositories import Repository
from app.repositories.models import Person
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: Where the live roster is published. `main` confirmed live as the default
#: branch (2026-10-16); this loader does not assume that forever -- see
#: `fetch_legislators_yaml` for what happens if the path ever moves.
LEGISLATORS_CURRENT_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators"
    "/main/legislators-current.yaml"
)

#: datamodel.md's seeded `bodies` ids (story 2.1). A term of any other source
#: `type` is skipped rather than guessed into one of these.
HOUSE_BODY_ID = "us-house"
SENATE_BODY_ID = "us-senate"

#: Source `terms[].type` -> the `bodies` id it maps to.
_BODY_ID_FOR_TERM_TYPE: dict[str, str] = {"rep": HOUSE_BODY_ID, "sen": SENATE_BODY_ID}

#: Congress start/end dates (sworn-in day to sworn-in day), used only to decide
#: which of a member's terms are *kept* -- see the module docstring. These are
#: the real, fixed convening dates (the 20th Amendment's January 3 unless that
#: falls on a weekend, which neither did for these two Congresses).
CONGRESS_118_START = "2023-01-03"
CONGRESS_119_END = "2027-01-03"

#: Source id-map keys to carry into `Person.external_ids`, in the order
#: datamodel.md names them ("bioguide/thomas/govtrack/opensecrets/fec
#: crosswalk ids") plus the others the live source actually publishes, so a
#: caller gets everything available rather than an arbitrary subset.
_EXTERNAL_ID_KEYS: tuple[str, ...] = (
    "bioguide",
    "thomas",
    "lis",
    "govtrack",
    "opensecrets",
    "fec",
    "votesmart",
    "cspan",
    "wikipedia",
    "wikidata",
    "house_history",
    "ballotpedia",
    "maplight",
    "icpsr",
    "google_entity_id",
    "pictorial",
)

#: How many rows one `upsert_many` call batches before pacing. Mirrors
#: `app.bootstrap`'s injectable pacer so a live load of ~539 people paces
#: itself against the observed 60 req/min ceiling instead of firing every
#: write back to back; see `LegislatorLoader.load`.
DEFAULT_MIN_INTERVAL_SECONDS = 1.1


class LegislatorSourceError(Exception):
    """The source file could not be fetched or did not parse as expected."""


@dataclass
class LoadReport:
    """What one load run wrote, mirroring `app.seed.SeedReport`'s shape.

    `people_written` holds every bioguide-based id this run wrote, whether or
    not the stored row's contents changed -- `upsert` restates a record's full
    current contents every time (see `Repository.upsert`), so "written" is the
    only honest count, exactly as `app.seed.SeedReport` documents for the same
    reason.
    """

    people_written: list[str] = field(default_factory=list)
    skipped: list[SkippedLegislator] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.people_written)


@dataclass
class SkippedLegislator:
    """One source record this loader could not turn into a `Person` row.

    Collected rather than raised, so one malformed record in a 500+-person
    roster does not fail the entire load -- the same "fix the data and
    re-run" posture `Repository.upsert_many` already takes for a missing
    reference partway through a batch. Reported by the CLI so a skip is
    visible rather than silent.
    """

    bioguide: str | None
    reason: str


def fetch_legislators_yaml(
    *, url: str = LEGISLATORS_CURRENT_URL, timeout_seconds: float = 30.0
) -> str:
    """Fetch the live roster's raw YAML text.

    Deliberately returns text, not parsed records: keeping the HTTP call and
    the parsing separate is what lets `transform_legislators` be fed from a
    fixture file in tests with no mock HTTP layer at all.

    Raises:
        LegislatorSourceError: the request failed, or did not come back 200.
            Carries the url and status so an operator sees immediately that
            (for example) congress-legislators moved `legislators-current.yaml`
            to a new path or renamed its default branch -- rather than a bare
            httpx traceback.
    """
    try:
        response = httpx.get(url, timeout=timeout_seconds, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise LegislatorSourceError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise LegislatorSourceError(
            f"GET {url} returned {response.status_code}, expected 200 "
            "-- congress-legislators may have moved or renamed this file"
        )
    return response.text


def parse_legislators_yaml(yaml_text: str) -> list[dict[str, Any]]:
    """Parse the roster's YAML text into raw source records.

    `safe_load` only: this text comes from a public GitHub raw URL, not a
    trusted process, so the full YAML loader (which can construct arbitrary
    Python objects) is never used on it.

    Raises:
        LegislatorSourceError: the text is not a YAML list of mappings.
    """
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise LegislatorSourceError(f"legislators file did not parse as YAML: {exc}") from exc
    if not isinstance(parsed, list):
        raise LegislatorSourceError(
            f"legislators file must be a YAML list of records, got {type(parsed).__name__}"
        )
    return parsed


def transform_legislators(
    records: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[SkippedLegislator]]:
    """Turn raw source records into `Person`-shaped row dicts.

    Returns `(rows, skipped)` rather than raising on the first bad record, for
    the same reason `LoadReport.skipped` exists: one malformed legislator must
    not block the other ~538.

    Each returned dict is plain data (not yet a validated `Person`) because
    this function has no repository to check references against and
    `Repository.upsert_many` validates on the way in; keeping validation at
    the repository boundary means this transform is reusable from a context
    that is not writing to ZeroDB at all (a dry-run report, say).
    """
    rows: list[dict[str, Any]] = []
    skipped: list[SkippedLegislator] = []
    for record in records:
        try:
            rows.append(_transform_one(record))
        except _SkipLegislator as exc:
            bioguide = None
            if isinstance(record, dict):
                bioguide = (record.get("id") or {}).get("bioguide")
            skipped.append(SkippedLegislator(bioguide=bioguide, reason=str(exc)))
            logger.warning("skipping legislator %r: %s", bioguide, exc)
    return rows, skipped


class _SkipLegislator(Exception):
    """Internal signal: this one record cannot become a `Person` row."""


def _transform_one(record: dict[str, Any]) -> dict[str, Any]:
    ids = record.get("id") or {}
    bioguide = ids.get("bioguide")
    if not bioguide:
        # Confirmed live: every one of the 539 current members carries a
        # bioguide id. A record without one is not something this source is
        # known to publish, but raising past it rather than minting a
        # `person:{uuid}` id keeps a schema surprise from silently inventing
        # an id nothing else will ever look up this member by.
        raise _SkipLegislator("record has no id.bioguide")

    return {
        "id": f"person:{bioguide}",
        "canonical_name": _canonical_name(record.get("name") or {}),
        "external_ids": _external_ids(ids),
        "photo_url": None,
        "terms": _terms_for(record.get("terms") or []),
    }


def _canonical_name(name: dict[str, Any]) -> str:
    official_full = name.get("official_full")
    if official_full:
        return str(official_full)
    parts = [name.get("first"), name.get("middle"), name.get("last")]
    assembled = " ".join(str(part) for part in parts if part)
    if not assembled:
        raise _SkipLegislator("record has neither name.official_full nor first/last")
    return assembled


def _external_ids(ids: dict[str, Any]) -> dict[str, str]:
    """Every crosswalk id the source publishes, stringified.

    `fec` is the one list-valued id in the source; joined with `;` rather than
    dropped, because `Person.external_ids` is flat (`dict[str, str]`) and a
    member who ran for both chambers legitimately has more than one FEC id --
    see the module docstring.
    """
    external: dict[str, str] = {}
    for key in _EXTERNAL_ID_KEYS:
        value = ids.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            if not value:
                continue
            external[key] = ";".join(str(item) for item in value)
        else:
            external[key] = str(value)
    return external


def _terms_for(raw_terms: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The subset of a member's terms that overlap the 118th or 119th Congress.

    See the module docstring's "Which terms are kept" section. Terms of a
    `type` this loader does not recognise are skipped with a warning rather
    than failing the whole member -- the member's other terms are still good
    data.
    """
    kept: list[dict[str, Any]] = []
    for term in raw_terms:
        term_type = term.get("type")
        body_id = _BODY_ID_FOR_TERM_TYPE.get(str(term_type))
        if body_id is None:
            logger.warning("skipping term with unrecognised type %r", term_type)
            continue
        if not _overlaps_either_congress(term):
            continue
        kept.append(
            {
                "body_id": body_id,
                "district_id": _provisional_district_id(term, body_id),
                "party": _stringify_or_none(term.get("party")),
                "start": _stringify_or_none(term.get("start")),
                "end": _stringify_or_none(term.get("end")),
            }
        )
    return kept


def _overlaps_either_congress(term: dict[str, Any]) -> bool:
    """Whether a term's [start, end) touches [118th start, 119th end).

    Both Congresses are treated as one contiguous window
    (`CONGRESS_118_START` to `CONGRESS_119_END`) rather than checked
    separately, because a term spanning the boundary (sworn in during the
    118th, still serving into the 119th) must be kept exactly once, not
    matched twice or dropped by an off-by-one at the midpoint.

    A missing bound does not disqualify a term -- the source always supplies
    both, but a term with, say, no `end` (an ongoing term from a source that
    omitted it) should still be evaluated on what it does carry rather than
    silently vanish, echoing `BallotService._is_current`'s own "absent is not
    disqualifying" rule for the identical reason: a voter-facing gap is worse
    than a loosely-dated row the nightly audit can flag.
    """
    start = term.get("start")
    end = term.get("end")
    if isinstance(end, str) and end < CONGRESS_118_START:
        return False
    return not (isinstance(start, str) and start >= CONGRESS_119_END)


def _provisional_district_id(term: dict[str, Any], body_id: str) -> str | None:
    """A provisional OCD-ID for this term, or None if `state` is missing.

    See the module docstring's "body_id and district_id" section for the
    scheme and why it is provisional. Matches
    `app.districts.resolver`'s own OCD-ID convention exactly, so
    `BallotService`'s delegation query is ready to match these rows the
    moment story 2.3 seeds `districts` rows with the same ids.
    """
    state = term.get("state")
    if not state:
        return None
    state_division = f"ocd-division/country:us/state:{str(state).lower()}"
    if body_id == SENATE_BODY_ID:
        return state_division

    district = term.get("district")
    if district in (None, 0, "0"):
        # At-large seat or non-voting delegate's territory/DC seat: no
        # congressional-district segment, matching `senate_division_for`'s own
        # handling of a statewide division.
        return state_division
    return f"{state_division}/cd:{district}"


def _stringify_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


class LegislatorLoader:
    """Writes transformed legislator rows through the repository layer.

    The one thing this class adds beyond calling `Repository.upsert_many`
    directly is **pacing**: a cold load of ~539 people, each costing a
    find-then-write pair (`Repository.upsert` -> `ZeroDBClient.upsert_row` ->
    `get_row` + `insert_row`/`_replace_row`, per story 1.2/1.4), is roughly
    1,000+ live requests -- comfortably past the ~60 req/min ceiling
    datamodel.md records if fired back to back. `pace()` is called before
    every person's write, exactly like `app.bootstrap.Bootstrapper` and
    `app.seed.Seeder` already do for the same reason, and for the same
    reason tests inject a no-op.
    """

    def __init__(self, client: ZeroDBClient, *, pace: Callable[[], None] | None = None) -> None:
        self._people: Repository[Person] = Repository(client=client, model=Person)
        self._pace = pace if pace is not None else (lambda: None)

    def load(self, rows: Sequence[dict[str, Any]]) -> LoadReport:
        """Upsert every row, one person per call so pacing is per-person.

        Not `upsert_many`: that method shares one reference cache across the
        whole batch (right for `member_votes` referencing one roll call many
        times over) but paces nothing, and `Person` has no declared references
        to cache in the first place (`RowModel.references` is empty for
        `Person`). Looping here, pacing before each call, is what actually
        protects the live rate limit for this table.
        """
        report = LoadReport()
        for row in rows:
            self._pace()
            person = self._people.upsert(row)
            assert person.id is not None  # noqa: S101 - upsert guarantees it
            report.people_written.append(person.id)
            logger.info("legislators: wrote %s", person.id)
        return report


__all__ = [
    "CONGRESS_118_START",
    "CONGRESS_119_END",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "HOUSE_BODY_ID",
    "LEGISLATORS_CURRENT_URL",
    "SENATE_BODY_ID",
    "LegislatorLoader",
    "LegislatorSourceError",
    "LoadReport",
    "SkippedLegislator",
    "fetch_legislators_yaml",
    "parse_legislators_yaml",
    "transform_legislators",
]
