"""Load House roll-call votes from the Congress.gov house-vote API (story 5.1).

What this is, and why it exists
================================

Story 2.2's `legislators.py` proved "fetch, transform to datamodel.md's
schema, pace through the repository layer, idempotent" as a pattern for an
ingest adapter. This module is the same pattern applied to prd.md's other
MVP source:

    US House | Congress.gov API house-vote | Roll calls and per-member
    positions, from the 118th Congress (2023) | Free API key

specifically the house-vote endpoints the Library of Congress added in May
2025, documented at
https://github.com/LibraryOfCongress/api.congress.gov/blob/main/Documentation/HouseRollCallVoteEndpoint.md
and announced at
https://blogs.loc.gov/law/2025/05/introducing-house-roll-call-votes-in-the-congress-gov-api/
(both fetched live during this story, 2026-10-16). This story's scope is the
`roll_calls` table only — per-member positions (the "member votes-level"
endpoint) are story 5.2's job, and bill ingest with sponsors is story 5.3's.

**No real Congress.gov API key was available this session.** `local.env` and
`.env.example` carry no `CONGRESS_GOV_API_KEY` or similar placeholder, and a
live, unauthenticated probe of `https://api.congress.gov/v3/house-vote/118/2/452`
during this story confirmed the host is reachable and the URL scheme is
correct, but every call -- including the OpenAPI schema itself -- answers
`403 API_KEY_MISSING` with no public fallback. Getting a key requires an
interactive signup at api.data.gov this agent cannot complete. This is a
genuine infrastructure blocker, the same shape as story 1.6's ZeroDB project
provisioning: this module is built and fully tested against a realistic
fixture (`tests/fixtures/house_rollcalls_sample.json`) built from the cited
documentation's exact field names and enum values, but it has NOT been run
against the live API, and `scripts/load_house_rollcalls.py` cannot be
exercised for real until a key exists. See this story's PR for the full
blocker writeup.

Shape of one source record (from the cited documentation, item level)
========================================================================

    {
      "houseRollCallVote": {
        "startDate": "2025-03-11T14:22:00-04:00",
        "updateDate": "2025-03-12T08:00:00-04:00",
        "identifier": 1192204520,
        "congress": 119,
        "sessionNumber": 2,
        "rollCallNumber": 452,
        "voteType": "Yea and Nay",
        "result": "Passed",
        "legislationType": "HR",            # or amendmentType: "HAMDT"
        "legislationNumber": "1234",         # or amendmentNumber
        "voteQuestion": "On Motion to Suspend the Rules and Pass",
        "amendmentAuthor": "...",            # only on an amendment vote
        "votePartyTotal": {"item": [{"party": {...}, "yeaTotal": ..., ...}]},
        "legislationUrl": "https://www.congress.gov/bill/...",
        "url": "https://api.congress.gov/v3/house-vote/119/2/452"
      }
    }

The list endpoint (`GET /v3/house-vote/{congress}/{session}`) returns the
same per-vote fields minus `voteQuestion`/`votePartyTotal` under a plural
`houseRollCallVotes` array -- this loader always calls the ITEM endpoint for
each roll number the list enumerates, because `votePartyTotal` (this story's
`tally`) and `voteQuestion` (this story's `vote_type` classification input)
are item-level-only fields per the documentation above.

Transform, field by field
==========================

  * **id** -- `us-house:{congress}-{session}:{roll}`, datamodel.md's
    documented pattern, built from `congress`, `sessionNumber`,
    `rollCallNumber` -- never from the source's own `identifier` (a
    differently-shaped int the source happens to publish) so the id matches
    the doc exactly rather than whatever the source calls its own key.
  * **voted_at** -- `startDate` verbatim (ISO 8601 with offset, already the
    shape datamodel.md's worked examples use elsewhere for timestamp fields).
  * **question** -- `voteQuestion` verbatim.
  * **vote_type** -- classified by `_classify_vote_type`, below.
  * **result** -- `result` verbatim ("Passed" / "Failed" / "Agreed to").
    datamodel.md types this field as a bare string, not a controlled
    vocabulary, so no mapping is needed.
  * **bill_id** -- always None in this story. datamodel.md's `bills` id is
    `{body}:{session}:{number}` and story 5.3 is what ingests `bills` rows at
    all; writing a `bill_id` this loader cannot verify exists would either
    violate `RollCall`'s declared `bill_id -> bills` reference (datamodel.md
    rule 5) or require `check_references=False`, silently disabling the
    exact check this story's data depends on later. Left null until 5.3
    exists and a follow-up re-ingest can backfill it -- the same posture
    `legislators.py` took leaving `photo_url` null for a source gap.
  * **amendment_ref** -- `"{amendmentType} {amendmentNumber}"` when the vote
    is on an amendment (e.g. `"HAMDT 6"`), else None.
  * **tally** -- summed from `votePartyTotal.item[]` across every party; see
    `_tally_from`. A vote with no `votePartyTotal` at all (the documentation
    does not guarantee it is always present) gets an all-zero `Tally()`
    rather than failing the whole record -- the same "a gap is a zero, not a
    crash" posture `legislators.py` takes for a missing `end` date.
  * **is_bundled** -- always False in this story. Nothing in the cited
    house-vote documentation names a "this vote covers multiple bills"
    field; House votes are one question per roll by construction (a bundled
    "en bloc" vote is still one `voteQuestion`), so there is no source signal
    to set this from. Left at the model's default rather than guessed at.
  * **topic_ids** -- always `[]` in this story. Mapping a roll call to
    `issue_topics` needs the bill's subject terms (story 5.3's `bills.subjects[]`,
    fed through a later classification story), which do not exist yet.
  * **source_url** -- the item endpoint's own `url` field: the exact resource
    this loader fetched, which is what datamodel.md's `source_url` and the
    integrity rule ("every row with a source_url has a matching
    provenance_log entry") are about -- not `legislationUrl`, which points at
    a different resource (the bill, not the vote).
  * **content_hash** -- sha256 of the raw fetched item JSON (object, not
    string -- see `content_hash_for`), so a byte-identical re-fetch hashes
    identically and a changed vote (a correction, per datamodel.md's own
    retention story) hashes differently. This is also reused as the
    provenance row's id -- see `write_provenance`.

vote_type classification
=========================

datamodel.md: "`vote_type` takes `final_passage`, `amendment`, `cloture`,
`motion_to_recommit`, `procedural`, or `other`". The source has no field
that maps onto this vocabulary directly -- it publishes `voteQuestion` (free
text, drawn from a documented but open-ended list) and, separately,
`amendmentType`/`amendmentNumber` when the vote is on an amendment. This
loader's classification, in priority order (`_classify_vote_type`):

  1. **amendment** -- `amendmentType == "HAMDT"` or `amendmentNumber` is
     present, REGARDLESS of `voteQuestion` wording. An amendment vote is
     unambiguous from this field alone and takes priority over the question
     text, which for an amendment vote is typically "On Agreeing to the
     Amendment" anyway.
  2. **final_passage** -- `voteQuestion` contains any of "suspend the rules
     and pass", "suspend the rules and agree", "on passage", "agreeing to
     the resolution" (not "as amended" alone -- "as amended" variants are
     still a passage question, just of the amended text, so they match too).
     These are the final-disposition questions the documentation's own
     `voteQuestion` option list names for passing a bill or resolution.
  3. **motion_to_recommit** -- `voteQuestion` contains "motion to recommit".
  4. **procedural** -- `voteQuestion` contains "previous question", "motion
     to reconsider", or "motion to table" -- parliamentary-process
     questions, not a vote on the substance of legislation.
  5. **other** -- anything else, including `voteQuestion` being absent
     entirely (the list endpoint's shape) or a future question type the
     House adds that this loader has never seen. Falling back rather than
     raising is deliberate: one newly-worded question must not stop the
     whole ingest run, exactly as `legislators.py` logs and skips one
     malformed legislator rather than failing the batch.

**Why nothing maps to `cloture`**: cloture is a Senate-only parliamentary
device (ending debate under Senate Rule XXII) -- the House has no cloture
motion at all, so no House roll call's `voteQuestion` will ever say
"cloture". The enum value exists in datamodel.md for the Senate side (story
8.x), and this loader simply never produces it. Documented here rather than
left as a silent gap so a reviewer does not read the missing case as an
oversight.

Idempotency and provenance
===========================

`roll_calls` is an ordinary (non-append-only) table: `RollCall.load` writes
it with `Repository.upsert`, exactly like `legislators.py` writes `people` --
re-ingesting the same roll call updates the existing row rather than
duplicating it (datamodel.md rule 1/2).

`provenance_log` is append-only (datamodel.md rule 6: "never updated in
place") AND, per this story's acceptance criterion, must gain "a provenance
entry" for every roll call ingested -- including a RE-ingested one, but
WITHOUT spamming a duplicate entry for data that did not actually change.
This story chose content-addressed idempotency (`prov:{sha256}` of the
fetched record, check-then-insert) over sequential versioning, and **story
16.3 promoted that choice into the repository layer**: see
`app.repositories.provenance`, which now holds the full reasoning and the
code, and `app.repositories.base.Repository._record_provenance`, which writes
the entry as part of the `roll_calls` write rather than as a second step this
loader has to remember.

What changed for this module, concretely: `HouseRollCallLoader` builds its
repository with a `ProvenanceContext(adapter_version=ADAPTER_VERSION)` and
calls `upsert` once. The row and its provenance entry are one call; the
behaviour (a byte-identical re-fetch records nothing, a corrected roll call
records a second entry) is unchanged and still asserted by
`tests/features/house_rollcalls.feature`. `content_hash_for` and
`write_provenance` are re-exported from here so this module's documented
surface is stable, but both now live in the repository layer where every
future source reaches them.

Fetching
========

`fetch_roll_call_numbers` (list endpoint, paged) and `fetch_roll_call_item`
(item endpoint, one roll at a time) are the only two functions that reach the
network, kept separate from `transform_roll_call` and `HouseRollCallLoader`
for the same reason `legislators.py` separates `fetch_legislators_yaml` from
`transform_legislators`: every other piece of this module is testable from
`tests/fixtures/house_rollcalls_sample.json` with no live HTTP call, and CI
must not depend on Congress.gov being reachable (CLAUDE.md). Only
`scripts/load_house_rollcalls.py` and a future live-verification script call
them for real -- and as this story's PR documents, neither has been run live
yet, for lack of a key.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.repositories import ProvenanceContext, Repository
from app.repositories.models import RollCall, Tally
from app.repositories.provenance import content_hash_for, write_provenance
from app.scoring.positions import ROLLCALL_INGESTED_TOPIC, emit_rollcall_ingested
from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBError

logger = logging.getLogger(__name__)

#: Base URL for the Congress.gov API v3 house-vote endpoints. Confirmed live
#: (2026-10-16): reachable, correct URL shape, answers 403 API_KEY_MISSING
#: with no key. See the module docstring's blocker note.
CONGRESS_GOV_BASE_URL = "https://api.congress.gov/v3"

#: datamodel.md's documented body id for this table's rows.
HOUSE_BODY = "us-house"

#: The adapter_version recorded on every provenance_log row this module
#: writes. Bumped when the transform's field mapping changes in a way that
#: would make an old provenance entry describe a different shape than this
#: version produces -- the same purpose `extractor_version`/`methodology_version`
#: serve elsewhere in datamodel.md.
ADAPTER_VERSION = "house_rollcalls/v1"

#: Earliest Congress this story's acceptance criterion covers ("every roll
#: call since the 118th Congress"), per prd.md's "Data sources and coverage"
#: table and the Library of Congress's own coverage note for this endpoint
#: ("House roll call votes in the 118th and 119th Congresses").
EARLIEST_CONGRESS = 118

#: The source's hard page cap on the LIST endpoint. Confirmed live
#: 2026-10-19: asking for `limit=300` still returns exactly 250 rows, so a
#: single call cannot enumerate a Congress (118-1 alone has 724). This is why
#: `fetch_roll_call_numbers` walks `offset` -- see its docstring.
LIST_PAGE_SIZE = 250

#: Which congress/session pairs story 5.8's backfill covers: "an incumbent's
#: full current-era record", which prd.md's coverage table and the Library of
#: Congress's own coverage note for this endpoint both put at the 118th
#: Congress onward.
#:
#: All four sessions confirmed live and non-empty on 2026-10-19, with the
#: source's own `pagination.count` for each:
#:
#:     118-1: 724 roll calls      119-1: 362 roll calls
#:     118-2: 517 roll calls      119-2: 314 roll calls
#:                                       ------------------
#:                                 total: 1,917 roll calls
#:
#: Ordered oldest-first so a long backfill's progress is chronological and a
#: run interrupted partway through has loaded a contiguous early stretch
#: rather than a scatter.
BACKFILL_CONGRESS_SESSIONS: tuple[tuple[int, int], ...] = (
    (118, 1),
    (118, 2),
    (119, 1),
    (119, 2),
)

#: Mirrors `app.ingest.legislators.DEFAULT_MIN_INTERVAL_SECONDS` against the
#: same ~60 req/min ceiling datamodel.md records -- this loader costs TWO
#: live requests per roll call ingested (the item fetch, then the
#: find-then-write upsert's own read), so pacing applies before each roll
#: call's full fetch-and-write, not just the write.
DEFAULT_MIN_INTERVAL_SECONDS = 1.1

#: `voteQuestion` substrings (lowercased) that classify as `final_passage`.
#: Order-independent; `_classify_vote_type` checks membership, not position.
_FINAL_PASSAGE_PHRASES: tuple[str, ...] = (
    "suspend the rules and pass",
    "suspend the rules and agree",
    "on passage",
    "agreeing to the resolution",
)

#: `voteQuestion` substrings that classify as `motion_to_recommit`.
_MOTION_TO_RECOMMIT_PHRASES: tuple[str, ...] = ("motion to recommit",)

#: `voteQuestion` substrings that classify as `procedural`.
_PROCEDURAL_PHRASES: tuple[str, ...] = (
    "previous question",
    "motion to reconsider",
    "motion to table",
)


class HouseRollCallSourceError(Exception):
    """The Congress.gov house-vote API could not be fetched or did not parse
    as expected."""


@dataclass
class LoadReport:
    """What one load run wrote, mirroring `app.ingest.legislators.LoadReport`.

    `roll_calls_written` holds every id this run upserted, whether or not the
    stored contents changed (an upsert always restates full current
    contents). `provenance_written` holds only the content-addressed ids that
    were ACTUALLY newly inserted this run -- re-ingesting unchanged data
    correctly adds nothing here, which is what the idempotency scenarios in
    `tests/features/house_rollcalls.feature` assert.
    """

    roll_calls_written: list[str] = field(default_factory=list)
    provenance_written: list[str] = field(default_factory=list)
    skipped: list[SkippedRollCall] = field(default_factory=list)
    #: Story 5.7: the roll call ids this run emitted a `rollcall.ingested`
    #: event for. A strict subset of `roll_calls_written` -- only roll calls
    #: whose content actually CHANGED get an event, so a rerun over unchanged
    #: data leaves this empty while `roll_calls_written` still lists every id
    #: the upsert restated. That difference is the "exactly once per roll
    #: call" criterion, measured.
    events_emitted: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.roll_calls_written)


@dataclass
class SkippedRollCall:
    """One source record this loader could not turn into a `RollCall` row."""

    roll_call_number: int | None
    reason: str


def _fetch_list_page(
    *,
    congress: int,
    session: int,
    api_key: str,
    offset: int,
    limit: int,
    base_url: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """One page of the LIST endpoint, as the raw parsed body.

    Raises:
        HouseRollCallSourceError: the request failed, did not come back 200,
            or the body did not contain a `houseRollCallVotes` array.
    """
    url = f"{base_url}/house-vote/{congress}/{session}"
    try:
        response = httpx.get(
            url,
            params={
                "api_key": api_key,
                "format": "json",
                "limit": limit,
                "offset": offset,
            },
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise HouseRollCallSourceError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise HouseRollCallSourceError(f"GET {url} returned {response.status_code}, expected 200")
    body = response.json()
    if not isinstance(body.get("houseRollCallVotes"), list):
        raise HouseRollCallSourceError(
            f"GET {url} did not return a houseRollCallVotes array: {body!r}"
        )
    return body


def fetch_source_total(
    *,
    congress: int,
    session: int,
    api_key: str,
    base_url: str = CONGRESS_GOV_BASE_URL,
    timeout_seconds: float = 30.0,
) -> int:
    """How many roll calls the SOURCE says exist for one congress/session.

    Reads `pagination.count` from the list endpoint, which is the source's
    own statement of its total and the only number story 5.8's acceptance
    criterion ("row counts match the source totals") can honestly be checked
    against. Confirmed live 2026-10-19: 724 for 118-1, 517 for 118-2, 362 for
    119-1, 314 for 119-2.

    Asked for with `limit=1`: the count is in the envelope, so there is no
    reason to transfer a page of records to read it.

    Raises:
        HouseRollCallSourceError: the request failed, or the body carried no
            readable `pagination.count`.
    """
    body = _fetch_list_page(
        congress=congress,
        session=session,
        api_key=api_key,
        offset=0,
        limit=1,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )
    pagination = body.get("pagination")
    count = pagination.get("count") if isinstance(pagination, dict) else None
    if not isinstance(count, int):
        raise HouseRollCallSourceError(
            f"the list endpoint for {congress}-{session} reported no pagination.count "
            f"(got {pagination!r}); story 5.8 cannot reconcile against an unknown total"
        )
    return count


def fetch_roll_call_numbers(
    *,
    congress: int,
    session: int,
    api_key: str,
    base_url: str = CONGRESS_GOV_BASE_URL,
    timeout_seconds: float = 30.0,
    page_size: int = LIST_PAGE_SIZE,
    pace: Callable[[], None] | None = None,
) -> list[int]:
    """EVERY roll-call number Congress.gov lists for one congress/session.

    Calls the LIST endpoint (`GET /house-vote/{congress}/{session}`) and
    returns every `rollCallNumber` it names -- the caller fetches each
    number's item separately, since the list level does not carry
    `voteQuestion`/`votePartyTotal` (see the module docstring).

    **Pages.** Story 5.1 asked for `limit=250` once and returned whatever
    came back. Confirmed live on 2026-10-19: 250 is a hard server-side cap
    (asking for 300 still returns 250) and 118-1 alone holds 724 roll calls,
    so that single call silently returned the first 34% of the Congress. A
    bounded hourly sync never noticed -- it only wanted the most recent
    handful -- but story 5.8's full backfill would have "completed" while
    missing two thirds of the record, which is exactly the failure its
    acceptance criterion exists to catch. This function now walks `offset`
    until the source stops returning rows, and cross-checks what it
    collected against `pagination.count`.

    Returns them de-duplicated and sorted descending (most recent first), so
    a caller taking `[:limit]` gets the newest roll calls -- which is what
    both the hourly sync and the CLI script already assumed by sorting the
    result themselves.

    Raises:
        HouseRollCallSourceError: a page failed, or the source's own
            `pagination.count` disagrees with how many distinct numbers the
            paged walk actually collected (a short read is not silently
            accepted -- see story 5.8).
    """
    pace = pace or (lambda: None)
    numbers: list[int] = []
    seen: set[int] = set()
    offset = 0
    expected: int | None = None

    while True:
        body = _fetch_list_page(
            congress=congress,
            session=session,
            api_key=api_key,
            offset=offset,
            limit=page_size,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
        if expected is None:
            pagination = body.get("pagination")
            if isinstance(pagination, dict) and isinstance(pagination.get("count"), int):
                expected = pagination["count"]

        page = body["houseRollCallVotes"]
        if not page:
            break

        new_this_page = 0
        for entry in page:
            number = entry.get("rollCallNumber") if isinstance(entry, dict) else None
            if isinstance(number, int) and number not in seen:
                seen.add(number)
                numbers.append(number)
                new_this_page += 1

        offset += len(page)
        if expected is not None and offset >= expected:
            break
        # Two independent termination guards, because an unbounded `while
        # True` against a remote source must not depend on that source
        # behaving. A SHORT page means the last page was reached (the
        # endpoint fills a page whenever it can). `new_this_page == 0` means
        # the source returned rows this walk has already seen -- which is
        # what a source that IGNORES `offset` does, and would otherwise loop
        # forever re-reading page one.
        if len(page) < page_size or new_this_page == 0:
            break
        pace()

    if expected is not None and len(numbers) != expected:
        raise HouseRollCallSourceError(
            f"the list endpoint for {congress}-{session} reports {expected} roll call(s) "
            f"but the paged walk collected {len(numbers)}; refusing to report a short "
            "listing as complete (story 5.8)"
        )
    return sorted(numbers, reverse=True)


def fetch_roll_call_item(
    *,
    congress: int,
    session: int,
    roll_call_number: int,
    api_key: str,
    base_url: str = CONGRESS_GOV_BASE_URL,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """One roll call's full item-level record, as the raw parsed JSON dict.

    Returns the whole response body (the `{"houseRollCallVote": {...}}`
    envelope), not just the inner object: `transform_roll_call` reads the
    inner object, but `content_hash_for` hashes the fetched record as the
    adapter received it, and the envelope is what was actually fetched.

    Raises:
        HouseRollCallSourceError: the request failed, did not come back 200,
            or the body had no `houseRollCallVote` object.
    """
    url = f"{base_url}/house-vote/{congress}/{session}/{roll_call_number}"
    try:
        response = httpx.get(
            url, params={"api_key": api_key, "format": "json"}, timeout=timeout_seconds
        )
    except httpx.HTTPError as exc:
        raise HouseRollCallSourceError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise HouseRollCallSourceError(f"GET {url} returned {response.status_code}, expected 200")
    body = response.json()
    if not isinstance(body.get("houseRollCallVote"), dict):
        raise HouseRollCallSourceError(
            f"GET {url} did not return a houseRollCallVote object: {body!r}"
        )
    return body


def roll_call_id(*, congress: int, session: int, roll_call_number: int) -> str:
    """datamodel.md's documented id: `us-house:{congress}-{session}:{roll}`."""
    return f"{HOUSE_BODY}:{congress}-{session}:{roll_call_number}"


