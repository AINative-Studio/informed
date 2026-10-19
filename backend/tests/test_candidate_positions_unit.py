"""`compute_position`'s arithmetic, as arithmetic (story 6.2).

Same split as `tests/test_alignment_formula_unit.py`: the computation is tested
as a pure function over `MemberVote`/`Stance` models with no client, and the
storage/event half lives in
`tests/step_defs/test_candidate_positions_steps.py`.

Nothing here is real. The person id is `person:TEST000000`, not a bioguide, so
no row from this suite could be mistaken for a real member's record.
"""

from __future__ import annotations

import pytest

from app.methodology.publish import DEFAULT_VOTE_TYPE_WEIGHTS
from app.repositories.models import MemberVote, Stance
from app.scoring.positions import (
    BASIS_VOTES,
    BUNDLED_WEIGHT_FACTOR,
    CONFIDENCE_SATURATION_WEIGHT,
    compute_position,
    votes_for_stance,
)

PERSON_ID = "person:TEST000000"
TOPIC_ID = "en-US:test-topic:v1"
OTHER_TOPIC_ID = "en-US:other-topic:v1"


def _stance(direction_code: str = "+1", *, topic_id: str = TOPIC_ID, slug: str = "for") -> Stance:
    return Stance(
        id=f"{topic_id}:{slug}",
        topic_id=topic_id,
        label="Synthetic stance",
        description="A synthetic stance card used only by this test suite.",
        direction_code=direction_code,
        wording_version=1,
    )


def _vote(
    *,
    position: str,
    vote_type: str = "final_passage",
    is_bundled: bool = False,
    roll: int = 1,
    topic_ids: list[str] | None = None,
) -> MemberVote:
    roll_call_id = f"us-house:119-2:{roll}"
    return MemberVote(
        id=f"{roll_call_id}:TEST000000",
        roll_call_id=roll_call_id,
        person_id=PERSON_ID,
        position=position,
        voted_at="2026-03-01T12:00:00Z",
        vote_type=vote_type,
        topic_ids=[TOPIC_ID] if topic_ids is None else topic_ids,
        is_bundled=is_bundled,
    )


def _compute(votes: list[MemberVote], stance: Stance | None = None):
    return compute_position(
        person_id=PERSON_ID,
        stance=stance or _stance(),
        votes=votes,
        vote_type_weights=DEFAULT_VOTE_TYPE_WEIGHTS,
    )


# --- the topic association, which is this story's scoping decision --------


def test_only_votes_on_the_stances_topic_are_evidence() -> None:
    """`app.scoring.positions`'s scoping decision: a vote is evidence about a
    stance when its denormalised `topic_ids[]` contains the stance's
    `topic_id`. A scorer that ignored that would score every member's position
    on every stance from their whole voting record.
    """
    relevant = _vote(position="yea", roll=1)
    unrelated = _vote(position="nay", roll=2, topic_ids=[OTHER_TOPIC_ID])
    selected = votes_for_stance([relevant, unrelated], _stance())
    assert [vote.roll_call_id for vote in selected] == [relevant.roll_call_id]


def test_a_vote_tagged_with_several_topics_counts_for_each_of_them() -> None:
    """A roll call can carry more than one `topic_ids` entry — an omnibus
    health bill touches several subtopics — and must be evidence about each."""
    multi = _vote(position="yea", topic_ids=[TOPIC_ID, OTHER_TOPIC_ID])
    assert votes_for_stance([multi], _stance()) == [multi]
    assert votes_for_stance([multi], _stance(topic_id=OTHER_TOPIC_ID)) == [multi]


def test_no_matching_votes_yields_no_position_rather_than_a_neutral_one() -> None:
    """ "No evidence" and "measured, and found neutral" are different claims.
    `CandidatePosition` would refuse the row anyway (confidence must be 0 when
    evidence_count is 0), so a zero-confidence neutral row is not even
    storable."""
    assert _compute([_vote(position="yea", topic_ids=[OTHER_TOPIC_ID])]) is None
    assert _compute([]) is None


def test_the_selected_votes_come_back_in_a_stable_order() -> None:
    """The position is a floating-point division of two sums, so the order the
    votes are summed in has to be a property of the function — the same
    reasoning as `app.scoring.formula._canonical`."""
    votes = [_vote(position="yea", roll=roll) for roll in (7, 3, 11, 1)]
    selected = votes_for_stance(votes, _stance())
    assert [vote.roll_call_id for vote in selected] == sorted(vote.roll_call_id for vote in votes)


# --- direction ------------------------------------------------------------


def test_a_yea_on_a_positive_stance_is_support() -> None:
    position = _compute([_vote(position="yea")])
    assert position is not None
    assert position.s_value == pytest.approx(1.0)


