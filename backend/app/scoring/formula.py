"""prd.md's alignment formula, and nothing else (story 6.4).

    Alignment = 50 * (1 + (sum_i w_i*k_i*c_i*s_i) / (sum_i w_i*k_i*c_i))

Why this is its own module with no imports from the rest of the app
====================================================================

This expression is the product. Every number a voter reads comes out of it, and
a bug in it does not look like a bug — it looks like a score. So it is isolated
from everything that could make it hard to read or hard to test: no ZeroDB
client, no profile model, no methodology reader, no clock, no logging. It takes
four numbers per interest and returns one number.

Everything about where those four numbers come from lives in
`app.scoring.service`: tier weight from the published `methodology_versions`
row, intensity from the voter's decrypted profile, position and confidence from
the current `candidate_positions` row. That split is what lets
`tests/test_alignment_formula_unit.py` test the arithmetic against
hand-computed values, and it is why that file has no fixtures at all.

Four decisions worth stating out loud
=====================================

**1. An unscoreable candidate returns `None`, not 50.**

The denominator `sum w_i*k_i*c_i` is zero in two real situations: the voter's
interests matched no measured position at all, and every matched position has
confidence 0 (which `CandidatePosition` guarantees means `evidence_count == 0`).
Returning 50.0 there would tell a voter "this candidate is exactly neutral on
your priorities" when the truth is "we know nothing about this candidate on
your priorities". prd.md's fourth product principle is "Show uncertainty", and
story 6.5's "Insufficient data" badge exists precisely for this case — it
cannot be distinguished downstream if the formula collapses it into a number.

**2. The terms are summed in a canonical order.**

`candidate_positions` rows come back from ZeroDB in whatever order the store
chooses, and floating-point addition is not associative: summing the same three
products in two orders can differ in the last bit. Story 6.4's acceptance
criterion is "the same input always gives the same output", and a score that
differs in the 15th decimal between two requests fails it in the only way that
matters — it makes a cached score and a fresh score disagree. So the terms are
sorted by `stance_id` before summing, which makes determinism a property of the
function rather than of the caller's read order.

**3. One stance contributes once.**

Two terms for the same stance would count that interest twice at its full tier
weight. `InterestProfile._one_stance_per_topic` already refuses the profile-side
version of this inflation; the scorer must not be a way around it, so duplicate
stance ids are refused rather than silently summed.

**4. The bounds on each input are enforced here, again.**

`CandidatePosition` already bounds `s_value` to [-1, 1] and `confidence` to
[0, 1] on the way into storage. `ScoredTerm` bounds them again on the way into
the formula for two reasons: a challenger's position (epic 10) is computed from
promises and never stored, so it never passes through that model; and an
out-of-range value produces a score outside [0, 100] that no downstream code
would flag — it would simply render as a 137% match.

What this module does NOT do
============================

* It does not read the methodology. `w_i` is a number handed in.
* It does not cap nice-to-have interests. prd.md's Feature 1 rule ("never
  lowers a score below the primary/secondary result by more than 10%") is a
  rule ABOUT two scores computed by this function, so it belongs to the caller
  that computes both — see `app.scoring.service`.
* It does not know about dealbreakers. Story 6.6 is deliberately a separate
  check that never touches this sum: prd.md's requirement is a warning "above
  the score", not an adjustment to it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

#: The published range of an alignment score. prd.md, Feature 3: "Each
#: candidate gets one alignment score (0-100)". Named constants rather than
#: literals because three modules assert against them (this one's bound check,
#: `app.scoring.service`'s nice-to-have cap, the response model's field bounds)
#: and a 0-100 scale that disagreed with itself across them would be worse than
#: one that was wrong consistently.
MIN_ALIGNMENT = 0.0
MAX_ALIGNMENT = 100.0

#: The midpoint, which means "measured, and found neutral" — NEVER "unknown".
#: See the module docstring, decision 1.
NEUTRAL_ALIGNMENT = 50.0

#: The multiplier in prd.md's formula: it maps a weighted mean position in
#: [-1, +1] onto [0, 100]. Half the published range, by construction.
_SCALE = (MAX_ALIGNMENT - MIN_ALIGNMENT) / 2.0


class ScoredTerm(BaseModel):
    """One interest's contribution to the sum: w_i, k_i, c_i and s_i.

    Frozen, because a term is an input to a pure function: a caller that could
    mutate one after building the list could make two scores of "the same"
    input differ, which is exactly the property story 6.4 pins.

    `stance_id` is carried for three jobs, none of them arithmetic: the
    canonical sum order (module docstring, decision 2), the duplicate check
    (decision 3), and story 6.7's reasons, which have to say which interest a
    contribution came from.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The `stances` row this term scores. Identifies the term; never summed.
    stance_id: str = Field(min_length=1)

    #: w_i — the voter's tier weight for this interest, from the published
    #: methodology. Bounded below at 0 and not above: a methodology may
    #: republish weights on any scale it likes (the score is invariant to
    #: rescaling them all), but a NEGATIVE weight would invert this interest's
    #: contribution and score the voter as wanting the opposite of what they
    #: said. Zero is allowed: a methodology that weights a tier at 0 has
    #: excluded it, which is a coherent choice.
    tier_weight: float = Field(ge=0.0)

    #: k_i — prd.md: "Intensity inside its tier (1-3) for tie-breaking."
    intensity: int = Field(ge=1, le=3)

    #: c_i — prd.md: "evidence confidence (0-1), so thin evidence moves the
    #: score less."
    confidence: float = Field(ge=0.0, le=1.0)

    #: s_i — prd.md: "the candidate's position s_i runs from -1 (acts against
    #: the voter's stance) to +1 (acts for it)."
    s_value: float = Field(ge=-1.0, le=1.0)

    @property
    def weight(self) -> float:
        """w_i * k_i * c_i — this term's share of the denominator."""
        return self.tier_weight * self.intensity * self.confidence

    @property
    def contribution(self) -> float:
        """w_i * k_i * c_i * s_i — this term's share of the numerator.

        Also what story 6.7 ranks reasons by, in absolute value: the terms that
        moved the score furthest from neutral are the ones a voter is owed an
        explanation for.
        """
        return self.weight * self.s_value


