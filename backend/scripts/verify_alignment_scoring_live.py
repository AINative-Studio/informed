#!/usr/bin/env python3
"""Verify alignment scoring against the LIVE "informed" project (6.2, 6.4-6.7).

Every story from 1.3 on has found at least one discrepancy between what the
in-memory fake agrees to and what ZeroDB actually does, so epic 6's core
claims are checked against the real API as well as the fake.

!!! THIS SCRIPT WRITES SYNTHETIC TEST DATA AND DELETES IT AGAIN !!!

This is the opposite posture from `verify_methodology_versions_live.py`. A
methodology version is permanent reference data and must never be cleaned up.
A `candidate_positions` row is a **claim about how a named person voted**, and
fabricating one for a real incumbent would be the worst thing this codebase
could put in its database: prd.md's first principle is "evidence over
opinion", and a position with no real votes behind it is opinion with a
number on it.

So everything this script writes is marked synthetic in its own id —
`person:TEST-SCORING-*`, `en-US:test-scoring-*:v1` — and every row is deleted
at the end, with a live re-read confirming it is gone. Run with `--keep` only
to debug a failure, and clean up by hand afterwards.

What it proves, in order:

  1. **The synthetic graph writes through the real repositories** — reference
     checks, deterministic ids, provenance — against live ZeroDB.
  2. **`candidate_positions` accepts the row shape story 6.2 writes**,
     including `methodology_version`, whose column this story added to
     `TABLE_SCHEMAS` (it was missing: nothing had ever written to the table).
  3. **`append_version` moves `is_current` live, on the COMPOUND key.**
     `CandidatePosition.version_key` is `(person_id, stance_id)`, not a single
     field. Two non-atomic requests against the real API, and the only place
     that ordering is exercised for real.
  4. **`s_value` and `confidence` round-trip as real floats** — `-1.0` coming
     back as `"-1.0"` would render and then break the first time the formula
     multiplied by it. The hazard story 12.2 pinned for tier weights, now for
     the two numbers every score is built from.
  5. **The alignment endpoint computes prd.md's formula over live rows**,
     driven through FastAPI's TestClient over a LIVE ZeroDB client, against a
     real encrypted voter profile saved through story 14.1's service.
  6. **The same call twice gives a byte-identical response** — story 6.4's
     determinism criterion, over a store that returns rows in whatever order
     it likes. This is the check the fake cannot make meaningfully.
  7. **The request wrote nothing.** Live row counts before and after are
     equal, and no new event was emitted. datamodel.md: "no table links a
     person's priorities to a candidate".
  8. **The dealbreaker warning fires, cites a real roll call, and does not
     move the number** — the score is identical with the flag cleared.
  9. **Every reason cites a roll call that resolves to a live row.**
 10. **The confidence badge comes from the live methodology row**, not from
     `DEFAULT_BADGE_THRESHOLDS`.
 11. **Cleanup removes every synthetic row**, confirmed by a live re-read of
     each id.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    export ZERODB_PROFILE_ROOT_KEY="$(
      python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())'
    )"
    python backend/scripts/verify_alignment_scoring_live.py
    python backend/scripts/verify_alignment_scoring_live.py --keep   # debug only

The root key is generated fresh for the run and never persisted, the same way
`verify_profiles_live.py` does it: the throwaway profile this script writes is
unreadable the moment the shell exits, which is belt and braces alongside the
delete. If `ZERODB_PROFILE_ROOT_KEY` is already set this script leaves it
alone — it never generates one over a real key, because doing so would make an
existing voter's row undecryptable.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from fastapi.testclient import TestClient

from app.main import create_app
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.methodology import MethodologyVersionService
from app.profiles.service import VoterProfileService
from app.repositories import ProvenanceContext, Repository
from app.repositories.models import (
    IssueTopic,
    MemberVote,
    Person,
    RollCall,
    Stance,
)
from app.scoring.badges import BADGE_THRESHOLDS_GROUP, badge_for
from app.scoring.router import zerodb_client_for_alignment
from app.scoring.service import AlignmentService
from app.zerodb import ZeroDBClient

# --- synthetic ids, every one marked as such -------------------------------
#
# `TEST-SCORING` in the middle of each id so a human scanning the live project
# can tell at a glance that nothing here is a real member, topic or vote. The
# person id is deliberately not a valid bioguide pattern.

PERSON_ID = "person:TEST-SCORING-000"
USER_ID = "test-scoring-voter-000"
TOPIC_IDS = [f"en-US:test-scoring-topic-{index}:v1" for index in range(3)]
STANCE_IDS = [f"{topic}:for" for topic in TOPIC_IDS]
ROLL_CALL_IDS = [f"us-house:119-2:99{index}" for index in range(6)]

#: An election and district that already exist live (story 2.x seeds them) are
#: NOT reused: a synthetic candidacy under a real district would appear on a
#: real ballot read. Both are synthetic and cleaned up.
ELECTION_ID = "test-scoring-election"
DISTRICT_ID = "ocd-division/country:us/state:zz/cd:test-scoring"
CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:{PERSON_ID}"

PROVENANCE = ProvenanceContext(adapter_version="verify_alignment_scoring_live/v1")


class Failure(AssertionError):
    """A live check failed. Raised rather than printed so cleanup still runs."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)
    print(f"  ok   {message}")


