"""The top reasons behind a score, each with its citations (story 6.7).

    backlog.md 6.7: "As a voter, I want the top 3 reasons behind each score
    with links to the votes, so that I can see why."
    Acceptance: "Each reason cites a roll call or bill; no reason lacks a
    citation."

How "top" is decided
====================

By `abs(contribution)` — `|w_i * k_i * c_i * s_i|`, the same product
`app.scoring.formula.ScoredTerm.contribution` computes for the numerator.

That is the honest answer to "why is this score what it is", because the
contribution is literally the amount each interest moved the score away from
neutral. Three alternatives were available and are worse:

  * **By `|s_value|`** — the strongest positions. This would rank a
    nice-to-have interest the candidate matches perfectly above a primary
    interest they match well, which contradicts what the voter said "primary"
    meant and would make the reasons disagree with the number they explain.
  * **By tier weight** — the voter's priorities in order. This is a list of the
    voter's priorities, not an explanation of a score: a primary interest the
    candidate has a *neutral* record on contributed nothing and explains
    nothing.
  * **By evidence count** — the best-evidenced positions. Confidence is already
    inside the contribution (as `c_i`), so this would double-count it and
    surface a thin-but-decisive position below a well-evidenced irrelevance.

Ties are broken by stance id, so the list is reproducible: story 6.4's
"the same input always gives the same output" covers the whole response, and
two requests that read `candidate_positions` back in different orders must
produce the same three reasons in the same order.

The citation guarantee
======================

Structural, not procedural. `AlignmentReason.citations` is `min_length=1` with
no default, so an uncited reason cannot be constructed — and this module
therefore cannot emit one even by mistake. What it does instead, when a term's
citations are missing, is **drop that reason** and move to the next candidate:
see `top_reasons`' own note on why dropping beats raising.

Why only three
==============

prd.md, Feature 3's comparison view: "each race lists candidates with an
alignment score, a confidence badge and the top three reasons". Three is the
product's published number, and it is a cap rather than a quota: a candidate
with one measured position gets one reason, not three padded ones.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.scoring.formula import ScoredTerm
from app.scoring.models import AlignmentReason, Citation

#: prd.md, Feature 3: "the top three reasons". A maximum, never a target.
MAX_REASONS = 3


def top_reasons(
    terms: Sequence[ScoredTerm],
    citations: Mapping[str, Sequence[Citation]],
    *,
    limit: int = MAX_REASONS,
) -> list[AlignmentReason]:
    """The interests that moved the score most, largest first, each cited.

    Args:
        terms: the scored terms that went into `alignment_score`. Terms with a
            zero contribution are excluded — an interest that did not move the
            score is not a reason the score is what it is, and listing one
            would be padding.
        citations: stance id -> the roll calls behind that stance's position.
        limit: at most this many reasons. Defaults to prd.md's three.

    Returns:
        At most `limit` reasons, ordered by how much each moved the score. Every
        one carries at least one citation, structurally (see the module
        docstring).

    Pure: no I/O, no clock, no randomness.
    """
    ranked = sorted(
        (term for term in terms if term.contribution != 0.0),
        # Descending by absolute contribution, then ascending by stance id to
        # break ties deterministically. Negated rather than `reverse=True` so
        # the two keys can sort in opposite directions in one pass.
        key=lambda term: (-abs(term.contribution), term.stance_id),
    )

    reasons: list[AlignmentReason] = []
    for term in ranked:
        if len(reasons) >= limit:
            break
        cited = list(citations.get(term.stance_id) or ())
        if not cited:
            # No citations for this stance's position. Skipped rather than
            # emitted uncited (the model would refuse) and rather than raising
            # (which would turn a missing citation into a failed request and
            # deny the voter a score that is itself perfectly sound). Moving on
            # means the voter sees the next-largest reason they CAN check,
            # which is the behaviour backlog.md 6.7's "no reason lacks a
            # citation" actually asks for.
            continue
        reasons.append(
            AlignmentReason(
                stance_id=term.stance_id,
                direction="raised" if term.contribution > 0 else "lowered",
                s_value=term.s_value,
                confidence=term.confidence,
                evidence_count=len(cited),
                citations=cited,
            )
        )
    return reasons


__all__ = ["MAX_REASONS", "top_reasons"]
