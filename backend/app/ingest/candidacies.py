"""Load federal candidacies from the OpenFEC API (story 2.6).

What this is, and why it exists
================================

Story 2.4's ballot lookup returns `data_state: "incumbents_only"` and says so
in as many words: "Only incumbents are nameable until story 2.6 loads
candidacies." A voter comparing their ballot can currently see the person who
already holds the seat and nobody running against them, which is the opposite
of the product's point. This module is what loads the rest of the ballot.

The source is the one story 2.5's spike chose, after checking FEC against
three states' own election offices
(`docs/spikes/2.5-challenger-candidate-source.md`):

    Use the FEC `/candidates` and `/candidates/search` API as the primary
    source for federal House and Senate candidacies, refreshed on the weekly
    cadence story 2.6 already specifies, and cross-check against each state's
    official candidate/ballot list once per cycle after that state's
    independent/minor-party filing deadline has passed.

This module is the first half of that: the weekly FEC pull. **The state-office
reconciliation pass is deliberately not built here** — see "What this does not
cover" below, which is a correctness limitation a reviewer must read, not a
nice-to-have.

Credentials
============

**No FEC API key exists in this environment.** `local.env` and `.env.example`
carry none, and getting one requires an interactive signup at
https://api.data.gov/signup/ that this agent cannot complete — the same
blocker shape as story 5.1's Congress.gov key.

What IS different from 5.1, and worth stating precisely because it changes how
much of this module is actually verified: OpenFEC accepts the public
`DEMO_KEY`, so during this story the endpoint was queried **live** (2026-10-16)
and answered `200` with real 2026 filings. Every field name, type and enum in
the transform below was taken from those live responses and cross-checked
against the endpoint's own swagger definition at https://api.open.fec.gov/swagger/
(the `Candidate` definition and the `/v1/candidates/` parameter list). The
fixture at `tests/fixtures/fec_candidates_sample.json` carries five of those
live records verbatim.

What that live probe does NOT amount to:

  * `DEMO_KEY` is rate-limited to **10 requests per hour** (confirmed from the
    live `x-ratelimit-limit: 10` response header). A national load is ~55
    state/office queries plus paging — low thousands of requests — so DEMO_KEY
    cannot run this loader even once, and certainly not weekly. A registered
    api.data.gov key raises this to ~1,000/hour, which is why one is required
    rather than optional.
  * No end-to-end run has happened: nothing in this module has written a
    `candidacies` row to the live ZeroDB project from live FEC data.
    `scripts/load_candidacies.py` is ready and has not been executed for real.

So: the source shape is live-verified, the pipeline is fixture-verified, and
the end-to-end load is blocked on a credential. See this story's PR.

Shape of one source record (captured live, 2026-10-16)
=======================================================

    GET https://api.open.fec.gov/v1/candidates/
        ?api_key=...&election_year=2026&office=H&state=PA&per_page=100&page=1

    {"api_version": "1.0",
     "pagination": {"count": 107, "is_count_exact": true,
                    "page": 1, "pages": 2, "per_page": 100},
     "results": [{
       "candidate_id": "H6PA02205",       # FEC's stable per-office id
       "name": "ARRIAGA, JESSICA",        # "LAST, FIRST [SUFFIX]", upper case
       "party": "REP",                    # three-letter code
       "party_full": "REPUBLICAN PARTY",
       "office": "H",                     # H | S | P
       "office_full": "House",
       "state": "PA",
       "district": "02",                  # ZERO-PADDED string
       "district_number": 2,              # the same value as an int
       "incumbent_challenge": "C",        # I | C | O
       "incumbent_challenge_full": "Challenger",
       "candidate_status": "N",           # C | F | N | P
       "candidate_inactive": false,
       "has_raised_funds": false,
       "election_years": [2026],          # years this id has run in
       "cycles": [2026],
       "active_through": 2026,
       "first_file_date": "2026-01-14",
       "last_file_date": "2026-01-14",
       "load_date": "2026-02-02T19:41:07",
       "federal_funds_flag": false,
       "election_districts": ["02"],
       "inactive_election_years": null
     }, ...]}

Transform, field by field
==========================

  * **id** -- derived by the repository from `Candidacy`'s natural key, giving
    datamodel.md's `{election_id}:{district_id}:{person_id}` with the
    `person:` prefix intact. Never built by hand here.
  * **election_id** -- the seeded election's id, passed in rather than guessed
    (see `candidacies_from_results`'s `election` parameter). This loader does
    not decide which election a filing belongs to by parsing a date; the caller
    names the election and this module checks the filing's `election_years`
    contains that election's year.
  * **district_id** -- an OCD-ID built by `ocd_district_id`, in exactly the
    scheme story 2.3 confirmed live and story 2.2 writes into
    `people.terms[].district_id`. See "Districts" below.
  * **person_id** -- `resolve_person_id`: an existing `people` row when the
    filer is already known, otherwise a new deterministic
    `person:fec-{candidate_id}`. See "Challengers are new people" below —
    this is the id-scheme decision this story had to make.
  * **person_name** -- the source's `name` verbatim ("ARRIAGA, JESSICA"). Not
    reformatted to "Jessica Arriaga": FEC publishes the name as the filer
    wrote it on Form 2, and re-casing it would mean guessing which token is a
    surname (wrong for "VAN HOLLEN", "DE LA CRUZ", "O'HALLERAN") and inventing
    a presentation the source never asserted. Formatting a name for display is
    the UI's business, from a field that still carries the filing's own text.
  * **party** -- the three-letter `party` code verbatim ("REP", "DEM", "LIB",
    "GRE", "IND", "UN", ...). NOT `party_full`, and NOT normalised to
    "Republican"/"Democrat": the code is the stable machine value, the full
    name is a label that changes case and wording between sources, and a
    normalisation table mapping dozens of minor parties into a short list is
    exactly the kind of judgment prd.md's nonpartisanship rule says one
    codebase must not make per party. Null when the source omits it.
  * **is_incumbent** -- cross-referenced against the real legislators story
    2.2 loaded, NOT copied from `incumbent_challenge`. See "Incumbency" below.
  * **status** -- `candidacy_status_for`, mapping FEC's `candidate_status`
    letters onto `models.CandidacyStatus`. See that function.

Districts
==========

The source gives `state` ("PA") plus `district` ("02", zero-padded) and
`district_number` (2, an int). `ocd_district_id` turns those into the OCD-ID
story 2.3 verified against 442 live House `district_id` values:

    office=H, state=PA, district_number=2   -> ocd-division/country:us/state:pa/cd:2
    office=H, state=DE, district_number=0   -> ocd-division/country:us/state:de
    office=S, state=DE                      -> ocd-division/country:us/state:de

Three things matter here, each a bug if got wrong:

1. **The padding must go.** FEC says `"02"`; the 442 live `district_id`s and
   story 2.3's `ocd_id_for` both say `/cd:2`. Because datamodel.md has no
   joins and the ballot query matches these ids as *strings*, a padded
   `/cd:02` would match nothing and every voter in a single-digit district
   would see an empty race with no error to explain it. `district_number`
   (already an int) is used rather than stripping `district` by hand.
2. **`00` is not district zero.** FEC uses `00` for a Senate seat, a
   presidential run, and a House at-large seat alike ("Presidential, Senate and
   House at-large candidates will have District 00", the endpoint's own
   parameter documentation). All of these map to the *state-only* division with
   no `/cd:` segment — the same thing story 2.2's loader does with its own
   `district: 0` and story 2.3's resolver does with the geocoder's `00`/`98`.
   A literal `/cd:0` would be a district that does not exist.
3. **A Senate candidacy is statewide, and that is a real limitation.** Both of
   a state's Senate seats, and its at-large House seat, share one OCD-ID — so
   the candidacy id for two people running for *different* Senate seats in the
   same state differs only by person, which is correct, but Informed cannot
   currently tell which of the two seats either is running for. datamodel.md's
   `districts` table has no concept of a Senate class, and inventing one here
   would be a schema decision this story is not scoped to make. Noted in
   "What this does not cover".

Because `Candidacy.district_id` is a declared reference, a `districts` row has
to exist before the candidacy is written. Story 2.3 creates those lazily, on
the first address that geocodes into them, so at ingest time most do not exist
yet — a nationwide candidacy load would otherwise fail for every district no
voter has looked up. `CandidacyLoader._ensure_district` writes the row from
FEC's own `state`/`district_number`, the same "create on first need, from the
response that produced the id" posture `app.districts.store` documents. The
`name` it writes ("Congressional District 2", "Delaware") is a plain
construction from those two fields and is deliberately restated by
`DistrictStore.ensure` with the geocoder's own label the first time a voter
resolves into that district — the geocoder is the better authority on a
district's name, and the upsert lets the better answer win without a migration.

Challengers are new people (the id-scheme decision)
====================================================

Story 2.2 loaded **only sitting members** of Congress, from
congress-legislators, all of whom have a bioguide id — so every `people` row
in the project today is `person:{bioguide}`. A challenger has no bioguide id,
because the Bioguide is a directory of people who have *served*. They still
need a `people` row: datamodel.md's integrity rules say "every
`candidacies.person_id` resolves to a `people` row", and `Candidacy` declares
that reference, so a candidacy for a person who does not exist is refused.

datamodel.md's id pattern for `people` is **`person:{bioguide}` or
`person:{uuid}`**, and a challenger plainly falls on the `{uuid}` side. The
decision this story had to make is *which* uuid, and a random one is wrong:

  * A random `uuid4` is not idempotent. The acceptance criterion is a load
    "refreshed weekly", so the same filer is transformed ~3 times before the
    election and ~55 times a year; a fresh uuid each run would mint a new
    `people` row and a new `candidacies` row every week, and datamodel.md's
    rule 1 ("re-ingesting a record is an idempotent upsert") would be broken
    on the single table this story adds most rows to.
  * Nothing else could ever find the person again. There is no join and no
    secondary index to look a candidate up by FEC id with, so a random id makes
    the row unreachable except by scanning.

**Chosen: `person:fec-{candidate_id}`**, e.g. `person:fec-H6PA02205`, with the
FEC id also stored in `external_ids["fec"]`.

  * Deterministic, so a weekly re-pull resolves to the same person and the
    upsert is genuinely idempotent.
  * Derived from FEC's own stable, documented identifier — "a unique
    identifier assigned to each candidate registered with the FEC" — not from
    the person's name, which changes spelling and casing between filings and
    is not unique.
  * The `fec-` segment makes the id's provenance legible: a reader can tell at
    a glance that this person entered Informed as an FEC filer and has no
    congressional service record, which is exactly the distinction story 9.6
    ("a challenger's promises... they have no voting record") turns on.
  * It is a `{uuid}`-form id in datamodel.md's sense — an opaque,
    non-bioguide, system-assigned string — and a *better* one than a random
    uuid, for the reasons above. Documented here rather than silently chosen
    because it pins a user-visible id scheme that later stories will build on.

**The known cost**, stated plainly: an FEC candidate id is per *office*, so a
person who runs for the House and later the Senate has two FEC ids and would
get two `people` rows. Nothing in this story's data can reliably merge them
(names collide; there is no national person identifier in this feed), and
merging on name would be worse than not merging. A later story that needs one
canonical person across offices gets a dedupe pass with human review; it will
find this one's rows by their `external_ids["fec"]`, which is why that field
is written. The same person running again for the *same* office keeps the same
FEC id and so stays one row — which is the common case.

A filer who IS already in `people` keeps their existing id. `resolve_person_id`
looks for a `people` row carrying this FEC id in `external_ids["fec"]` —
story 2.2 writes every FEC id a member has there, semicolon-joined — so an
incumbent's candidacy points at `person:{bioguide}`, not at a second
`person:fec-...` row. That lookup is the one place this module reads the whole
`people` table, and it is cached per run (`_PersonIndex`) rather than re-read
per filer: 539 members is one paged read, against potentially thousands of
filings.

Incumbency
===========

`is_incumbent` is **not** `incumbent_challenge == "I"`. FEC's letter is
self-reported on Form 2 and is wrong in the live data often enough to matter
(the fixture carries a record flagged `I` by a filer who holds no seat). Story
2.6's scope says to cross-reference "against the already-loaded 539 real
legislators from story 2.2", and `_is_incumbent_for` does exactly that: a
candidacy is incumbent when the resolved person has a *current term* in that
same `district_id`.

Both halves are required, and each rules out a real false positive:

  * **Same person** — resolved through `external_ids["fec"]`, so a challenger
    who shares a surname with the sitting member is not flagged.
  * **Same seat** — a sitting representative running for their state's Senate
    seat is a *challenger* for that seat, not its incumbent, even though they
    are very much an incumbent officeholder. Comparing `district_id` is what
    draws that line, and showing such a candidate as "incumbent" on a Senate
    race would actively mislead a voter about who currently holds the seat.

A person with no `people` row yet (every challenger) is never incumbent, which
follows without a special case.

What this does not cover
=========================

Read this as a correctness boundary, not a backlog:

1. **Minor-party, independent and write-in candidates who never file FEC Form
   2 are MISSING.** This is the spike's headline finding: FEC registration is
   triggered by raising or spending $5,000 (or a voluntary Form 2 filing), so a
   minor-party nominee chosen by convention, a late-qualifying independent, or
   a write-in can be on a real ballot and absent from this feed at any
   freshness. The spike's remedy is a per-state reconciliation pass against
   each state's own certified list, behind the `JurisdictionAdapter` pattern,
   timed to each state's filing deadline — and it explicitly budgets that as
   non-trivial engineering, not a corollary of this story. Until it exists,
   `candidacies` is "every federally registered candidate", which is not the
   same as "everyone on your ballot". A ballot view built on this table alone
   must not claim completeness, which is why story 2.4's `DataState` is an
   enum with room for a third value rather than a bool.
2. **Senate seat class is not modelled** — see "Districts" point 3.
3. **Presidential candidacies are skipped.** There is no congressional
   district to place one in, `districts` has no national row, and 2026 is not
   a presidential election year. The records exist in this feed year-round, so
   they are skipped explicitly rather than allowed to produce a nonsense
   district id.
4. **Scheduling is not built.** "Refreshed weekly" is satisfied here by making
   the load correct and idempotent to re-run; nothing in this repo schedules
   it, because no scheduler or worker framework exists yet. See "Refreshed
   weekly" below.

Refreshed weekly
=================

The acceptance criterion's "refreshed weekly" has two halves. This story does
the first and documents the second:

  * **Safe to re-run, which is the hard half.** Every write is an idempotent
    upsert on a deterministic id — the election (seeded), the district, the
    person, the candidacy — so running this loader any number of times leaves
    exactly one row each, and a filer whose status changed (withdrawn, party
    switch) has their existing row *updated* rather than a second row
    appended. `candidacies` is an ordinary table, not append-only
    (datamodel.md rule 6 names verdicts, positions and the provenance log,
    not this one), so updating in place is correct. Asserted by the
    idempotency scenarios in
    `tests/features/elections_and_candidacies.feature`.
  * **Actually invoking it weekly is infrastructure that does not exist.**
    There is no scheduler, worker, cron or queue in this repo — every ingest
    to date is a hand-run script (`scripts/load_legislators.py`,
    `scripts/load_house_rollcalls.py`), and `scripts/load_candidacies.py`
    follows them. Standing up a scheduler is a deployment concern spanning
    every ingest story, not a side effect of this one, and building a bespoke
    one here would be a sixth way to run a loader. Follow-up, flagged in this
    story's PR: one scheduled runner for all ingest adapters. The function is
    ready for it with no code change.

Fetching
=========

`fetch_candidates` is the only function that reaches the network, kept
separate from `candidacies_from_results` and `CandidacyLoader` for the same
reason `legislators.py` and `house_rollcalls.py` separate theirs: every other
piece of this module is driven from the fixture in CI with no mock HTTP layer
and no network at all (CLAUDE.md). Only `scripts/load_candidacies.py` calls it
for real — and, as above, it has not been run with a real key.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.repositories import Repository
from app.repositories.errors import RepositoryError
from app.repositories.models import Candidacy, District, Election, Person
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: OpenFEC API base. Confirmed live (2026-10-16): reachable, answers 200 for
#: `/candidates/` with the public DEMO_KEY. See the module docstring's
#: credentials note for why DEMO_KEY cannot actually run this loader.
OPENFEC_BASE_URL = "https://api.open.fec.gov/v1"

#: The public demo key api.data.gov documents for trying an API without
#: signing up. Rate-limited to 10 requests/hour (confirmed from the live
#: `x-ratelimit-limit` header), which is enough to inspect the schema and far
#: too little to load a cycle — so it is the default only so that a caller
#: with no key gets a working probe and a clear rate-limit error, never a
#: silent 403.
DEMO_API_KEY = "DEMO_KEY"

#: Federal offices this loader ingests: House and Senate. 'P' (president) is
#: deliberately absent — see the module docstring's "What this does not cover".
INGESTED_OFFICES: tuple[str, ...] = ("H", "S")

#: Every state and territory that elects a voting member or delegate to
#: Congress, as the two-letter codes OpenFEC's `state` filter accepts. Issued
#: per state rather than as one national query so each response stays small
#: and one state's failure can be retried without re-fetching the other 55.
#: Shared by `scripts/load_candidacies.py` and `app.scheduling.jobs`'s weekly
#: job so the list of states exists in exactly one place (story 16, the
#: scheduled-job runner).
# fmt: off
# Kept as a hand-wrapped block: the formatter would otherwise put each of the
# 56 codes on its own line, turning a scannable list into three screens.
ALL_STATES: tuple[str, ...] = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "GU",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND",
    "MP", "OH", "OK", "OR", "PA", "PR", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "VI", "WA", "WV", "WI", "WY", "AS",
)
# fmt: on

#: `office` -> the `bodies` id the seat belongs to, matching story 2.1's seed
#: and story 2.2's loader exactly.
_BODY_ID_FOR_OFFICE: dict[str, str] = {"H": "us-house", "S": "us-senate"}

#: FEC `candidate_status` -> `models.CandidacyStatus`. The source's letters are
#: documented on the `/v1/candidates/` endpoint itself:
#:   C present candidate, F future candidate,
#:   N not yet a candidate, P prior candidate
#: See `candidacy_status_for` for why each maps where it does.
_STATUS_FOR_FEC_CODE: dict[str, str] = {
    "C": "filed",
    "F": "filed",
    "N": "statement",
    "P": "withdrawn",
}

#: Page size for the one-off `people` scan `_PersonIndex` runs. Matches
#: `app.ballot.service`'s own `PEOPLE_PAGE_SIZE` for the same table: a page is
#: a page, and 539 members is six reads rather than one, which is the cost of
#: the repository layer not exposing an unpaged read (deliberately — see
#: `Repository.query`).
PEOPLE_PAGE_SIZE = 100

#: Mirrors every other loader's pacing constant against the ~60 req/min ZeroDB
#: ceiling datamodel.md records. One candidacy costs up to four find-then-write
#: pairs (district, person, candidacy, plus the reference checks the repository
#: runs), so pacing applies per candidacy, not per table.
DEFAULT_MIN_INTERVAL_SECONDS = 1.1

#: `districts.valid_from` for a row this loader creates. The same constant and
#: the same reasoning as `app.districts.store.DEFAULT_VALID_FROM`: it is a
#: redistricting boundary, not an ingest timestamp, and the 119th Congress
#: convened on this date. Imported by value rather than from that module to
#: avoid an ingest adapter depending on the ballot-lookup package; if the two
#: ever need to disagree, that is a redistricting story's decision (2.7).
DEFAULT_DISTRICT_VALID_FROM = "2025-01-03"


class CandidacySourceError(Exception):
    """The OpenFEC API could not be fetched or did not parse as expected."""


@dataclass
class SkippedCandidate:
    """One source record this loader could not or would not turn into a row.

    Collected rather than raised, exactly as in `legislators.py` and
    `house_rollcalls.py`: one unusable filing among thousands must not fail the
    whole run, and a skip has to be *visible* (the CLI prints these) rather
    than silent.
    """

    candidate_id: str | None
    reason: str


@dataclass
class LoadReport:
    """What one load run wrote.

    `candidacies_written` and `people_written` hold every id the run upserted,
    whether or not the stored contents changed — an upsert restates a row's
    full current contents, so "written" is the only honest count, the same call
    every prior loader's report documents.

    `people_written` carries only the people this loader CREATED for
    challengers; a candidacy resolved to a pre-existing `person:{bioguide}` row
    is not re-written, because this loader is not the authority on a sitting
    member's record — story 2.2's is, and overwriting their `terms[]` with an
    empty list would delete the voting history scoring depends on.
    """

    candidacies_written: list[str] = field(default_factory=list)
    people_written: list[str] = field(default_factory=list)
    districts_written: list[str] = field(default_factory=list)
    skipped: list[SkippedCandidate] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.candidacies_written)


# --- fetching ---------------------------------------------------------------


def fetch_candidates(
    *,
    election_year: int,
    office: str,
    state: str | None = None,
    api_key: str = DEMO_API_KEY,
    base_url: str = OPENFEC_BASE_URL,
    per_page: int = 100,
    timeout_seconds: float = 30.0,
) -> list[dict[str, Any]]:
    """Every candidate record OpenFEC lists for one year/office[/state].

    Walks `pagination.pages` to completion rather than returning the first
    page: PA alone had 107 House filings for 2026 (live, 2026-10-16), so a
    single page would silently drop candidates off voters' ballots — the exact
    failure this story exists to fix.

    `state` is optional because the API accepts the filter as optional, but a
    national pull is better issued per state (see
    `scripts/load_candidacies.py`): it keeps each response small and lets one
    state's failure be retried without re-fetching the other 49.

    Raises:
        CandidacySourceError: the request failed, did not come back 200, or the
            body had no `results` array. A 429 is surfaced as this error with
            its status visible, because the most likely cause by far is running
            with DEMO_KEY's 10/hour limit instead of a registered key.
    """
    url = f"{base_url}/candidates/"
    params: dict[str, Any] = {
        "api_key": api_key,
        "election_year": election_year,
        "office": office,
        "per_page": per_page,
        "sort": "name",
    }
    if state:
        params["state"] = state

    records: list[dict[str, Any]] = []
    page = 1
    while True:
        params["page"] = page
        try:
            response = httpx.get(url, params=params, timeout=timeout_seconds)
        except httpx.HTTPError as exc:
            raise CandidacySourceError(f"could not reach {url}: {exc}") from exc
        if response.status_code != 200:
            raise CandidacySourceError(
                f"GET {url} (page {page}) returned {response.status_code}, expected 200"
                + (
                    " -- DEMO_KEY allows only 10 requests/hour; a registered "
                    "api.data.gov key is required for a real load"
                    if response.status_code == 429
                    else ""
                )
            )
        body = response.json()
        results = body.get("results")
        if not isinstance(results, list):
            raise CandidacySourceError(f"GET {url} did not return a results array: {body!r}")
        records.extend(results)

        pagination = body.get("pagination") or {}
        total_pages = pagination.get("pages")
        if not isinstance(total_pages, int) or page >= total_pages:
            break
        page += 1

    return records


# --- transform --------------------------------------------------------------


def ocd_district_id(*, office: str, state: str, district_number: Any) -> str:
    """The OCD-ID for a filing's seat, in story 2.3's confirmed scheme.

    See the module docstring's "Districts" section for the full reasoning. In
    short: a Senate seat and an at-large/`00` House seat are the state-only
    division; a numbered House seat appends `/cd:{n}` **unpadded**.

    Raises:
        ValueError: `state` is empty. Without it there is no division to
            name, and a filing with no state is not something this feed
            publishes.
    """
    if not state:
        raise ValueError("a filing with no state has no district")
    state_division = f"ocd-division/country:us/state:{str(state).lower()}"
    if office == "S":
        return state_division
    number = _as_int_or_none(district_number)
    if number is None or number == 0:
        # FEC's `00`: a Senate seat, a presidential run, or a House at-large
        # seat. All are statewide; `/cd:0` would be a district that does not
        # exist.
        return state_division
    return f"{state_division}/cd:{number}"


def district_name_for(*, office: str, state: str, district_number: Any) -> str:
    """A plain label for a district row this loader has to create.

    Deliberately modest. `app.districts.store` writes the *geocoder's* own
    label ("Congressional District 7", "Delegate District (at Large)"), which
    is the better authority, and its upsert restates this value the first time
    a voter resolves into the district. This is the placeholder that keeps the
    reference check satisfiable in the meantime, built only from fields the
    filing actually carries — never invented detail.
    """
    state_code = str(state).upper()
    if office == "S":
        return f"{state_code} statewide (U.S. Senate)"
    number = _as_int_or_none(district_number)
    if number is None or number == 0:
        return f"{state_code} at-large congressional district"
    return f"{state_code} congressional district {number}"


def fec_person_id(candidate_id: str) -> str:
    """`person:fec-{candidate_id}` — a challenger's deterministic person id.

    datamodel.md's `person:{uuid}` form, made deterministic from FEC's own
    stable candidate id. See the module docstring's "Challengers are new
    people" for why a random uuid was rejected and what this choice costs.
    """
    return f"person:fec-{candidate_id}"


def candidacy_status_for(record: dict[str, Any]) -> str:
    """FEC's `candidate_status` letter as a `models.CandidacyStatus`.

    The source's own documented vocabulary, and where each letter lands:

      * **C, present candidate -> `filed`.** The ordinary case: a registered
        candidate for this cycle.
      * **F, future candidate -> `filed`.** Someone registered for a *later*
        cycle than the one being loaded. They are grouped with `filed` rather
        than given a fourth status because, by the time this filing is loaded
        against an election whose year is in its `election_years`, "future"
        describes FEC's bookkeeping and not the voter's ballot — and
        `candidacies_from_results` has already refused any filing whose
        `election_years` does not contain the loaded election's year, so an
        'F' record that reaches here is running in this election.
      * **N, not yet a candidate -> `statement`.** A real, visible filing
        (these are the `has_raised_funds: false` records story 2.5's spike
        verified live) by someone who has not crossed the statutory
        $5,000 threshold that makes them a candidate in law. They belong on a
        voter's ballot view — they are running — but conflating them with 'C'
        would erase a distinction the source is explicit about, and a voter
        comparing candidates deserves to know a name has only declared.
      * **P, prior candidate -> `withdrawn`.** Within the loaded cycle, a
        filer who has stopped running (withdrawn, lost a primary, or
        deregistered).

    An absent or unrecognised letter falls back to `statement`, the weakest
    claim of the three: the filing exists (FEC published it), so dropping the
    candidate would hide a real name, and asserting `filed` would claim a
    statutory status the source did not give. Falling back rather than raising
    is the same posture `house_rollcalls._classify_vote_type` takes for a new
    question type — one unfamiliar code must not stop a national ingest.
    """
    code = str(record.get("candidate_status") or "").strip().upper()
    status = _STATUS_FOR_FEC_CODE.get(code)
    if status is None:
        logger.warning(
            "unrecognised FEC candidate_status %r for %r; recording as 'statement'",
            record.get("candidate_status"),
            record.get("candidate_id"),
        )
        return "statement"
    return status


class _SkipCandidate(Exception):
    """Internal signal: this one record must not become a `Candidacy` row."""


def candidacies_from_results(
    records: Iterable[dict[str, Any]],
    *,
    election: Election,
) -> tuple[list[dict[str, Any]], list[SkippedCandidate]]:
    """Turn fetched FEC records into partial `Candidacy`-shaped row dicts.

    Returns `(rows, skipped)` rather than raising on the first bad record, for
    the same reason every prior loader does: one unusable filing must not
    block the rest of a state.

    **`person_id` and `is_incumbent` are deliberately NOT set here.** Both
    require reading the stored `people` table — the first to reuse an existing
    person rather than mint a duplicate, the second to check a real current
    term — and this function has no repository. Each row instead carries a
    private `_fec` block (the fields `CandidacyLoader` needs to finish it),
    which the loader consumes and removes before the row is validated. Keeping
    the pure transform separate from the reads is what lets the field mapping
    be tested with no ZeroDB at all.

    `election` is passed in, not inferred: the caller names the election being
    loaded, and this function checks each filing's `election_years` actually
    contains that election's year — which is how a stale 2024 filing is kept
    off a 2026 ballot.
    """
    election_id = str(election.id)
    election_year = _election_year_of(election)

    rows: list[dict[str, Any]] = []
    skipped: list[SkippedCandidate] = []
    for record in records:
        candidate_id = record.get("candidate_id") if isinstance(record, dict) else None
        try:
            rows.append(
                _transform_one(record, election_id=election_id, election_year=election_year)
            )
        except _SkipCandidate as exc:
            skipped.append(
                SkippedCandidate(candidate_id=_str_or_none(candidate_id), reason=str(exc))
            )
            logger.info("skipping FEC candidate %r: %s", candidate_id, exc)
        except ValueError as exc:
            skipped.append(
                SkippedCandidate(candidate_id=_str_or_none(candidate_id), reason=str(exc))
            )
            logger.warning("skipping FEC candidate %r: %s", candidate_id, exc)
    return rows, skipped


def _transform_one(
    record: dict[str, Any], *, election_id: str, election_year: int
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise _SkipCandidate(f"record is not an object: {type(record).__name__}")

    candidate_id = record.get("candidate_id")
    if not candidate_id:
        # Without FEC's id there is no stable person id to mint and no way to
        # recognise this filer on next week's pull, so the row could never be
        # idempotent. Not something this feed publishes; refused rather than
        # keyed off the name.
        raise _SkipCandidate("record has no candidate_id")

    office = str(record.get("office") or "").strip().upper()
    if office not in INGESTED_OFFICES:
        raise _SkipCandidate(
            f"office {office!r} is not ingested by this loader "
            f"(expected one of {list(INGESTED_OFFICES)})"
        )

    election_years = record.get("election_years")
    if not _runs_in(election_years, election_year):
        raise _SkipCandidate(
            f"election_years {election_years!r} does not include {election_year} "
            f"— this filing is not for the election being loaded"
        )

    state = record.get("state")
    name = record.get("name")
    if not name:
        raise _SkipCandidate("record has no name")

    district_id = ocd_district_id(
        office=office, state=str(state or ""), district_number=record.get("district_number")
    )

    return {
        "election_id": election_id,
        "district_id": district_id,
        # person_id and is_incumbent are filled in by CandidacyLoader; see the
        # docstring above for why they cannot be decided here.
        "person_name": str(name),
        "party": _str_or_none(record.get("party")),
        "status": candidacy_status_for(record),
        "_fec": {
            "candidate_id": str(candidate_id),
            "office": office,
            "state": str(state or ""),
            "district_number": record.get("district_number"),
        },
    }


def _runs_in(election_years: Any, election_year: int) -> bool:
    """Whether a filing's `election_years` covers the election being loaded.

    `election_years` is a list of ints in the live data ("Years in which a
    candidate ran for office"). A record missing the field entirely is treated
    as NOT running in this election rather than assumed to be: assuming would
    put a candidate with no evidence of running onto a real ballot, and an
    absent field is a schema surprise worth skipping visibly.
    """
    if not isinstance(election_years, list):
        return False
    return any(_as_int_or_none(year) == election_year for year in election_years)


def _election_year_of(election: Election) -> int:
    """The calendar year of a seeded election, from its `election_date`.

    Raises:
        ValueError: the date does not start with a four-digit year. The seed
            writes ISO dates, so this is a corrupted or hand-edited row, and
            loading a national candidate list against an election whose year
            cannot be read would silently accept every filing from every
            cycle.
    """
    date = str(election.election_date)
    year = date[:4]
    if len(date) < 4 or not year.isdigit():
        raise ValueError(
            f"election {election.id!r} has an unparseable election_date {date!r}; "
            "expected ISO YYYY-MM-DD"
        )
    return int(year)


def _as_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# --- loading ----------------------------------------------------------------


class _PersonIndex:
    """FEC candidate id -> existing `people` id, read once per run.

    `resolve_person_id` has to answer "is this filer already a known person?"
    for every filing, and the only available evidence is
    `people.external_ids["fec"]` — which no index covers, so the answer costs
    a full table read. Doing that per filing would be thousands of reads of
    the same 539 rows; doing it once is one paged read.

    The cache is per *run*, not per process: a later weekly run re-reads, so a
    member added by story 2.2 between runs is seen. Within a run the `people`
    table only grows (challengers this loader creates), and those are recorded
    here as they are written so two filings for one new person resolve to the
    same row.
    """

    def __init__(self, people: Repository[Person]) -> None:
        self._people = people
        self._by_fec_id: dict[str, str] | None = None

    def existing_person_for(self, fec_candidate_id: str) -> str | None:
        """The stored `people` id carrying this FEC candidate id, if any."""
        if self._by_fec_id is None:
            self._by_fec_id = self._build()
        return self._by_fec_id.get(fec_candidate_id)

    def remember(self, *, fec_candidate_id: str, person_id: str) -> None:
        """Record a person this run just created, so the next filing reuses it."""
        if self._by_fec_id is None:
            self._by_fec_id = self._build()
        self._by_fec_id[fec_candidate_id] = person_id

    def _build(self) -> dict[str, str]:
        """Scan `people` once, mapping every FEC id it carries to its row.

        `external_ids["fec"]` is semicolon-joined by story 2.2's loader,
        because a member who has run for both chambers legitimately holds more
        than one FEC candidate id — so each part is indexed separately. That
        is also precisely what lets an incumbent's Senate run resolve to their
        existing `person:{bioguide}` row rather than minting a second person.
        """
        index: dict[str, str] = {}
        skip = 0
        while True:
            page = self._people.query(limit=PEOPLE_PAGE_SIZE, skip=skip)
            for person in page:
                person_id = person.id
                if not person_id:
                    continue
                joined = person.external_ids.get("fec")
                if not joined:
                    continue
                for part in str(joined).split(";"):
                    fec_id = part.strip()
                    if fec_id:
                        index[fec_id] = person_id
            if len(page) < PEOPLE_PAGE_SIZE:
                break
            skip += PEOPLE_PAGE_SIZE
        return index


class CandidacyLoader:
    """Writes candidacies and the rows they reference, paced and idempotent.

    One `load` call per batch of rows from `candidacies_from_results`. For each
    row this writes, in dependency order, whatever is missing:

      1. the `districts` row (reference; created from the filing's own
         state/district — see the module docstring's "Districts");
      2. the `people` row (reference; only for a filer not already known —
         see "Challengers are new people");
      3. the `candidacies` row itself, with `person_id` and `is_incumbent`
         resolved against the stored people.

    The order is not cosmetic: all three are declared references on
    `Candidacy`, and the repository refuses a write whose reference is absent
    (datamodel.md rule 5), so a candidacy written before its person simply
    fails.

    `pace` is the same injectable pacer every loader takes, called once per
    candidacy — which is per *filing*, covering all of its writes — because a
    cycle's national load is thousands of filings and several writes each,
    comfortably past the ~60 req/min ceiling if fired back to back.
    """

    def __init__(self, client: ZeroDBClient, *, pace: Callable[[], None] | None = None) -> None:
        self._pace = pace if pace is not None else (lambda: None)
        self._candidacies: Repository[Candidacy] = Repository(client=client, model=Candidacy)
        self._people: Repository[Person] = Repository(client=client, model=Person)
        self._districts: Repository[District] = Repository(client=client, model=District)
        self._person_index = _PersonIndex(self._people)
        #: District ids this run has already ensured, so a state's 30 filings
        #: do not each re-read the same district row.
        self._ensured_districts: set[str] = set()

    def load(self, rows: Sequence[dict[str, Any]]) -> LoadReport:
        """Write every row, reporting what was written and what was skipped.

        A row that cannot be written (a reference that will not resolve, a
        value the model rejects) is collected as a skip rather than raised, so
        one bad filing does not abandon the rest of the run — the same
        "fix the data and re-run" posture every prior loader takes. A
        `ZeroDBError` is NOT caught: a failing transport means the run's
        remaining writes would fail too, and pretending otherwise would
        report a successful load of nothing.
        """
        report = LoadReport()
        for row in rows:
            self._pace()
            try:
                self._load_one(row, report)
            except (ValueError, RepositoryError) as exc:
                candidate_id = (row.get("_fec") or {}).get("candidate_id")
                report.skipped.append(
                    SkippedCandidate(candidate_id=_str_or_none(candidate_id), reason=str(exc))
                )
                logger.warning("candidacies: could not write %r: %s", candidate_id, exc)
        return report

    def _load_one(self, row: dict[str, Any], report: LoadReport) -> None:
        row = dict(row)
        fec = row.pop("_fec", None) or {}
        candidate_id = str(fec.get("candidate_id") or "")
        if not candidate_id:
            raise ValueError("row carries no _fec.candidate_id; was it produced by this module?")

        district_id = str(row["district_id"])
        if self._ensure_district(
            district_id,
            office=str(fec.get("office") or ""),
            state=str(fec.get("state") or ""),
            district_number=fec.get("district_number"),
        ):
            report.districts_written.append(district_id)

        person_id, created = self._resolve_person(
            candidate_id=candidate_id, person_name=str(row["person_name"])
        )
        if created:
            report.people_written.append(person_id)

        row["person_id"] = person_id
        row["is_incumbent"] = self._is_incumbent_for(person_id=person_id, district_id=district_id)

        stored = self._candidacies.upsert(row)
        assert stored.id is not None  # noqa: S101 - upsert guarantees it
        report.candidacies_written.append(stored.id)
        logger.info("candidacies: wrote %s", stored.id)

    def _ensure_district(
        self, district_id: str, *, office: str, state: str, district_number: Any
    ) -> bool:
        """Make sure the district exists; True if this call created it.

        Only created when absent — unlike `app.districts.store.ensure`, this
        does NOT restate an existing row. That store writes the geocoder's own
        label, which is the better name for a district, and overwriting it with
        this module's constructed placeholder on every weekly run would
        downgrade real data on a schedule.
        """
        if district_id in self._ensured_districts:
            return False
        self._ensured_districts.add(district_id)
        if self._districts.get(district_id) is not None:
            return False
        body_id = _BODY_ID_FOR_OFFICE.get(office)
        if body_id is None:
            raise ValueError(f"office {office!r} has no known body_id")
        self._districts.upsert(
            {
                "id": district_id,
                "body_id": body_id,
                "name": district_name_for(
                    office=office, state=state, district_number=district_number
                ),
                "state": str(state).upper(),
                "valid_from": DEFAULT_DISTRICT_VALID_FROM,
            }
        )
        logger.info("candidacies: created districts row %s", district_id)
        return True

    def _resolve_person(self, *, candidate_id: str, person_name: str) -> tuple[str, bool]:
        """`(person_id, created)` for one filer.

        An existing person — any `people` row carrying this FEC candidate id —
        is reused and **not rewritten**: story 2.2 owns a sitting member's row,
        and restating it from this feed would replace their `terms[]` with an
        empty list, deleting the service record scoring reads.

        A filer not already known gets a new minimal `people` row under
        `person:fec-{candidate_id}` with no terms, because a challenger has
        never held this seat and inventing a term would put somebody in office
        who is not in office.
        """
        existing = self._person_index.existing_person_for(candidate_id)
        if existing is not None:
            return existing, False

        person_id = fec_person_id(candidate_id)
        if self._people.get(person_id) is not None:
            # Already created by an earlier run. Recorded in the index so a
            # second filing for the same person this run skips the read.
            self._person_index.remember(fec_candidate_id=candidate_id, person_id=person_id)
            return person_id, False

        self._people.upsert(
            {
                "id": person_id,
                "canonical_name": person_name,
                "external_ids": {"fec": candidate_id},
                "photo_url": None,
                "terms": [],
            }
        )
        self._person_index.remember(fec_candidate_id=candidate_id, person_id=person_id)
        logger.info("candidacies: created people row %s for FEC %s", person_id, candidate_id)
        return person_id, True

    def _is_incumbent_for(self, *, person_id: str, district_id: str) -> bool:
        """Whether this person currently holds this exact seat.

        Cross-referenced against the real legislators story 2.2 loaded, never
        taken from FEC's self-reported `incumbent_challenge` letter — see the
        module docstring's "Incumbency" for why both the person AND the seat
        have to match.

        A person with no stored terms (every challenger) is not incumbent,
        which falls out of the loop rather than needing a special case.
        """
        person = self._people.get(person_id)
        if person is None:
            return False
        return any(term.district_id == district_id for term in person.terms)


__all__ = [
    "ALL_STATES",
    "DEFAULT_DISTRICT_VALID_FROM",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "DEMO_API_KEY",
    "INGESTED_OFFICES",
    "OPENFEC_BASE_URL",
    "CandidacyLoader",
    "CandidacySourceError",
    "LoadReport",
    "SkippedCandidate",
    "candidacies_from_results",
    "candidacy_status_for",
    "district_name_for",
    "fec_person_id",
    "fetch_candidates",
    "ocd_district_id",
]