def roll_call_item_url(
    *,
    congress: int,
    session: int,
    roll_call_number: int,
    base_url: str = CONGRESS_GOV_BASE_URL,
) -> str:
    """The item endpoint's own URL for one roll call.

    **Why this is derived rather than read from the record.** Story 5.1 read
    `source_url` from the record's own `url` field, per the documentation it
    was built from, and skipped any record without one. Confirmed live on
    2026-10-19 across 12 real item-level records: **no record carries a `url`
    field at all.** So `transform_roll_call` skipped 100% of live records --
    `transform_roll_calls` collected every one as a `SkippedRollCall` and the
    loader wrote nothing. A bounded hourly sync would have logged "0 roll
    call(s) written, 20 skipped" and exited 0.

    Deriving it is correct rather than a workaround, because this is
    precisely the URL the adapter fetched: `fetch_roll_call_item` builds the
    same string from the same three values. datamodel.md's `source_url` and
    the integrity rule it backs ("every row with a source_url has a matching
    provenance_log entry") are about the resource this adapter actually read,
    which is this URL whether or not the source chooses to echo it back.

    `legislationUrl` is deliberately NOT used as a fallback: it points at the
    bill, a different resource from the vote. The record's own `url` still
    wins when present, so a source that starts sending one is honoured.

    Mirrors `senate_rollcalls.roll_call_item_url`, which derives its own
    source_url the same way for the same reason (the LIS XML does not carry
    its own address either).
    """
    return f"{base_url}/house-vote/{congress}/{session}/{roll_call_number}"


