"""The alignment response's wire shapes (stories 6.4, 6.5, 6.6, 6.7).

What is and is not in this response, and why
=============================================

prd.md's Core API table: `GET /v1/candidates/{id}/alignment?profile_id=` —
"Score, confidence, dealbreakers, top reasons". Four things, and this module is
four models.

Two absences are deliberate and load-bearing:

**No interest, stance wording, tier, intensity or note from the voter's
profile.** A voter's priorities are political-opinion data (prd.md, privacy
first). This response is derived from one, so it carries only what the voter
has already told us about themselves, expressed as a stance id they chose — and
`extra="forbid"` on every model here is what stops a later story adding a field
that carries more. `test_the_alignment_response_models_have_no_profile_fields`
pins the absence, in the same spirit as story 14.6's leak-proof summary card.

**No score when there is no evidence.** `score` is optional, and
`confidence_badge` is `insufficient` in that case. prd.md's "Show uncertainty"
principle and story 6.5's "Insufficient data" badge both need "we have no
evidence" to be distinguishable from "measured, and found neutral" (which is
50). A response that defaulted to 50 would be the single most misleading thing
this API could say.

The citation guarantee (story 6.7), made structural
====================================================

backlog.md 6.7: "Each reason cites a roll call or bill; no reason lacks a
citation."

That is enforced by `AlignmentReason` being **unconstructable without at least
one citation** — `citations` has no default and `min_length=1`, so
`AlignmentReason(...)` without them raises `ValidationError` before any
response exists. Not a service-layer check, not a test that happens to pass:
the type cannot hold an uncited reason, which is the same technique story 3.2
used to make "no stance is preselected" true (the concept has nowhere to live)
and story 14.6 used for its no-personalization endpoint.

The same guarantee covers the dealbreaker warning, for the stronger reason that
a warning is the most consequential thing this response says about a candidate:
`DealbreakerWarning.citations` is likewise `min_length=1`. prd.md's
"Evidence over opinion" applies with most force to an accusation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.scoring.badges import ConfidenceBadge
from app.scoring.formula import MAX_ALIGNMENT, MIN_ALIGNMENT

#: Which way one reason moved the score. A named direction rather than letting
#: the client infer it from the sign of a number: the UI colours a row by this
#: (prd.md's comparison view, "one row per interest, colored by direction"), and
#: a client that had to work it out would eventually work it out differently
#: from the scorer.
ReasonDirection = Literal["raised", "lowered"]


class Citation(BaseModel):
    """A link back to the primary source behind a claim.

    prd.md's third product principle: "Every score drills down to the primary
    source: the promise (with its URL and date) and the vote (roll-call number,
    chamber, date, bill text)."

    `roll_call_id` is required, not optional. backlog.md 6.7 says a reason
    "cites a roll call or bill", and this story's positions come only from
    votes — so every citation it can produce has a roll call, and making the
    field optional would create a shape ("a citation with neither") that is not
    a citation at all. When story 6.3 blends sponsorships and statements in,
    those need their own citation type with their own required field rather
    than this one loosened.
    """

    model_config = ConfigDict(extra="forbid")

    roll_call_id: str = Field(
        min_length=1,
        description=(
            "The `roll_calls` row id, e.g. `us-house:119-2:452` — chamber, "
            "session and roll number, which is what a reader needs to look the "
            "vote up at the source."
        ),
    )
    position: str = Field(
        description="How this candidate voted: `yea`, `nay`, `present` or `not_voting`."
    )
    vote_type: str = Field(
        description=(
            "`final_passage`, `amendment`, `cloture`, `motion_to_recommit`, "
            "`procedural` or `other`. Published so a reader can see for "
            "themselves that a procedural vote was weighted as one."
        )
    )
    voted_at: str = Field(description="When the vote was taken, ISO-8601.")
    bill_id: str | None = Field(
        default=None,
        description=(
            "The `bills` row id, when the roll call attached to a bill. Null "
            "for a procedural motion that did not — which is why the roll "
            "call, not the bill, is the required half of a citation."
        ),
    )
    is_bundled: bool = Field(
        default=False,
        description=(
            "Whether this was a vote on an omnibus measure. Published rather "
            "than hidden, because prd.md requires that a bundled vote be "
            "*shown* as bundled and not merely discounted: 'a vote on a "
            "1,000-page bill is not a vote for every clause'."
        ),
    )


class AlignmentReason(BaseModel):
    """One of the top reasons behind a score, with its citations (story 6.7).

    **Cannot be constructed without a citation.** See the module docstring:
    that is the whole mechanism by which "no reason lacks a citation" is true.
    """

    model_config = ConfigDict(extra="forbid")

    stance_id: str = Field(
        min_length=1,
        description=(
            "The `stances` row this reason is about — an id the voter already "
            "chose, never its wording and never the tier they filed it under."
        ),
    )
    direction: ReasonDirection = Field(
        description="Whether this interest raised or lowered the alignment score."
    )
    s_value: float = Field(
        ge=-1.0,
        le=1.0,
        description=(
            "The candidate's measured position on this stance: -1 acted "
            "against it, +1 acted for it."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="How much evidence that position rests on, 0-1.",
    )
    evidence_count: int = Field(
        ge=1,
        description=(
            "How many recorded votes produced this position. At least 1: a "
            "reason with no evidence is not a reason, and `citations` below "
            "could not be non-empty without one."
        ),
    )
    citations: list[Citation] = Field(
        min_length=1,
        description=(
            "The roll calls behind this reason. Never empty — the model "
            "refuses to be built without one (backlog.md 6.7: 'no reason "
            "lacks a citation')."
        ),
    )


class DealbreakerWarning(BaseModel):
    """A candidate acted against something the voter called a dealbreaker (6.6).

    prd.md: "any dealbreaker violation is shown above the score rather than
    averaged away", and its BDD scenario: "candidate A shows a dealbreaker
    warning above the score".

    Like `AlignmentReason`, unconstructable without a citation — with more
    force, because this is the most consequential thing the response says about
    a candidate, and prd.md's "Evidence over opinion" applies hardest to an
    accusation.
    """

    model_config = ConfigDict(extra="forbid")

    stance_id: str = Field(
        min_length=1,
        description="The dealbreaker stance the candidate acted against.",
    )
    s_value: float = Field(
        ge=-1.0,
        lt=0.0,
        description=(
            "The candidate's measured position, which is strictly negative — "
            "that is what makes this a violation rather than a neutral or "
            "unmeasured record. Bounded above by 0 in the type, so a warning "
            "about a candidate who did NOT act against the stance cannot be "
            "constructed."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="How much evidence the violating position rests on.",
    )
    evidence_count: int = Field(
        ge=1, description="How many recorded votes produced it. At least 1."
    )
    citations: list[Citation] = Field(
        min_length=1,
        description="The roll calls on which the candidate acted against this stance.",
    )


class CandidateAlignment(BaseModel):
    """One candidate's alignment with one voter's profile, computed per request.

    **Never stored.** datamodel.md: "Per-user alignment scores are computed per
    request and never stored... so no table links a person's priorities to a
    candidate." There is no `candidate_alignments` table and this model is not
    a `RowModel`: it exists for the duration of one response.
    """

    model_config = ConfigDict(extra="forbid")

    candidacy_id: str = Field(description="The `candidacies` row this score is for.")
    person_id: str = Field(description="The `people` row the candidacy names.")

    score: float | None = Field(
        default=None,
        ge=MIN_ALIGNMENT,
        le=MAX_ALIGNMENT,
        description=(
            "prd.md's alignment score, 0-100. **Null, not 50, when nothing "
            "could be measured** — 50 means 'measured, and found neutral', "
            "which is a different and much stronger claim. See "
            "`confidence_badge`."
        ),
    )
    confidence_badge: ConfidenceBadge = Field(
        description=(
            "How much evidence the score rests on: `high` (>=10 relevant "
            "records), `medium`, `low`, or `insufficient` — the last meaning "
            "there is no score at all. Thresholds come from the published "
            "methodology, not from this codebase."
        )
    )
    evidence_count: int = Field(
        ge=0,
        description=(
            "Total recorded votes behind every position that entered this "
            "score. What the badge is computed from, published so a reader can "
            "check the badge rather than take it."
        ),
    )
    scored_interest_count: int = Field(
        ge=0,
        description=(
            "How many of the voter's interests had a measured position. Lower "
            "than their total when the candidate has no record on some of "
            "them — which is information the voter needs and prd.md's 'Show "
            "uncertainty' principle requires."
        ),
    )
    dealbreaker_warnings: list[DealbreakerWarning] = Field(
        default_factory=list,
        description=(
            "Dealbreakers this candidate acted against. **Computed entirely "
            "separately from `score`, and never folded into it** (prd.md: "
            "'shown above the score rather than averaged away'). An empty list "
            "means no violation was found, which includes the case where the "
            "candidate has no measured position on a dealbreaker at all."
        ),
    )
    reasons: list[AlignmentReason] = Field(
        default_factory=list,
        description=(
            "The top reasons behind the score, largest contribution first, at "
            "most three (prd.md: 'the top three reasons'). Every one cites at "
            "least one roll call — structurally, see `AlignmentReason`."
        ),
    )
    methodology_version: str = Field(
        description=(
            "The methodology version whose weights produced this score. "
            "prd.md: 'every verdict links to the methodology version that "
            "produced it'; a score is scored evidence under the same rule."
        )
    )

    @property
    def has_dealbreaker_violation(self) -> bool:
        """Whether a warning should be shown above the score."""
        return bool(self.dealbreaker_warnings)


__all__ = [
    "AlignmentReason",
    "CandidateAlignment",
    "Citation",
    "DealbreakerWarning",
    "ReasonDirection",
]
