#!/usr/bin/env python3
"""Verify the `candidate_cards` read model against the LIVE project (7.2).

Every story from 1.3 on has found at least one discrepancy between what the
in-memory fake agrees to and what ZeroDB actually does, so story 7.2's core
claims are checked against the real API as well as the fake.

!!! THIS SCRIPT WRITES SYNTHETIC TEST DATA AND DELETES IT AGAIN !!!

The same posture `verify_alignment_scoring_live.py` takes, for the same
reason. A `candidate_cards` row is a **claim about a named candidate's
record** — their verdict tallies, their say/do gap — and fabricating one for a
real candidate would be the worst thing this codebase could put in its
database: prd.md's first principle is "evidence over opinion", and a verdict
count with no verdicts behind it is opinion with a number on it. Worse than a
`candidate_positions` row, in fact, because a card is the row the ballot view
*renders*.

So everything this script writes is marked synthetic in its own id —
`person:TEST-CARDS-*`, `en-US:test-cards-*:v1` — and every row is deleted at
the end, with a live re-read confirming it is gone. The synthetic election and
district are synthetic too, deliberately: a synthetic candidacy under a REAL
district would appear in a real ballot read. Run with `--keep` only to debug
a failure, and clean up by hand afterwards.

===============================================================================
WHAT THIS SCRIPT CAN AND CANNOT VERIFY — READ THIS FIRST
===============================================================================

Story 7.2's acceptance criterion names two triggers, and they are in
genuinely different states. This script is explicit about which half it
proves, because claiming otherwise would be exactly the kind of unearned
verification this project keeps catching.

**VERIFIED LIVE: the `positions.recomputed` path, end to end.** Not simulated
and not stubbed: section [3] calls story 6.2's REAL
`PositionComputer.recompute_person` against live ZeroDB, which writes real
`candidate_positions` rows and emits a real `positions.recomputed` event into
the live event log; section [4] then runs story 7.2's real
`CandidateCardRebuildConsumer`, which reads that real event back from the live
log and rebuilds a real `candidate_cards` row. Every row and every event in
that chain is a live row and a live event.

**NOT VERIFIED LIVE: the `verdict.published` path.** Epic 11 (the verdict
engine) is entirely unbuilt. Nothing in this codebase emits the topic, the
live `verdicts` table holds zero rows, and the live event log holds zero
events of it — all three measured on 2026-10-19 and re-measured by section [6]
of this script on every run, so the claim is a measurement rather than a
memory. Section [7] therefore drives the verdict rebuild through a
**synthetic verdict chain this script writes itself**: real live rows in
`promise_sources`, `promises` and `verdicts`, and a real live
`verdict.published` event emitted by `emit_verdict_published` — but emitted by
this script, not by a verdict engine. So what [7] proves is that the row
shapes, the reference checks, the `is_current` handling and the consumer all
work against live ZeroDB. What it does NOT prove is that epic 11's eventual
output is what this consumer expects. Story 11.6 owns that, and
`CANDIDATE_CARD_REBUILD_PROMISE` is the cadence it will measure against.

===============================================================================
WHY THE SYNTHETIC CHAIN HAS TO BE SYNTHETIC, EVEN FOR THE POSITIONS PATH
===============================================================================

The honest answer, because "why not just recompute a real member" is the
obvious question and it has a real answer.

A real live incumbent produces **zero** positions, and that is correct rather
than broken. Story 6.2's scorer matches a vote to a stance through
`member_votes.topic_ids[]`, and nothing live populates that field: story 5.5's
bill-to-stance classifier measured 54.2% precision against its own 85% bar, so
it set `ACCEPT_THRESHOLD = None` and accepts nothing automatically — every tag
goes to QA instead. Section [2] measures this on every run rather than
asserting it from memory: it reads live `member_votes`, `roll_calls` and
`bills` and reports how many carry a topic. Measured 2026-10-19: zero of the
500 `member_votes` rows, zero of the first 1,000 of 1,922 `roll_calls` rows,
and zero of 3 `bills` rows. The roll-call figure is a page rather than the
whole table, deliberately — this section is a sanity measurement that explains
a design choice, not an integrity check over the full table, and
`app.integrity.checks` is where full-table scans belong.

So a recompute of a real member emits no event (6.2 emits only when a position
actually changed) and there is nothing for the card consumer to consume. The
only way to drive the real producer is to give it the one input it is missing,
which is what sections [2] and [3] do: a synthetic topic, stance, roll call
and member vote carrying that topic. Every other part of the chain — the
scorer, the event, the consumer, the repositories, the reference checks — is
the real production code path against the real API.

===============================================================================
WHAT IT PROVES, IN ORDER
===============================================================================

  1. **The live topic-classification state, measured** — before this script
     writes anything, so the measurement is of the project and not of this
     run's own output. Why a real member cannot drive this test, re-measured
     rather than remembered.
  2. **The synthetic graph writes through the real repositories** — reference
     checks, deterministic ids, provenance — against live ZeroDB.
  3. **Story 6.2's real scorer writes a real position and emits a real
     `positions.recomputed` event** into the live event log.
  4. **Story 7.2's real consumer reads that real event and rebuilds a real
     card**, whose `positions_summary` reflects the real recompute.
  5. **A card read is a SINGLE-TABLE read that aggregates nothing** — proved
     by counting the live HTTP calls a read makes and asserting that none of
     them touched `candidate_positions`, `verdicts`, `member_votes`, `people`
     or `districts`. This is story 7.2's "never aggregates at request time"
     criterion, measured against the live API rather than the fake.
  6. **The live `verdict.published` state, measured** — zero `verdicts` rows
     and zero events of the topic, which is the honest limit of this script.
  7. **The verdict rebuild works against live ZeroDB over a synthetic verdict
     chain**, including `is_current` on a superseded verdict.
  8. **A positions rebuild does not clear verdict counts, and vice versa** —
     the partial-merge property, against a store that returns rows in
     whatever order it likes.
  9. **datamodel.md's documented (election, district) query returns the right
     cards and excludes a card from another district** — against the live
     server-side filter, which matches nothing (story 1.4), so this exercises
     the client's paging fallback for real.
 10. **`say_do_gap` and the floats round-trip as real numbers** — `1.0` coming
     back as `"1.0"` would render and then break the first time a UI compared
     it. The hazard story 12.2 pinned for tier weights, now for the number a
     voter reads off a card.
 11. **No voter-shaped field reached the stored row.**
 12. **Cleanup removes every synthetic row**, confirmed by a live re-read of
     each id.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_candidate_cards_live.py
    python backend/scripts/verify_candidate_cards_live.py --keep   # debug only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ballot.cards import (  # noqa: E402
    POSITIONS_RECOMPUTED_TOPIC,
    VERDICT_PUBLISHED_TOPIC,
    CandidateCardBuilder,
    CandidateCardReader,
    CandidateCardRebuildConsumer,
    emit_verdict_published,
)
from app.repositories import ProvenanceContext, Repository  # noqa: E402
from app.repositories.models import (  # noqa: E402
    CandidateCard,
    IssueTopic,
    MemberVote,
    Person,
    Promise,
    RollCall,
    Stance,
    Verdict,
)
from app.scoring.positions import PositionComputer  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402

# --- synthetic ids, every one marked as such -------------------------------
#
# `TEST-CARDS` in the middle of each id so a human scanning the live project
# can tell at a glance that nothing here is a real candidate, topic or vote.
# The person id is deliberately not a valid bioguide pattern.

PERSON_ID = "person:TEST-CARDS-000"
TOPIC_ID = "en-US:test-cards-topic:v1"
STANCE_ID = f"{TOPIC_ID}:for"
ROLL_CALL_IDS = [f"us-house:119-2:98{index}" for index in range(3)]

#: Synthetic, NOT a reuse of the live `us-2026-11-03-general` election or any
#: real district: a synthetic candidacy under a real district would appear in
#: a real ballot read, which is the one thing this script must not do.
ELECTION_ID = "test-cards-election"
DISTRICT_ID = "ocd-division/country:us/state:zz/cd:test-cards"
OTHER_DISTRICT_ID = "ocd-division/country:us/state:zz/cd:test-cards-other"
CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:{PERSON_ID}"
OTHER_CANDIDACY_ID = f"{ELECTION_ID}:{OTHER_DISTRICT_ID}:{PERSON_ID}"

PROMISE_SOURCE_ID = "src:test-cards-0001"
PROMISE_IDS = [f"prm:test-cards-{index:04d}" for index in range(3)]

PROVENANCE = ProvenanceContext(adapter_version="verify_candidate_cards_live/v1")

#: Tables whose appearance in a card read would mean the read aggregated.
#: Checked in section [5] against the live HTTP calls.
FORBIDDEN_ON_A_CARD_READ = (
    "candidate_positions",
    "verdicts",
    "member_votes",
    "promises",
    "people",
    "districts",
    "candidacies",
)


class Failure(AssertionError):
    """A live check failed. Raised rather than printed so cleanup still runs."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)
    print(f"  ok   {message}")