def transform_roll_call(envelope: dict[str, Any]) -> dict[str, Any]:
    """One fetched item-level envelope into a `RollCall`-shaped row dict.

    Raises `_SkipRollCall` (caught by `transform_roll_calls`) rather than a
    bare `KeyError`/`ValueError` when a required field is missing, so one
    malformed record does not need its own try/except at every call site --
    mirrors `legislators.py`'s `_transform_one`/`_SkipLegislator` split.
    """
    vote = envelope.get("houseRollCallVote")
    if not isinstance(vote, dict):
        raise _SkipRollCall("envelope has no houseRollCallVote object")

    congress = vote.get("congress")
    session = vote.get("sessionNumber")
    roll = vote.get("rollCallNumber")
    if not isinstance(congress, int) or not isinstance(session, int) or not isinstance(roll, int):
        raise _SkipRollCall(
            f"record is missing congress/sessionNumber/rollCallNumber "
            f"(got congress={congress!r}, sessionNumber={session!r}, rollCallNumber={roll!r})"
        )

    voted_at = vote.get("startDate")
    if not voted_at:
        raise _SkipRollCall("record has no startDate")

    result = vote.get("result")
    if not result:
        raise _SkipRollCall("record has no result")

    source_url = vote.get("url") or roll_call_item_url(
        congress=congress, session=session, roll_call_number=roll
    )

    return {
        "id": roll_call_id(congress=congress, session=session, roll_call_number=roll),
        "voted_at": str(voted_at),
        "question": str(vote.get("voteQuestion") or ""),
        "vote_type": _classify_vote_type(vote),
        "result": str(result),
        "tally": _tally_from(vote),
        "bill_id": None,
        "amendment_ref": _amendment_ref(vote),
        "is_bundled": False,
        "topic_ids": [],
        "source_url": str(source_url),
        "content_hash": content_hash_for(envelope),
    }


