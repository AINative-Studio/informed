"""Step definitions for tests/features/candidate_positions.feature (story 6.2).

Runs against story 1.2's in-memory fake transport, never live ZeroDB, so CI
needs no credentials — the rule every backend feature in this suite follows.
The live confirmation lives in `scripts/verify_alignment_scoring_live.py`.

`break_server_side_filter()` is applied for the reason every story since 1.4
applies it: live ZeroDB's server-side `/query` filter matches nothing, so every
read here rests on the client's own paging fallback. It matters more in this
story than in most — `PositionComputer` reads `member_votes` filtered by
`person_id`, and a filter that silently worked in tests and silently did not in
production would score one member's position from another member's votes.

All synthetic. The test person is `person:TEST000000`, which is not a real
bioguide id, and no real incumbent's position is computed anywhere in this
suite.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.methodology.publish import DEFAULT_VOTE_TYPE_WEIGHTS, MethodologyPublisher
from app.repositories import Repository
from app.repositories.models import (
    CandidatePosition,
    IssueTopic,
    MemberVote,
    Person,
    RollCall,
    Stance,
    Term,
)
from app.scoring.positions import (
    BUNDLED_WEIGHT_FACTOR,
    CONFIDENCE_SATURATION_WEIGHT,
    POSITIONS_RECOMPUTED_TOPIC,
    PositionComputer,
    PositionRecomputeConsumer,
    emit_rollcall_ingested,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/candidate_positions.feature")

#: Synthetic ids. `TEST000000` is deliberately not a valid bioguide pattern, so
#: a row from this suite can never be mistaken for a real member's.
PERSON_ID = "person:TEST000000"
MIRROR_PERSON_ID = "person:TEST000001"
TOPIC_ID = "en-US:test-scoring-topic:v1"
OTHER_TOPIC_ID = "en-US:test-scoring-other:v1"
STANCE_ID = f"{TOPIC_ID}:for"
BODY_ID = "us-house"


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(client, pace=lambda: None).run()
    return client


@pytest.fixture
def seen() -> dict[str, Any]:
    """Carries state between Given, When and Then. Nothing voter-derived can
    reach it: this story computes from public votes against public stance
    cards."""
    return {"roll_calls": 0}


@pytest.fixture
def computer(zerodb: ZeroDBClient) -> PositionComputer:
    return PositionComputer(zerodb)


# --- helpers --------------------------------------------------------------


def _seed_graph(zerodb: ZeroDBClient) -> None:
    """The reference rows `member_votes` and `candidate_positions` reference.

    Written through real repositories rather than raw `upsert_row`, so the
    reference checks story 1.4 enforces are actually exercised: a test that
    bypassed them could pass against data the production write path would
    refuse.
    """
    Repository(client=zerodb, model=IssueTopic).upsert_many(
        [
            {
                "id": TOPIC_ID,
                "name": "Synthetic scoring topic",
                "locale": "en-US",
                "taxonomy_version": 1,
            },
            {
                "id": OTHER_TOPIC_ID,
                "name": "Synthetic unrelated topic",
                "locale": "en-US",
                "taxonomy_version": 1,
            },
        ]
    )
    Repository(client=zerodb, model=Stance).upsert_many(
        [
            {
                "id": STANCE_ID,
                "topic_id": TOPIC_ID,
                "label": "Synthetic stance, positive direction",
                "description": "A synthetic stance card used only by this test suite.",
                "direction_code": "+1",
                "wording_version": 1,
            },
            {
                "id": f"{TOPIC_ID}:against",
                "topic_id": TOPIC_ID,
                "label": "Synthetic stance, negative direction",
                "description": "The mirror of the stance above, for symmetry checks.",
                "direction_code": "-1",
                "wording_version": 1,
            },
        ]
    )
    people = Repository(client=zerodb, model=Person)
    term = Term(body_id=BODY_ID, party="Synthetic", start="2025-01-03")
    for person_id in (PERSON_ID, MIRROR_PERSON_ID):
        people.upsert(
            {
                "id": person_id,
                "canonical_name": f"Synthetic Test Member {person_id[-1]}",
                "terms": [term.model_dump(mode="json")],
            }
        )


def _record_vote(
    zerodb: ZeroDBClient,
    seen: dict[str, Any],
    *,
    position: str,
    vote_type: str = "final_passage",
    is_bundled: bool = False,
    person_id: str = PERSON_ID,
    topic_ids: list[str] | None = None,
    roll_call_id: str | None = None,
) -> str:
    """Write one synthetic roll call and one member vote on it.

    Returns the roll call id. The roll call carries `topic_ids` because that is
    the association story 6.2 computes from (see `app.scoring.positions`'s
    scoping note), and the vote carries the same list because datamodel.md
    denormalises it onto `member_votes` so "scoring reads one table".
    """
    seen["roll_calls"] += 1
    roll = roll_call_id or f"us-house:119-2:{900 + seen['roll_calls']}"
    topics = TOPIC_ID if topic_ids is None else None
    resolved_topics = topic_ids if topic_ids is not None else [topics or TOPIC_ID]

    rolls: Repository[RollCall] = Repository(
        client=zerodb,
        model=RollCall,
        provenance=_provenance(),
    )
    if zerodb.get_row(RollCall.table_name, roll) is None:
        rolls.upsert(
            {
                "id": roll,
                "voted_at": f"2026-0{1 + (seen['roll_calls'] % 9)}-01T12:00:00Z",
                "question": "On passage of a synthetic test measure",
                "vote_type": vote_type,
                "result": "Passed",
                "is_bundled": is_bundled,
                "topic_ids": resolved_topics,
                "source_url": f"https://example.invalid/synthetic/{roll}",
                "content_hash": f"synthetic-{roll}",
            }
        )
    Repository(client=zerodb, model=MemberVote).upsert(
        {
            "roll_call_id": roll,
            "person_id": person_id,
            "position": position,
            "voted_at": f"2026-0{1 + (seen['roll_calls'] % 9)}-01T12:00:00Z",
            "vote_type": vote_type,
            "topic_ids": resolved_topics,
            "is_bundled": is_bundled,
        }
    )
    seen["last_roll_call_id"] = roll
    return roll


def _provenance() -> Any:
    from app.repositories import ProvenanceContext

    return ProvenanceContext(adapter_version="test_candidate_positions/v1")


def _stance(zerodb: ZeroDBClient, stance_id: str = STANCE_ID) -> Stance:
    row = zerodb.get_row(Stance.table_name, stance_id)
    assert row is not None, f"{stance_id} was not seeded"
    return Stance.model_validate(row.row_data)


def _positions(zerodb: ZeroDBClient, person_id: str = PERSON_ID) -> list[CandidatePosition]:
    page = zerodb.query_rows(CandidatePosition.table_name, limit=500)
    return [
        CandidatePosition.model_validate(row.row_data)
        for row in page.rows
        if row.row_data.get("person_id") == person_id
    ]


def _current(
    zerodb: ZeroDBClient,
    person_id: str = PERSON_ID,
    stance_id: str = STANCE_ID,
) -> CandidatePosition | None:
    """The current position for ONE (person, stance).

    Scoped to a stance deliberately: datamodel.md's invariant is "exactly one
    `is_current = true` row per (person, stance)", not per person, and this
    suite seeds two mirrored stance cards under one topic so the symmetry
    check has something to mirror. A recompute legitimately writes a current
    row for each.
    """
    live = [
        row
        for row in _positions(zerodb, person_id)
        if row.is_current is True and row.stance_id == stance_id
    ]
    assert len(live) <= 1, f"more than one current position: {[row.id for row in live]}"
    return live[0] if live else None


# --- Background -----------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _fake_client(zerodb: ZeroDBClient) -> None:
    assert zerodb.project_id == "proj-test"


@given("a published methodology with real vote type weights")
def _published_methodology(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    published = MethodologyPublisher(zerodb).publish_new_version("2027.1")
    assert published.weights["vote_types"] == DEFAULT_VOTE_TYPE_WEIGHTS
    seen["methodology_version"] = published.id


@given("a synthetic incumbent with a stance to be scored")
def _synthetic_incumbent(zerodb: ZeroDBClient) -> None:
    _seed_graph(zerodb)


# --- Given: votes ---------------------------------------------------------


@given("the incumbent voted for the stance on a final-passage roll call")
@when("the incumbent voted for the stance on a final-passage roll call")
def _voted_for_final(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _record_vote(zerodb, seen, position="yea")


@given("the incumbent voted against the stance on a procedural roll call")
def _voted_against_procedural(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _record_vote(zerodb, seen, position="nay", vote_type="procedural")


@given("the incumbent voted for the stance on a procedural roll call")
def _voted_for_procedural(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _record_vote(zerodb, seen, position="yea", vote_type="procedural")


@given("the incumbent voted for the stance on a bundled final-passage roll call")
def _voted_for_bundled(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _record_vote(zerodb, seen, position="yea", is_bundled=True)


@given(parsers.parse("the incumbent voted for the stance on {count:d} final-passage roll calls"))
def _voted_for_n(zerodb: ZeroDBClient, seen: dict[str, Any], count: int) -> None:
    for _ in range(count):
        _record_vote(zerodb, seen, position="yea")


@given(
    parsers.parse("the incumbent voted against the stance on {count:d} final-passage roll calls")
)
def _voted_against_n(zerodb: ZeroDBClient, seen: dict[str, Any], count: int) -> None:
    for _ in range(count):
        _record_vote(zerodb, seen, position="nay")


@when("the incumbent votes against the stance on another final-passage roll call")
def _votes_against_later(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _record_vote(zerodb, seen, position="nay")


@given("the incumbent was recorded not voting on a final-passage roll call")
def _recorded_not_voting(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _record_vote(zerodb, seen, position="not_voting")


@given("the incumbent was recorded present on a final-passage roll call")
def _recorded_present(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _record_vote(zerodb, seen, position="present")


@given("the incumbent has no votes on this stance's topic")
def _votes_on_another_topic(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """A real voting record, on a topic the stance does not cover.

    Not "no votes at all": the interesting failure is a scorer that ignores
    `topic_ids` and scores every vote against every stance.
    """
    _record_vote(zerodb, seen, position="yea", topic_ids=[OTHER_TOPIC_ID])


@given("a second synthetic incumbent whose record mirrors the first")
def _mirror_incumbent(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _record_vote(zerodb, seen, position="yea", person_id=PERSON_ID)
    _record_vote(zerodb, seen, position="nay", person_id=MIRROR_PERSON_ID)


# --- Given/When: computing ------------------------------------------------


@given("positions have been computed once")
def _computed_once(computer: PositionComputer, seen: dict[str, Any]) -> None:
    result = computer.recompute_person(PERSON_ID)
    seen["first_result"] = result
    seen["first_position"] = None


@when("positions are computed for the incumbent")
def _compute(computer: PositionComputer, zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    seen["result"] = computer.recompute_person(PERSON_ID)
    seen["position"] = _current(zerodb)


@when("positions are computed for both incumbents")
def _compute_both(computer: PositionComputer, zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    computer.recompute_person(PERSON_ID)
    computer.recompute_person(MIRROR_PERSON_ID)
    seen["position"] = _current(zerodb, PERSON_ID)
    seen["mirror_position"] = _current(zerodb, MIRROR_PERSON_ID)


# --- Given/When: the event path -------------------------------------------


@given(parsers.parse('a "{topic}" event was emitted for that roll call'))
def _event_for_roll_call(zerodb: ZeroDBClient, seen: dict[str, Any], topic: str) -> None:
    assert topic == "rollcall.ingested"
    emit_rollcall_ingested(
        zerodb,
        roll_call_id=seen["last_roll_call_id"],
        bill_id=None,
        topic_ids=[TOPIC_ID],
    )


@given(parsers.parse('a "{topic}" event was emitted for a roll call with no votes'))
def _event_for_empty_roll_call(zerodb: ZeroDBClient, seen: dict[str, Any], topic: str) -> None:
    assert topic == "rollcall.ingested"
    Repository(client=zerodb, model=RollCall, provenance=_provenance()).upsert(
        {
            "id": "us-house:119-2:999",
            "voted_at": "2026-05-01T12:00:00Z",
            "question": "On a synthetic measure nobody voted on",
            "vote_type": "final_passage",
            "result": "Passed",
            "topic_ids": [TOPIC_ID],
            "source_url": "https://example.invalid/synthetic/999",
            "content_hash": "synthetic-999",
        }
    )
    emit_rollcall_ingested(
        zerodb, roll_call_id="us-house:119-2:999", bill_id=None, topic_ids=[TOPIC_ID]
    )


@pytest.fixture
def consumer(zerodb: ZeroDBClient, computer: PositionComputer) -> PositionRecomputeConsumer:
    return PositionRecomputeConsumer(zerodb, computer=computer)


@when("the position recompute consumer runs")
def _consumer_runs(
    consumer: PositionRecomputeConsumer, zerodb: ZeroDBClient, seen: dict[str, Any]
) -> None:
    seen["result"] = consumer.run()
    seen["position"] = _current(zerodb)


@when("the position recompute consumer runs again")
def _consumer_runs_again(
    consumer: PositionRecomputeConsumer, zerodb: ZeroDBClient, seen: dict[str, Any]
) -> None:
    seen["second_result"] = consumer.run()
    seen["position"] = _current(zerodb)


# --- Then: the computed value ---------------------------------------------


@then("the stance position is positive")
def _position_positive(seen: dict[str, Any]) -> None:
    position = seen["position"]
    assert position is not None, "no position was written"
    assert position.s_value > 0, position.s_value


@then("the stance position is closer to +1 than to 0")
def _position_closer_to_one(seen: dict[str, Any]) -> None:
    """backlog.md 6.2: "Final passage outweighs procedural and bundled votes".

    One final-passage Yea (weight 1.0) against one procedural Nay (0.2) lands
    at (1.0 - 0.2) / 1.2 = +0.667 — nearer +1 than 0, which is the
    quantitative statement of "outweighs".
    """
    position = seen["position"]
    assert position is not None
    assert position.s_value > 0.5, position.s_value
    expected = (1.0 - DEFAULT_VOTE_TYPE_WEIGHTS["procedural"]) / (
        1.0 + DEFAULT_VOTE_TYPE_WEIGHTS["procedural"]
    )
    assert position.s_value == pytest.approx(expected)


@then("the stance confidence is lower than it would be for a final-passage vote")
def _confidence_lower_than_final(seen: dict[str, Any]) -> None:
    position = seen["position"]
    assert position is not None
    final_confidence = 1.0 / CONFIDENCE_SATURATION_WEIGHT
    assert position.confidence < final_confidence
    assert position.confidence == pytest.approx(
        DEFAULT_VOTE_TYPE_WEIGHTS["procedural"] / CONFIDENCE_SATURATION_WEIGHT
    )


@then("the stance confidence is lower than it would be for an unbundled final-passage vote")
def _confidence_lower_than_unbundled(seen: dict[str, Any]) -> None:
    """prd.md: "a vote on a 1,000-page bill is not a vote for every clause —
    flagged as bundled, low weight"."""
    position = seen["position"]
    assert position is not None
    unbundled = 1.0 / CONFIDENCE_SATURATION_WEIGHT
    assert position.confidence < unbundled
    expected = BUNDLED_WEIGHT_FACTOR / CONFIDENCE_SATURATION_WEIGHT
    assert position.confidence == pytest.approx(expected)


