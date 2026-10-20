"""Step definitions for tests/features/senate_vote_typing.feature (8.3, 8.4, 8.5).

Runs entirely against story 1.2's in-memory fake transport and the labeled
fixture at tests/fixtures/senate_vote_typing_labeled.json -- never the live
network, so CI needs no credential and no reachable senate.gov (CLAUDE.md).
The fixture's rows are REAL Senate roll calls captured live during this
story; see its own `_comment` for how the sample was drawn and labeled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.senate_rollcalls import SenateRollCallLoader, roll_call_id
from app.ingest.senate_vote_typing import (
    BUNDLED_REASON_EXPLANATIONS,
    DEFAULT_BUNDLED_MEASURE_THRESHOLD,
    NO_RECORDED_VOTE_MESSAGE,
    NO_RECORDED_VOTE_VERDICT,
    VoteSignals,
    classify,
    unrecorded_outcome,
)
from app.methodology.publish import DEFAULT_VOTE_TYPE_WEIGHTS
from app.repositories import Repository
from app.repositories.models import MemberVote, Person, RollCall
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/senate_vote_typing.feature")

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "senate_vote_typing_labeled.json"
)

#: The six values datamodel.md documents for `vote_type`.
PUBLISHED_VOCABULARY = frozenset(
    {"final_passage", "amendment", "cloture", "motion_to_recommit", "procedural", "other"}
)


def _labeled() -> list[dict[str, Any]]:
    return json.loads(FIXTURE_PATH.read_text())["roll_calls"]


def _by_label_id(label_id: str) -> dict[str, Any]:
    for record in _labeled():
        if record["label_id"] == label_id:
            return record
    raise AssertionError(f"no labeled roll call {label_id!r} in the fixture")


def _signals(record: dict[str, Any]) -> VoteSignals:
    return VoteSignals.from_source_fields(
        question=record["question"],
        vote_question_text=record["vote_question_text"],
        vote_result=record["vote_result"],
        vote_title=record["vote_title"],
        document_type=record["document_type"],
        document_number=record["document_number"],
        document_title=record["document_title"],
        amendment_number=record["amendment_number"],
    )


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {"threshold": DEFAULT_BUNDLED_MEASURE_THRESHOLD, "classifications": {}}


# --- background --------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="typing_client")
def typing_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def reference_tables(zerodb: ZeroDBClient) -> None:
    Bootstrapper(zerodb, pace=lambda: None).run()


@given("every senator in the labeled crosswalk exists as a people row")
def senators_exist(zerodb: ZeroDBClient) -> None:
    people: Repository[Person] = Repository(client=zerodb, model=Person)
    for bioguide in ("B001261", "B001267", "B001277"):
        people.upsert({"id": f"person:{bioguide}", "canonical_name": f"Senator {bioguide}"})


# --- 8.3: classifying --------------------------------------------------------


@when("I classify every roll call in the labeled sample")
def classify_every(result: dict[str, Any]) -> None:
    records = _labeled()
    outcomes = []
    for record in records:
        decision = classify(_signals(record))
        outcomes.append((record, decision))
    result["sample"] = outcomes


@then(parsers.parse("at least {percent:d}% of the labeled sample is classified correctly"))
def accuracy_at_least(result: dict[str, Any], percent: int) -> None:
    outcomes = result["sample"]
    wrong = [
        (r["label_id"], r["expected_vote_type"], d.vote_type, d.rule)
        for r, d in outcomes
        if d.vote_type != r["expected_vote_type"]
    ]
    accuracy = 100.0 * (len(outcomes) - len(wrong)) / len(outcomes)
    assert accuracy >= percent, (
        f"accuracy {accuracy:.1f}% over {len(outcomes)} labeled roll calls, "
        f"needed >= {percent}%. Misclassified: {wrong}"
    )


@then("the labeled sample covers every vote_type in the published vocabulary")
def sample_covers_vocabulary() -> None:
    covered = {r["expected_vote_type"] for r in _labeled()}
    assert covered == PUBLISHED_VOCABULARY, f"sample covers {sorted(covered)}"


@then(parsers.parse("the labeled sample holds at least {count:d} real roll calls"))
def sample_size(count: int) -> None:
    assert len(_labeled()) >= count


@when(parsers.parse('I classify the labeled roll call "{label_id}"'))
def classify_one(result: dict[str, Any], label_id: str) -> None:
    record = _by_label_id(label_id)
    decision = classify(_signals(record), bundled_measure_threshold=result["threshold"])
    result["classifications"][label_id] = decision
    result["last"] = decision
    result["last_record"] = record


@then(parsers.parse('its vote_type is "{vote_type}"'))
def its_vote_type(result: dict[str, Any], vote_type: str) -> None:
    assert result["last"].vote_type == vote_type, (
        f"{result['last_record']['label_id']}: classified "
        f"{result['last'].vote_type!r} by rule {result['last'].rule!r}, expected {vote_type!r}"
    )


@then(parsers.parse('the labeled roll call "{label_id}" carries an amendment reference'))
def carries_amendment_ref(label_id: str) -> None:
    assert _by_label_id(label_id)["amendment_number"], (
        f"{label_id} has no amendment_number, so it does not prove cloture/table "
        "outranks the amendment signal"
    )


@when("I classify a roll call whose question is empty")
def classify_empty_question(result: dict[str, Any]) -> None:
    result["last"] = classify(VoteSignals.from_source_fields(question=""))
    result["last_record"] = {"label_id": "<empty question>"}


@then("the cloture vote and the final-passage vote carry different vote_types")
def cloture_and_final_differ(result: dict[str, Any]) -> None:
    cloture = result["classifications"]["cloture-on-the-motion-to-proceed"]
    final = result["classifications"]["passage-of-the-bill"]
    assert cloture.vote_type == "cloture"
    assert final.vote_type == "final_passage"
    assert cloture.vote_type != final.vote_type


@then("the cloture vote weighs less than the final-passage vote under the published weights")
def cloture_weighs_less(result: dict[str, Any]) -> None:
    """prd.md: "Procedural and cloture votes are shown but weighted lower than
    final passage." Story 6.1 published the numbers; this step is what ties
    8.3's classification to them, since a classification that did not change
    the weight would not satisfy "so that weights apply correctly"."""
    cloture = result["classifications"]["cloture-on-the-motion-to-proceed"].vote_type
    final = result["classifications"]["passage-of-the-bill"].vote_type
    assert DEFAULT_VOTE_TYPE_WEIGHTS[cloture] < DEFAULT_VOTE_TYPE_WEIGHTS[final]


@then("the classification names the rule that decided it")
def classification_is_explainable(result: dict[str, Any]) -> None:
    rule = result["last"].rule
    assert rule, "the decision carries no rule name"
    assert isinstance(rule, str) and rule.strip()


# --- 8.4: bundling -----------------------------------------------------------


@given(parsers.parse("a bundling threshold of {threshold:d} measures"))
def set_threshold(result: dict[str, Any], threshold: int) -> None:
    result["threshold"] = threshold


@then("it is flagged bundled")
def flagged_bundled(result: dict[str, Any]) -> None:
    assert result["last"].is_bundled, (
        f"{result['last_record']['label_id']} was not flagged bundled "
        f"(reason {result['last'].bundled_reason!r})"
    )


@then("it is not flagged bundled")
def not_flagged_bundled(result: dict[str, Any]) -> None:
    assert not result["last"].is_bundled, (
        f"{result['last_record']['label_id']} was flagged bundled: "
        f"{result['last'].bundled_reason!r}"
    )


@then(parsers.parse('its bundled reason is "{reason}"'))
def bundled_reason_is(result: dict[str, Any], reason: str) -> None:
    assert result["last"].bundled_reason == reason, (
        f"{result['last_record']['label_id']}: reason {result['last'].bundled_reason!r}"
    )


@then(parsers.parse('its bundled reason is not "{reason}"'))
def bundled_reason_is_not(result: dict[str, Any], reason: str) -> None:
    assert result["last"].bundled_reason != reason


@then("its bundled reason is empty")
def bundled_reason_empty(result: dict[str, Any]) -> None:
    assert result["last"].bundled_reason is None


@then("every bundled reason has a plain-language explanation")
def every_reason_explained() -> None:
    """8.4's acceptance criterion: "the UI explains it". A reason code with no
    text is a reason the UI cannot render, so the mapping must be total."""
    from app.ingest.senate_vote_typing import BundledReason

    for reason in BundledReason:
        text = BUNDLED_REASON_EXPLANATIONS.get(reason.value)
        assert text, f"bundled reason {reason.value!r} has no plain-language explanation"
        assert len(text) > 20, f"explanation for {reason.value!r} is too terse to render: {text!r}"


# --- 8.4: bundling, through the real write path -------------------------------


def _ingest_labeled(
    zerodb: ZeroDBClient, label_id: str, *, with_members: bool = False
) -> dict[str, Any]:
    """Transform one labeled record into a `roll_calls` row through the same
    classifier the loader uses, and write it with the real loader."""
    record = _by_label_id(label_id)
    decision = classify(_signals(record))
    row = {
        "id": roll_call_id(
            congress=record["congress"],
            session=record["session"],
            vote_number=record["vote_number"],
        ),
        "voted_at": "2026-10-19 12:00 PM",
        "question": record["question"],
        "vote_type": decision.vote_type,
        "result": record["vote_result"],
        "bill_id": None,
        "amendment_ref": record["amendment_number"] or None,
        "is_bundled": decision.is_bundled,
        "bundled_reason": decision.bundled_reason,
        "topic_ids": [],
        "source_url": f"https://www.senate.gov/synthetic/{label_id}",
        "content_hash": "0" * 64,
    }
    member_rows = []
    if with_members:
        member_rows = [
            {
                "roll_call_id": row["id"],
                "person_id": f"person:{bioguide}",
                "position": "yea",
                "voted_at": row["voted_at"],
                "vote_type": row["vote_type"],
                "bill_id": row["bill_id"],
                "topic_ids": row["topic_ids"],
                "is_bundled": row["is_bundled"],
                "bundled_reason": row["bundled_reason"],
            }
            for bioguide in ("B001261", "B001267", "B001277")
        ]
    SenateRollCallLoader(zerodb, pace=lambda: None).load(
        [row], member_vote_rows_by_roll_call={row["id"]: member_rows}
    )
    return row


@when(parsers.parse('I ingest the labeled roll call "{label_id}"'))
def ingest_labeled(zerodb: ZeroDBClient, result: dict[str, Any], label_id: str) -> None:
    result["stored_row"] = _ingest_labeled(zerodb, label_id)


@when(parsers.parse('I ingest the labeled roll call "{label_id}" with member positions'))
def ingest_labeled_with_members(
    zerodb: ZeroDBClient, result: dict[str, Any], label_id: str
) -> None:
    result["stored_row"] = _ingest_labeled(zerodb, label_id, with_members=True)


@then("the stored roll call is flagged bundled")
def stored_is_bundled(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    repo: Repository[RollCall] = Repository(client=zerodb, model=RollCall)
    stored = repo.get(result["stored_row"]["id"])
    assert stored is not None, "the roll call was not stored"
    assert stored.is_bundled is True


@then(parsers.parse("the stored roll call's bundled reason is \"{reason}\""))
def stored_bundled_reason(zerodb: ZeroDBClient, result: dict[str, Any], reason: str) -> None:
    repo: Repository[RollCall] = Repository(client=zerodb, model=RollCall)
    stored = repo.get(result["stored_row"]["id"])
    assert stored is not None
    assert stored.bundled_reason == reason


@then("every stored member_vote for that roll call is flagged bundled")
def stored_member_votes_bundled(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    roll_call = result["stored_row"]["id"]
    rows = [
        row["row_data"]
        for row in fake.rows.get("member_votes", {}).values()
        if row["row_data"].get("roll_call_id") == roll_call
    ]
    assert rows, f"no member_votes rows for {roll_call!r}"
    for row in rows:
        assert row["is_bundled"] is True, row
        assert row["bundled_reason"] == result["stored_row"]["bundled_reason"], row


# --- 8.5: no recorded vote ---------------------------------------------------


@given("a bill disposed of by voice vote")
def bill_voice_vote(result: dict[str, Any]) -> None:
    result["outcome_text"] = "Passed Senate by Voice Vote"


@given("a bill agreed to by unanimous consent")
def bill_unanimous_consent(result: dict[str, Any]) -> None:
    result["outcome_text"] = "Agreed to in Senate by Unanimous Consent"


@when("I ask whether the Senate's position on it is recorded")
def ask_whether_recorded(result: dict[str, Any]) -> None:
    result["unrecorded"] = unrecorded_outcome(result["outcome_text"])


@then("the answer is that there is no recorded vote")
def answer_no_recorded_vote(result: dict[str, Any]) -> None:
    outcome = result["unrecorded"]
    assert outcome is not None, f"{result['outcome_text']!r} was not recognized as unrecorded"
    assert outcome.message == NO_RECORDED_VOTE_MESSAGE


@then(parsers.parse('the reason given is "{reason}"'))
def unrecorded_reason(result: dict[str, Any], reason: str) -> None:
    assert result["unrecorded"].reason == reason


@when("I try to ingest it as a roll call")
def try_ingest_unrecorded(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    from app.ingest.senate_vote_typing import SkippedUnrecordedOutcome, guard_recorded_vote

    try:
        guard_recorded_vote(result["outcome_text"])
    except SkippedUnrecordedOutcome as exc:
        result["skip"] = exc
        return
    raise AssertionError("an unrecorded outcome was accepted as a roll call")


@then("nothing is stored")
def nothing_stored(fake: FakeZeroDBTransport) -> None:
    rows = fake.rows.get("roll_calls", {})
    assert not rows, f"{len(rows)} roll_calls row(s) were written for an unrecorded outcome"


@then("the skip reason says there is no recorded vote")
def skip_reason_says_no_recorded_vote(result: dict[str, Any]) -> None:
    assert NO_RECORDED_VOTE_MESSAGE in str(result["skip"])


@then("no member_votes row is stored")
def no_member_votes_stored(fake: FakeZeroDBTransport) -> None:
    rows = fake.rows.get("member_votes", {})
    assert not rows, f"{len(rows)} member_votes row(s) were written for an unrecorded outcome"


@then("it is a recorded vote")
def it_is_a_recorded_vote(result: dict[str, Any]) -> None:
    record = result["last_record"]
    assert unrecorded_outcome(record["vote_result"]) is None, (
        f"{record['label_id']}'s result {record['vote_result']!r} was mistaken "
        "for an unrecorded outcome"
    )


@then("a senator who did not vote on it is recorded as not_voting, not as unrecorded")
def absent_senator_is_not_voting(result: dict[str, Any]) -> None:
    """prd.md draws the line this step pins: "Only recorded (roll-call) votes
    are scored. Voice votes and unanimous consent are shown as 'no recorded
    vote'." An individual senator's absence from a real roll call is a
    `not_voting` POSITION on a vote that WAS recorded -- a different thing
    entirely, and conflating the two would erase a real roll call."""
    assert unrecorded_outcome("Not Voting") is None
    assert unrecorded_outcome(result["last_record"]["vote_result"]) is None


# --- 8.5: prd.md's own scenario, verbatim ------------------------------------


@given("a promise maps only to a bill passed by voice vote")
def promise_maps_to_voice_vote_only(result: dict[str, Any]) -> None:
    result["outcome_text"] = "Passed Senate by Voice Vote"
    result["unrecorded"] = unrecorded_outcome(result["outcome_text"])


@then("the promise has no recorded position to score")
def promise_has_no_position(result: dict[str, Any]) -> None:
    assert result["unrecorded"] is not None
    assert result["unrecorded"].scorable is False


@then(parsers.parse('the UI message for it is "{message}"'))
def ui_message_is(result: dict[str, Any], message: str) -> None:
    assert result["unrecorded"].message == message


@then(parsers.parse('the verdict it implies is "{verdict}"'))
def verdict_implied(result: dict[str, Any], verdict: str) -> None:
    assert result["unrecorded"].verdict == verdict
    assert verdict == NO_RECORDED_VOTE_VERDICT