def info(message: str) -> None:
    print(f"  info {message}")


# --- [1] the synthetic graph ------------------------------------------------


def write_graph(client: ZeroDBClient) -> None:
    print("\n[2] writing the synthetic graph through the real repositories")

    Repository(client=client, model=IssueTopic).upsert(
        {
            "id": TOPIC_ID,
            "name": "SYNTHETIC TEST TOPIC (verify_candidate_cards_live)",
            "locale": "en-US",
            "taxonomy_version": 1,
        }
    )
    Repository(client=client, model=Stance).upsert(
        {
            "id": STANCE_ID,
            "topic_id": TOPIC_ID,
            "label": "SYNTHETIC TEST STANCE (verify_candidate_cards_live)",
            "description": (
                "Synthetic stance card written by "
                "backend/scripts/verify_candidate_cards_live.py. Not editorial "
                "content; deleted at the end of the run."
            ),
            "direction_code": "+1",
            "wording_version": 1,
        }
    )
    Repository(client=client, model=Person).upsert(
        {
            "id": PERSON_ID,
            "canonical_name": "SYNTHETIC TEST CANDIDATE (not a real person)",
            "photo_url": "https://example.invalid/synthetic-test-cards.jpg",
            "terms": [{"body_id": "us-house", "party": "Synthetic", "start": "2025-01-03"}],
        }
    )

    # `elections`, `districts` and `candidacies` written through the client
    # rather than their repositories, for the reason
    # `verify_alignment_scoring_live.py` already documents: `Election`
    # references `jurisdictions`, and resolving that would mean either reusing
    # the real `us-federal` row (making these synthetic rows children of real
    # reference data) or writing a synthetic jurisdiction too. Every table the
    # CODE UNDER TEST reads or writes — `candidate_cards`,
    # `candidate_positions`, `verdicts`, `member_votes`, `roll_calls` — goes
    # through a real repository, so the reference checks and provenance rules
    # are genuinely exercised.
    client.upsert_row(
        "elections",
        ELECTION_ID,
        {
            "id": ELECTION_ID,
            "jurisdiction_id": "us-federal",
            "name": "SYNTHETIC TEST ELECTION (verify_candidate_cards_live)",
            "election_date": "2026-11-03",
            "kind": "general",
        },
    )
    for district_id, name in (
        (DISTRICT_ID, "SYNTHETIC TEST DISTRICT"),
        (OTHER_DISTRICT_ID, "SYNTHETIC TEST DISTRICT (other)"),
    ):
        client.upsert_row(
            "districts",
            district_id,
            {
                "id": district_id,
                "body_id": "us-house",
                "name": name,
                "state": "ZZ",
                "valid_from": "2025-01-03",
            },
        )
    for candidacy_id, district_id in (
        (CANDIDACY_ID, DISTRICT_ID),
        (OTHER_CANDIDACY_ID, OTHER_DISTRICT_ID),
    ):
        client.upsert_row(
            "candidacies",
            candidacy_id,
            {
                "id": candidacy_id,
                "election_id": ELECTION_ID,
                "district_id": district_id,
                "person_id": PERSON_ID,
                "person_name": "SYNTHETIC TEST CANDIDATE (not a real person)",
                "party": "Synthetic",
                "is_incumbent": True,
                "status": "filed",
            },
        )

    rolls: Repository[RollCall] = Repository(client=client, model=RollCall, provenance=PROVENANCE)
    votes: Repository[MemberVote] = Repository(client=client, model=MemberVote)
    # Three roll calls, all Yea on the synthetic topic, so story 6.2's real
    # scorer computes a real position of +1.0 with an evidence count of 3.
    for roll_id in ROLL_CALL_IDS:
        rolls.upsert(
            {
                "id": roll_id,
                "voted_at": "2026-04-01T12:00:00Z",
                "question": "SYNTHETIC TEST ROLL CALL (verify_candidate_cards_live)",
                "vote_type": "final_passage",
                "result": "Passed",
                "topic_ids": [TOPIC_ID],
                "source_url": f"https://example.invalid/synthetic-cards/{roll_id}",
                "content_hash": f"synthetic-cards-{roll_id}",
            }
        )
        votes.upsert(
            {
                "roll_call_id": roll_id,
                "person_id": PERSON_ID,
                "position": "yea",
                "voted_at": "2026-04-01T12:00:00Z",
                "vote_type": "final_passage",
                "topic_ids": [TOPIC_ID],
                "is_bundled": False,
            }
        )

    check(client.get_row("people", PERSON_ID) is not None, "the synthetic person reads back")
    check(
        client.get_row("candidacies", CANDIDACY_ID) is not None,
        "the synthetic candidacy reads back",
    )
    check(
        all(
            client.get_row("member_votes", f"{roll}:TEST-CARDS-000") is not None
            for roll in ROLL_CALL_IDS
        ),
        f"all {len(ROLL_CALL_IDS)} synthetic member_votes read back",
    )


