"""Unit tests for app.ingest.senate_vote_typing (stories 8.3, 8.4, 8.5).

Covers the classifier's priority order, the bundling rule and its reason
codes, and the unrecorded-outcome guard, against the real labeled sample at
tests/fixtures/senate_vote_typing_labeled.json plus the minimal synthetic
signal sets each individual rule needs to be pinned in isolation.

No network, no credentials: the labeled sample was captured live during story
8.3 and is a fixture from here on (CLAUDE.md -- CI must not depend on
senate.gov being reachable).
"""

from __future__ import annotations

import collections
import json
from pathlib import Path
from typing import Any

import pytest

from app.ingest.senate_vote_typing import (
    BUNDLED_REASON_EXPLANATIONS,
    DEFAULT_BUNDLED_MEASURE_THRESHOLD,
    NO_RECORDED_VOTE_MESSAGE,
    NO_RECORDED_VOTE_VERDICT,
    OMNIBUS_TITLE_MARKERS,
    BundledReason,
    SkippedUnrecordedOutcome,
    VoteSignals,
    classify,
    count_measures,
    explain_bundled_reason,
    guard_recorded_vote,
    unrecorded_outcome,
)
from app.methodology.publish import DEFAULT_VOTE_TYPE_WEIGHTS

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "senate_vote_typing_labeled.json"

PUBLISHED_VOCABULARY = frozenset(
    {"final_passage", "amendment", "cloture", "motion_to_recommit", "procedural", "other"}
)


def _labeled() -> list[dict[str, Any]]:
    return json.loads(FIXTURE_PATH.read_text())["roll_calls"]


def _signals_for(record: dict[str, Any]) -> VoteSignals:
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


def _signals(**kwargs: Any) -> VoteSignals:
    return VoteSignals.from_source_fields(**kwargs)


# --- 8.3: the acceptance criterion, measured ---------------------------------


def test_the_labeled_sample_is_big_enough_to_measure_95_percent() -> None:
    """A "95% accuracy" claim over 10 rows is a claim about 0.5 of a row. 75
    real roll calls make one misclassification 1.3%, so the threshold is
    actually measurable."""
    assert len(_labeled()) >= 50


def test_the_labeled_sample_covers_the_whole_published_vocabulary() -> None:
    covered = {r["expected_vote_type"] for r in _labeled()}
    assert covered == PUBLISHED_VOCABULARY


def test_every_labeled_expectation_is_in_the_published_vocabulary() -> None:
    """A label outside datamodel.md's six values would be unwritable, so a
    typo in the fixture must fail here rather than at the first live write."""
    for record in _labeled():
        assert record["expected_vote_type"] in PUBLISHED_VOCABULARY, record["label_id"]


def test_label_ids_are_unique() -> None:
    ids = [r["label_id"] for r in _labeled()]
    duplicates = [k for k, n in collections.Counter(ids).items() if n > 1]
    assert not duplicates, f"duplicate label_id(s): {duplicates}"


def test_classifier_accuracy_on_the_labeled_sample_is_at_least_95_percent() -> None:
    """Story 8.3's acceptance criterion, measured rather than asserted."""
    records = _labeled()
    wrong = [
        (r["label_id"], r["expected_vote_type"], d.vote_type, d.rule)
        for r in records
        if (d := classify(_signals_for(r))).vote_type != r["expected_vote_type"]
    ]
    accuracy = 100.0 * (len(records) - len(wrong)) / len(records)
    assert accuracy >= 95.0, (
        f"accuracy {accuracy:.1f}% over {len(records)} labeled roll calls. Misclassified: {wrong}"
    )


def test_every_classification_is_in_the_published_vocabulary() -> None:
    for record in _labeled():
        assert classify(_signals_for(record)).vote_type in PUBLISHED_VOCABULARY


def test_every_classification_names_the_rule_that_decided_it() -> None:
    """A classification nobody can explain is one nobody can correct -- and
    prd.md's whole posture is "evidence over opinion"."""
    for record in _labeled():
        decision = classify(_signals_for(record))
        assert decision.rule, record["label_id"]