class _SkipRollCall(Exception):
    """Internal signal: this one record cannot become a `RollCall` row."""


def transform_roll_calls(
    envelopes: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[SkippedRollCall]]:
    """`transform_roll_call` over a batch, collecting skips instead of raising.

    Mirrors `app.ingest.legislators.transform_legislators`'s contract exactly,
    for the same reason: one bad record in a batch of hundreds must not fail
    the other hundreds.
    """
    rows: list[dict[str, Any]] = []
    skipped: list[SkippedRollCall] = []
    for envelope in envelopes:
        try:
            rows.append(transform_roll_call(envelope))
        except _SkipRollCall as exc:
            number = None
            vote = envelope.get("houseRollCallVote")
            if isinstance(vote, dict):
                number = vote.get("rollCallNumber")
            skipped.append(SkippedRollCall(roll_call_number=number, reason=str(exc)))
            logger.warning("skipping roll call %r: %s", number, exc)
    return rows, skipped


def _amendment_ref(vote: dict[str, Any]) -> str | None:
    """`"{amendmentType} {amendmentNumber}"` when this vote is on an
    amendment, else None. See the module docstring's field-by-field notes.
    """
    amendment_type = vote.get("amendmentType")
    amendment_number = vote.get("amendmentNumber")
    if not amendment_type or not amendment_number:
        return None
    return f"{amendment_type} {amendment_number}"