# --- [2] why a real member cannot drive this test --------------------------


def measure_live_topic_coverage(client: ZeroDBClient) -> None:
    """Re-measure, rather than remember, why the chain below is synthetic.

    If this section ever reports a non-zero count, story 5.5's classifier has
    started accepting tags and a FOLLOW-UP is available: drive this whole
    script against a real incumbent instead. That is a better test than this
    one and it is deliberately gated on a measurement rather than on someone
    remembering to check.

    **Runs BEFORE `write_graph`, and this ordering is load-bearing.** The
    first version of this script ran it after, and it failed on its own
    output: the three synthetic `member_votes` rows it had just written carry
    the synthetic `topic_ids` that are the whole point, so the measurement
    reported "3 of 503 rows carry a topic_id" and the check tripped. A
    measurement of the live state has to be taken before this script perturbs
    the live state. The `TEST-` filter below is belt and braces for a run
    where a previous `--keep` left rows behind.
    """
    print("\n[1] measuring the live topic-classification state (BEFORE writing anything)")
    for table, limit in (("member_votes", 1000), ("roll_calls", 1000), ("bills", 1000)):
        page = client.query_rows(table, limit=limit)
        real_rows = [row for row in page.rows if "TEST" not in str(row.row_data.get("id", ""))]
        with_topics = [row for row in real_rows if row.row_data.get("topic_ids")]
        info(f"{table}: {len(with_topics)} of {len(real_rows)} real rows read carry a topic_id")
        check(
            len(with_topics) == 0,
            f"no real live {table} row carries a topic_id, so a real member scores nothing "
            "(story 5.5 accepts no tag automatically: 54.2% precision vs an 85% bar). "
            "If this check ever FAILS, that is good news: rerun this script against a "
            "real incumbent.",
        )