# --- writing the synthetic graph -------------------------------------------


def write_graph(client: ZeroDBClient) -> None:
    print("\n[1] writing the synthetic graph through the real repositories")

    Repository(client=client, model=IssueTopic).upsert_many(
        [
            {
                "id": topic,
                "name": f"SYNTHETIC TEST TOPIC {index} (verify_alignment_scoring_live)",
                "locale": "en-US",
                "taxonomy_version": 1,
            }
            for index, topic in enumerate(TOPIC_IDS)
        ]
    )
    Repository(client=client, model=Stance).upsert_many(
        [
            {
                "id": stance,
                "topic_id": TOPIC_IDS[index],
                "label": f"SYNTHETIC TEST STANCE {index}",
                "description": (
                    "Synthetic stance card written by "
                    "backend/scripts/verify_alignment_scoring_live.py. Not editorial "
                    "content; deleted at the end of the run."
                ),
                "direction_code": "+1",
                "wording_version": 1,
            }
            for index, stance in enumerate(STANCE_IDS)
        ]
    )
    Repository(client=client, model=Person).upsert(
        {
            "id": PERSON_ID,
            "canonical_name": "SYNTHETIC TEST MEMBER (not a real person)",
            "terms": [{"body_id": "us-house", "party": "Synthetic", "start": "2025-01-03"}],
        }
    )
    # `elections`, `districts` and `candidacies` written directly rather than
    # through their repositories: `Election` references `jurisdictions` and
    # `Candidacy` references `districts`, and resolving those would mean
    # either reusing REAL reference rows (so a synthetic candidacy would show
    # up in a real ballot read — the one thing this script must not do) or
    # writing synthetic jurisdictions too. The rows the SCORER reads
    # (`candidate_positions`, `member_votes`, `roll_calls`, `stances`,
    # `people`) all go through real repositories above and below.
    client.upsert_row(
        "elections",
        ELECTION_ID,
        {
            "id": ELECTION_ID,
            "jurisdiction_id": "us-federal",
            "name": "SYNTHETIC TEST ELECTION (verify_alignment_scoring_live)",
            "election_date": "2026-11-03",
            "kind": "general",
        },
    )
    client.upsert_row(
        "districts",
        DISTRICT_ID,
        {
            "id": DISTRICT_ID,
            "body_id": "us-house",
            "name": "SYNTHETIC TEST DISTRICT",
            "state": "ZZ",
            "valid_from": "2023-01-03",
        },
    )
    client.upsert_row(
        "candidacies",
        CANDIDACY_ID,
        {
            "id": CANDIDACY_ID,
            "election_id": ELECTION_ID,
            "district_id": DISTRICT_ID,
            "person_id": PERSON_ID,
            "person_name": "SYNTHETIC TEST MEMBER (not a real person)",
            "party": "Synthetic",
            "is_incumbent": True,
            "status": "filed",
        },
    )

    rolls: Repository[RollCall] = Repository(client=client, model=RollCall, provenance=PROVENANCE)
    votes: Repository[MemberVote] = Repository(client=client, model=MemberVote)
    # Six roll calls: three supporting stance 0's topic (so the dealbreaker can
    # be violated with a real citation trail), two supporting stance 1, one
    # supporting stance 2.
    layout = [
        (0, TOPIC_IDS[0], "nay"),
        (1, TOPIC_IDS[0], "nay"),
        (2, TOPIC_IDS[0], "nay"),
        (3, TOPIC_IDS[1], "yea"),
        (4, TOPIC_IDS[1], "yea"),
        (5, TOPIC_IDS[2], "yea"),
    ]
    for index, topic, position in layout:
        roll_id = ROLL_CALL_IDS[index]
        rolls.upsert(
            {
                "id": roll_id,
                "voted_at": "2026-04-01T12:00:00Z",
                "question": "SYNTHETIC TEST ROLL CALL (verify_alignment_scoring_live)",
                "vote_type": "final_passage",
                "result": "Passed",
                "topic_ids": [topic],
                "source_url": f"https://example.invalid/synthetic/{roll_id}",
                "content_hash": f"synthetic-verify-{roll_id}",
            }
        )
        votes.upsert(
            {
                "roll_call_id": roll_id,
                "person_id": PERSON_ID,
                "position": position,
                "voted_at": "2026-04-01T12:00:00Z",
                "vote_type": "final_passage",
                "topic_ids": [topic],
                "is_bundled": False,
            }
        )

    check(
        client.get_row("people", PERSON_ID) is not None,
        "the synthetic person wrote and reads back",
    )
    check(
        all(client.get_row("roll_calls", roll) is not None for roll in ROLL_CALL_IDS),
        f"all {len(ROLL_CALL_IDS)} synthetic roll calls wrote and read back",
    )