def _party_total_items(vote: dict[str, Any]) -> list[Any] | None:
    """The per-party total entries, under either shape this source publishes.

    **Both shapes are real and this is the live-shape fix story 5.8 needed.**
    The documentation story 5.1 built its fixture from shows

        "votePartyTotal": {"item": [ {...}, {...} ]}

    but the endpoint actually returns a BARE LIST:

        "votePartyTotal": [ {...}, {...} ]

    confirmed across 12 real item-level records on 2026-10-19, with no
    record using the documented wrapper. Reading only the documented shape
    meant `_tally_from` fell through to `Tally()` on every live record, so
    every roll call would have stored yea 0 / nay 0 -- a silent zero, not an
    error, which is the worst kind: `RollCall` accepts it, the integrity
    checks have nothing to compare it against, and scoring would have read a
    unanimous-abstention House.

    Both shapes are accepted rather than the live one swapped in, because
    the wrapper is what the Library of Congress's own documentation
    specifies and a source that starts honouring its documentation must not
    break this adapter.
    """
    party_total = vote.get("votePartyTotal")
    if isinstance(party_total, list):
        return party_total
    if isinstance(party_total, dict):
        items = party_total.get("item")
        if isinstance(items, list):
            return items
        # A single-party vote can arrive as a lone object rather than a
        # one-element list; treated as a one-element list rather than
        # dropped.
        if isinstance(items, dict):
            return [items]
    return None


