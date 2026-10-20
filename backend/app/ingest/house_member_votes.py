"""Per-member House positions, denormalized onto `member_votes` (story 5.2).

What this is, and why it exists
================================

Story 5.1 (`house_rollcalls.py`) ingests the `roll_calls` row — one row per
recorded House vote. This module is the other half: one `member_votes` row per
representative who cast a vote on that roll call, carrying the roll call's own
fields copied onto it.

The acceptance criterion is exactly that copy: "`member_votes` rows carry
voted_at, vote_type, bill_id, topic_ids and is_bundled; the drift check
passes." datamodel.md italicises those five fields on `member_votes` and says
why in one clause — "copied from the roll call ... so scoring reads one
table". There are no joins in ZeroDB, so a scorer answering "how did this
person vote on climate bills in 2025" either reads one table filtered by
person and topic, or it reads `member_votes`, collects several hundred distinct
`roll_call_id`s, and fetches each one. At ~600k `member_votes` rows per
Congress the second option is not a slower version of the first; it is a
different product.

**This module ran live.** Unlike story 5.1 — which shipped against a
hand-built fixture because no `CONGRESS_GOV_API_KEY` existed — a real, working
key was available for this story, and `scripts/verify_house_bills_live.py`
exercises this module (and 5.3's) against the real api.congress.gov and the
real ZeroDB project. See that script and this story's PR for what it proved.

The source: an endpoint story 5.1 did not know existed
=======================================================

    GET /v3/house-vote/{congress}/{session}/{roll}/members

Confirmed live 2026-10-19. Story 5.1's module docstring called the per-member
endpoint "the member votes-level endpoint" without a URL, because it could not
reach the API to find one. The real shape, captured into
`tests/fixtures/house_member_votes_sample.json`:

    {
      "houseRollCallVoteMemberVotes": {
        "congress": 119, "sessionNumber": 1, "rollCallNumber": 240,
        "legislationType": "HR", "legislationNumber": "3424",
        "voteQuestion": "On Motion to Suspend the Rules and Pass",
        "result": "Passed",
        "results": [
          {"bioguideID": "A000055", "firstName": "Robert",
           "lastName": "Aderholt", "voteCast": "Yea",
           "voteParty": "R", "voteState": "AL"},
          ...
        ]
      }
    }

Two properties of this response that shaped this module:

  * **`results` is not paginated.** The response for roll 240 of the 119th
    carries all 430 members in one body, and the response has no `pagination`
    block at all (confirmed across rolls 1, 150 and 240). So one fetch gets
    every position for a roll call, and this module takes the whole list at
    once rather than paging.
  * **`bioguideID` is the real bioguide id** — the same identifier
    `people.id` is built from (`person:{bioguide}`), so unlike the Senate
    source (story 8.1), which publishes an `lis_member_id` needing a
    crosswalk, no mapping table is needed here at all. This is the one place
    the House source is strictly easier than the Senate's.

voteCast: the live source publishes TWO position vocabularies
==============================================================

This is the finding story 5.1 could not have made from documentation, and the
reason this module exists as its own file rather than four lines inside
`house_rollcalls.py`. Sampling rolls 1, 5, 20, 50, 100, 150, 200, 250, 300 and
240 of the 119th Congress session 1 live, `voteCast` takes:

    Yea / Nay / Not Voting     — rolls 5, 20, 50, 100, 200, 240, 300
    Aye / No  / Not Voting     — rolls 150, 250
    Present   / Not Voting     — roll 1

`Aye`/`No` are not a different kind of vote. They are the House's spelling for
a vote taken in the Committee of the Whole, where the question is put as "aye
or no" rather than "yea or nay"; the position a member took is identical.
datamodel.md's vocabulary has four values (`yea`, `nay`, `present`,
`not_voting`), so this module maps `Aye -> yea` and `No -> nay`
(`_VOTE_CAST_TO_POSITION`). Keeping them distinct would mean either a
five-value vocabulary datamodel.md does not have, or a scorer that has to
know House parliamentary procedure to count a member's record — and would
split one representative's voting history across two spellings of the same
position, which is precisely the bug that makes an alignment score wrong.

`Present` maps to `present` and is NOT folded into either side: a member voting
present has deliberately declined to take a position, and prd.md's scoring
treats that differently from a yea or a nay. `Not Voting` maps to
`not_voting` — absence, not a position.

An unrecognized `voteCast` is SKIPPED, not guessed (`_SkipMemberVote`), and
counted in `LoadReport.member_votes_skipped`. Guessing would be worse than a
gap: a wrong position on a real representative's record is the exact failure
prd.md's "evidence over opinion" principle exists to prevent, and a missing
row is visible to the integrity checks while a wrong one is not.

Why the denormalized copy is made HERE, from the roll call's own row
=====================================================================

`transform_member_votes_for` takes the already-transformed `roll_calls` row
dict — not the raw source envelope — and copies the five fields off it
verbatim. That ordering is the whole correctness argument:

  * The roll call's `vote_type` is a CLASSIFICATION
    (`house_rollcalls._classify_vote_type`), not a source field. If this
    module re-derived it from the members response's own `voteQuestion` (which
    that response does carry), two classifiers would exist and could disagree
    — and a disagreement would be invisible until the nightly drift check
    caught 430 rows at once.
  * `bill_id` and `topic_ids` are likewise computed, not copied from source:
    `bill_id` is resolved against stored `bills` rows (story 5.3's
    `resolve_roll_call_bill_id`), and `topic_ids` is still `[]` pending a
    subject-to-topic mapping story. Neither is derivable from the members
    response at all.

So there is exactly one producer of each of the five values, and this module
copies rather than computes. That is what makes "the drift check passes" a
property of the write rather than a coincidence.

Re-ingest re-stamps every member row, which is what repairs drift
==================================================================

`HouseRollCallLoader.load` upserts the `roll_calls` row and then
`upsert_many`s the member rows handed to it, every run — not only the first.
So when a roll call changes (the House corrects a vote, or this codebase's
classification improves and `ADAPTER_VERSION` bumps), re-ingesting restates
all ~430 member rows with the new values, and the drift check passes again
without waiting for the nightly `repair_member_votes_denormalization` job.

The nightly job (story 16.4, `app.integrity.repair`) is still the safety net
and is not redundant: it repairs drift nobody re-ingested, which is the normal
case for a roll call from a prior Congress that no scheduled run touches any
more. The two paths agree because both copy from the same `roll_calls` row,
and `app.integrity.checks.drifted_fields` is the single definition of what
"agree" means — `tests/features/house_member_votes.feature` asserts the
loader's output against that function rather than restating it.

What this module does NOT do
=============================

  * **It does not write `people`.** `MemberVote.person_id` references
    `people` (datamodel.md rule 5), and the repository checks it before every
    write. A representative with no `people` row has their position skipped by
    the repository's own reference check, not by a list in this module — story
    2.2's `legislators.py` is the only writer of `people`, and inventing a
    person row from a vote record would create a person with no term history.
  * **It does not fetch.** `fetch_member_votes` is the one function here that
    reaches the network, kept separate from the transform for the same reason
    `house_rollcalls.py` separates its two fetches: every other piece is
    testable from the fixture with no live call, and CI must not depend on
    Congress.gov being reachable (CLAUDE.md).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from app.ingest.house_rollcalls import (
    CONGRESS_GOV_BASE_URL,
    CONGRESS_GOV_USER_AGENT,
    HouseRollCallSourceError,
)

logger = logging.getLogger(__name__)

#: The adapter_version a provenance entry for this data would carry. Not
#: currently used for a write — `member_votes` has no `source_url` of its own
#: (datamodel.md lists none for that table: a member vote is a denormalized
#: copy of its roll call, not an independently-sourced record), so the
#: provenance entry for this data is the roll call's. Named here so a future
#: story that does give `member_votes` its own provenance has one obvious
#: place to reach for, and so this module's version is greppable alongside
#: every other adapter's.
ADAPTER_VERSION = "house_member_votes/v1"

#: datamodel.md: "`position` takes `yea`, `nay`, `present` or `not_voting`".
#: Keys are the live source's `voteCast` values, lowercased for comparison.
#: See the module docstring's "voteCast" section for why `Aye`/`No` fold onto
#: `yea`/`nay` and why `Present` does not fold onto either.
_VOTE_CAST_TO_POSITION: dict[str, str] = {
    "yea": "yea",
    "aye": "yea",
    "nay": "nay",
    "no": "nay",
    "present": "present",
    "not voting": "not_voting",
}

#: The five fields datamodel.md italicises on `member_votes` as copies of the
#: roll call's. Kept as a tuple here, and asserted against
#: `app.integrity.checks.DENORMALIZED_MEMBER_VOTE_FIELDS` by
#: `tests/test_house_member_votes_unit.py`, so the writer and the checker
#: cannot drift apart about WHICH fields are denormalized — a subtler version
#: of the drift this story is about.
DENORMALIZED_FIELDS: tuple[str, ...] = (
    "voted_at",
    "vote_type",
    "bill_id",
    "topic_ids",
    "is_bundled",
)


@dataclass
class SkippedMemberVote:
    """One source position this loader could not turn into a `member_votes` row.

    `bioguide` is None when the record had no `bioguideID` at all — there is
    no id to report, which is itself the reason for the skip.
    """

    bioguide: str | None
    reason: str


class _SkipMemberVote(Exception):
    """Internal signal: this one member record cannot become a row."""


def fetch_member_votes(
    *,
    congress: int,
    session: int,
    roll_call_number: int,
    api_key: str,
    base_url: str = CONGRESS_GOV_BASE_URL,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Every member's position on one roll call, as the raw parsed JSON dict.

    Returns the whole response body (the `{"houseRollCallVoteMemberVotes":
    {...}}` envelope), matching `house_rollcalls.fetch_roll_call_item`'s own
    contract. No paging: the real endpoint returns all ~430 members in one
    body and publishes no pagination block (confirmed live 2026-10-19 — see
    the module docstring).

    Raises:
        HouseRollCallSourceError: the request failed, did not come back 200,
            or the body had no `houseRollCallVoteMemberVotes` object. Reusing
            story 5.1's exception type deliberately: a caller ingesting a roll
            call and its member positions wants one failure mode for "the
            Congress.gov house-vote API did not answer as expected", not two.
    """
    url = f"{base_url}/house-vote/{congress}/{session}/{roll_call_number}/members"
    try:
        response = httpx.get(
            url,
            params={"api_key": api_key, "format": "json"},
            headers={"User-Agent": CONGRESS_GOV_USER_AGENT},
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise HouseRollCallSourceError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise HouseRollCallSourceError(f"GET {url} returned {response.status_code}, expected 200")
    body = response.json()
    if not isinstance(body.get("houseRollCallVoteMemberVotes"), dict):
        raise HouseRollCallSourceError(
            f"GET {url} did not return a houseRollCallVoteMemberVotes object: {body!r}"
        )
    return body


def transform_member_votes_for(
    roll_call_row: dict[str, Any],
    members_envelope: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[SkippedMemberVote]]:
    """One roll call's member positions into `MemberVote`-shaped row dicts.

    Args:
        roll_call_row: the ALREADY-TRANSFORMED `roll_calls` row dict, as
            `house_rollcalls.transform_roll_call` produced it. The five
            denormalized fields are copied off this verbatim — see the module
            docstring for why they are not re-derived from the source.
        members_envelope: the raw `{"houseRollCallVoteMemberVotes": {...}}`
            body from `fetch_member_votes` (or the fixture).

    Returns:
        `(rows, skipped)`. One row per member whose `voteCast` is in the
        documented vocabulary and who has a `bioguideID`; one `SkippedMemberVote`
        for every other source record, so a caller can report the gap rather
        than discover it as a count mismatch later. Mirrors
        `house_rollcalls.transform_roll_calls`'s contract: one bad record in a
        batch of 430 must not fail the other 429.

    Note the `id` is NOT set here. `MemberVote.natural_key` is
    `("roll_call_id", "person_id")` with `person_id` in `strip_id_prefix`, so
    the repository composes `{roll_call_id}:{bioguide}` itself (story 1.4). A
    loader setting the id by hand would be a second producer of datamodel.md's
    id pattern, which is the thing `build_id` exists to prevent.
    """
    members = members_envelope.get("houseRollCallVoteMemberVotes")
    if not isinstance(members, dict):
        return [], [
            SkippedMemberVote(
                bioguide=None,
                reason="envelope has no houseRollCallVoteMemberVotes object",
            )
        ]

    results = members.get("results")
    if not isinstance(results, list):
        return [], [
            SkippedMemberVote(
                bioguide=None,
                reason="houseRollCallVoteMemberVotes has no results array",
            )
        ]

    denormalized = {field: roll_call_row[field] for field in DENORMALIZED_FIELDS}

    rows: list[dict[str, Any]] = []
    skipped: list[SkippedMemberVote] = []
    for entry in results:
        try:
            rows.append(
                _transform_one(
                    entry,
                    roll_call_id=str(roll_call_row["id"]),
                    denormalized=denormalized,
                )
            )
        except _SkipMemberVote as exc:
            bioguide = entry.get("bioguideID") if isinstance(entry, dict) else None
            skipped.append(
                SkippedMemberVote(bioguide=str(bioguide) if bioguide else None, reason=str(exc))
            )
            logger.warning("skipping member position %r: %s", bioguide, exc)
    return rows, skipped


def _transform_one(
    entry: Any,
    *,
    roll_call_id: str,
    denormalized: dict[str, Any],
) -> dict[str, Any]:
    """One `results[]` entry into a `MemberVote`-shaped row dict."""
    if not isinstance(entry, dict):
        raise _SkipMemberVote(f"member record is not an object: {entry!r}")

    bioguide = entry.get("bioguideID")
    if not bioguide:
        raise _SkipMemberVote("member record has no bioguideID")

    position = _position_from(entry.get("voteCast"))

    return {
        "roll_call_id": roll_call_id,
        "person_id": f"person:{bioguide}",
        "position": position,
        # Shallow-copied per row rather than shared: `topic_ids` is a list, and
        # 430 rows holding the same list object would mean one row's later
        # mutation silently changing all of them.
        **{
            field: (list(value) if isinstance(value, list) else value)
            for field, value in denormalized.items()
        },
    }


def _position_from(vote_cast: Any) -> str:
    """One `voteCast` value into datamodel.md's `position` vocabulary.

    Raises `_SkipMemberVote` for anything not in `_VOTE_CAST_TO_POSITION` —
    see the module docstring for why an unknown position is a skip and never a
    guess.
    """
    if not vote_cast:
        raise _SkipMemberVote("member record has no voteCast")
    key = str(vote_cast).strip().lower()
    position = _VOTE_CAST_TO_POSITION.get(key)
    if position is None:
        raise _SkipMemberVote(f"voteCast {vote_cast!r} is not a recognized position")
    return position


def member_votes_by_roll_call(
    pairs: Iterable[tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], list[SkippedMemberVote]]:
    """`transform_member_votes_for` over many (roll call, members) pairs.

    Shaped to be handed straight to `HouseRollCallLoader.load`'s
    `member_vote_rows_by_roll_call` argument, so a batch ingest does not need
    to build that dict at every call site.
    """
    by_roll_call: dict[str, list[dict[str, Any]]] = {}
    all_skipped: list[SkippedMemberVote] = []
    for roll_call_row, members_envelope in pairs:
        rows, skipped = transform_member_votes_for(roll_call_row, members_envelope)
        by_roll_call[str(roll_call_row["id"])] = rows
        all_skipped.extend(skipped)
    return by_roll_call, all_skipped


__all__ = [
    "ADAPTER_VERSION",
    "DENORMALIZED_FIELDS",
    "SkippedMemberVote",
    "fetch_member_votes",
    "member_votes_by_roll_call",
    "transform_member_votes_for",
]
