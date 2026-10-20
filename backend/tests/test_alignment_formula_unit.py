"""prd.md's alignment formula, tested as arithmetic (story 6.4).

Why this file has no ZeroDB client, no fake transport and no TestClient
=======================================================================

This is the single most consequential expression in the product:

    Alignment = 50 * (1 + (sum_i w_i*k_i*c_i*s_i) / (sum_i w_i*k_i*c_i))

Every number a voter ever sees comes out of it. A bug here is not a 500 that
someone notices — it is a wrong score that looks exactly like a right one. So
it is tested the way a formula is tested: against hand-computed values, at its
boundaries, and for the algebraic properties prd.md's neutrality rule depends
on (antisymmetry, scale invariance, weight monotonicity). None of that needs a
database, and mixing it with one would mean a storage bug could mask an
arithmetic bug or vice versa.

The HTTP and ZeroDB halves are covered separately, by
`tests/test_alignment_api_unit.py` and
`tests/step_defs/test_alignment_score_steps.py`.

What is deliberately NOT tested here
====================================

Where `w_i`, `k_i`, `c_i` and `s_i` come from. That is the scorer's job (read
tier weights from the published methodology, read positions from
`candidate_positions`, read intensity from the voter's profile) and it is
tested there. This file takes the four numbers as given and checks only that
50*(1 + weighted mean) is computed correctly — which is the one thing no
integration test can isolate.
"""

from __future__ import annotations

import math

import pytest

from app.scoring.formula import (
    MAX_ALIGNMENT,
    MIN_ALIGNMENT,
    NEUTRAL_ALIGNMENT,
    ScoredTerm,
    alignment_score,
    weighted_mean_position,
)


def _term(
    *,
    tier_weight: float,
    intensity: int = 1,
    confidence: float = 1.0,
    s_value: float,
    stance_id: str = "stance",
) -> ScoredTerm:
    return ScoredTerm(
        stance_id=stance_id,
        tier_weight=tier_weight,
        intensity=intensity,
        confidence=confidence,
        s_value=s_value,
    )


# --- the three fixed points prd.md's shape guarantees ---------------------


def test_perfect_agreement_scores_one_hundred() -> None:
    """Every s_i = +1 makes the weighted mean 1, so 50*(1+1) = 100."""
    terms = [
        _term(tier_weight=3.0, s_value=1.0, stance_id="a"),
        _term(tier_weight=0.5, s_value=1.0, stance_id="b"),
    ]
    assert alignment_score(terms) == pytest.approx(100.0)


def test_perfect_disagreement_scores_zero() -> None:
    """Every s_i = -1 makes the weighted mean -1, so 50*(1-1) = 0."""
    terms = [
        _term(tier_weight=3.0, s_value=-1.0, stance_id="a"),
        _term(tier_weight=0.5, s_value=-1.0, stance_id="b"),
    ]
    assert alignment_score(terms) == pytest.approx(0.0)


def test_no_measured_direction_scores_fifty() -> None:
    """Every s_i = 0 makes the weighted mean 0, so 50*(1+0) = 50.

    50 is therefore "measured, and found neutral" — NOT "unknown". Unknown is
    `None`, which the formula returns when the denominator is zero; the
    distinction is the whole reason `alignment_score` returns an optional
    rather than defaulting to 50.
    """
    terms = [_term(tier_weight=3.0, s_value=0.0)]
    assert alignment_score(terms) == pytest.approx(50.0)


def test_the_published_bounds_are_zero_and_one_hundred() -> None:
    assert (MIN_ALIGNMENT, NEUTRAL_ALIGNMENT, MAX_ALIGNMENT) == (0.0, 50.0, 100.0)


# --- hand-computed cases --------------------------------------------------


def test_a_hand_computed_two_interest_score() -> None:
    """One primary (w=3.0, k=2, c=1.0, s=+1) and one secondary
    (w=1.5, k=1, c=0.5, s=-1):

        numerator   = 3.0*2*1.0*(+1) + 1.5*1*0.5*(-1) = 6.0 - 0.75 = 5.25
        denominator = 3.0*2*1.0      + 1.5*1*0.5      = 6.0 + 0.75 = 6.75
        mean        = 5.25 / 6.75                      = 0.7777...
        alignment   = 50 * (1 + 0.7777...)             = 88.888...
    """
    terms = [
        _term(tier_weight=3.0, intensity=2, confidence=1.0, s_value=1.0, stance_id="a"),
        _term(tier_weight=1.5, intensity=1, confidence=0.5, s_value=-1.0, stance_id="b"),
    ]
    assert weighted_mean_position(terms) == pytest.approx(5.25 / 6.75)
    assert alignment_score(terms) == pytest.approx(50.0 * (1.0 + 5.25 / 6.75))