def _tally_from(vote: dict[str, Any]) -> Tally:
    """Sum the per-party totals across every party into one `Tally`.

    Returns an all-zero `Tally()` when the field is absent or empty, rather
    than raising -- see the module docstring's "tally" note. Each per-party
    total that is present and an int is added; a missing or non-numeric
    per-party field contributes 0 rather than crashing the whole record.
    """
    totals = {"yea": 0, "nay": 0, "present": 0, "not_voting": 0}
    items = _party_total_items(vote)
    if items is None:
        return Tally()
    for entry in items:
        if not isinstance(entry, dict):
            continue
        totals["yea"] += _as_int(entry.get("yeaTotal"))
        totals["nay"] += _as_int(entry.get("nayTotal"))
        totals["present"] += _as_int(entry.get("presentTotal"))
        totals["not_voting"] += _as_int(entry.get("notVotingTotal"))
    return Tally(**totals)


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _classify_vote_type(vote: dict[str, Any]) -> str:
    """datamodel.md's `vote_type` vocabulary, from this source's fields.

    See the module docstring's "vote_type classification" section for the
    full reasoning and priority order. Returns a plain string rather than
    the `VoteType` Literal directly so this function has no import-time
    dependency on the repository model; `RollCall.model_validate` is what
    actually enforces the vocabulary, at write time.
    """
    if vote.get("amendmentType") or vote.get("amendmentNumber"):
        return "amendment"

    question = str(vote.get("voteQuestion") or "").lower()
    if any(phrase in question for phrase in _FINAL_PASSAGE_PHRASES):
        return "final_passage"
    if any(phrase in question for phrase in _MOTION_TO_RECOMMIT_PHRASES):
        return "motion_to_recommit"
    if any(phrase in question for phrase in _PROCEDURAL_PHRASES):
        return "procedural"
    return "other"