# --- [3] the real producer -------------------------------------------------


def recompute_positions(client: ZeroDBClient) -> dict[str, Any]:
    print("\n[3] running story 6.2's REAL scorer against live ZeroDB")

    before = len(client.list_events(topic=POSITIONS_RECOMPUTED_TOPIC, limit=500).events)
    info(f"live {POSITIONS_RECOMPUTED_TOPIC} events before: {before}")

    computer = PositionComputer(client)
    stance = Repository(client=client, model=Stance).get(STANCE_ID)
    if stance is None:
        raise Failure("the synthetic stance did not read back")
    result = computer.recompute_person(PERSON_ID, stances=[stance])

    check(
        result.positions_written == 1,
        f"the real scorer wrote {result.positions_written} position(s) from the "
        "synthetic votes (expected 1)",
    )
    stored = computer.current_position(person_id=PERSON_ID, stance_id=STANCE_ID)
    check(stored is not None, "the position reads back from live candidate_positions")
    assert stored is not None
    info(
        f"live position: s_value={stored.s_value}, confidence={stored.confidence}, "
        f"evidence_count={stored.evidence_count}, methodology={stored.methodology_version}"
    )
    check(
        stored.s_value == 1.0 and stored.evidence_count == len(ROLL_CALL_IDS),
        f"three Yea final-passage votes give s_value {stored.s_value} on "
        f"{stored.evidence_count} records (expected 1.0 on {len(ROLL_CALL_IDS)})",
    )

    after = len(client.list_events(topic=POSITIONS_RECOMPUTED_TOPIC, limit=500).events)
    info(f"live {POSITIONS_RECOMPUTED_TOPIC} events after: {after}")
    check(
        after > before,
        f"the real recompute emitted a real {POSITIONS_RECOMPUTED_TOPIC} event into the "
        f"live log ({before} -> {after})",
    )
    return {
        "methodology_version": stored.methodology_version,
        "s_value": stored.s_value,
        "evidence_count": stored.evidence_count,
    }


# --- [4] the real consumer -------------------------------------------------


def consumer_starting_from_now(
    client: ZeroDBClient, pre_existing: set[str]
) -> CandidateCardRebuildConsumer:
    """A real consumer that will ignore every event that predates this run.

    ===========================================================================
    WHY THIS EXISTS — A REAL FINDING, NOT A CONVENIENCE
    ===========================================================================

    The first version of this script ran a bare consumer, passed on its first
    run, and then **failed on its second** with:

        FAILED: a positions-only rebuild left verdict_counts ABSENT

    The cause is a genuine property of the system rather than a flake.
    ZeroDB's event log is **append-only and has no delete route**, so the
    `verdict.published` event section [7] emits survives the run that emitted
    it — cleanup can remove every row but not that event. On the next run,
    section [4]'s consumer polls BOTH topics, reads the previous run's
    `verdict.published` event, correctly rebuilds verdict counts from it, and
    section [4]'s "a positions-only rebuild leaves verdict_counts absent"
    assertion is then false. The consumer was right; the script was wrong to
    assume a clean log.

    **What this means beyond the script.** `CandidateCardRebuildConsumer`
    tracks handled events in-process (`self._handled`) and says in its own
    docstring that this is lost on restart, which is "deliberately tolerable
    because a rebuild is idempotent". That is true of the ROW — a replay
    restates the same card — and this finding does not contradict it. What a
    replay is not is *free*, and in a deployment that restarts often, every
    restart re-reads and re-rebuilds the whole retained window. A durable
    cursor is the fix, it belongs with the durable scheduler this codebase
    does not have, and it is flagged in this branch's PR rather than smuggled
    in here.

    This helper is that durable cursor, for one run: it pre-loads the
    consumer's handled set with every event id that already existed, so the
    consumer processes exactly the events this run caused. Reaching into
    `_handled` is deliberate and is the honest version of the alternative —
    which would have been to drop section [4]'s absence assertion, i.e. to
    delete the check that caught this.
    """
    consumer = CandidateCardRebuildConsumer(client)
    consumer._handled.update(pre_existing)  # noqa: SLF001 - see this function's docstring
    info(f"consumer primed to ignore {len(pre_existing)} pre-existing event(s)")
    return consumer


def pre_existing_event_ids(client: ZeroDBClient) -> set[str]:
    """Every rebuild-topic event id already in the live log, before this run."""
    found: set[str] = set()
    for topic in (VERDICT_PUBLISHED_TOPIC, POSITIONS_RECOMPUTED_TOPIC):
        for event in client.list_events(topic=topic, limit=500).events:
            if event.event_id:
                found.add(event.event_id)
    return found