def test_a_nay_on_a_positive_stance_is_opposition() -> None:
    position = _compute([_vote(position="nay")])
    assert position is not None
    assert position.s_value == pytest.approx(-1.0)


def test_two_mirrored_stances_get_opposite_positions_from_one_vote() -> None:
    """prd.md's "nonpartisan by construction", applied to the one place this
    story makes a directional judgment (`_stance_direction`).

    A vote read as supporting a `+1` stance must read as opposing the `-1`
    stance on the same topic, exactly. If it did not, one side of every axis
    would be advantaged by the arithmetic alone — which is the failure the
    symmetry audit exists to catch.
    """
    vote = _vote(position="yea")
    positive = _compute([vote], _stance("+1", slug="for"))
    negative = _compute([vote], _stance("-1", slug="against"))
    assert positive is not None and negative is not None
    assert positive.s_value == pytest.approx(-negative.s_value)
    assert positive.confidence == pytest.approx(negative.confidence)


def test_a_strongly_worded_stance_is_not_scored_higher_than_a_moderate_one() -> None:
    """The magnitude of `direction_code` (+-1 vs +-2) is deliberately not a
    multiplier: `s_value` is defined against the stance ("acts for it"), and
    prd.md puts the voter's own strength of feeling in `intensity`, which is
    the voter's to set rather than the stance card's."""
    vote = _vote(position="yea")
    moderate = _compute([vote], _stance("+1", slug="for"))
    strong = _compute([vote], _stance("+2", slug="for-strongly"))
    assert moderate is not None and strong is not None
    assert moderate.s_value == pytest.approx(strong.s_value)


def test_a_status_quo_stance_gets_no_position_from_a_vote_to_change_policy() -> None:
    """`direction_code` of `0` means "this position does not move along the
    axis". A vote to change policy is not evidence for or against a voter who
    asked for no change in either direction, and inventing a sign for it would
    be the scorer taking a political position."""
    assert _compute([_vote(position="yea")], _stance("0", slug="status-quo")) is None


# --- vote-type weighting, 6.2's first acceptance criterion ----------------


def test_final_passage_outweighs_a_contrary_procedural_vote() -> None:
    """backlog.md 6.2: "Final passage outweighs procedural and bundled votes".

    One final-passage Yea (1.0) against one procedural Nay (0.2) must land
    positive: (1.0 - 0.2) / 1.2 = +0.667.
    """
    position = _compute(
        [
            _vote(position="yea", roll=1),
            _vote(position="nay", roll=2, vote_type="procedural"),
        ]
    )
    assert position is not None
    procedural = DEFAULT_VOTE_TYPE_WEIGHTS["procedural"]
    assert position.s_value == pytest.approx((1.0 - procedural) / (1.0 + procedural))
    assert position.s_value > 0.5


def test_no_number_of_procedural_votes_can_overturn_a_final_passage_record() -> None:
    """The weighted MEAN is what makes this true, and it is the reason the
    module uses a mean rather than a clamped sum.

    Under a sum, procedural votes would accumulate without bound against a
    final-passage vote, so "final passage outweighs procedural" would hold only
    until a member had enough procedural votes. Under a mean, each vote's
    weight is its SHARE of the position, so the published weights fix the
    exchange rate exactly: a final-passage vote (1.0) is worth five procedural
    votes (0.2), and no amount of restating the same procedural position
    changes that rate.

    So the boundary is a known, published number rather than an emergent one:
    four contrary procedural votes leave the sign positive, five are an exact
    tie. That is a statement about the methodology's weights, not an accident
    of the arithmetic, which is exactly the property a reviewable scoring rule
    needs.
    """
    procedural = DEFAULT_VOTE_TYPE_WEIGHTS["procedural"]
    assert procedural == 0.2, "this test's arithmetic is pinned to the published weight"

    four_against = _compute(
        [_vote(position="yea", roll=1)]
        + [_vote(position="nay", roll=roll, vote_type="procedural") for roll in range(2, 6)]
    )
    assert four_against is not None
    assert four_against.s_value > 0, four_against.s_value

    exact_tie = _compute(
        [_vote(position="yea", roll=1)]
        + [_vote(position="nay", roll=roll, vote_type="procedural") for roll in range(2, 7)]
    )
    assert exact_tie is not None
    assert exact_tie.s_value == pytest.approx(0.0)


def test_each_vote_type_weight_comes_from_the_published_methodology() -> None:
    """A position computed under a different published weight must differ. The
    weights are an argument, not a constant read inside the function, so a
    methodology republication re-scores without a code change."""
    votes = [
        _vote(position="yea", roll=1),
        _vote(position="nay", roll=2, vote_type="procedural"),
    ]
    published = _compute(votes)
    assert published is not None

    # A methodology that weighted procedural votes equally with final passage
    # would make this record exactly neutral.
    flat = compute_position(
        person_id=PERSON_ID,
        stance=_stance(),
        votes=votes,
        vote_type_weights={**DEFAULT_VOTE_TYPE_WEIGHTS, "procedural": 1.0},
    )
    assert flat is not None
    assert flat.s_value == pytest.approx(0.0)
    assert published.s_value != pytest.approx(0.0)