# --- story 6.2 live --------------------------------------------------------


def compute_positions(client: ZeroDBClient) -> None:
    print("\n[2] computing candidate_positions live (story 6.2)")

    from app.scoring.positions import PositionComputer

    computer = PositionComputer(client)
    stances = [
        Stance.model_validate(client.get_row("stances", stance).row_data)  # type: ignore[union-attr]
        for stance in STANCE_IDS
    ]
    result = computer.recompute_person(PERSON_ID, stances=stances)
    check(
        result.positions_written == 3,
        f"3 positions computed from the synthetic votes (got {result.positions_written})",
    )

    live = computer.positions_for(PERSON_ID)
    check(len(live) == 3, f"3 current positions read back live (got {len(live)})")

    by_stance = {row.stance_id: row for row in live}
    check(
        by_stance[STANCE_IDS[0]].s_value == -1.0,
        "three Nay votes on stance 0's topic computed s_value -1.0 live",
    )
    check(
        by_stance[STANCE_IDS[1]].s_value == 1.0,
        "two Yea votes on stance 1's topic computed s_value +1.0 live",
    )
    for row in live:
        check(
            isinstance(row.s_value, float) and isinstance(row.confidence, float),
            f"{row.stance_id}: s_value and confidence round-trip as real floats, not strings",
        )
        check(
            row.methodology_version != "",
            f"{row.stance_id}: the row names the methodology version that produced it",
        )

    print("\n[3] append_version moves is_current on the COMPOUND key, live")
    # The id is read back rather than predicted. `Repository._version_id_prefix`
    # builds it from the model's NATURAL key, which for `CandidatePosition` is
    # `(person_id, stance_id, methodology_version)` — so the stored id is
    # `{person}:{stance}:{methodology_version}:v{n}`, with the version number
    # appended after the full natural key rather than after the two-field
    # `version_key`. Predicting it here was this check's first draft and it was
    # wrong; printing what is actually stored is both correct and more useful
    # to a reader of the run output.
    stored_ids = sorted(
        str(row.row_data["id"])
        for row in client.query_rows("candidate_positions", limit=500).rows
        if row.row_data.get("person_id") == PERSON_ID
        and row.row_data.get("stance_id") == STANCE_IDS[0]
    )
    check(
        len(stored_ids) == 1 and stored_ids[0].endswith(":v1"),
        f"the first position is stored as one v1 row: {stored_ids}",
    )
    check(
        stored_ids[0].startswith(f"{PERSON_ID}:{STANCE_IDS[0]}:"),
        f"its id is composed from the natural key, as datamodel.md documents: {stored_ids[0]}",
    )
    result = computer.recompute_person(PERSON_ID, stances=stances)
    check(
        result.positions_written == 0,
        "a recompute with unchanged votes appends no duplicate version",
    )

    # Force a real second version by adding a contrary vote.
    Repository(client=client, model=RollCall, provenance=PROVENANCE).upsert(
        {
            "id": ROLL_CALL_IDS[0],
            "voted_at": "2026-04-01T12:00:00Z",
            "question": "SYNTHETIC TEST ROLL CALL (verify_alignment_scoring_live)",
            "vote_type": "final_passage",
            "result": "Passed",
            "topic_ids": [TOPIC_IDS[0]],
            "source_url": f"https://example.invalid/synthetic/{ROLL_CALL_IDS[0]}",
            "content_hash": f"synthetic-verify-{ROLL_CALL_IDS[0]}",
        }
    )
    Repository(client=client, model=MemberVote).upsert(
        {
            "roll_call_id": ROLL_CALL_IDS[0],
            "person_id": PERSON_ID,
            "position": "yea",  # was nay
            "voted_at": "2026-04-01T12:00:00Z",
            "vote_type": "final_passage",
            "topic_ids": [TOPIC_IDS[0]],
            "is_bundled": False,
        }
    )
    result = computer.recompute_person(PERSON_ID, stances=stances)
    check(result.positions_written == 1, "a changed vote appends exactly one new version")

    rows = [
        row.row_data
        for row in client.query_rows("candidate_positions", limit=500).rows
        if row.row_data.get("person_id") == PERSON_ID
        and row.row_data.get("stance_id") == STANCE_IDS[0]
    ]
    check(len(rows) == 2, f"two versions stored for (person, stance 0) (got {len(rows)})")
    current = [row for row in rows if row.get("is_current") is True]
    check(len(current) == 1, "exactly one of them is marked current, live")
    superseded = [row for row in rows if row.get("is_current") is not True]
    check(
        superseded[0]["s_value"] == -1.0,
        "the superseded version still reports the value it was computed with",
    )

    # Put the record back so the alignment checks below score the intended
    # dealbreaker violation.
    Repository(client=client, model=MemberVote).upsert(
        {
            "roll_call_id": ROLL_CALL_IDS[0],
            "person_id": PERSON_ID,
            "position": "nay",
            "voted_at": "2026-04-01T12:00:00Z",
            "vote_type": "final_passage",
            "topic_ids": [TOPIC_IDS[0]],
            "is_bundled": False,
        }
    )
    computer.recompute_person(PERSON_ID, stances=stances)