def test_the_classifier_is_deterministic() -> None:
    """Re-ingesting a record is an idempotent upsert (CLAUDE.md), which is
    only true if the same input classifies the same way every time."""
    for record in _labeled():
        signals = _signals_for(record)
        first = classify(signals)
        second = classify(signals)
        assert first == second, record["label_id"]


# --- 8.3: priority order, rule by rule ---------------------------------------


def test_cloture_outranks_the_amendment_signal() -> None:
    """A cloture vote on an amendment is a vote about ending debate, not about
    the amendment's text. Real case in the sample: 119-2:240."""
    decision = classify(_signals(question="On the Cloture Motion", amendment_number="S.Amdt. 6776"))
    assert decision.vote_type == "cloture"


def test_cloture_outranks_final_passage_wording() -> None:
    """ "On Cloture on the Motion to Proceed" names the bill being proceeded
    to; it must never fall through to final_passage because of it."""
    decision = classify(
        _signals(
            question="On Cloture on the Motion to Proceed",
            vote_question_text="On Cloture on the Motion to Proceed H.R. 5371",
            document_type="H.R.",
        )
    )
    assert decision.vote_type == "cloture"


def test_a_motion_to_table_an_amendment_is_procedural_not_an_amendment_vote() -> None:
    """Tabling kills a measure without a vote on its merits, so the structural
    amendment signal must not win. Real case in the sample: 119-2:17."""
    decision = classify(
        _signals(question="On the Motion to Table", amendment_number="S.Amdt. 4286")
    )
    assert decision.vote_type == "procedural"


def test_a_budget_waiver_motion_on_an_amendment_is_procedural() -> None:
    """The real case story 8.1's structural rule got wrong (119-2:159): the
    question is the generic "On the Motion", `amendment_number` is populated,
    but the motion waives a budget point of order rather than adopting the
    amendment."""
    decision = classify(
        _signals(
            question="On the Motion",
            vote_question_text=(
                "On the Motion (Motion to Waive All Applicable Budgetary "
                "Discipline Re: Cassidy Amdt. No. 5812)"
            ),
            amendment_number="S.Amdt. 5812",
        )
    )
    assert decision.vote_type == "procedural"


def test_an_amendment_vote_under_a_generic_question_is_still_an_amendment() -> None:
    decision = classify(
        _signals(
            question="On the Amendment",
            vote_question_text="On the Amendment S.Amdt. 1217 to S.Amdt. 1092 to H.R. 4366",
            amendment_number="S.Amdt. 1217",
            document_type="S.Amdt.",
        )
    )
    assert decision.vote_type == "amendment"


def test_the_amendment_document_type_alone_is_not_enough_without_a_question() -> None:
    """`document_type == "S.Amdt."` appears on motions to table and on
    decisions of the chair too, so it cannot be the deciding signal on its
    own -- only alongside a question that is about adopting the amendment."""
    decision = classify(
        _signals(
            question="On the Decision of the Chair",
            amendment_number="S.Amdt. 2360",
            document_type="S.Amdt.",
        )
    )
    assert decision.vote_type == "procedural"


@pytest.mark.parametrize(
    "question",
    [
        "On Passage of the Bill",
        "On the Joint Resolution",
        "On the Concurrent Resolution",
        "On the Resolution",
        "On the Conference Report",
        "On Overriding the Veto",
        "On the Resolution of Ratification",
        "On Passage of the Joint Resolution",
        "On Agreeing to the Conference Report",
    ],
)
def test_final_disposition_questions_classify_final_passage(question: str) -> None:
    assert classify(_signals(question=question)).vote_type == "final_passage"


def test_motion_to_recommit_classifies_as_itself() -> None:
    decision = classify(_signals(question="On the Motion to Recommit"))
    assert decision.vote_type == "motion_to_recommit"


@pytest.mark.parametrize(
    "question",
    [
        "On the Motion to Table",
        "On the Motion to Proceed",
        "On the Motion to Discharge",
        "On the Motion to Reconsider",
        "On the Motion to Refer",
        "On the Motion to Adjourn",
        "On the Motion to Postpone",
        "On the Point of Order",
        "On the Decision of the Chair",
        "On the Motion for Attendance",
        "On the Previous Question",
    ],
)
def test_parliamentary_questions_classify_procedural(question: str) -> None:
    assert classify(_signals(question=question)).vote_type == "procedural"