@then(parsers.parse("the stance position is {value:g}"))
def _position_is(seen: dict[str, Any], value: float) -> None:
    position = seen["position"]
    assert position is not None, "no position was written"
    assert position.s_value == pytest.approx(value)


@then("no position is written for the stance")
def _no_position(seen: dict[str, Any]) -> None:
    assert seen["position"] is None


@then("nothing was written to candidate_positions")
def _nothing_written(zerodb: ZeroDBClient) -> None:
    page = zerodb.query_rows(CandidatePosition.table_name, limit=500)
    assert page.rows == [], [row.row_data for row in page.rows]


# --- Then: append-only with is_current ------------------------------------


@then(parsers.parse("candidate_positions holds {count:d} versions for that person and stance"))
@then(parsers.parse("candidate_positions holds {count:d} version for that person and stance"))
def _version_count(zerodb: ZeroDBClient, count: int) -> None:
    rows = [row for row in _positions(zerodb) if row.stance_id == STANCE_ID]
    assert len(rows) == count, [row.id for row in rows]


@then("exactly one of them is marked current")
def _one_current(zerodb: ZeroDBClient) -> None:
    rows = [row for row in _positions(zerodb) if row.stance_id == STANCE_ID]
    assert sum(1 for row in rows if row.is_current is True) == 1