# --- stories 6.4-6.7 live --------------------------------------------------


def _profile_payload(*, dealbreaker: bool) -> dict[str, Any]:
    return {
        "interests": [
            {
                "topic_id": TOPIC_IDS[0],
                "stance_id": STANCE_IDS[0],
                "tier": "primary",
                "intensity": 2,
                "dealbreaker": dealbreaker,
            },
            {
                "topic_id": TOPIC_IDS[1],
                "stance_id": STANCE_IDS[1],
                "tier": "primary",
                "intensity": 3,
                "dealbreaker": False,
            },
            {
                "topic_id": TOPIC_IDS[2],
                "stance_id": STANCE_IDS[2],
                "tier": "secondary",
                "intensity": 3,
                "dealbreaker": False,
            },
        ]
    }


def score_alignment(client: ZeroDBClient, profiles: VoterProfileService) -> None:
    print("\n[4] the alignment endpoint over live rows (6.4, 6.5, 6.6, 6.7)")

    profiles.save(USER_ID, _profile_payload(dealbreaker=True))

    app = create_app()
    app.dependency_overrides[zerodb_client_for_alignment] = lambda: client
    from app.scoring.router import alignment_service

    app.dependency_overrides[alignment_service] = lambda: AlignmentService(
        client=client, profiles=profiles
    )

    before_counts = _row_counts(client)
    before_events = _event_counts(client)

    with TestClient(app) as http:
        response = http.get(
            f"/v1/candidates/{CANDIDACY_ID}/alignment", params={"profile_id": USER_ID}
        )
        check(
            response.status_code == 200,
            f"the endpoint answered 200 (got {response.status_code})",
        )
        first = response.json()

        print("\n[5] determinism over a live store (story 6.4)")
        again = http.get(
            f"/v1/candidates/{CANDIDACY_ID}/alignment", params={"profile_id": USER_ID}
        ).json()
        check(again == first, "two identical calls returned a byte-identical response")
        third = http.get(
            f"/v1/candidates/{CANDIDACY_ID}/alignment", params={"profile_id": USER_ID}
        ).json()
        check(third == first, "a third call agreed too")

    print("\n[6] the formula's own arithmetic, checked by hand against live weights")
    weights = MethodologyVersionService(client).current_version()
    tiers = weights.tier_weights
    # stance 0: w=tiers[primary], k=2, c=?, s=-1
    # stance 1: w=tiers[primary], k=3, c=?, s=+1
    # stance 2: w=tiers[secondary], k=3, c=?, s=+1
    positions = {
        row.row_data["stance_id"]: row.row_data
        for row in client.query_rows("candidate_positions", limit=500).rows
        if row.row_data.get("person_id") == PERSON_ID and row.row_data.get("is_current") is True
    }
    numerator = 0.0
    denominator = 0.0
    for stance_id, tier, intensity, sign in (
        (STANCE_IDS[0], "primary", 2, -1.0),
        (STANCE_IDS[1], "primary", 3, 1.0),
        (STANCE_IDS[2], "secondary", 3, 1.0),
    ):
        row = positions[stance_id]
        weight = tiers[tier] * intensity * row["confidence"]
        denominator += weight
        numerator += weight * row["s_value"]
        check(
            (row["s_value"] > 0) == (sign > 0),
            f"{stance_id}: the live position's sign matches the synthetic record",
        )
    expected = 50.0 * (1.0 + numerator / denominator)
    check(
        abs(first["score"] - expected) < 1e-9,
        f"the endpoint's score {first['score']} equals the hand-computed "
        f"50*(1 + {numerator}/{denominator}) = {expected}",
    )

    print("\n[7] the request wrote nothing (datamodel.md's no-join rule)")
    check(_row_counts(client) == before_counts, "live row counts are unchanged by the request")
    check(_event_counts(client) == before_events, "no new event was emitted by the request")
    row_id = ProfileKeyring(client=client, root_key=ProfileRootKey.from_env()).row_id(USER_ID)
    linking = [
        (table, row.row_data.get("id"))
        for table in ("candidate_positions", "candidate_cards", "member_votes")
        for row in client.query_rows(table, limit=500).rows
        if PERSON_ID in str(row.row_data) and row_id in str(row.row_data)
    ]
    check(linking == [], f"no live row links the voter to the candidate (found {linking})")

    print("\n[8] the dealbreaker warning (story 6.6)")
    warnings = first["dealbreaker_warnings"]
    check(len(warnings) == 1, f"exactly one dealbreaker warning (got {len(warnings)})")
    check(warnings[0]["stance_id"] == STANCE_IDS[0], "it names the violated dealbreaker stance")
    check(warnings[0]["s_value"] < 0, "its s_value is strictly negative")
    check(bool(warnings[0]["citations"]), "it carries at least one citation")
    for citation in warnings[0]["citations"]:
        live_roll = client.get_row("roll_calls", citation["roll_call_id"])
        check(
            live_roll is not None,
            f"the warning's citation {citation['roll_call_id']} resolves to a live roll call",
        )

    print("\n[9] the warning did not move the number")
    profiles.save(USER_ID, _profile_payload(dealbreaker=False))
    cleared = AlignmentService(client=client, profiles=profiles).alignment_for(
        CANDIDACY_ID, user_id=USER_ID
    )
    check(cleared.dealbreaker_warnings == [], "clearing the flag removes the warning")
    check(
        abs((cleared.score or 0.0) - first["score"]) < 1e-9,
        f"the score is identical with the flag cleared ({cleared.score} == {first['score']})",
    )

    print("\n[10] reasons and their citations (story 6.7)")
    reasons = first["reasons"]
    check(1 <= len(reasons) <= 3, f"between 1 and 3 reasons (got {len(reasons)})")
    for reason in reasons:
        check(
            bool(reason["citations"]),
            f"reason for {reason['stance_id']} carries at least one citation",
        )
        for citation in reason["citations"]:
            live_roll = client.get_row("roll_calls", citation["roll_call_id"])
            check(
                live_roll is not None,
                f"citation {citation['roll_call_id']} resolves to a live roll_calls row",
            )
            check(
                bool(live_roll.row_data.get("source_url")),
                f"{citation['roll_call_id']} carries a source_url a reader can follow",
            )
        check(
            reason["direction"] in {"raised", "lowered"},
            f"reason for {reason['stance_id']} says which way it moved the score",
        )

    print("\n[11] the confidence badge comes from the live methodology row (6.5)")
    thresholds = weights.weights.get(BADGE_THRESHOLDS_GROUP, {})
    if not thresholds:
        # The live current version predates story 6.5 and carries no
        # `badge_thresholds`, so the badge came from
        # `DEFAULT_BADGE_THRESHOLDS` — which is the documented fallback (see
        # `app.scoring.badges.badge_for`), not a bug. Reported rather than
        # failed, and the fix is a real methodology publication, which is
        # permanent reference data and therefore opt-in:
        #
        #   python backend/scripts/verify_alignment_scoring_live.py --publish-methodology 2026.3
        #
        # The same posture `verify_methodology_versions_live.py` takes for
        # exactly the same reason.
        from app.scoring.badges import DEFAULT_BADGE_THRESHOLDS

        print(
            f"  info  the live current methodology ({weights.id}) predates story 6.5 and "
            f"carries no {BADGE_THRESHOLDS_GROUP}; the badge came from the published "
            f"default. Run with --publish-methodology to put it in force."
        )
        thresholds = DEFAULT_BADGE_THRESHOLDS
    else:
        check(
            True,
            f"the live current methodology ({weights.id}) publishes "
            f"{BADGE_THRESHOLDS_GROUP}: {thresholds}",
        )
    check(
        first["confidence_badge"] == badge_for(first["evidence_count"], thresholds),
        f"the badge {first['confidence_badge']!r} matches the live thresholds applied to "
        f"{first['evidence_count']} vote(s) of evidence",
    )

    print("\n[12] the response carries no profile content")
    for forbidden in ("payload", "key_ref", "interests", "tier", "intensity", "vp:"):
        check(forbidden not in response.text, f"{forbidden!r} does not appear in the response")