def test_a_motion_named_only_in_the_long_question_text_still_classifies() -> None:
    """The bare `question` is "On the Motion" for several distinct motions;
    only `vote_question_text` says which. Story 8.1 read `question` alone,
    which is the single largest source of its `other` bucket."""
    assert (
        classify(
            _signals(
                question="On the Motion",
                vote_question_text="On the Motion (Motion to Close the Doors)",
            )
        ).vote_type
        == "procedural"
    )


def test_a_bare_nomination_is_other_not_final_passage() -> None:
    """datamodel.md's vocabulary has no "confirmation" value, and a nomination
    is not a vote on legislation. `other` is the honest answer, not a forced
    fit -- carried over from story 8.1 deliberately."""
    decision = classify(_signals(question="On the Nomination", document_type="PN"))
    assert decision.vote_type == "other"


def test_an_empty_question_falls_back_to_other() -> None:
    assert classify(_signals(question="")).vote_type == "other"


def test_an_unseen_question_falls_back_to_other_rather_than_raising() -> None:
    decision = classify(_signals(question="On Some Question The Senate Has Never Asked"))
    assert decision.vote_type == "other"


def test_classification_is_case_and_whitespace_insensitive() -> None:
    """The live index's `question` carries trailing newlines and padding
    (confirmed: 'On the Cloture Motion\\n         ')."""
    assert classify(_signals(question="  ON THE CLOTURE MOTION\n  ")).vote_type == "cloture"
    assert classify(_signals(question="\ton passage of the bill \n")).vote_type == "final_passage"


# --- 8.3: tying classification to the published weights ----------------------


def test_every_classification_the_classifier_can_emit_has_a_published_weight() -> None:
    """ "so that weights apply correctly" (the story's own So-That) is only true
    if every value the classifier emits is a key story 6.1 published."""
    for record in _labeled():
        assert classify(_signals_for(record)).vote_type in DEFAULT_VOTE_TYPE_WEIGHTS


def test_cloture_weighs_less_than_final_passage() -> None:
    """prd.md's "procedural vote" scenario depends on exactly this ordering."""
    assert DEFAULT_VOTE_TYPE_WEIGHTS["cloture"] < DEFAULT_VOTE_TYPE_WEIGHTS["final_passage"]
    assert DEFAULT_VOTE_TYPE_WEIGHTS["procedural"] < DEFAULT_VOTE_TYPE_WEIGHTS["final_passage"]


# --- 8.4: bundling -----------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "A bill making further consolidated appropriations for the fiscal year ending "
        "September 30, 2026, and for other purposes.",
        "A bill making continuing appropriations and extensions for fiscal year 2026, "
        "and for other purposes.",
        "A bill making appropriations for military construction, the Department of "
        "Veterans Affairs, and related agencies for the fiscal year ending September 30, 2024.",
        "A bill to provide for reconciliation pursuant to title II of H. Con. Res. 14.",
        "An original concurrent resolution setting forth the congressional budget for the "
        "United States Government for fiscal year 2025 and setting forth the appropriate "
        "budgetary levels.",
        "An executive resolution authorizing the en bloc consideration in Executive "
        "Session of certain nominations on the Executive Calendar.",
    ],
)
def test_real_omnibus_titles_are_flagged_bundled(title: str) -> None:
    """Every one of these is a real `document_title` captured live from
    senate.gov during this story."""
    decision = classify(_signals(question="On Passage of the Bill", document_title=title))
    assert decision.is_bundled is True
    assert decision.bundled_reason == BundledReason.OMNIBUS_MARKER.value


def test_a_single_subject_bill_is_not_flagged_bundled() -> None:
    decision = classify(
        _signals(
            question="On Passage of the Bill",
            document_title=(
                "A bill to extend the period for filing claims under the Radiation "
                "Exposure Compensation Act."
            ),
        )
    )
    assert decision.is_bundled is False
    assert decision.bundled_reason is None


def test_a_nomination_is_not_flagged_bundled() -> None:
    decision = classify(
        _signals(
            question="On the Nomination",
            document_type="PN",
            vote_question_text="On the Nomination PN1250",
            document_title="Gretchen S. Lund, of Indiana, to be United States District Judge",
        )
    )
    assert decision.is_bundled is False