def rebuild_from_the_real_event(
    client: ZeroDBClient, expected: dict[str, Any], pre_existing: set[str]
) -> None:
    print("\n[4] running story 7.2's REAL consumer over the live event log")

    consumer = consumer_starting_from_now(client, pre_existing)
    result = consumer.run(limit=500)
    info(f"rebuild result: {result.summary()}")
    check(
        CANDIDACY_ID in result.candidacy_ids,
        f"the consumer rebuilt the synthetic card {CANDIDACY_ID} from a real "
        f"{POSITIONS_RECOMPUTED_TOPIC} event it read back from the live log",
    )

    card = Repository(client=client, model=CandidateCard).get(CANDIDACY_ID)
    check(card is not None, "the card reads back from live candidate_cards")
    assert card is not None
    info(
        f"live card: person_name={card.person_name!r}, party={card.party!r}, "
        f"district={card.district!r}, methodology={card.methodology_version!r}"
    )
    check(card.id == CANDIDACY_ID, "the card's id is datamodel.md's {candidacy_id}")
    check(card.person_name.startswith("SYNTHETIC"), "the card names the candidate as filed")
    check(card.district == "SYNTHETIC TEST DISTRICT", "the card carries the readable district name")
    check(
        card.photo_url == "https://example.invalid/synthetic-test-cards.jpg",
        "the card carries the person's photo_url, joined from `people` at rebuild time",
    )
    check(
        card.positions_summary is not None and len(card.positions_summary) == 1,
        "the card's positions_summary holds the one real recomputed position",
    )
    assert card.positions_summary is not None
    entry = card.positions_summary[0]
    info(
        f"live summary entry: stance={entry.stance_id}, s_value={entry.s_value}, "
        f"confidence={entry.confidence}, evidence_count={entry.evidence_count}"
    )
    check(
        entry.stance_id == STANCE_ID
        and entry.s_value == expected["s_value"]
        and entry.evidence_count == expected["evidence_count"],
        "the summarised position matches the live candidate_positions row exactly",
    )
    check(
        card.methodology_version == expected["methodology_version"],
        f"the card names the live methodology version {expected['methodology_version']!r} "
        "the event carried",
    )
    # The honest-absence half of the criterion, against live data.
    check(
        card.verdict_counts is None,
        "a positions-only rebuild left verdict_counts ABSENT, not an all-zero tally "
        "that would read as 'kept none of their promises'",
    )
    check(card.say_do_gap is None, "a positions-only rebuild left say_do_gap absent")


# --- [5] never aggregates at request time ----------------------------------


def prove_a_card_read_aggregates_nothing(client: ZeroDBClient) -> None:
    """Story 7.2's second acceptance criterion, measured on the live wire.

    Counts the actual HTTP calls a live card read makes and asserts which
    tables they touched. The claim is about TABLES, not request count: live
    ZeroDB's server-side filter matches nothing (story 1.4), so one logical
    read is a `/query` POST plus one or more paging `/rows` GETs — a client
    defect with a named escalation, not something this read model can route
    around. What matters, and what is asserted, is that every one of those
    calls is against `candidate_cards` and none of them aggregates.
    """
    print("\n[5] proving a live card read is one table and no aggregation")

    calls: list[tuple[str, str]] = []
    original_request = client._transport.request  # noqa: SLF001 - instrumenting the wire

    def recording_request(method: str, path: str, **kwargs: Any) -> Any:
        calls.append((method, path))
        return original_request(method, path, **kwargs)

    client._transport.request = recording_request  # type: ignore[method-assign]  # noqa: SLF001
    try:
        card = CandidateCardReader(client).card_for(CANDIDACY_ID)
    finally:
        client._transport.request = original_request  # type: ignore[method-assign]  # noqa: SLF001

    check(card is not None, "the live read returned the card")
    tables = {path.split("/tables/")[1].split("/")[0] for _, path in calls if "/tables/" in path}
    info(f"the live read made {len(calls)} HTTP call(s), touching tables {sorted(tables)}")
    check(
        tables == {"candidate_cards"},
        f"a live card read touched ONLY candidate_cards (touched: {sorted(tables)})",
    )
    for forbidden in FORBIDDEN_ON_A_CARD_READ:
        check(
            forbidden not in tables,
            f"the live card read never touched {forbidden} — no request-time aggregation",
        )

    # And the race query, which is the read story 7.1's ballot view actually
    # makes: still one table, for a whole race rather than one candidate.
    calls.clear()
    client._transport.request = recording_request  # type: ignore[method-assign]  # noqa: SLF001
    try:
        cards = CandidateCardReader(client).cards_for_race(
            election_id=ELECTION_ID, district_id=DISTRICT_ID
        )
    finally:
        client._transport.request = original_request  # type: ignore[method-assign]  # noqa: SLF001
    race_tables = {
        path.split("/tables/")[1].split("/")[0] for _, path in calls if "/tables/" in path
    }
    info(
        f"the live race read made {len(calls)} HTTP call(s) for {len(cards)} card(s), "
        f"touching tables {sorted(race_tables)}"
    )
    check(
        race_tables == {"candidate_cards"},
        f"a live race read touched ONLY candidate_cards (touched: {sorted(race_tables)})",
    )


# --- [6] the honest limit --------------------------------------------------


