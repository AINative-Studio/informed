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

Idempotency and provenance -- the design this story had to choose
=====================================================================

`roll_calls` is an ordinary (non-append-only) table: `RollCall.load` writes
it with `Repository.upsert`, exactly like `legislators.py` writes `people` --
re-ingesting the same roll call updates the existing row rather than
duplicating it (datamodel.md rule 1/2).

`provenance_log` is append-only (datamodel.md rule 6: "never updated in
place") AND, per this story's acceptance criterion, must gain "a provenance
entry" for every roll call ingested -- including a RE-ingested one, but
WITHOUT spamming a duplicate entry for data that did not actually change.
Two designs were available:

  * Treat it like `Verdict`/`CandidatePosition` and run every write through
    `Repository.append_version`, numbering `prov:{roll_call_id}:v{n}`. Rejected:
    datamodel.md's documented id for this table is `prov:{sha256}`, not
    `prov:{entity_id}:v{n}` -- forcing the sequential-version shape onto a
    table documented with a content-addressed one would store an id nothing
    else in this codebase expects, and `append_version`'s "exactly one
    `is_current` row" bookkeeping has no meaning here (the write that reads
    "every stored version" to number the next one is solving a problem this
    table's id scheme does not have).
  * **Chosen: content-addressed idempotency.** The provenance row's id is
    `prov:{content_hash}`, where `content_hash` is the sha256 of the exact
    fetched record (the same hash stored on the `roll_calls` row). Writing it
    is `write_provenance`, below: read by that id first; insert ONLY if
    absent. A byte-identical re-fetch produces the same hash, so it lands on
    the row that already exists and nothing new is written -- "append-only"
    here means "once this exact fetch is recorded, it is never recorded a
    second time or edited", which is a stronger, simpler reading of
    datamodel.md's rule than sequential versioning would give, and it is
    also why `ProvenanceLog` (`app.repositories.models`) carries no
    `version_key`: there is no "current version" to track, only "has this
    exact content been logged yet". A genuinely CHANGED roll call (a
    correction to its result, say) hashes differently and therefore writes a
    SECOND, distinct provenance row -- which is correct: the record now has
    two fetches on file, at two different times, and datamodel.md's
    reproducibility goal ("what did Informed see, and when") is served by
    keeping both, never by overwriting the first.

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

import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.repositories import Repository
from app.repositories.models import ProvenanceLog, RollCall, Tally
from app.zerodb.client import ZeroDBClient

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

    @property
    def row_count(self) -> int:
        return len(self.roll_calls_written)


@dataclass
class SkippedRollCall:
    """One source record this loader could not turn into a `RollCall` row."""

    roll_call_number: int | None
    reason: str