def test_a_vote_on_many_measures_at_once_is_flagged_by_size() -> None:
    """The real en-bloc case (119-1:524): `vote_question_text` names 48
    separate nominations joined by " and ". One vote, 48 measures -- exactly
    prd.md's "one vote isn't read as support for every clause"."""
    text = "On the Nomination " + " and ".join(f"PN{n}" for n in range(1, 49))
    decision = classify(_signals(question="On the Nomination", vote_question_text=text))
    assert decision.is_bundled is True
    assert decision.bundled_reason == BundledReason.MEASURE_COUNT_EXCEEDS_THRESHOLD.value


def test_a_vote_on_one_measure_is_not_flagged_by_size() -> None:
    decision = classify(
        _signals(question="On the Nomination", vote_question_text="On the Nomination PN1250")
    )
    assert decision.is_bundled is False


def test_the_measure_count_threshold_is_configurable() -> None:
    text = "On the Nomination PN1 and PN2 and PN3"
    assert classify(_signals(question="On the Nomination", vote_question_text=text)).is_bundled is (
        count_measures(text) > DEFAULT_BUNDLED_MEASURE_THRESHOLD
    )
    tight = classify(
        _signals(question="On the Nomination", vote_question_text=text),
        bundled_measure_threshold=2,
    )
    assert tight.is_bundled is True
    loose = classify(
        _signals(question="On the Nomination", vote_question_text=text),
        bundled_measure_threshold=500,
    )
    assert loose.is_bundled is False


def test_the_default_threshold_is_above_one_so_an_ordinary_vote_is_never_bundled() -> None:
    """A threshold of 1 would flag every single-measure vote in the chamber."""
    assert DEFAULT_BUNDLED_MEASURE_THRESHOLD > 1


def test_the_omnibus_marker_wins_over_the_size_rule_when_both_apply() -> None:
    """Deterministic precedence matters: `bundled_reason` is a stored field
    the UI renders, so two runs must not disagree about which reason to show."""
    text = "On the Motion PN1 and PN2 and PN3 and PN4 and PN5 and PN6 and PN7 and PN8"
    decision = classify(
        _signals(
            question="On Passage of the Bill",
            vote_question_text=text,
            document_title="A bill making further consolidated appropriations.",
        ),
        bundled_measure_threshold=2,
    )
    assert decision.bundled_reason == BundledReason.OMNIBUS_MARKER.value


def test_bundling_does_not_change_the_vote_type() -> None:
    """datamodel.md models `is_bundled` as a separate boolean precisely so the
    omnibus discount composes with the vote-type weight (story 6.1's note).
    A bundled final passage must still be typed `final_passage`."""
    decision = classify(
        _signals(
            question="On Passage of the Bill",
            document_title="A bill making further consolidated appropriations.",
        )
    )
    assert decision.vote_type == "final_passage"
    assert decision.is_bundled is True


def test_every_bundled_reason_has_a_plain_language_explanation() -> None:
    """8.4's acceptance criterion: "the UI explains it"."""
    for reason in BundledReason:
        text = BUNDLED_REASON_EXPLANATIONS.get(reason.value)
        assert text, reason.value
        assert len(text) > 20, f"{reason.value}: {text!r}"


def test_the_explanation_map_has_no_reason_the_enum_does_not_define() -> None:
    assert set(BUNDLED_REASON_EXPLANATIONS) == {r.value for r in BundledReason}


def test_explain_bundled_reason_renders_every_stored_code() -> None:
    """The accessor the UI actually calls, over every code it can be handed."""
    for reason in BundledReason:
        assert explain_bundled_reason(reason.value) == BUNDLED_REASON_EXPLANATIONS[reason.value]


def test_explain_bundled_reason_returns_none_for_an_unbundled_vote() -> None:
    assert explain_bundled_reason(None) is None


def test_explain_bundled_reason_returns_none_for_an_unknown_code_rather_than_raising() -> None:
    """A row written by a newer adapter version carrying a reason this build
    has never heard of must render without the explanation, not 500 the page
    -- which is also why `RollCall.bundled_reason` is a bare `str | None`
    rather than a Literal."""
    assert explain_bundled_reason("some_future_reason_code") is None


