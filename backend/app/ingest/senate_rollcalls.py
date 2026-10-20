"""Load Senate roll-call votes from the senate.gov LIS XML feed (story 8.1).

What this is, and why it exists
================================

Story 5.1's `house_rollcalls.py` proved "fetch, transform to datamodel.md's
schema, pace through the repository layer, idempotent" against Congress.gov's
house-vote API, but that story never actually ran live: no
`CONGRESS_GOV_API_KEY` was available, so it shipped fully tested against a
hand-built fixture and nothing more. This story's source is different in the
one way that matters most for that gap:

    US Senate | senate.gov LIS roll-call XML feed | Senate roll calls;
    Congress.gov API exposes no Senate votes | Public XML | MVP

**No API key. No signup. Confirmed live and keyless during this story
(2026-10-19)**, by fetching real, current XML directly:

  * `https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml`
    -- the INDEX. Lists every `vote_number` cast in that congress/session
    (352 for 118-1, confirmed live), each with its own `question`, `result`
    and summary tally. This is how roll numbers are DISCOVERED -- never
    guessed or assumed contiguous from 1, even though in practice they are
    (confirmed: 118-1's numbers run 1..352 with no gap).
  * `https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/vote_{congress}_{session}_{roll:05d}.xml`
    -- one vote's full record: `question`, `vote_result`, the `document`
    (bill/amendment/nomination this vote is on), the `amendment` block, the
    summary `count`, and -- unlike the House API, which has no public
    member-level endpoint at all -- a `<members>` list with every senator's
    own `vote_cast` and `lis_member_id` INLINE in the same document. One
    fetch gets both the roll call and all 100 individual positions.

Confirmed directly against both the 118th Congress (`vote1181`,
`vote_118_1_00001.xml` through `_00352.xml`, 2023) and the 119th (`vote1192`,
2026) during this story -- congress 118 session 1, 118 session 2 (reachable,
not probed in depth) and 119 session 2 all resolve with HTTP 200 and parse as
expected. No `robots.txt` restriction and no published rate limit were found;
this loader paces identically to every other ingest adapter in this codebase
(`DEFAULT_MIN_INTERVAL_SECONDS`, the same ~60 req/min ceiling datamodel.md
records) out of politeness, not because senate.gov asked for it.

Shape of one source record (item level, captured live 2026-10-19)
====================================================================

    <roll_call_vote>
      <congress>119</congress>
      <session>2</session>
      <vote_number>254</vote_number>
      <vote_date>September 30, 2026,  ...</vote_date>
      <question>On Cloture on the Motion to Proceed</question>
      <vote_result>Cloture on the Motion to Proceed Rejected</vote_result>
      <document>
        <document_type>H.R.</document_type>
        <document_number>7008</document_number>
        <document_title>...</document_title>
      </document>
      <amendment>
        <amendment_number/>            <!-- "S.Amdt. 5803" when this vote IS on an amendment -->
        <amendment_purpose>...</amendment_purpose>
      </amendment>
      <count>
        <yeas>53</yeas><nays>47</nays><present/><absent>0</absent>
      </count>
      <members>
        <member>
          <last_name>Alsobrooks</last_name>
          <party>D</party><state>MD</state>
          <vote_cast>Nay</vote_cast>
          <lis_member_id>S428</lis_member_id>
        </member>
        ... (one per senator who cast a vote, confirmed 100 members on a
            full-attendance vote)
      </members>
    </roll_call_vote>

Confirmed `question` values across live samples this story fetched: "On the
Nomination", "On the Amendment", "On the Motion", "On Cloture on the Motion
to Proceed", "On Passage of the Bill", "On the Motion to Table". `document.
document_type` is one of a bill prefix (`S.`, `H.R.`, `H.J.Res.`, ...), `PN`
(a nomination), or absent; `amendment.amendment_number` is populated (e.g.
`"S.Amdt. 5803"`) exactly when this vote is on an amendment, mirroring the
House source's `amendmentType`/`amendmentNumber` pair closely enough that
amendment detection uses the same priority-one rule.

Transform, field by field (the `roll_calls` row)
==================================================

  * **id** -- `us-senate:{congress}-{session}:{roll}`, the same
    `{body}:{session}:{roll}` pattern `house_rollcalls.roll_call_id` uses,
    built from `congress`/`session`/`vote_number`, never from any id the
    source itself mints (it has none at the vote level beyond this triple).
  * **voted_at** -- `vote_date` verbatim. Unlike the House source, this is
    NOT ISO 8601 (`"September 30, 2026,  12:34 PM"`) -- stored as the exact
    string the source gives, consistent with `house_rollcalls`'s own posture
    of storing `voted_at` "verbatim" rather than reformatting a source's
    timestamp (datamodel.md types the field as a bare string, not a parsed
    datetime).
  * **question** -- `question` verbatim: the short field, which is the
    cleaner evidence-citation text. 8.1 stored this field and read only this
    field, calling `vote_question_text` redundant. **Story 8.3 found that
    judgment half wrong**: `question` is still the right thing to STORE, but
    105 live roll calls carry the bare `question` "On the Motion" for several
    different motions and only `vote_question_text` says which, so the
    classifier now reads both. The stored value is unchanged.
  * **vote_type** -- classified by `app.ingest.senate_vote_typing` (story
    8.3), via `classify_vote`/`_signals_from` below. This is where cloture, a
    real and common Senate vote type the House generally does not produce,
    actually gets exercised for the first time in this codebase.
  * **result** -- `vote_result` verbatim (e.g. "Cloture on the Motion to
    Proceed Rejected", "Nomination Confirmed", "Bill Passed"). Like House's
    `result`, datamodel.md types this as a bare string with no vocabulary.
  * **bill_id** -- always None in this story, same posture and same reason
    as `house_rollcalls.transform_roll_call`: datamodel.md's `bills` id is
    `{body}:{session}:{number}` and no story ingests Senate bills yet (5.3
    is House-only); writing an unverifiable `bill_id` would either violate
    `RollCall.references["bill_id"]` or require `check_references=False`.
    Also correctly None for the many Senate votes that are not on a bill at
    all (nominations, which have no `bills` row to reference regardless).
  * **amendment_ref** -- `amendment.amendment_number` verbatim when present
    and non-empty (already shaped like `"S.Amdt. 5803"`, so no reformatting
    needed -- contrast House, which builds the string from two separate
    fields), else None.
  * **tally** -- `count.yeas`/`count.nays`/`count.present`/`count.absent`.
    **`absent` maps to this codebase's `not_voting`**, not a new field:
    datamodel.md's `Tally` has exactly `{yea, nay, present, not_voting}` and
    the Senate LIS feed's "Absent" is semantically identical to the House
    feed's "Not Voting" -- a senator who cast no recorded position, for
    whatever reason (datamodel.md's "no recorded vote" rule treats both the
    same way at the scoring layer). A tally missing a sub-field entirely
    (confirmed possible: `<present/>` is a self-closing empty element on a
    vote where nobody voted "present") contributes 0, mirroring
    `house_rollcalls._tally_from`'s "a gap is a zero, not a crash".
  * **is_bundled** / **bundled_reason** -- **story 8.4 replaced 8.1's
    always-False with a real rule.** 8.1 wrote here that the LIS feed
    "carries no 'this vote covers multiple distinct bills' marker"; that
    turned out to be half right. The feed carries no dedicated FIELD, but it
    carries two usable signals that 8.4 reads: the measure's own
    `document_title` (whose text names an omnibus by construction -- "further
    consolidated appropriations", "continuing appropriations", "for
    reconciliation pursuant to", "en bloc consideration", all captured live),
    and `vote_question_text`, which on an en-bloc vote lists every measure
    disposed of (confirmed: 119-1:524 names 48 separate nominations). 8.1's
    guess that the signal would come from "the bill's own size/text rather
    than this feed" was wrong only about WHERE: it is the feed's own copy of
    the title, not a separate bill ingest. See
    `app.ingest.senate_vote_typing`'s 8.4 section for the rule and the
    reason codes.
  * **topic_ids** -- always `[]`, same reasoning as House: needs a bill's
    subject terms, which nothing ingests for the Senate yet.
  * **source_url** -- the exact item URL this loader fetched (built from
    `congress`/`session`/`roll_call_number`, since the LIS XML does not echo
    its own URL as a field the way Congress.gov's JSON does) -- what
    datamodel.md's integrity rule ("every row with a source_url has a
    matching provenance_log entry") is about.
  * **content_hash** -- sha256 of the raw fetched XML TEXT (not re-parsed or
    re-serialized -- `content_hash_for` normally hashes a JSON-able object,
    so this loader hashes `{"xml": raw_text}` rather than the parsed dict, to
    avoid a hash that silently changes if a future refactor reorders parsed
    fields without the underlying document changing at all).

vote_type classification: where it lives now
==============================================

datamodel.md: "`vote_type` takes `final_passage`, `amendment`, `cloture`,
`motion_to_recommit`, `procedural`, or `other`". `VoteType` (repositories/
models.py) already carried all six values body-agnostically when 8.1 was
written -- nothing in the model needed to change for it.

**Story 8.3 moved the rules out of this module into
`app.ingest.senate_vote_typing`**, and substantially extended them. The
reasoning for every rule, the priority order, the live evidence behind each
phrase, and the measured accuracy against a 75-roll-call labeled sample all
live in that module's docstring now, so there is one place to read and one
place to correct. What remains here is `_signals_from` (this source's XML ->
the classifier's plain strings) and `classify_vote` (the public seam).

Two things worth keeping in this module's own record, because they were
8.1's judgments and 8.3 confirmed both against live data:

  * **Cloture was 8.1's highest-value case and still is.** It is checked
    first, ahead of every other rule, because a cloture vote names the
    measure it is about and so every later rule would also match it. Live
    counts over congresses 118-119: 529 cloture votes out of 1,606. prd.md's
    "procedural vote" scenario ("B voted Nay on a cloture motion but Yea on
    final passage of the same bill / Then the verdict is 'Kept' with a
    'procedural vote' note") only works if those two votes on the same bill
    type differently, which is exactly what this ordering guarantees.
  * **A bare "On the Nomination" stays `other`.** 446 of 1,606 live roll
    calls, deliberately left unclassified rather than forced into
    `final_passage` (a nomination is not a vote on legislation) or
    `procedural` (it is substantive and consequential). datamodel.md's
    vocabulary has no "confirmation" value; adding one is a data-model and
    published-methodology change, not a classifier change. Recorded as an
    open question on 8.3's PR.

8.3 did reverse one of 8.1's orderings: **procedural is now checked BEFORE
amendment.** 8.1 gave the structural `amendment_number` signal priority over
the question text; two real live roll calls show that is wrong -- 119-2:17
("On the Motion to Table", S.Amdt. 4286) and 119-2:159 ("On the Motion",
"Motion to Waive All Applicable Budgetary Discipline Re: Cassidy Amdt. No.
5812", S.Amdt. 5812). Neither is a vote on the amendment's substance, and
8.1's order weighted both at 0.7 as if they were.

Member positions: this story's scope, and why it differs from 5.1's
========================================================================

`house_rollcalls.py`'s own docstring is explicit that 5.1's scope was
`roll_calls` ONLY, deliberately leaving `member_votes` to story 5.2 ("Member
positions"). backlog.md's entry for 5.1 lists `ZeroDB data: roll_calls,
provenance_log`.

**8.1's own backlog.md entry lists `ZeroDB data: roll_calls, member_votes,
provenance_log`** -- a deliberately WIDER scope than 5.1's, stated in the
backlog itself rather than inferred. This is also the only choice the live
source supports symmetrically with House: the House API's member-level
positions sit behind a different endpoint 5.2 was always going to need
separately, while the Senate LIS item XML already carries every senator's
`vote_cast` inline in the SAME document this loader fetches for `roll_calls`
-- there is no second fetch to defer. Leaving that data sitting unused in an
already-fetched response, when the backlog line for this exact story names
`member_votes` as in scope, would be the inconsistent choice, not this one.

**The one real gap this creates: `MemberVote.person_id` must resolve to a
bioguide id (`people` is keyed `person:{bioguide}`, per
`app.ingest.legislators`), but the LIS feed only gives `lis_member_id`
(e.g. "S428").** Story 8.2 ("LIS-to-bioguide crosswalk") is the story whose
explicit job is turning that mapping into a durable, written, alertable
`people.external_ids` crosswalk, with its own acceptance criterion ("100% of
sitting senators map; an unmapped id raises an alert") -- this loader does
NOT do that write, to avoid quietly completing 8.2's own deliverable inside
8.1's PR. What this loader DOES do is resolve `lis_member_id -> bioguide`
read-only, in memory, for the one purpose of writing a valid `member_votes`
row at all: `fetch_lis_to_bioguide_map` reads the *same*
`legislators-current.yaml` file `app.ingest.legislators.fetch_legislators_yaml`
already fetches from the same URL -- not a new, unknown source, just one
more field of a file this codebase already parses.

**A real finding from this story's own live run, corrected after the first
draft assumed otherwise: `id.lis` is a SENATE-ONLY identifier.** Fetching the
live file during this story found exactly 100 of its 539 records carry an
`id.lis` at all (every one of those 100 also carries `id.bioguide`) -- the
Senate's own Legislative Information System id, which House members never
receive. This is actually convenient for 8.2: the crosswalk it needs to make
durable and alertable is a ~100-row mapping, not a ~539-row one, and
`fetch_lis_to_bioguide_map`'s returned dict size is itself a live signal of
how close to "100% of sitting senators" the source data already is, before
8.2 adds its own write and alerting. A senator whose `lis_member_id` is not
found in that map is skipped (counted in `LoadReport.member_votes_skipped`,
logged, never raised) rather than written with a guessed or null
`person_id` -- the same "a gap is a skip, not a crash" posture every other
loader in this codebase takes, and exactly the
gap 8.2's "an unmapped id raises an alert" acceptance criterion exists to
close properly later.

Idempotency and provenance
=============================

`roll_calls` is written through `Repository.upsert` -- idempotent by id,
exactly like `house_rollcalls`. `member_votes` is written through
`Repository.upsert_many` (not a loop of `upsert`): every row in one roll
call's member list references the SAME `roll_calls` row, and `upsert_many`'s
reference cache resolves that id once per batch rather than once per senator
(100 senators, one roll call -- without the cache this is 100 redundant
reads against the same parent, straight into the ~60 req/min ceiling this
module already paces against for the fetch side).

`member_votes` carries no `source_url` of its own (datamodel.md does not list
one for that table -- it is a denormalized copy of its roll call's fields,
not an independently-sourced record), so `MemberVote`'s repository is built
with NO `ProvenanceContext`, exactly as `MissingProvenanceError` requires
(only models declaring `source_url` may be given one). `roll_calls`' own
repository IS given one, same as `house_rollcalls`, so each roll call's
provenance entry is written as part of that single `upsert` call.

Fetching
========

`fetch_vote_numbers` (the index) and `fetch_roll_call_xml` (one item, raw
text) are the only two functions that reach the network, kept separate from
`transform_roll_call`/`transform_member_votes` for the same reason
`house_rollcalls.py` separates fetch from transform: everything else is
testable from a captured fixture with no live HTTP call, and CI must not
depend on senate.gov being reachable (CLAUDE.md). Only
`scripts/load_senate_rollcalls.py`, `scripts/verify_senate_rollcalls_live.py`
and the scheduled job call them for real.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

import httpx
import yaml

from app.ingest.senate_vote_typing import (
    DEFAULT_BUNDLED_MEASURE_THRESHOLD,
    VoteSignals,
    VoteTypeDecision,
    classify,
    guard_recorded_vote,
)
from app.repositories import ProvenanceContext, Repository
from app.repositories.models import MemberVote, RollCall, Tally
from app.repositories.provenance import write_provenance
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: Base host for the senate.gov LIS roll-call feed. Confirmed live
#: (2026-10-19): both the index and item endpoints below are reachable with
#: no API key and no auth of any kind.
SENATE_LIS_BASE_URL = "https://www.senate.gov/legislative/LIS"

#: datamodel.md's documented body id for this table's rows.
SENATE_BODY = "us-senate"

#: The same `legislators-current.yaml` URL `app.ingest.legislators` already
#: fetches from -- confirmed live during this story to carry `id.lis`
#: alongside `id.bioguide` on exactly the ~100 Senate records (the Senate's
#: own LIS id is senate-only; the other ~439 House/territory records have
#: no `id.lis` at all), which is this loader's whole `lis_member_id ->
#: bioguide` resolution path. Not re-exported from
#: `app.ingest.legislators` on purpose: that module's own fetch function
#: returns the full legislator record shape its own transform needs: this
#: loader needs only the id crosswalk, and duplicating the one `httpx.get`
#: call rather than importing a cross-module dependency on a House/Senate
#: shared ingest module keeps each loader's fetch surface independently
#: testable, exactly as `house_rollcalls.py` and `legislators.py` already
#: are from each other.
LEGISLATORS_YAML_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators"
    "/main/legislators-current.yaml"
)

#: The adapter_version recorded on every provenance_log row this module
#: writes, mirroring `house_rollcalls.ADAPTER_VERSION`'s purpose.
ADAPTER_VERSION = "senate_rollcalls/v1"

#: Earliest Congress this story's acceptance criterion covers ("every roll
#: call since the 118th Congress"), per prd.md's "Data sources and coverage"
#: table -- confirmed live reachable for 118-1 and 118-2 during this story.
EARLIEST_CONGRESS = 118

#: Mirrors `house_rollcalls.DEFAULT_MIN_INTERVAL_SECONDS`: no published rate
#: limit was found for senate.gov (no restrictive robots.txt, no documented
#: ceiling), so this loader paces against the same ~60 req/min ceiling
#: datamodel.md records for every other live source out of politeness.
DEFAULT_MIN_INTERVAL_SECONDS = 1.1

#: Story 8.3 moved the classification phrase lists
#: (`_FINAL_PASSAGE_QUESTIONS`, `_MOTION_TO_RECOMMIT_PHRASES`,
#: `_PROCEDURAL_PHRASES`) to `app.ingest.senate_vote_typing`, together with
#: the priority order and the live evidence behind each rule. They are not
#: re-exported here: one definition, measured against a labeled sample in
#: one place.


class SenateRollCallSourceError(Exception):
    """The senate.gov LIS feed could not be fetched or did not parse as
    expected."""


@dataclass
class LoadReport:
    """What one load run wrote, mirroring `house_rollcalls.LoadReport`.

    `member_votes_written` and `member_votes_skipped` exist because this
    story's scope includes `member_votes` (unlike 5.1's `roll_calls`-only
    report) -- see the module docstring's "Member positions" section.
    """

    roll_calls_written: list[str] = field(default_factory=list)
    provenance_written: list[str] = field(default_factory=list)
    member_votes_written: list[str] = field(default_factory=list)
    member_votes_skipped: list[SkippedMemberVote] = field(default_factory=list)
    skipped: list[SkippedRollCall] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.roll_calls_written)


@dataclass
class SkippedRollCall:
    """One source record this loader could not turn into a `RollCall` row."""

    vote_number: int | None
    reason: str


@dataclass
class SkippedMemberVote:
    """One senator's position this loader could not turn into a
    `member_votes` row -- almost always an `lis_member_id` with no bioguide
    mapping (see the module docstring's "Member positions" section).
    """

    roll_call_id: str
    lis_member_id: str | None
    reason: str


# --- fetching ----------------------------------------------------------------


def _vote_dir(congress: int, session: int) -> str:
    """`vote{congress}{session}`, confirmed live (`vote1192`, `vote1181`)."""
    return f"vote{congress}{session}"


def roll_call_item_url(*, congress: int, session: int, vote_number: int, base_url: str) -> str:
    """The exact item URL for one vote, confirmed live during this story."""
    return (
        f"{base_url}/roll_call_votes/{_vote_dir(congress, session)}"
        f"/vote_{congress}_{session}_{vote_number:05d}.xml"
    )


def fetch_vote_numbers(
    *,
    congress: int,
    session: int,
    base_url: str = SENATE_LIS_BASE_URL,
    timeout_seconds: float = 30.0,
) -> list[int]:
    """Every `vote_number` the LIS index names for one congress/session.

    Calls `roll_call_lists/vote_menu_{congress}_{session}.xml` -- confirmed
    live during this story to list 352 votes for 118-1 with no gap in the
    range, and used here as the discovery mechanism rather than assuming
    numbers run contiguously from 1 (true in every sample checked, but never
    guaranteed by the source itself).

    Raises:
        SenateRollCallSourceError: the request failed or did not come back
            200, or the body did not parse as the expected XML shape.
    """
    url = f"{base_url}/roll_call_lists/vote_menu_{congress}_{session}.xml"
    try:
        response = httpx.get(url, timeout=timeout_seconds)
    except httpx.HTTPError as exc:
        raise SenateRollCallSourceError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise SenateRollCallSourceError(f"GET {url} returned {response.status_code}, expected 200")
    try:
        root = ElementTree.fromstring(response.text)
    except ElementTree.ParseError as exc:
        raise SenateRollCallSourceError(f"GET {url} did not parse as XML: {exc}") from exc
    if root.tag != "vote_summary":
        raise SenateRollCallSourceError(
            f"GET {url} returned a <{root.tag}>, expected <vote_summary>"
        )
    numbers: list[int] = []
    for vote_el in root.findall("./votes/vote"):
        number_text = vote_el.findtext("vote_number")
        if number_text and number_text.strip().isdigit():
            numbers.append(int(number_text))
    return numbers


def fetch_roll_call_xml(
    *,
    congress: int,
    session: int,
    vote_number: int,
    base_url: str = SENATE_LIS_BASE_URL,
    timeout_seconds: float = 30.0,
) -> str:
    """One vote's full item-level record, as the raw XML text.

    Returns raw text (not a parsed tree): `transform_roll_call` parses it,
    but `content_hash_for_xml` hashes exactly the bytes this loader fetched,
    so a byte-identical re-fetch hashes identically and any real change
    (even whitespace the source reformats) hashes differently -- the same
    content-addressed idempotency `house_rollcalls`/story 16.3 established,
    adapted for an XML source instead of JSON.

    Raises:
        SenateRollCallSourceError: the request failed, did not come back
            200, or did not parse as a `<roll_call_vote>` document.
    """
    url = roll_call_item_url(
        congress=congress, session=session, vote_number=vote_number, base_url=base_url
    )
    try:
        response = httpx.get(url, timeout=timeout_seconds)
    except httpx.HTTPError as exc:
        raise SenateRollCallSourceError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise SenateRollCallSourceError(f"GET {url} returned {response.status_code}, expected 200")
    try:
        root = ElementTree.fromstring(response.text)
    except ElementTree.ParseError as exc:
        raise SenateRollCallSourceError(f"GET {url} did not parse as XML: {exc}") from exc
    if root.tag != "roll_call_vote":
        raise SenateRollCallSourceError(
            f"GET {url} returned a <{root.tag}>, expected <roll_call_vote>"
        )
    return response.text


def fetch_lis_to_bioguide_map(
    *,
    url: str = LEGISLATORS_YAML_URL,
    timeout_seconds: float = 30.0,
) -> dict[str, str]:
    """`{lis_member_id: bioguide}` for every currently-serving senator.

    Reads the same source file `app.ingest.legislators.fetch_legislators_yaml`
    already fetches, but returns only the id pair this loader needs -- see
    the module docstring's "Member positions" section for why this is a
    read-only, in-memory lookup and not a write of story 8.2's own crosswalk
    table.

    **Returns roughly 100 entries, not ~539** -- confirmed live during this
    story: `id.lis` is a Senate-only identifier, present only on the file's
    ~100 Senate records (every one of which also carries `id.bioguide`); the
    file's House/territory records simply have no `id.lis` field, so they
    are correctly excluded here rather than being a sign of a parsing bug.
    A record missing either id is silently excluded from the map for the
    same "a gap is a skip, not a crash" reason -- the caller resolving a
    `lis_member_id` against this map treats "not found" as a skip, never a
    crash.

    Raises:
        SenateRollCallSourceError: the request failed, did not come back 200,
            or did not parse as a YAML list.
    """
    try:
        response = httpx.get(url, timeout=timeout_seconds)
    except httpx.HTTPError as exc:
        raise SenateRollCallSourceError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise SenateRollCallSourceError(f"GET {url} returned {response.status_code}, expected 200")
    try:
        records = yaml.safe_load(response.text)
    except yaml.YAMLError as exc:
        raise SenateRollCallSourceError(f"GET {url} did not parse as YAML: {exc}") from exc
    if not isinstance(records, list):
        raise SenateRollCallSourceError(f"GET {url} did not return a YAML list")

    mapping: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        ids = record.get("id")
        if not isinstance(ids, dict):
            continue
        lis_id = ids.get("lis")
        bioguide = ids.get("bioguide")
        if isinstance(lis_id, str) and isinstance(bioguide, str):
            mapping[lis_id] = bioguide
    return mapping


# --- transform: roll_calls ---------------------------------------------------


def roll_call_id(*, congress: int, session: int, vote_number: int) -> str:
    """datamodel.md's documented id: `us-senate:{congress}-{session}:{roll}`."""
    return f"{SENATE_BODY}:{congress}-{session}:{vote_number}"


def content_hash_for_xml(raw_xml: str) -> str:
    """sha256 of the exact fetched XML text -- see `fetch_roll_call_xml`'s
    docstring for why this hashes raw text rather than a re-parsed dict."""
    return hashlib.sha256(raw_xml.encode("utf-8")).hexdigest()


class _SkipRollCall(Exception):
    """Internal signal: this one record cannot become a `RollCall` row."""


def transform_roll_call(
    raw_xml: str,
    *,
    source_url: str,
    bundled_measure_threshold: int = DEFAULT_BUNDLED_MEASURE_THRESHOLD,
) -> dict[str, Any]:
    """One fetched `<roll_call_vote>` document into a `RollCall`-shaped row
    dict. Raises `_SkipRollCall` (caught by `transform_roll_calls`) rather
    than a bare parse error, mirroring `house_rollcalls.transform_roll_call`.

    `vote_type`, `is_bundled` and `bundled_reason` all come from
    `app.ingest.senate_vote_typing.classify` (stories 8.3 and 8.4) --
    `bundled_measure_threshold` is 8.4's "configurable size", passed through
    so a caller can tune it without reaching into the classifier.
    """
    try:
        root = ElementTree.fromstring(raw_xml)
    except ElementTree.ParseError as exc:
        raise _SkipRollCall(f"could not parse XML: {exc}") from exc

    congress = _as_int(root.findtext("congress"))
    session = _as_int(root.findtext("session"))
    vote_number = _as_int(root.findtext("vote_number"))
    if congress is None or session is None or vote_number is None:
        raise _SkipRollCall(
            f"record is missing congress/session/vote_number "
            f"(got congress={root.findtext('congress')!r}, "
            f"session={root.findtext('session')!r}, "
            f"vote_number={root.findtext('vote_number')!r})"
        )

    voted_at = root.findtext("vote_date")
    if not voted_at:
        raise _SkipRollCall("record has no vote_date")

    result = root.findtext("vote_result")
    if not result:
        raise _SkipRollCall("record has no vote_result")

    # Story 8.5: a voice-vote or unanimous-consent disposition must never be
    # minted into a roll_calls row -- see senate_vote_typing's 8.5 section.
    # In practice unreachable from THIS source (a voice vote gets no roll
    # number, so the LIS index never lists it), and checked anyway because
    # "unreachable" is a property of the current feed, not of the rule.
    try:
        guard_recorded_vote(result)
    except Exception as exc:  # SkippedUnrecordedOutcome
        raise _SkipRollCall(str(exc)) from exc

    question = root.findtext("question") or ""
    decision = classify(_signals_from(root), bundled_measure_threshold=bundled_measure_threshold)

    return {
        "id": roll_call_id(congress=congress, session=session, vote_number=vote_number),
        "voted_at": voted_at.strip(),
        "question": question.strip(),
        "vote_type": decision.vote_type,
        "result": result.strip(),
        "tally": _tally_from(root),
        "bill_id": None,
        "amendment_ref": _amendment_ref(root),
        "is_bundled": decision.is_bundled,
        "bundled_reason": decision.bundled_reason,
        "topic_ids": [],
        "source_url": source_url,
        "content_hash": content_hash_for_xml(raw_xml),
    }


def transform_roll_calls(
    documents: Iterable[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[SkippedRollCall]]:
    """`transform_roll_call` over a batch of `(raw_xml, source_url)` pairs,
    collecting skips instead of raising -- mirrors
    `house_rollcalls.transform_roll_calls`'s contract exactly.
    """
    rows: list[dict[str, Any]] = []
    skipped: list[SkippedRollCall] = []
    for raw_xml, source_url in documents:
        try:
            rows.append(transform_roll_call(raw_xml, source_url=source_url))
        except _SkipRollCall as exc:
            number = None
            try:
                root = ElementTree.fromstring(raw_xml)
                number = _as_int(root.findtext("vote_number"))
            except ElementTree.ParseError:
                pass
            skipped.append(SkippedRollCall(vote_number=number, reason=str(exc)))
            logger.warning("skipping Senate roll call %r: %s", number, exc)
    return rows, skipped


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    stripped = value.strip()
    return int(stripped) if stripped.isdigit() else None


def _amendment_ref(root: ElementTree.Element) -> str | None:
    """`amendment/amendment_number` verbatim when present and non-empty, else
    None. Already shaped like `"S.Amdt. 5803"` -- see the module docstring's
    field-by-field notes for why this needs no reformatting, unlike House's
    `_amendment_ref`.
    """
    text = root.findtext("amendment/amendment_number")
    return text.strip() if text and text.strip() else None


def _tally_from(root: ElementTree.Element) -> Tally:
    """`count.yeas/nays/present/absent` into one `Tally`, mapping `absent` to
    this codebase's `not_voting` -- see the module docstring's "tally" note.
    A missing or non-numeric sub-field contributes 0 rather than crashing the
    whole record, mirroring `house_rollcalls._tally_from`.
    """
    return Tally(
        yea=_count_int(root, "yeas"),
        nay=_count_int(root, "nays"),
        present=_count_int(root, "present"),
        not_voting=_count_int(root, "absent"),
    )


def _count_int(root: ElementTree.Element, tag: str) -> int:
    text = root.findtext(f"count/{tag}")
    if text is None:
        return 0
    stripped = text.strip()
    return int(stripped) if stripped.isdigit() else 0


def _signals_from(root: ElementTree.Element) -> VoteSignals:
    """Every field `senate_vote_typing.classify` reads, off one parsed item.

    The one adapter between this module's XML and the classifier's plain
    strings. Keeping it here rather than in `senate_vote_typing` is what lets
    that module stay source-agnostic (and therefore measurable from a
    fixture with no XML at all) -- see its module docstring's "Signals"
    section.
    """
    return VoteSignals.from_source_fields(
        question=root.findtext("question"),
        vote_question_text=root.findtext("vote_question_text"),
        vote_result=root.findtext("vote_result"),
        vote_title=root.findtext("vote_title"),
        document_type=root.findtext("document/document_type"),
        document_number=root.findtext("document/document_number"),
        document_title=root.findtext("document/document_title"),
        amendment_number=root.findtext("amendment/amendment_number"),
    )


def classify_vote(
    root: ElementTree.Element,
    *,
    bundled_measure_threshold: int = DEFAULT_BUNDLED_MEASURE_THRESHOLD,
) -> VoteTypeDecision:
    """This source's full classification for one parsed item (8.3 + 8.4).

    The public seam for a caller that wants the decision -- vote type, the
    rule that fired, the bundled flag and its reason -- without going
    through the whole `transform_roll_call`. The live verification script
    uses it to report classifications for real roll calls.
    """
    return classify(_signals_from(root), bundled_measure_threshold=bundled_measure_threshold)


def _classify_vote_type(root: ElementTree.Element, question: str) -> str:
    """datamodel.md's `vote_type` vocabulary, from this source's fields.

    **Story 8.3 moved the rules themselves to
    `app.ingest.senate_vote_typing`**, which is where the priority order,
    the live evidence behind each rule, and the measured accuracy against a
    labeled sample now live. This wrapper remains because 8.1's own tests
    and call sites name it, and because it keeps the XML-element signature
    (`question` is now redundant -- it is read off `root` -- and is accepted
    for signature compatibility with 8.1's callers).

    Returns a plain string rather than the `VoteType` Literal, same posture
    as before: `RollCall.model_validate` enforces the vocabulary at write
    time.
    """
    return classify_vote(root).vote_type


# --- transform: member_votes --------------------------------------------------


@dataclass
class _MemberCast:
    lis_member_id: str
    vote_cast: str


def _members_from(root: ElementTree.Element) -> list[_MemberCast]:
    """Every `<member>` element's `lis_member_id`/`vote_cast` pair, in
    document order. A member missing either field is silently excluded here
    (not every historical record is guaranteed clean) -- the same "a gap is
    a skip, not a crash" posture this module takes throughout.
    """
    members: list[_MemberCast] = []
    for member_el in root.findall("./members/member"):
        lis_id = member_el.findtext("lis_member_id")
        cast = member_el.findtext("vote_cast")
        if lis_id and lis_id.strip() and cast and cast.strip():
            members.append(_MemberCast(lis_member_id=lis_id.strip(), vote_cast=cast.strip()))
    return members


#: senate.gov LIS `vote_cast` values -> datamodel.md's `VotePosition`
#: vocabulary. Confirmed live: "Yea", "Nay", "Not Voting" all occur in
#: samples this story fetched; "Present" did not occur in any live sample
#: fetched but is a documented LIS value (the Senate equivalent of the House
#: feed's own rare "Present" case) and is mapped here rather than left to
#: fall through to `other`.
_VOTE_CAST_TO_POSITION: dict[str, str] = {
    "yea": "yea",
    "nay": "nay",
    "present": "present",
    "not voting": "not_voting",
}


def transform_member_votes_for(
    row: dict[str, Any],
    raw_xml: str,
    *,
    bioguide_by_lis_id: dict[str, str],
) -> tuple[list[dict[str, Any]], list[SkippedMemberVote]]:
    """Every senator's position on one already-transformed roll call `row`
    into `MemberVote`-shaped row dicts, resolved against `bioguide_by_lis_id`.

    `row` must be the dict `transform_roll_call` produced for this same
    `raw_xml` -- its `id`, `voted_at`, `vote_type`, `bill_id`, `topic_ids`
    and `is_bundled` are copied onto every member row verbatim, per
    datamodel.md's documented denormalization (the italicised fields on
    `member_votes`).

    `bioguide_by_lis_id` is `fetch_lis_to_bioguide_map`'s result (or a test
    fixture's equivalent). A member whose `lis_member_id` is not present in
    it, or whose `vote_cast` is not in `_VOTE_CAST_TO_POSITION`, is skipped
    and recorded in the returned list, never raised -- one senator's
    unmapped id or unrecognized cast must not fail the other 99 rows on the
    same roll call.
    """
    try:
        root = ElementTree.fromstring(raw_xml)
    except ElementTree.ParseError as exc:
        return [], [
            SkippedMemberVote(
                roll_call_id=row["id"], lis_member_id=None, reason=f"could not parse XML: {exc}"
            )
        ]

    rows: list[dict[str, Any]] = []
    skipped: list[SkippedMemberVote] = []
    for member in _members_from(root):
        bioguide = bioguide_by_lis_id.get(member.lis_member_id)
        if bioguide is None:
            skipped.append(
                SkippedMemberVote(
                    roll_call_id=row["id"],
                    lis_member_id=member.lis_member_id,
                    reason="no bioguide mapping for this lis_member_id (story 8.2's crosswalk)",
                )
            )
            continue
        position = _VOTE_CAST_TO_POSITION.get(member.vote_cast.lower())
        if position is None:
            skipped.append(
                SkippedMemberVote(
                    roll_call_id=row["id"],
                    lis_member_id=member.lis_member_id,
                    reason=f"unrecognized vote_cast {member.vote_cast!r}",
                )
            )
            continue
        rows.append(
            {
                "roll_call_id": row["id"],
                "person_id": f"person:{bioguide}",
                "position": position,
                "voted_at": row["voted_at"],
                "vote_type": row["vote_type"],
                "bill_id": row["bill_id"],
                "topic_ids": row["topic_ids"],
                "is_bundled": row["is_bundled"],
                # Story 8.4: the reason travels with the flag, so the screen
                # that reads member_votes by (person, topic) can explain the
                # discount without a second read of roll_calls. `.get` rather
                # than `[...]` so a row built by 8.1-era code (no such key)
                # still transforms -- it simply carries no reason.
                "bundled_reason": row.get("bundled_reason"),
            }
        )
    return rows, skipped


# --- loading -------------------------------------------------------------------


class SenateRollCallLoader:
    """Writes transformed roll-call rows, their member votes, and provenance,
    paced. Mirrors `house_rollcalls.HouseRollCallLoader`'s shape for
    `roll_calls`, and additionally writes `member_votes` per this story's
    wider scope (see the module docstring's "Member positions" section).
    """

    def __init__(self, client: ZeroDBClient, *, pace: Callable[[], None] | None = None) -> None:
        self._client = client
        self._pace = pace if pace is not None else (lambda: None)
        self._newly_recorded: list[str] = []
        self._roll_calls: Repository[RollCall] = Repository(
            client=client,
            model=RollCall,
            provenance=ProvenanceContext(
                adapter_version=ADAPTER_VERSION,
                on_write=lambda entry: self._newly_recorded.append(str(entry.id)),
            ),
        )
        # member_votes carries no source_url (a denormalized copy, not an
        # independently-sourced record -- see the module docstring), so this
        # repository is built with NO ProvenanceContext, exactly as
        # MissingProvenanceError requires.
        self._member_votes: Repository[MemberVote] = Repository(client=client, model=MemberVote)

    def load(
        self,
        rows: Sequence[dict[str, Any]],
        *,
        member_vote_rows_by_roll_call: dict[str, list[dict[str, Any]]] | None = None,
    ) -> LoadReport:
        """Upsert every roll call (and its provenance), then every member
        vote for it.

        `member_vote_rows_by_roll_call` maps a roll call's `id` to the list
        `transform_member_votes_for` produced for it; omitted or missing
        entries simply write no `member_votes` for that roll call (the
        caller's choice -- e.g. a test exercising `roll_calls` alone).
        """
        member_vote_rows_by_roll_call = member_vote_rows_by_roll_call or {}
        report = LoadReport()
        for row in rows:
            self._pace()
            self._newly_recorded.clear()
            stored = self._roll_calls.upsert(row)
            assert stored.id is not None  # noqa: S101 - upsert guarantees it
            report.roll_calls_written.append(stored.id)
            report.provenance_written.extend(self._newly_recorded)
            logger.info("senate_rollcalls: wrote %s", stored.id)

            member_rows = member_vote_rows_by_roll_call.get(stored.id, [])
            if member_rows:
                self._pace()
                written = self._member_votes.upsert_many(member_rows)
                report.member_votes_written.extend(str(m.id) for m in written)
        return report


__all__ = [
    "ADAPTER_VERSION",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "EARLIEST_CONGRESS",
    "LEGISLATORS_YAML_URL",
    "SENATE_BODY",
    "SENATE_LIS_BASE_URL",
    "LoadReport",
    "SenateRollCallLoader",
    "SenateRollCallSourceError",
    "SkippedMemberVote",
    "SkippedRollCall",
    "classify_vote",
    "content_hash_for_xml",
    "fetch_lis_to_bioguide_map",
    "fetch_roll_call_xml",
    "fetch_vote_numbers",
    "roll_call_id",
    "roll_call_item_url",
    "transform_member_votes_for",
    "transform_roll_call",
    "transform_roll_calls",
    "write_provenance",
]