def test_an_unknown_vote_type_falls_back_to_the_other_weight_not_to_full() -> None:
    """Defaulting an unrecognised type to full final-passage weight would make
    a schema change silently promote every unclassified vote to the strongest
    evidence there is."""
    vote = _vote(position="yea")
    unknown = vote.model_copy(update={"vote_type": "amendment"})
    position = compute_position(
        person_id=PERSON_ID,
        stance=_stance(),
        # A map missing `amendment` entirely, as a stale methodology would be.
        votes=[unknown],
        vote_type_weights={"final_passage": 1.0, "other": 0.2},
    )
    assert position is not None
    assert position.confidence == pytest.approx(0.2 / CONFIDENCE_SATURATION_WEIGHT)


def test_a_vote_type_weighted_zero_is_excluded_from_the_evidence_count() -> None:
    """A methodology that weights a vote type at 0 has excluded it, and an
    excluded vote must not inflate `evidence_count` — which is what story 6.5's
    confidence badge reads."""
    position = compute_position(
        person_id=PERSON_ID,
        stance=_stance(),
        votes=[
            _vote(position="yea", roll=1),
            _vote(position="nay", roll=2, vote_type="procedural"),
        ],
        vote_type_weights={**DEFAULT_VOTE_TYPE_WEIGHTS, "procedural": 0.0},
    )
    assert position is not None
    assert position.evidence_count == 1
    assert position.s_value == pytest.approx(1.0)


# --- bundled votes --------------------------------------------------------


def test_a_bundled_vote_weighs_less_than_an_unbundled_one() -> None:
    """prd.md, Feature 2: "a vote on a 1,000-page bill is not a vote for every
    clause — flagged as bundled, low weight"."""
    bundled = _compute([_vote(position="yea", is_bundled=True)])
    plain = _compute([_vote(position="yea")])
    assert bundled is not None and plain is not None
    assert bundled.confidence < plain.confidence
    assert bundled.confidence == pytest.approx(BUNDLED_WEIGHT_FACTOR / CONFIDENCE_SATURATION_WEIGHT)


def test_an_unbundled_final_passage_vote_outweighs_a_contrary_bundled_one() -> None:
    position = _compute(
        [
            _vote(position="yea", roll=1),
            _vote(position="nay", roll=2, is_bundled=True),
        ]
    )
    assert position is not None
    assert position.s_value > 0
    assert position.s_value == pytest.approx(
        (1.0 - BUNDLED_WEIGHT_FACTOR) / (1.0 + BUNDLED_WEIGHT_FACTOR)
    )


def test_the_bundled_discount_applies_to_every_vote_type_proportionally() -> None:
    """A multiplier rather than a replacement weight, so bundling discounts an
    amendment in the same proportion as a final passage, with no second
    table."""
    for vote_type in ("final_passage", "amendment", "cloture"):
        bundled = _compute([_vote(position="yea", vote_type=vote_type, is_bundled=True)])
        plain = _compute([_vote(position="yea", vote_type=vote_type)])
        assert bundled is not None and plain is not None
        assert bundled.confidence == pytest.approx(plain.confidence * BUNDLED_WEIGHT_FACTOR)


def test_the_bundled_factor_sits_between_procedural_and_final_passage() -> None:
    """The ordering backlog.md 6.2 actually requires: a bundled final-passage
    vote weighs less than an unbundled one and more than a bare procedural
    vote."""
    assert (
        DEFAULT_VOTE_TYPE_WEIGHTS["procedural"]
        < BUNDLED_WEIGHT_FACTOR * DEFAULT_VOTE_TYPE_WEIGHTS["final_passage"]
        < DEFAULT_VOTE_TYPE_WEIGHTS["final_passage"]
    )


# --- recorded non-positions ----------------------------------------------


@pytest.mark.parametrize("position", ["present", "not_voting"])
def test_a_recorded_non_position_is_not_evidence_either_way(position: str) -> None:
    """prd.md: "Procedural votes, amendments, absences and ambiguous promises
    are labelled, not silently scored." A member who was counted but took no
    side has given no evidence of a position."""
    assert _compute([_vote(position=position)]) is None


@pytest.mark.parametrize("abstention", ["present", "not_voting"])
def test_an_abstention_does_not_inflate_the_evidence_count(abstention: str) -> None:
    """`evidence_count` is what story 6.5's badge reads, so an abstention that
    counted would buy a candidate a better badge for having said nothing."""
    position = _compute([_vote(position="yea", roll=1), _vote(position=abstention, roll=2)])
    assert position is not None
    assert position.evidence_count == 1
    assert position.s_value == pytest.approx(1.0)