def test_not_bundled_carries_no_reason() -> None:
    """A reason on an unbundled vote is a reason the UI would render as an
    explanation for a flag that is not set."""
    decision = classify(_signals(question="On Passage of the Bill"))
    assert decision.is_bundled is False
    assert decision.bundled_reason is None


def test_omnibus_markers_are_lowercase_so_matching_is_predictable() -> None:
    for marker in OMNIBUS_TITLE_MARKERS:
        assert marker == marker.lower(), marker


def test_omnibus_marker_matching_is_case_insensitive() -> None:
    decision = classify(
        _signals(
            question="On Passage of the Bill",
            document_title="A BILL MAKING FURTHER CONSOLIDATED APPROPRIATIONS.",
        )
    )
    assert decision.is_bundled is True


def test_count_measures_counts_distinct_identifiers_not_separators() -> None:
    assert count_measures("On the Nomination PN1250") == 1
    assert count_measures("On the Nomination PN1 and PN2 and PN3") == 3
    # The same identifier twice is one measure, not two.
    assert count_measures("On the Motion PN1 and PN1") == 1
    assert count_measures("") == 0


def test_count_measures_does_not_mistake_an_amendment_chain_for_a_bundle() -> None:
    """ "S.Amdt. 5808 to S.Amdt. 5453 to S. 2" is one amendment's place in a
    chain, not three bundled measures -- a real live `vote_question_text`."""
    text = "On the Amendment S.Amdt. 5808 to S.Amdt. 5453 to S. 2 (No short title on file)"
    assert count_measures(text) <= DEFAULT_BUNDLED_MEASURE_THRESHOLD


def test_bundling_is_the_minority_case_on_the_real_sample() -> None:
    """A false positive down-weights a real vote by 0.4x
    (`BUNDLED_WEIGHT_FACTOR`) for no reason, so the rule must be quiet on the
    ordinary case. 21% of this sample is bundled, which is plausible for a
    sample deliberately over-weighted toward appropriations fights; a rule
    firing on half the chamber's votes would be measuring nothing.
    """
    flagged = [r for r in _labeled() if classify(_signals_for(r)).is_bundled]
    share = len(flagged) / len(_labeled())
    assert 0.0 < share < 0.35, (
        f"{len(flagged)}/{len(_labeled())} ({share:.0%}) of the real sample was flagged bundled"
    )


def test_every_bundled_flag_on_the_real_sample_is_justified_by_its_own_evidence() -> None:
    """Rather than hardcoding which label_ids may be flagged -- which hid a
    real finding on the first run, since several DIFFERENT roll calls in the
    sample are votes on the SAME omnibus measure (three separate cloture and
    passage votes on H.R. 5371, the continuing resolution; three on H.R.
    4366, the MilCon-VA appropriations bill) and every one of them is
    correctly bundled -- this asserts the property that actually matters:
    each flag must be traceable to the specific marker or measure count in
    that row's own fields.
    """
    for record in _labeled():
        decision = classify(_signals_for(record))
        if not decision.is_bundled:
            continue
        if decision.bundled_reason == BundledReason.OMNIBUS_MARKER.value:
            title = record["document_title"].lower()
            matched = [m for m in OMNIBUS_TITLE_MARKERS if m in title]
            assert matched, (
                f"{record['label_id']} was flagged omnibus_marker but its title "
                f"matches no marker: {record['document_title']!r}"
            )
        else:
            assert decision.bundled_reason == BundledReason.MEASURE_COUNT_EXCEEDS_THRESHOLD.value
            counted = count_measures(record["vote_question_text"])
            assert counted > DEFAULT_BUNDLED_MEASURE_THRESHOLD, (
                f"{record['label_id']} was flagged by size but names only {counted} measure(s)"
            )