@then("the current version is the newer one")
def _current_is_newest(zerodb: ZeroDBClient) -> None:
    """The newer version is the one with the higher `:v{n}` suffix, which
    `Repository.append_version` assigns from stored state."""
    rows = [row for row in _positions(zerodb) if row.stance_id == STANCE_ID]
    current = [row for row in rows if row.is_current is True]
    assert len(current) == 1
    versions = sorted(int(str(row.id).rsplit(":v", 1)[1]) for row in rows)
    assert int(str(current[0].id).rsplit(":v", 1)[1]) == versions[-1]


@then("the superseded version still reports the value it was computed with")
def _superseded_keeps_value(zerodb: ZeroDBClient) -> None:
    """datamodel.md: "never update history in place".

    The first computation saw one Yea and so stored +1.0. After a Nay is added
    the current row is 0.0, and the superseded row must still say +1.0 — that
    IS the history this table exists to keep.
    """
    rows = [row for row in _positions(zerodb) if row.stance_id == STANCE_ID]
    superseded = [row for row in rows if row.is_current is not True]
    assert len(superseded) == 1
    assert superseded[0].s_value == pytest.approx(1.0)
    current = [row for row in rows if row.is_current is True]
    assert current[0].s_value == pytest.approx(0.0)


@then("the stored position names the current methodology version")
def _names_methodology(seen: dict[str, Any]) -> None:
    position = seen["position"]
    assert position is not None
    assert position.methodology_version == seen["methodology_version"]