def test_a_hand_computed_three_interest_score() -> None:
    """Three interests across all three tiers, computed by hand:

    primary      w=3.0 k=3 c=0.8 s=+0.5  ->  num  +3.6   den 7.2
    secondary    w=1.5 k=2 c=0.6 s=-1.0  ->  num  -1.8   den 1.8
    nice_to_have w=0.5 k=1 c=1.0 s=+1.0  ->  num  +0.5   den 0.5

    num = 3.6 - 1.8 + 0.5 = 2.3
    den = 7.2 + 1.8 + 0.5 = 9.5
    alignment = 50 * (1 + 2.3/9.5) = 62.105263...
    """
    terms = [
        _term(tier_weight=3.0, intensity=3, confidence=0.8, s_value=0.5, stance_id="a"),
        _term(tier_weight=1.5, intensity=2, confidence=0.6, s_value=-1.0, stance_id="b"),
        _term(tier_weight=0.5, intensity=1, confidence=1.0, s_value=1.0, stance_id="c"),
    ]
    assert alignment_score(terms) == pytest.approx(50.0 * (1.0 + 2.3 / 9.5))
    assert alignment_score(terms) == pytest.approx(62.10526315789474)


def test_a_single_interest_score_is_just_its_s_value_rescaled() -> None:
    """With one term the weights cancel entirely: the mean IS s_i.

    This is the property that makes the formula readable — 50*(1+s) maps
    [-1, +1] onto [0, 100] linearly — and it must hold for ANY weight, which
    is what the loop checks.
    """
    for weight in (0.5, 1.5, 3.0, 17.0):
        for s_value in (-1.0, -0.5, 0.0, 0.25, 1.0):
            score = alignment_score([_term(tier_weight=weight, s_value=s_value)])
            assert score == pytest.approx(50.0 * (1.0 + s_value))


# --- the algebraic properties neutrality depends on -----------------------


def test_the_score_is_antisymmetric_under_flipping_every_position() -> None:
    """prd.md's symmetry audit: "two synthetic candidates with mirrored voting
    records ... each voter's score for their aligned candidate is identical".

    Mirroring every s_i must map a score of `x` to `100 - x` exactly. If it did
    not, one direction of the political axis would score higher than the other
    for arithmetic reasons alone, which is the precise failure
    "nonpartisan by construction" forbids.
    """
    terms = [
        _term(tier_weight=3.0, intensity=3, confidence=0.9, s_value=0.6, stance_id="a"),
        _term(tier_weight=1.5, intensity=1, confidence=0.4, s_value=-0.2, stance_id="b"),
        _term(tier_weight=0.5, intensity=2, confidence=1.0, s_value=1.0, stance_id="c"),
    ]
    mirrored = [
        _term(
            tier_weight=term.tier_weight,
            intensity=term.intensity,
            confidence=term.confidence,
            s_value=-term.s_value,
            stance_id=term.stance_id,
        )
        for term in terms
    ]
    original = alignment_score(terms)
    flipped = alignment_score(mirrored)
    assert original is not None and flipped is not None
    assert original + flipped == pytest.approx(100.0)


def test_the_score_is_invariant_to_rescaling_every_weight() -> None:
    """Doubling every tier weight cannot change a score.

    A weighted mean is scale-invariant in its weights, and it matters here
    because the methodology may republish tier weights as (6.0, 3.0, 1.0)
    rather than (3.0, 1.5, 0.5) — the same *relative* priorities. A score that
    moved under that republication would make the methodology version a hidden
    input to the number rather than a stated one.
    """
    terms = [
        _term(tier_weight=3.0, intensity=2, confidence=0.7, s_value=0.3, stance_id="a"),
        _term(tier_weight=0.5, intensity=1, confidence=0.9, s_value=-0.8, stance_id="b"),
    ]
    scaled = [
        _term(
            tier_weight=term.tier_weight * 2.0,
            intensity=term.intensity,
            confidence=term.confidence,
            s_value=term.s_value,
            stance_id=term.stance_id,
        )
        for term in terms
    ]
    assert alignment_score(terms) == pytest.approx(alignment_score(scaled))