def _row_counts(client: ZeroDBClient) -> dict[str, int]:
    return {
        table: len(client.query_rows(table, limit=500).rows)
        for table in ("candidate_positions", "member_votes", "roll_calls", "voter_profiles")
    }


def _event_counts(client: ZeroDBClient) -> dict[str, int]:
    return {
        topic: len(client.list_events(topic=topic, limit=200).events)
        for topic in ("positions.recomputed", "rollcall.ingested", "profile.updated")
    }


# --- cleanup ---------------------------------------------------------------


def clean_up(client: ZeroDBClient, profiles: VoterProfileService) -> None:
    print("\n[13] deleting every synthetic row, then re-reading to confirm")

    # `candidate_positions` is append-only and `Repository.delete` refuses it
    # by design (datamodel.md keeps positions indefinitely for
    # reproducibility). That rule is about REAL positions; these are fabricated
    # rows for a person who does not exist, and leaving them would mean the
    # live project permanently asserted how a non-existent member voted. So
    # they are deleted through the client directly, which is the one
    # legitimate use of that escape hatch in this codebase and is why this is
    # called out here rather than done quietly.
    position_ids = [
        str(row.row_data["id"])
        for row in client.query_rows("candidate_positions", limit=500).rows
        if row.row_data.get("person_id") == PERSON_ID
    ]
    vote_ids = [
        str(row.row_data["id"])
        for row in client.query_rows("member_votes", limit=500).rows
        if row.row_data.get("person_id") == PERSON_ID
    ]
    provenance_ids = [
        str(row.row_data["id"])
        for row in client.query_rows("provenance_log", limit=500).rows
        if "synthetic-verify-" in str(row.row_data.get("entity_id", ""))
        or str(row.row_data.get("entity_id", "")) in ROLL_CALL_IDS
    ]

    targets: list[tuple[str, str]] = []
    targets += [("candidate_positions", row_id) for row_id in position_ids]
    targets += [("member_votes", row_id) for row_id in vote_ids]
    targets += [("roll_calls", roll_id) for roll_id in ROLL_CALL_IDS]
    targets += [("provenance_log", row_id) for row_id in provenance_ids]
    targets += [("candidacies", CANDIDACY_ID)]
    targets += [("districts", DISTRICT_ID)]
    targets += [("elections", ELECTION_ID)]
    targets += [("stances", stance) for stance in STANCE_IDS]
    targets += [("issue_topics", topic) for topic in TOPIC_IDS]
    targets += [("people", PERSON_ID)]

    deleted = 0
    for table, row_id in targets:
        try:
            client.delete_row(table, row_id)
            deleted += 1
        except Exception as exc:  # noqa: BLE001 - report and keep deleting
            print(f"  warn  {table}/{row_id}: {exc}")

    # The voter profile goes through its own service, so the data key is
    # shredded too (story 14.4) rather than leaving an undecryptable row.
    try:
        profiles.delete(USER_ID)
        deleted += 1
    except Exception as exc:  # noqa: BLE001
        print(f"  warn  voter profile: {exc}")

    print(f"  info  issued {deleted} delete(s)")

    print("\n[14] confirming the synthetic data is gone")
    remaining: list[str] = []
    for table, row_id in targets:
        if client.get_row(table, row_id) is not None:
            remaining.append(f"{table}/{row_id}")
    check(remaining == [], f"every synthetic row is gone (still present: {remaining})")
    check(
        profiles.load(USER_ID) is None,
        "the synthetic voter profile is gone and its key is shredded",
    )
    check(
        [
            row.row_data["id"]
            for row in client.query_rows("candidate_positions", limit=500).rows
            if PERSON_ID in str(row.row_data)
        ]
        == [],
        "no candidate_positions row mentions the synthetic person any more",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help=(
            "skip cleanup. Debug only: the synthetic rows assert how a "
            "non-existent member voted and must not be left in the live project."
        ),
    )
    parser.add_argument(
        "--publish-methodology",
        metavar="VERSION",
        help=(
            "publish a NEW methodology version carrying story 6.5's "
            "badge_thresholds, e.g. 2026.3. This writes PERMANENT reference "
            "data and is deliberately never cleaned up — deleting a "
            "methodology version would break the citation on anything scored "
            "under it, which is the one thing docs/methodology-changelog.md "
            "promises a reader never happens. Opt-in for exactly that reason, "
            "the same posture verify_methodology_versions_live.py takes."
        ),
    )
    args = parser.parse_args()

    client = ZeroDBClient.from_env()

    if args.publish_methodology:
        from app.methodology.publish import MethodologyPublisher

        published = MethodologyPublisher(client).publish_new_version(args.publish_methodology)
        print(
            f"published methodology {published.id} with weight groups {sorted(published.weights)}"
        )

    profiles = VoterProfileService(
        client=client, keyring=ProfileKeyring(client=client, root_key=ProfileRootKey.from_env())
    )
    print(f"live project: {client.project_id}")

    failure: BaseException | None = None
    try:
        write_graph(client)
        compute_positions(client)
        score_alignment(client, profiles)
    except BaseException as exc:  # noqa: BLE001 - cleanup must run regardless
        failure = exc
        print(f"\nFAILED: {exc}")

    if args.keep:
        print("\n--keep: synthetic rows left in place. Clean them up by hand.")
    else:
        try:
            clean_up(client, profiles)
        except BaseException as exc:  # noqa: BLE001
            print(f"\nCLEANUP FAILED: {exc}")
            failure = failure or exc

    if failure is not None:
        return 1
    print("\nall live checks passed, and all synthetic data was removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