def measure_live_verdict_state(client: ZeroDBClient) -> None:
    """Measure, rather than assert from memory, that epic 11 has not shipped.

    This section is the reason this script can be trusted about what it did
    not verify. If it ever reports non-zero, a real `verdict.published` path
    has appeared and story 11.6's measurement becomes possible.
    """
    print("\n[6] measuring the live verdict.published state (the honest limit)")

    verdict_rows = client.query_rows("verdicts", limit=500)
    real_verdicts = [
        row for row in verdict_rows.rows if "TEST-CARDS" not in str(row.row_data.get("person_id"))
    ]
    info(f"live verdicts rows (excluding this script's own): {len(real_verdicts)}")
    events = client.list_events(topic=VERDICT_PUBLISHED_TOPIC, limit=500)
    info(f"live {VERDICT_PUBLISHED_TOPIC} events: {len(events.events)}")
    check(
        len(real_verdicts) == 0,
        "the live verdicts table holds no real rows, so no real card can carry a real "
        "verdict count — epic 11 is unbuilt and this script says so rather than "
        "implying otherwise",
    )
    info(
        "CONSEQUENCE: section [7] drives verdict.published from a synthetic chain this "
        "script writes. The row shapes, reference checks, is_current handling and "
        "consumer are verified live; epic 11's eventual output is NOT. Story 11.6 owns "
        "that measurement."
    )


# --- [7] the verdict rebuild over a synthetic chain ------------------------


def rebuild_from_verdicts(client: ZeroDBClient, pre_existing: set[str]) -> None:
    print("\n[7] verdict rebuild against live ZeroDB, over a synthetic verdict chain")

    client.upsert_row(
        "promise_sources",
        PROMISE_SOURCE_ID,
        {
            "id": PROMISE_SOURCE_ID,
            "candidacy_id": CANDIDACY_ID,
            "source_type": "campaign_site",
            "url": "https://example.invalid/synthetic-cards/promises",
            "archived_file_id": "file:synthetic-test-cards-0001",
            "captured_at": "2026-10-01T00:00:00Z",
        },
    )
    promises: Repository[Promise] = Repository(client=client, model=Promise)
    verdicts: Repository[Verdict] = Repository(client=client, model=Verdict)
    # Three promises: kept, kept, broken. Gap = 1/3, which is a non-trivial
    # float rather than 0.0 or 1.0 — so a store that stringified it, rounded
    # it, or dropped its precision would be caught in section [10].
    for index, (promise_id, value) in enumerate(
        zip(PROMISE_IDS, ("kept", "kept", "broken"), strict=True)
    ):
        promises.upsert(
            {
                "id": promise_id,
                "candidacy_id": CANDIDACY_ID,
                "person_id": PERSON_ID,
                "source_id": PROMISE_SOURCE_ID,
                "quote_span": f"SYNTHETIC: I will vote for synthetic measure {index}.",
                "actor": "I",
                "action": "vote for",
                "object": f"synthetic measure {index}",
                "direction": "support",
                "testable": True,
                "extractor_version": "verify_candidate_cards_live/v1",
                "confidence": 0.9,
            }
        )
        verdicts.append_version(
            {
                "promise_id": promise_id,
                "person_id": PERSON_ID,
                "verdict": value,
                "confidence": 0.8,
                "methodology_version": "2026.1",
                "qa_status": "approved",
                "created_at": "2026-10-19T11:00:00Z",
            }
        )

    # A superseded verdict on the FIRST promise: appended second, so
    # `is_current` moves to it and the first row is cleared. The card must
    # count one verdict for this promise, not two — the property that makes a
    # recompute-from-rows correct where a counter bump would not be.
    verdicts.append_version(
        {
            "promise_id": PROMISE_IDS[0],
            "person_id": PERSON_ID,
            "verdict": "partial",
            "confidence": 0.85,
            "methodology_version": "2026.1",
            "qa_status": "approved",
            "created_at": "2026-10-19T12:00:00Z",
        }
    )
    stored_versions = [
        row
        for row in client.query_rows("verdicts", limit=500).rows
        if row.row_data.get("promise_id") == PROMISE_IDS[0]
    ]
    info(f"live verdicts stored for the superseded promise: {len(stored_versions)}")
    check(
        len(stored_versions) == 2,
        "the superseded verdict is still stored (append-only, datamodel.md rule 6)",
    )
    check(
        sum(1 for row in stored_versions if row.row_data.get("is_current") is True) == 1,
        "exactly one of the two live verdict versions is marked current",
    )

    before = len(client.list_events(topic=VERDICT_PUBLISHED_TOPIC, limit=500).events)
    emit_verdict_published(
        client,
        verdict_id=f"{PROMISE_IDS[0]}:v2",
        person_id=PERSON_ID,
        verdict="partial",
    )
    after = len(client.list_events(topic=VERDICT_PUBLISHED_TOPIC, limit=500).events)
    check(
        after == before + 1,
        f"a real {VERDICT_PUBLISHED_TOPIC} event was written to the live log "
        "(by THIS SCRIPT, not by a verdict engine — see section [6])",
    )

    result = consumer_starting_from_now(client, pre_existing).run(limit=500)
    info(f"rebuild result: {result.summary()}")
    check(
        CANDIDACY_ID in result.candidacy_ids,
        "the consumer rebuilt the card from the live verdict.published event",
    )

    card = Repository(client=client, model=CandidateCard).get(CANDIDACY_ID)
    assert card is not None
    assert card.verdict_counts is not None
    counts = card.verdict_counts
    info(
        f"live verdict_counts: kept={counts.kept}, partial={counts.partial}, "
        f"broken={counts.broken}, total={counts.total}; say_do_gap={card.say_do_gap}"
    )
    check(
        counts.total == len(PROMISE_IDS),
        f"the card counted {counts.total} verdicts for {len(PROMISE_IDS)} promises — "
        "the superseded version was not double-counted",
    )
    check(
        counts.kept == 1 and counts.partial == 1 and counts.broken == 1,
        "the counts reflect the CURRENT verdict on each promise (kept, partial, broken)",
    )
    expected_gap = (0.0 + 0.5 + 1.0) / 3
    check(
        card.say_do_gap is not None and abs(card.say_do_gap - expected_gap) < 1e-9,
        f"say_do_gap is {card.say_do_gap} (expected {expected_gap:.6f}: one kept, one "
        "partial at 0.5, one broken)",
    )