def test_raising_a_matching_interests_weight_raises_the_score() -> None:
    """prd.md's tier table is only meaningful if weight monotonicity holds: a
    voter who promotes a matched interest from nice-to-have to primary must see
    the score go up, never down."""
    opposed = _term(tier_weight=1.5, s_value=-1.0, stance_id="opposed")
    low = alignment_score([_term(tier_weight=0.5, s_value=1.0, stance_id="matched"), opposed])
    high = alignment_score([_term(tier_weight=3.0, s_value=1.0, stance_id="matched"), opposed])
    assert low is not None and high is not None
    assert high > low


def test_raising_intensity_on_a_matching_interest_raises_the_score() -> None:
    """prd.md: "Intensity inside its tier (1-3) for tie-breaking"."""
    opposed = _term(tier_weight=3.0, intensity=2, s_value=-1.0, stance_id="opposed")
    weak = alignment_score(
        [_term(tier_weight=3.0, intensity=1, s_value=1.0, stance_id="matched"), opposed]
    )
    strong = alignment_score(
        [_term(tier_weight=3.0, intensity=3, s_value=1.0, stance_id="matched"), opposed]
    )
    assert weak is not None and strong is not None
    assert strong > weak


def test_lower_confidence_pulls_a_position_toward_neutral() -> None:
    """prd.md: "c_i is evidence confidence (0-1), so thin evidence moves the
    score less."

    Checked against the one term whose confidence varies while a confident
    neutral term holds the other side: as c -> 0 the score -> 50.
    """
    anchor = _term(tier_weight=3.0, confidence=1.0, s_value=0.0, stance_id="anchor")
    scores = [
        alignment_score(
            [_term(tier_weight=3.0, confidence=c, s_value=1.0, stance_id="thin"), anchor]
        )
        for c in (0.01, 0.25, 0.5, 1.0)
    ]
    assert all(score is not None for score in scores)
    assert scores == sorted(scores)  # monotonically rising with confidence
    assert scores[0] == pytest.approx(50.0, abs=0.5)


def test_a_zero_confidence_term_cannot_move_the_score() -> None:
    """A position with no evidence behind it has to be inert, not merely small.

    `candidate_positions` already refuses `confidence > 0` with
    `evidence_count == 0` (see `app.repositories.models.CandidatePosition`), so
    a zero-confidence term is exactly the "we have no evidence" case, and it
    must contribute nothing to either sum.
    """
    measured = _term(tier_weight=3.0, confidence=1.0, s_value=0.4, stance_id="measured")
    unmeasured = _term(tier_weight=3.0, confidence=0.0, s_value=-1.0, stance_id="unmeasured")
    assert alignment_score([measured]) == pytest.approx(alignment_score([measured, unmeasured]))


# --- the degenerate cases, which are where a formula silently lies --------


def test_no_terms_at_all_scores_none_rather_than_fifty() -> None:
    """An unscoreable candidate must be distinguishable from a neutral one.

    Returning 50.0 here would tell a voter "this candidate is exactly neutral
    on your priorities" when the truth is "we have no evidence about this
    candidate". prd.md's "Show uncertainty" principle and story 6.5's
    "Insufficient data" badge both depend on these being different answers.
    """
    assert alignment_score([]) is None
    assert weighted_mean_position([]) is None


def test_terms_whose_weights_all_cancel_to_zero_score_none() -> None:
    """The denominator can be zero even with terms present: every term's
    confidence is 0. Same reasoning as the empty case — no evidence means no
    score, not a neutral one — and a bare division would have raised
    ZeroDivisionError into a 500."""
    terms = [
        _term(tier_weight=3.0, confidence=0.0, s_value=1.0, stance_id="a"),
        _term(tier_weight=1.5, confidence=0.0, s_value=-1.0, stance_id="b"),
    ]
    assert alignment_score(terms) is None


def test_the_score_never_leaves_zero_to_one_hundred() -> None:
    """A weighted mean of values in [-1, 1] is itself in [-1, 1], so the score
    is in [0, 100] by construction. Checked over a grid anyway: the bound is a
    promise to every caller (a UI progress bar, a comparison table), and the
    cost of pinning it is one loop."""
    for s_a in (-1.0, -0.5, 0.0, 0.5, 1.0):
        for s_b in (-1.0, -0.5, 0.0, 0.5, 1.0):
            for confidence in (0.1, 0.5, 1.0):
                score = alignment_score(
                    [
                        _term(tier_weight=3.0, confidence=confidence, s_value=s_a, stance_id="a"),
                        _term(tier_weight=0.5, confidence=1.0, s_value=s_b, stance_id="b"),
                    ]
                )
                assert score is not None
                assert MIN_ALIGNMENT <= score <= MAX_ALIGNMENT


