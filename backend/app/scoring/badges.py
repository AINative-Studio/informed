"""How much evidence a score rests on (story 6.5).

    prd.md, Feature 3: "Confidence badge: High (>=10 relevant votes or clear
    commitments), Medium, Low, Insufficient data."

backlog.md 6.5's acceptance criterion: "Badges are High, Medium, Low or
Insufficient, with **thresholds from the methodology**."

Why this story extends `methodology_versions.weights` with a fourth group
=========================================================================

datamodel.md documents three weight groups — `tiers`, `evidence_blend`,
`vote_types` — and story 6.1 published real numbers for all three. None of
them is a badge threshold, and prd.md's ">=10 relevant votes" is exactly the
kind of number datamodel.md says must not be hard-coded: "The live values come
from `methodology_versions` and are never hard-coded."

So this story adds a group, `badge_thresholds`, the same way story 6.1 added
`evidence_blend` and `vote_types` — which also did not exist until it wrote
them. Three things make that a documented extension rather than a schema
invention:

  1. `MethodologyVersion.weights` is deliberately an open
     `dict[str, dict[str, float]]`: story 4.2's model docstring says "nothing
     here forbids extra keys inside `weights`" precisely so a later story can
     add a group without editing a schema.
  2. `DOCUMENTED_WEIGHT_GROUPS` in `app.methodology.models` is the single list
     the public page reports against, so adding the name there makes the new
     group visible on `/v1/methodology` rather than a private side channel.
     Story 12.2's `weights_not_yet_published` then reports it honestly for the
     `2026.1` row that predates it.
  3. The numbers are prd.md's own. Only the ">=10 -> High" boundary is
     published there; `MEDIUM` and `LOW` are this story's judgment call and are
     documented as such below and in `docs/methodology-changelog.md`, the same
     treatment story 6.1 gave its vote-type figures.

Why the badge is computed from an evidence COUNT and not from the confidences
=============================================================================

The formula already uses `c_i` to damp a thin position's influence on the
number (prd.md: "thin evidence moves the score less"). The badge answers a
different question — "should I trust this number at all?" — and prd.md states
it in units of evidence: ">=10 relevant votes or clear commitments". A badge
derived from the mean confidence would say "High" for one position measured
from one unanimous final-passage vote, which is the over-trust the badge exists
to prevent.

So the badge counts `candidate_positions.evidence_count` across the positions
that actually entered the score, and nothing else. `app.scoring.service` is
responsible for passing only those; a position on a stance the voter did not
pick is not "relevant" in prd.md's sense.
"""

from __future__ import annotations

from typing import Literal

#: The four badges, exactly as backlog.md 6.5 names them. Lowercase on the wire
#: like every other enum in this codebase (`qa_status`, `vote_type`); the UI
#: owns the capitalised prose.
ConfidenceBadge = Literal["high", "medium", "low", "insufficient"]

#: The weight group this story adds to `methodology_versions.weights`. See the
#: module docstring for why a new group rather than a constant here.
BADGE_THRESHOLDS_GROUP = "badge_thresholds"

#: Minimum relevant-evidence counts per badge, as published defaults.
#:
#:   * `high` **10** — prd.md's own figure: "High (>=10 relevant votes or clear
#:     commitments)". The only number in this map that prd.md states.
#:   * `medium` **4** — this story's judgment call. Reasoning: a position
#:     resting on four or more recorded votes has survived more than one
#:     occasion, so it is not a single-vote artefact, but it is well short of
#:     the record prd.md calls High. Four rather than five because a member's
#:     votes on one stance in a two-year term are often a final passage plus
#:     an amendment plus a cloture plus a procedural — one legislative episode
#:     — and the badge should say "some record" at the point a reader could
#:     plausibly read a pattern.
#:   * `low` **1** — any recorded evidence at all. Non-zero deliberately: with
#:     zero evidence the formula returns no score (see
#:     `app.scoring.formula.alignment_score`), so `low` must start at 1 or
#:     `insufficient` and `low` would overlap and the badge would stop being a
#:     function of the count.
#:
#: Anything below `low` is `insufficient` — prd.md's "Insufficient data", the
#: case where there is no score to badge at all.
#:
#: Stored as floats because `methodology_versions.weights` is typed
#: `dict[str, dict[str, float]]` and ZeroDB's column for it is `"object"`;
#: compared against an integer count after `int()`, so a hand-edited 10.5
#: behaves as 10 rather than raising on a public endpoint.
DEFAULT_BADGE_THRESHOLDS: dict[str, float] = {
    "high": 10.0,
    "medium": 4.0,
    "low": 1.0,
}


def badge_for(
    evidence_count: int,
    thresholds: dict[str, float] | None = None,
) -> ConfidenceBadge:
    """The badge for a score resting on `evidence_count` relevant records.

    Args:
        evidence_count: how many recorded votes (or, from story 6.3 onward,
            sponsorships and statements) the positions that entered this score
            were computed from. Summed across positions by
            `app.scoring.service`, never across positions the voter's profile
            did not ask about.
        thresholds: the live `methodology_versions.weights.badge_thresholds`.
            `None` falls back to `DEFAULT_BADGE_THRESHOLDS` — which is the
            published default, not a second opinion: a version that carries no
            thresholds yet (the live `2026.1` row) must still badge rather than
            crash, exactly as story 4.2's reader seeds rather than fails.

    Returns:
        `"high"`, `"medium"`, `"low"` or `"insufficient"`.

    A pure function of its two arguments, like `alignment_score`: the badge is
    part of a response story 6.4 promises is reproducible, so it cannot depend
    on a clock or on anything it reads for itself.
    """
    live = thresholds or DEFAULT_BADGE_THRESHOLDS
    # Resolved per badge with the default as the per-key fallback, so a
    # partially-filled group (a methodology that published only `high`) badges
    # sensibly rather than treating the missing keys as zero — which would make
    # every scoreable candidate "medium" and silently retire the `low` badge.
    high = int(live.get("high", DEFAULT_BADGE_THRESHOLDS["high"]))
    medium = int(live.get("medium", DEFAULT_BADGE_THRESHOLDS["medium"]))
    low = int(live.get("low", DEFAULT_BADGE_THRESHOLDS["low"]))

    if evidence_count >= high:
        return "high"
    if evidence_count >= medium:
        return "medium"
    if evidence_count >= low:
        return "low"
    return "insufficient"


__all__ = [
    "BADGE_THRESHOLDS_GROUP",
    "DEFAULT_BADGE_THRESHOLDS",
    "ConfidenceBadge",
    "badge_for",
]