def fetch_roll_call_numbers(
    *,
    congress: int,
    session: int,
    api_key: str,
    base_url: str = CONGRESS_GOV_BASE_URL,
    timeout_seconds: float = 30.0,
) -> list[int]:
    """The roll-call numbers Congress.gov lists for one congress/session.

    Calls the LIST endpoint (`GET /house-vote/{congress}/{session}`) and
    returns every `rollCallNumber` it names, in whatever order the source
    gives them -- the caller fetches each number's item separately, since the
    list level does not carry `voteQuestion`/`votePartyTotal` (see the module
    docstring).

    Raises:
        HouseRollCallSourceError: the request failed or did not come back
            200, or the body did not contain a `houseRollCallVotes` array.
    """
    url = f"{base_url}/house-vote/{congress}/{session}"
    try:
        response = httpx.get(
            url,
            params={"api_key": api_key, "format": "json", "limit": 250},
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise HouseRollCallSourceError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise HouseRollCallSourceError(f"GET {url} returned {response.status_code}, expected 200")
    body = response.json()
    votes = body.get("houseRollCallVotes")
    if not isinstance(votes, list):
        raise HouseRollCallSourceError(
            f"GET {url} did not return a houseRollCallVotes array: {body!r}"
        )
    numbers: list[int] = []
    for entry in votes:
        number = entry.get("rollCallNumber")
        if isinstance(number, int):
            numbers.append(number)
    return numbers


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


def content_hash_for(record: dict[str, Any]) -> str:
    """The sha256 hex digest of one fetched record, for `content_hash` and
    the provenance row's content-addressed id.

    Hashed via `json.dumps(..., sort_keys=True)` rather than the raw response
    bytes: key order in a JSON object carries no meaning, and hashing the
    raw bytes would make two byte-for-byte-different-but-semantically-equal
    responses (a reordered object, different whitespace) hash differently --
    exactly the spurious "content changed" this story's idempotency design
    depends on NOT happening. `sort_keys=True` makes the hash a function of
    the data, not of the source's serialization choices.
    """
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def roll_call_id(*, congress: int, session: int, roll_call_number: int) -> str:
    """datamodel.md's documented id: `us-house:{congress}-{session}:{roll}`."""
    return f"{HOUSE_BODY}:{congress}-{session}:{roll_call_number}"


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

    source_url = vote.get("url")
    if not source_url:
        raise _SkipRollCall("record has no url (this story's source_url)")

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


def _tally_from(vote: dict[str, Any]) -> Tally:
    """Sum `votePartyTotal.item[]` across every party into one `Tally`.

    Returns an all-zero `Tally()` when the field is absent or empty, rather
    than raising -- see the module docstring's "tally" note. Each per-party
    total that is present and an int is added; a missing or non-numeric
    per-party field contributes 0 rather than crashing the whole record.
    """
    totals = {"yea": 0, "nay": 0, "present": 0, "not_voting": 0}
    party_total = vote.get("votePartyTotal")
    items = party_total.get("item") if isinstance(party_total, dict) else None
    if not isinstance(items, list):
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


def write_provenance(
    client: ZeroDBClient,
    *,
    entity_table: str,
    entity_id: str,
    source_url: str,
    fetched_at: str,
    content_hash: str,
    adapter_version: str = ADAPTER_VERSION,
) -> ProvenanceLog | None:
    """Insert one provenance_log row if, and only if, it is not already there.

    See the module docstring's "Idempotency and provenance" section for why
    this is a plain check-then-insert through `ZeroDBClient` rather than
    `Repository.append_version`: `provenance_log`'s id is content-addressed
    (`prov:{content_hash}`), not sequentially versioned, so there is no
    version number to compute here. `Repository.upsert` is equally wrong --
    it would make this table non-append-only in spirit even though it could
    never actually overwrite a differently-hashed row under the same id
    (the ids would simply never collide for different content).

    Returns the written row, or None when a row with this exact content hash
    already existed (nothing was written) -- the caller uses this to decide
    whether to count it in `LoadReport.provenance_written`.
    """
    row_id = f"prov:{content_hash}"
    existing = client.get_row("provenance_log", row_id)
    if existing is not None:
        return None
    row = ProvenanceLog.model_validate(
        {
            "id": row_id,
            "entity_table": entity_table,
            "entity_id": entity_id,
            "source_url": source_url,
            "fetched_at": fetched_at,
            "adapter_version": adapter_version,
        }
    )
    client.insert_row("provenance_log", row.row_data())
    return row


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
        self._roll_calls: Repository[RollCall] = Repository(client=client, model=RollCall)
        self._pace = pace if pace is not None else (lambda: None)

    def load(self, rows: Sequence[dict[str, Any]]) -> LoadReport:
        """Upsert every roll call, writing its provenance entry alongside it.

        For each row:
          1. pace;
          2. `Repository.upsert` the `roll_calls` row (idempotent by id);
          3. `write_provenance` for it, keyed off the row's own
             `content_hash` -- so a row whose content has not changed since
             the last run writes NO new provenance entry, and a row whose
             content HAS changed writes a second, distinct one. See the
             module docstring.

        `fetched_at` is stamped as "now" at call time, once per row -- it
        records when THIS loader fetched the data, which is distinct from
        `voted_at` (when the House voted) and cannot be derived from the
        row's own fields.
        """
        report = LoadReport()
        for row in rows:
            self._pace()
            stored = self._roll_calls.upsert(row)
            assert stored.id is not None  # noqa: S101 - upsert guarantees it
            report.roll_calls_written.append(stored.id)

            content_hash = row.get("content_hash")
            source_url = row.get("source_url")
            if content_hash and source_url:
                written = write_provenance(
                    self._client,
                    entity_table="roll_calls",
                    entity_id=stored.id,
                    source_url=str(source_url),
                    fetched_at=datetime.now(UTC).isoformat(),
                    content_hash=str(content_hash),
                )
                if written is not None:
                    report.provenance_written.append(written.id)  # type: ignore[arg-type]

            logger.info("house_rollcalls: wrote %s", stored.id)
        return report


__all__ = [
    "ADAPTER_VERSION",
    "CONGRESS_GOV_BASE_URL",
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