class HouseRollCallLoader:
    """Writes transformed roll-call rows and their provenance, paced.

    One call to `load` per batch of already-transformed rows (the caller
    fetched and transformed them; this class only writes). Mirrors
    `app.ingest.legislators.LegislatorLoader`'s shape: pacing before each
    roll call's write, because an ingest of "every roll call since the 118th
    Congress" is thousands of rows, comfortably past the ~60 req/min ceiling
    datamodel.md records if fired back to back.
    """

    def __init__(self, client: ZeroDBClient, *, pace: Callable[[], None] | None = None) -> None:
        self._client = client
        self._pace = pace if pace is not None else (lambda: None)
        # Provenance is no longer this loader's second step -- story 16.3
        # promoted the pattern into the repository layer, so the `roll_calls`
        # write and its `provenance_log` entry are one call that cannot be
        # half-done by forgetting. `_newly_recorded` is where the context's
        # `on_write` callback deposits the entries this run actually inserted,
        # which is what `LoadReport.provenance_written` reports.
        self._newly_recorded: list[str] = []
        self._roll_calls: Repository[RollCall] = Repository(
            client=client,
            model=RollCall,
            provenance=ProvenanceContext(
                adapter_version=ADAPTER_VERSION,
                on_write=lambda entry: self._newly_recorded.append(str(entry.id)),
            ),
        )

    def load(self, rows: Sequence[dict[str, Any]]) -> LoadReport:
        """Upsert every roll call; the repository records its provenance.

        For each row:
          1. pace;
          2. `Repository.upsert` the `roll_calls` row (idempotent by id),
             which -- because `RollCall` carries a `source_url` and this
             repository was built with a `ProvenanceContext` -- also writes
             the matching `provenance_log` entry, keyed off the row's own
             `content_hash`. A row whose content has not changed since the
             last run writes NO new provenance entry; a row whose content HAS
             changed writes a second, distinct one. Same behaviour story 5.1
             implemented by hand, now a property of the write rather than a
             step this loader has to remember.

        `fetched_at` is stamped by the `ProvenanceContext`'s clock at write
        time -- when THIS loader fetched the data, which is distinct from
        `voted_at` (when the House voted) and cannot be derived from the
        row's own fields.
        """
        report = LoadReport()
        for row in rows:
            self._pace()
            self._newly_recorded.clear()
            stored = self._roll_calls.upsert(row)
            assert stored.id is not None  # noqa: S101 - upsert guarantees it
            report.roll_calls_written.append(stored.id)
            report.provenance_written.extend(self._newly_recorded)

            # Story 5.7: announce the roll call, exactly once.
            #
            # The trigger is `self._newly_recorded` -- did the repository
            # record a NEW provenance entry for this write -- rather than
            # "did `upsert` succeed". That is what makes "exactly once per
            # roll call" true without this loader keeping its own cursor:
            # provenance is content-addressed (`prov:{sha256}` of the fetched
            # record), so a byte-identical re-fetch records nothing and
            # therefore emits nothing, while a genuinely corrected roll call
            # records a second entry and correctly emits a second event. The
            # hourly sync (story 5.6) re-fetches the same recent window every
            # hour, so without this an incumbent's positions would be
            # recomputed ~24 times a day for no new evidence.
            #
            # Reusing the provenance signal rather than inventing a second
            # idempotency mechanism also means the two cannot disagree about
            # whether a roll call is new.
            if self._newly_recorded:
                self._emit_ingested(stored)
                report.events_emitted.append(stored.id)

            logger.info("house_rollcalls: wrote %s", stored.id)
        return report

    def _emit_ingested(self, stored: RollCall) -> None:
        """Emit datamodel.md's `rollcall.ingested` for one newly-stored roll call.

        Calls `app.scoring.positions.emit_rollcall_ingested`, which story 6.2
        wrote as the single definition of this documented payload
        (`roll_call_id`, `bill_id`, `topic_ids`) specifically so a producer
        would not spell it a second time. Story 6.2's own module docstring
        left the producing call to "story 5.1's loader" as a known gap and
        its scheduled job documented itself as "an honest no-op today because
        nothing emits `rollcall.ingested` yet" -- this is that gap closed.

        Emitted from the values that were actually STORED, not from the
        source row: `bill_id` and `topic_ids` are both still null/empty today
        (story 5.3 ingests `bills`, story 5.5 classifies topics), and when
        those stories land the event starts carrying their values with no
        change here. The consumer already tolerates an empty `topic_ids` by
        re-reading the stored row (see `PositionRecomputeConsumer.handle_event`).

        **A failed emit must not fail the ingest.** The roll call is already
        written and its provenance recorded; raising here would abandon the
        rest of the batch over an event the consumer polls for anyway, and
        the next genuine change re-emits. Logged at `error` so it is visible
        rather than silent.
        """
        assert stored.id is not None  # noqa: S101 - caller checked
        try:
            emit_rollcall_ingested(
                self._client,
                roll_call_id=stored.id,
                bill_id=stored.bill_id,
                topic_ids=stored.topic_ids,
            )
        except ZeroDBError as exc:
            logger.error(
                "house_rollcalls: wrote %s but could not emit %s: %s",
                stored.id,
                ROLLCALL_INGESTED_TOPIC,
                exc,
            )