# --- confidence -----------------------------------------------------------


def test_confidence_rises_with_the_number_of_votes() -> None:
    confidences = []
    for count in (1, 3, 5, 10):
        position = _compute([_vote(position="yea", roll=roll) for roll in range(count)])
        assert position is not None
        confidences.append(position.confidence)
    assert confidences == sorted(confidences)
    assert confidences[0] < confidences[-1]


def test_confidence_saturates_at_the_published_high_badge_threshold() -> None:
    """`CONFIDENCE_SATURATION_WEIGHT` is read from the published `high`
    threshold so the two cannot drift: a record that earns prd.md's "High
    (>=10 relevant votes)" badge is one whose per-stance confidence has
    saturated."""
    position = _compute(
        [_vote(position="yea", roll=roll) for roll in range(int(CONFIDENCE_SATURATION_WEIGHT))]
    )
    assert position is not None
    assert position.confidence == pytest.approx(1.0)


def test_confidence_never_exceeds_one() -> None:
    """`CandidatePosition` bounds it to [0, 1] and would refuse the row, so an
    unclamped value would turn a long voting record into a failed write."""
    position = _compute([_vote(position="yea", roll=roll) for roll in range(60)])
    assert position is not None
    assert 0.0 <= position.confidence <= 1.0
    assert position.confidence == pytest.approx(1.0)


def test_quantity_changes_confidence_without_changing_direction() -> None:
    """The point of keeping the two separate: five identical votes evidence the
    same position as one, but better."""
    one = _compute([_vote(position="yea", roll=1)])
    five = _compute([_vote(position="yea", roll=roll) for roll in range(5)])
    assert one is not None and five is not None
    assert one.s_value == pytest.approx(five.s_value)
    assert five.confidence > one.confidence


# --- the row it produces --------------------------------------------------


def test_the_position_stays_inside_the_documented_bounds() -> None:
    """datamodel.md bounds `s_value` to [-1, 1]. A weighted mean of +-1 values
    is in [-1, 1] by construction; checked over a grid of mixed records anyway,
    because the bound is what `CandidatePosition` enforces on the way in."""
    for yeas in range(0, 4):
        for nays in range(0, 4):
            if yeas + nays == 0:
                continue
            votes = [_vote(position="yea", roll=roll) for roll in range(yeas)]
            votes += [_vote(position="nay", roll=100 + roll) for roll in range(nays)]
            position = _compute(votes)
            assert position is not None
            assert -1.0 <= position.s_value <= 1.0


def test_the_row_carries_everything_datamodel_md_documents() -> None:
    position = _compute([_vote(position="yea")])
    assert position is not None
    row = position.row("2027.1", computed_at="2026-10-19T00:00:00Z")
    assert row == {
        "person_id": PERSON_ID,
        "stance_id": f"{TOPIC_ID}:for",
        "s_value": pytest.approx(1.0),
        "confidence": pytest.approx(1.0 / CONFIDENCE_SATURATION_WEIGHT),
        "evidence_count": 1,
        "basis": BASIS_VOTES,
        "methodology_version": "2027.1",
        "computed_at": "2026-10-19T00:00:00Z",
    }


def test_the_row_carries_no_id_or_is_current() -> None:
    """Both are `Repository.append_version`'s to assign, which is what keeps
    "exactly one current row per (person, stance)" an invariant rather than a
    convention. A caller able to set either could mint a second current row."""
    position = _compute([_vote(position="yea")])
    assert position is not None
    row = position.row("2027.1")
    assert "id" not in row
    assert "is_current" not in row


def test_the_evidence_is_carried_out_of_the_computation_for_story_6_7() -> None:
    """`candidate_positions` stores a count, not a list, so story 6.7's
    citations have to come from here or be re-derived by re-reading
    `member_votes` on every alignment request."""
    position = _compute([_vote(position="yea", roll=1), _vote(position="nay", roll=2)])
    assert position is not None
    assert len(position.evidence) == 2
    assert {item.roll_call_id for item in position.evidence} == {
        "us-house:119-2:1",
        "us-house:119-2:2",
    }
    for item in position.evidence:
        assert item.roll_call_id, "a citation with no roll call id is not a citation"


def test_the_computation_is_deterministic() -> None:
    votes = [
        _vote(position="yea", roll=3),
        _vote(position="nay", roll=1, vote_type="cloture"),
        _vote(position="yea", roll=2, is_bundled=True),
    ]
    first = _compute(votes)
    reordered = _compute(list(reversed(votes)))
    assert first is not None and reordered is not None
    assert first.s_value == reordered.s_value
    assert first.confidence == reordered.confidence