# --- [8] the partial-merge property ---------------------------------------


def prove_the_merge_is_partial(client: ZeroDBClient) -> None:
    print("\n[8] proving a rebuild merges rather than overwrites, live")

    builder = CandidateCardBuilder(client)
    cards: Repository[CandidateCard] = Repository(client=client, model=CandidateCard)

    # A positions rebuild, after the verdict rebuild in [7] filled the other
    # half. Nothing about verdicts may be lost.
    builder.rebuild_from_positions(PERSON_ID)
    card = cards.get(CANDIDACY_ID)
    assert card is not None
    check(
        card.verdict_counts is not None and card.verdict_counts.total == len(PROMISE_IDS),
        "a positions rebuild did NOT clear the verdict counts a verdict rebuild wrote",
    )
    check(card.say_do_gap is not None, "a positions rebuild did NOT clear say_do_gap")
    check(
        card.positions_summary is not None and len(card.positions_summary) == 1,
        "the positions rebuild restated the positions summary",
    )

    # And the other direction.
    builder.rebuild_from_verdicts(PERSON_ID)
    card = cards.get(CANDIDACY_ID)
    assert card is not None
    check(
        card.positions_summary is not None and len(card.positions_summary) == 1,
        "a verdict rebuild did NOT clear the positions summary",
    )
    check(
        card.methodology_version is not None,
        "a verdict rebuild did NOT clear the methodology version",
    )


# --- [9] datamodel.md's documented query ----------------------------------


def prove_the_race_query(client: ZeroDBClient) -> None:
    print("\n[9] datamodel.md's 'By (election, district)' query, against the live filter")

    # The second candidacy is in a DIFFERENT district, so it must be excluded.
    CandidateCardBuilder(client).rebuild_candidacy(OTHER_CANDIDACY_ID)
    reader = CandidateCardReader(client)

    cards = reader.cards_for_race(election_id=ELECTION_ID, district_id=DISTRICT_ID)
    returned = [card.candidacy_id for card in cards]
    info(f"live cards for ({ELECTION_ID}, {DISTRICT_ID}): {returned}")
    check(
        returned == [CANDIDACY_ID],
        f"the race query returned exactly the card for this district (got {returned})",
    )
    check(
        OTHER_CANDIDACY_ID not in returned,
        "the card from the OTHER district was excluded — the live server-side filter "
        "matches nothing (story 1.4), so this is the client's paging fallback working",
    )

    other = reader.cards_for_race(election_id=ELECTION_ID, district_id=OTHER_DISTRICT_ID)
    check(
        [card.candidacy_id for card in other] == [OTHER_CANDIDACY_ID],
        "and the other district's query returns its own card",
    )


# --- [10] float round-tripping and [11] no voter fields -------------------


def prove_the_row_shape(client: ZeroDBClient) -> None:
    print("\n[10] floats round-trip as numbers, and [11] no voter-shaped field")

    row = client.get_row("candidate_cards", CANDIDACY_ID)
    check(row is not None, "the raw live row reads back")
    assert row is not None
    data = row.row_data

    gap = data.get("say_do_gap")
    info(f"raw live say_do_gap: {gap!r} ({type(gap).__name__})")
    check(
        isinstance(gap, float),
        f"say_do_gap round-tripped as a real number (got {type(gap).__name__}) — a string "
        "here would render and then break the first time a UI compared it (the hazard "
        "story 12.2 pinned for tier weights)",
    )
    summary = data.get("positions_summary")
    check(isinstance(summary, list) and summary, "positions_summary round-tripped as an array")
    assert isinstance(summary, list)
    entry = summary[0]
    info(f"raw live summary entry: {entry!r}")
    check(
        isinstance(entry.get("s_value"), float) and isinstance(entry.get("confidence"), float),
        "the summary entry's s_value and confidence round-tripped as floats",
    )
    check(
        isinstance(entry.get("evidence_count"), int),
        "the summary entry's evidence_count round-tripped as an int",
    )
    counts = data.get("verdict_counts")
    check(isinstance(counts, dict), "verdict_counts round-tripped as an object")
    assert isinstance(counts, dict)
    info(f"raw live verdict_counts: {counts!r}")
    check(
        all(isinstance(value, int) for value in counts.values()),
        "every verdict count round-tripped as an int",
    )

    forbidden = ("profile", "interest", "voter", "user", "alignment", "dealbreaker", "tier")
    offending = [key for key in data if any(word in key.lower() for word in forbidden)]
    check(
        offending == [],
        f"no stored card field is voter-shaped (offending: {offending}). datamodel.md: "
        "'no table links a person's priorities to a candidate'",
    )


# --- [12] cleanup ---------------------------------------------------------