@dataclass(frozen=True)
class Reconciliation:
    """Story 5.8's acceptance criterion, measured for one congress/session.

        backlog.md 5.8: "Backfill completes; row counts match the source totals."

    `expected` is the SOURCE's own total (`pagination.count` from the list
    endpoint); `stored` is how many `roll_calls` rows this project actually
    holds for that congress/session. `matches` is the criterion.

    This type exists because "the backfill ran without raising" is not the
    criterion and was never going to catch the two live-shape defects story
    5.8 actually found: with `source_url` missing from every record, story
    5.1's transform skipped 100% of live records and the loader exited 0
    having written nothing. A run that reports `stored=0, expected=724` is
    the honest version of that same run.
    """

    congress: int
    session: int
    stored: int
    expected: int

    @property
    def matches(self) -> bool:
        return self.stored == self.expected

    @property
    def missing(self) -> int:
        """How many of the source's roll calls are not stored. Negative would
        mean more rows stored than the source reports, which is itself a
        finding (a stale row under a since-renumbered id) rather than a pass.
        """
        return self.expected - self.stored

    def summary(self) -> str:
        verdict = "matches" if self.matches else f"MISMATCH ({self.missing:+d})"
        return (
            f"congress {self.congress} session {self.session}: "
            f"{self.stored} stored / {self.expected} at source -- {verdict}"
        )


def stored_roll_call_count(
    client: ZeroDBClient, *, congress: int, session: int, page_size: int = 1000
) -> int:
    """How many `roll_calls` rows this project holds for one congress/session.

    Counted by id prefix (`us-house:{congress}-{session}:`) over a paged
    scan, rather than with a server-side filter: story 1.4 found live
    ZeroDB's `/query` filter drops matching rows, and a reconciliation that
    undercounts because of that bug would report a false mismatch and send
    someone re-running a backfill that had in fact completed. The id pattern
    is deterministic (datamodel.md), so a prefix count is exact.

    Deliberately a full paged scan of `roll_calls` and not cheaper: the table
    holds ~1,900 rows for both Congresses (confirmed live), which is a few
    pages, and this runs once at the end of a backfill rather than per
    request.
    """
    prefix = f"{HOUSE_BODY}:{congress}-{session}:"
    count = 0
    skip = 0
    while True:
        page = client.query_rows(RollCall.table_name, limit=page_size, skip=skip)
        if not page.rows:
            break
        for row in page.rows:
            row_id = row.row_data.get("id")
            if isinstance(row_id, str) and row_id.startswith(prefix):
                count += 1
        skip += len(page.rows)
        if not page.has_more:
            break
    return count


def reconcile_congress_session(
    client: ZeroDBClient,
    *,
    congress: int,
    session: int,
    api_key: str,
    base_url: str = CONGRESS_GOV_BASE_URL,
) -> Reconciliation:
    """Compare stored `roll_calls` rows against the source's own total.

    Story 5.8's definition of done, as one call. See `Reconciliation`.
    """
    expected = fetch_source_total(
        congress=congress, session=session, api_key=api_key, base_url=base_url
    )
    stored = stored_roll_call_count(client, congress=congress, session=session)
    return Reconciliation(congress=congress, session=session, stored=stored, expected=expected)


__all__ = [
    "ADAPTER_VERSION",
    "BACKFILL_CONGRESS_SESSIONS",
    "CONGRESS_GOV_BASE_URL",
    "LIST_PAGE_SIZE",
    "Reconciliation",
    "fetch_source_total",
    "reconcile_congress_session",
    "roll_call_item_url",
    "stored_roll_call_count",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "EARLIEST_CONGRESS",
    "HOUSE_BODY",
    "HouseRollCallLoader",
    "HouseRollCallSourceError",
    "LoadReport",
    "SkippedRollCall",
    "content_hash_for",
    "fetch_roll_call_item",
    "fetch_roll_call_numbers",
    "roll_call_id",
    "transform_roll_call",
    "transform_roll_calls",
    "write_provenance",
]