def test_an_ordinary_single_subject_vote_in_the_real_sample_is_not_bundled() -> None:
    """The specific real rows that must stay unflagged: a single-subject
    bill, a single nomination, an ordinary amendment, a War Powers joint
    resolution. If the rule ever starts flagging these, it is flagging the
    chamber's normal business.
    """
    for label_id in (
        "passage-of-the-bill",
        "nomination",
        "amendment",
        "joint-resolution",
        "motion-to-recommit",
        "resolution",
        "point-of-order",
    ):
        record = next(r for r in _labeled() if r["label_id"] == label_id)
        decision = classify(_signals_for(record))
        assert not decision.is_bundled, (
            f"{label_id} ({record['document_title'][:60]!r}) was flagged "
            f"{decision.bundled_reason!r}"
        )


# --- 8.5: no recorded vote ---------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        "Passed Senate by Voice Vote",
        "Agreed to in Senate by Voice Vote",
        "by voice vote",
        "VOICE VOTE",
    ],
)
def test_a_voice_vote_is_an_unrecorded_outcome(outcome: str) -> None:
    result = unrecorded_outcome(outcome)
    assert result is not None
    assert result.reason == "voice_vote"
    assert result.message == NO_RECORDED_VOTE_MESSAGE
    assert result.scorable is False
    assert result.verdict == NO_RECORDED_VOTE_VERDICT


@pytest.mark.parametrize(
    "outcome",
    [
        "Agreed to in Senate by Unanimous Consent",
        "Passed Senate without amendment by Unanimous Consent",
        "by unanimous consent",
        "UC",
    ],
)
def test_a_unanimous_consent_outcome_is_unrecorded(outcome: str) -> None:
    result = unrecorded_outcome(outcome)
    assert result is not None
    assert result.reason == "unanimous_consent"
    assert result.message == NO_RECORDED_VOTE_MESSAGE
    assert result.scorable is False


def test_the_message_is_prd_md_s_own_wording_verbatim() -> None:
    """prd.md's scenario: "the UI says 'no recorded vote'". Not "No Recorded
    Vote", not "no recorded vote."  -- the exact string the scenario quotes."""
    assert NO_RECORDED_VOTE_MESSAGE == "no recorded vote"


def test_the_verdict_is_prd_md_s_no_opportunity() -> None:
    """prd.md: "the verdict is 'No opportunity'". Stored as the model's own
    enum spelling (`VerdictValue`), which is the snake_case form."""
    assert NO_RECORDED_VOTE_VERDICT == "no_opportunity"


@pytest.mark.parametrize(
    "outcome",
    [
        "Bill Passed",
        "Nomination Confirmed",
        "Cloture Motion Agreed to",
        "Amendment Rejected",
        "Motion to Table Agreed to",
        "Veto Sustained",
        "Point of Order Well Taken",
        "Not Voting",
        "",
    ],
)
def test_a_recorded_outcome_is_not_mistaken_for_an_unrecorded_one(outcome: str) -> None:
    assert unrecorded_outcome(outcome) is None


def test_every_real_labeled_result_is_recognized_as_recorded() -> None:
    """75 real roll-call results, every one of which WAS recorded by
    definition (a voice vote never gets a roll call number). A false positive
    here would delete a real vote from the record."""
    for record in _labeled():
        assert unrecorded_outcome(record["vote_result"]) is None, record["label_id"]


def test_an_individual_senator_not_voting_is_not_an_unrecorded_outcome() -> None:
    """The distinction prd.md draws: an absent senator has a `not_voting`
    POSITION on a vote that was recorded. Conflating that with a voice vote
    would erase the roll call for all 99 others."""
    assert unrecorded_outcome("Not Voting") is None
    assert unrecorded_outcome("Absent") is None


def test_the_guard_raises_on_an_unrecorded_outcome() -> None:
    with pytest.raises(SkippedUnrecordedOutcome) as excinfo:
        guard_recorded_vote("Passed Senate by Voice Vote")
    assert NO_RECORDED_VOTE_MESSAGE in str(excinfo.value)
    assert excinfo.value.reason == "voice_vote"


def test_the_guard_passes_a_real_recorded_outcome_through() -> None:
    guard_recorded_vote("Bill Passed")


def test_the_guard_accepts_none_rather_than_guessing() -> None:
    """A record with no result at all is a different skip (story 8.1's
    `_SkipRollCall` already covers it); this guard must not claim it as its
    own, or the skip report would blame the wrong thing."""
    guard_recorded_vote(None)