@then(parsers.parse("the stored position reports an evidence count of {count:d}"))
def _evidence_count(seen: dict[str, Any], count: int) -> None:
    position = seen["position"]
    assert position is not None
    assert position.evidence_count == count


@then("the stored position's basis names votes")
def _basis_votes(seen: dict[str, Any]) -> None:
    position = seen["position"]
    assert position is not None
    assert position.basis == "votes"


# --- Then: the event path -------------------------------------------------


@then(parsers.parse("the recompute reports {count:d} person recomputed"))
@then(parsers.parse("the recompute reports {count:d} people recomputed"))
def _people_recomputed(seen: dict[str, Any], count: int) -> None:
    assert seen["result"].people_recomputed == count


@then(parsers.parse('exactly one "{topic}" event has been emitted'))
def _one_event(zerodb: ZeroDBClient, topic: str) -> None:
    events = zerodb.list_events(topic=topic, limit=100).events
    assert len(events) == 1, [event.event_payload for event in events]


@then("the event names the person and the methodology version")
def _event_names_person(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    events = zerodb.list_events(topic=POSITIONS_RECOMPUTED_TOPIC, limit=100).events
    assert len(events) == 1
    payload = dict(events[0].event_payload or {})
    # datamodel.md's payload for this event is exactly these two fields.
    assert payload == {
        "person_id": PERSON_ID,
        "methodology_version": seen["methodology_version"],
    }


# --- Then: neutrality -----------------------------------------------------


@then("their stance positions are exact opposites")
def _mirrored_positions(seen: dict[str, Any]) -> None:
    """prd.md: "One scoring codebase for all candidates; no party-specific
    rules." Two mirrored records must produce exactly opposite positions, with
    no tolerance for drift in either direction."""
    first = seen["position"]
    mirror = seen["mirror_position"]
    assert first is not None and mirror is not None
    assert first.s_value == pytest.approx(-mirror.s_value)
    assert first.s_value != 0.0


@then("their stance confidences are equal")
def _mirrored_confidence(seen: dict[str, Any]) -> None:
    first = seen["position"]
    mirror = seen["mirror_position"]
    assert first is not None and mirror is not None
    assert first.confidence == pytest.approx(mirror.confidence)