def clean_up(client: ZeroDBClient) -> None:
    print("\n[12] deleting every synthetic row, then re-reading to confirm")

    # `candidate_positions` and `verdicts` are append-only and
    # `Repository.delete` refuses them by design (datamodel.md keeps both
    # indefinitely for reproducibility). That rule is about REAL rows; these
    # are fabricated claims about a person who does not exist, and leaving
    # them would mean the live project permanently asserted a non-existent
    # candidate's record. So they go through the client directly — the same
    # legitimate use of that escape hatch
    # `verify_alignment_scoring_live.py` documents, called out here rather
    # than done quietly.
    def ids_for(table: str, predicate: Any) -> list[str]:
        return [
            str(row.row_data["id"])
            for row in client.query_rows(table, limit=500).rows
            if predicate(row.row_data)
        ]

    is_synthetic_person = lambda data: data.get("person_id") == PERSON_ID  # noqa: E731
    targets: list[tuple[str, str]] = []
    targets += [("candidate_cards", CANDIDACY_ID), ("candidate_cards", OTHER_CANDIDACY_ID)]
    targets += [
        ("candidate_positions", row_id)
        for row_id in ids_for("candidate_positions", is_synthetic_person)
    ]
    targets += [("verdicts", row_id) for row_id in ids_for("verdicts", is_synthetic_person)]
    targets += [("promises", row_id) for row_id in PROMISE_IDS]
    targets += [("promise_sources", PROMISE_SOURCE_ID)]
    targets += [("member_votes", row_id) for row_id in ids_for("member_votes", is_synthetic_person)]
    targets += [("roll_calls", roll_id) for roll_id in ROLL_CALL_IDS]
    targets += [
        ("provenance_log", row_id)
        for row_id in ids_for(
            "provenance_log", lambda data: str(data.get("entity_id", "")) in ROLL_CALL_IDS
        )
    ]
    targets += [("candidacies", CANDIDACY_ID), ("candidacies", OTHER_CANDIDACY_ID)]
    targets += [("districts", DISTRICT_ID), ("districts", OTHER_DISTRICT_ID)]
    targets += [("elections", ELECTION_ID)]
    targets += [("stances", STANCE_ID)]
    targets += [("issue_topics", TOPIC_ID)]
    targets += [("people", PERSON_ID)]

    deleted = 0
    for table, row_id in targets:
        try:
            client.delete_row(table, row_id)
            deleted += 1
        except Exception as exc:  # noqa: BLE001 - report and keep deleting
            print(f"  warn  {table}/{row_id}: {exc}")
    info(f"issued {deleted} delete(s)")

    remaining = [
        f"{table}/{row_id}"
        for table, row_id in targets
        if client.get_row(table, row_id) is not None
    ]
    check(remaining == [], f"every synthetic row is gone (still present: {remaining})")
    check(
        ids_for("candidate_cards", lambda data: PERSON_ID in str(data)) == [],
        "no candidate_cards row mentions the synthetic candidate any more",
    )
    info(
        "NOTE: the events this run emitted are NOT deleted. ZeroDB's event log is "
        "append-only and has no delete route, and the events name only the synthetic "
        "person id. The same residue every event-emitting verification script in this "
        "repo leaves."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help=(
            "skip cleanup. Debug only: the synthetic rows assert a non-existent "
            "candidate's record and must not be left in the live project."
        ),
    )
    args = parser.parse_args()

    client = ZeroDBClient.from_env()
    print(f"live project: {client.project_id}")

    failure: BaseException | None = None
    try:
        # The live-state measurement runs FIRST: it has to read the project
        # before this script perturbs it. See
        # `measure_live_topic_coverage`'s own docstring for the failure that
        # taught this ordering.
        measure_live_topic_coverage(client)
        # Snapshotted BEFORE anything is emitted, so both consumers below
        # process only the events this run causes. ZeroDB's event log is
        # append-only with no delete route, so a previous run's events are
        # still there -- see `consumer_starting_from_now` for the real finding
        # that made this necessary.
        pre_existing = pre_existing_event_ids(client)
        write_graph(client)
        expected = recompute_positions(client)
        rebuild_from_the_real_event(client, expected, pre_existing)
        prove_a_card_read_aggregates_nothing(client)
        measure_live_verdict_state(client)
        rebuild_from_verdicts(client, pre_existing)
        prove_the_merge_is_partial(client)
        prove_the_race_query(client)
        prove_the_row_shape(client)
    except BaseException as exc:  # noqa: BLE001 - cleanup must run regardless
        failure = exc
        print(f"\nFAILED: {exc}")

    if args.keep:
        print("\n--keep: synthetic rows left in place. Clean them up by hand.")
    else:
        try:
            clean_up(client)
        except BaseException as exc:  # noqa: BLE001
            print(f"\nCLEANUP FAILED: {exc}")
            failure = failure or exc

    client.close()
    if failure is not None:
        return 1
    print(
        "\nall live checks passed, and all synthetic data was removed.\n"
        "VERIFIED LIVE: the positions.recomputed trigger, end to end through the real\n"
        "  scorer, a real event in the live log, and the real consumer.\n"
        "NOT VERIFIED LIVE: the verdict.published trigger against a real producer —\n"
        "  epic 11 is unbuilt and section [6] measures that on every run. Story 11.6\n"
        "  owns that measurement."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