def test_an_out_of_range_s_value_is_refused() -> None:
    """`s_value` outside [-1, 1] would push the score outside [0, 100] and
    would not error anywhere downstream — it would just render as a 137% match.
    `CandidatePosition` bounds it on the way into storage; `ScoredTerm` bounds
    it on the way into the formula, because the formula also scores
    positions that were never stored (a challenger's, epic 10)."""
    with pytest.raises(ValueError, match="s_value"):
        _term(tier_weight=3.0, s_value=1.5)
    with pytest.raises(ValueError, match="s_value"):
        _term(tier_weight=3.0, s_value=-1.5)


def test_an_out_of_range_confidence_is_refused() -> None:
    with pytest.raises(ValueError, match="confidence"):
        _term(tier_weight=3.0, confidence=1.2, s_value=0.0)
    with pytest.raises(ValueError, match="confidence"):
        _term(tier_weight=3.0, confidence=-0.1, s_value=0.0)


def test_an_out_of_range_intensity_is_refused() -> None:
    """prd.md bounds intensity to 1-3. A 7 would not error anywhere — it would
    quietly outweigh every other interest in the same tier, which is exactly
    the hazard `app.profiles.models.Interest` documents for the same field."""
    with pytest.raises(ValueError, match="intensity"):
        _term(tier_weight=3.0, intensity=0, s_value=0.0)
    with pytest.raises(ValueError, match="intensity"):
        _term(tier_weight=3.0, intensity=4, s_value=0.0)


def test_a_negative_tier_weight_is_refused() -> None:
    """A negative weight would invert that interest's contribution: the voter
    would be scored as wanting the opposite of what they said. Nothing
    downstream would notice."""
    with pytest.raises(ValueError, match="tier_weight"):
        _term(tier_weight=-3.0, s_value=1.0)


# --- determinism, which is story 6.4's third acceptance criterion ---------


def test_the_same_terms_always_produce_the_same_score() -> None:
    """backlog.md 6.4: "the same input always gives the same output".

    `alignment_score` is a pure function of its argument — no clock, no
    randomness, no iteration over a set, no floating-point accumulation whose
    order depends on anything but the list it was handed. Pinned over repeated
    calls rather than argued for in a comment.
    """
    terms = [
        _term(tier_weight=3.0, intensity=3, confidence=0.73, s_value=0.41, stance_id="a"),
        _term(tier_weight=1.5, intensity=2, confidence=0.29, s_value=-0.87, stance_id="b"),
        _term(tier_weight=0.5, intensity=1, confidence=0.91, s_value=0.05, stance_id="c"),
    ]
    scores = {alignment_score(terms) for _ in range(100)}
    assert len(scores) == 1


def test_the_score_does_not_depend_on_the_order_of_the_terms() -> None:
    """A weighted mean is order-independent in exact arithmetic, and must be in
    floating point too for a score to be reproducible: `candidate_positions`
    rows come back from ZeroDB in whatever order the store feels like, and two
    requests that read them in different orders must agree to the last digit.

    The implementation therefore sorts by `stance_id` before summing, which is
    what makes this test pass rather than nearly-pass.
    """
    terms = [
        _term(tier_weight=3.0, intensity=3, confidence=0.73, s_value=0.41, stance_id="a"),
        _term(tier_weight=1.5, intensity=2, confidence=0.29, s_value=-0.87, stance_id="b"),
        _term(tier_weight=0.5, intensity=1, confidence=0.91, s_value=0.05, stance_id="c"),
        _term(tier_weight=3.0, intensity=1, confidence=0.11, s_value=0.99, stance_id="d"),
    ]
    forward = alignment_score(terms)
    backward = alignment_score(list(reversed(terms)))
    shuffled = alignment_score([terms[2], terms[0], terms[3], terms[1]])
    assert forward == backward == shuffled
    assert forward is not None and not math.isnan(forward)


def test_two_terms_for_the_same_stance_are_refused() -> None:
    """One stance contributes once. Two terms for one stance would double-count
    it at its full tier weight, which is the same inflation
    `InterestProfile._one_stance_per_topic` refuses on the profile side — and
    the scorer must not be a route around it."""
    duplicate = [
        _term(tier_weight=3.0, s_value=1.0, stance_id="same"),
        _term(tier_weight=3.0, s_value=-1.0, stance_id="same"),
    ]
    with pytest.raises(ValueError, match="once"):
        alignment_score(duplicate)