def weighted_mean_position(terms: Iterable[ScoredTerm]) -> float | None:
    """The bracketed quotient of prd.md's formula, in [-1, +1].

    Returns `None` when the denominator is zero — no evidence rather than a
    neutral position. See the module docstring, decision 1.

    Raises:
        ValueError: two terms name the same stance. See decision 3.
    """
    ordered = _canonical(terms)
    denominator = _sum_in_order(term.weight for term in ordered)
    if denominator <= 0.0:
        return None
    numerator = _sum_in_order(term.contribution for term in ordered)
    return numerator / denominator


def alignment_score(terms: Iterable[ScoredTerm]) -> float | None:
    """prd.md's alignment score, 0-100, or `None` when nothing is measured.

    Pure: no clock, no randomness, no I/O, no mutation of its argument. The
    same list always returns the same float, bit for bit — which is story 6.4's
    third acceptance criterion and is pinned by
    `test_the_same_terms_always_produce_the_same_score`.

    Raises:
        ValueError: two terms name the same stance.
    """
    mean = weighted_mean_position(terms)
    if mean is None:
        return None
    score = _SCALE * (1.0 + mean)
    # Clamped, not merely asserted. The mean of values in [-1, 1] is in
    # [-1, 1], so this can only ever correct a last-bit floating-point
    # overshoot at the extremes (a perfect +1 record computing to
    # 100.00000000000001). A caller rendering a progress bar should not have to
    # defend against that, and the alternative — asserting — would turn a
    # rounding artefact into a 500 on the product's most-requested endpoint.
    return min(MAX_ALIGNMENT, max(MIN_ALIGNMENT, score))


def _canonical(terms: Iterable[ScoredTerm]) -> Sequence[ScoredTerm]:
    """The terms in the one order this module ever sums them in.

    Sorted by `stance_id`, having refused duplicates. See the module docstring,
    decisions 2 and 3.
    """
    materialized = list(terms)
    seen: set[str] = set()
    for term in materialized:
        if term.stance_id in seen:
            raise ValueError(
                f"stance {term.stance_id!r} appears twice in one score; each stance "
                "contributes once, or the interest is counted twice at its full tier weight"
            )
        seen.add(term.stance_id)
    return sorted(materialized, key=lambda term: term.stance_id)


def _sum_in_order(values: Iterable[float]) -> float:
    """`sum` over an already-ordered iterable, named so the ordering is visible.

    A plain `sum()` call here would read as incidental; the whole point is that
    the sequence reaching it was sorted by `_canonical` first, because floating
    point addition is not associative and story 6.4 promises reproducibility.
    """
    return sum(values)


__all__ = [
    "MAX_ALIGNMENT",
    "MIN_ALIGNMENT",
    "NEUTRAL_ALIGNMENT",
    "ScoredTerm",
    "alignment_score",
    "weighted_mean_position",
]
