"""The response shape for `GET /v1/candidates/{candidacy_id}/summary-card`
(story 14.6).

datamodel.md's `candidate_cards` read model is specified as:

    {candidacy_id} | person_name, party, district, photo_url,
                      verdict_counts {kept, partial, broken, …}, say_do_gap,
                      positions_summary, updated_at

rebuilt from events (`verdict.published`, `positions.recomputed`) that no
story has published yet — epics 6, 7 and 11, which compute verdicts,
say/do gaps and position summaries, are not built. This model carries
datamodel.md's exact field list, with the fields nothing can compute yet
set to `None` rather than invented: a voter sharing this card sees "not
available yet" (an absent field), never a fabricated verdict count.

Why this model structurally cannot carry a profile field
==========================================================

Story 14.6's acceptance criterion is "a test asserts no profile fields
leak" — and the strongest version of that guarantee is one the type system
enforces, not one a reviewer has to keep re-checking by eye. Two choices
make it structural rather than conventional:

  1. **`extra="forbid"`.** A caller — including a future edit to
     `SummaryCardService` — cannot attach a `profile_id`, `user_id` or
     `interests` field to an instance even by passing it as a kwarg; Pydantic
     raises immediately. Pinned by
     `test_the_response_model_structurally_forbids_profile_fields`.
  2. **No field here is named, or resembles, a voter identity.** Every field
     is drawn from datamodel.md's documented `candidate_cards` spec, which
     is itself a read model over public candidate/campaign data — there is
     no voter-shaped column in that spec to copy from, so there was never a
     profile field to leave out by discipline.

Together these mean "no profile field can leak through this response" is
true because there is no field *through which* one could, not because
nothing currently writes one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VerdictCounts(BaseModel):
    """How many of a candidate's tracked promises landed in each verdict.

    datamodel.md: `verdict_counts {kept, partial, broken, …}`. Not populated
    by this story — no `verdicts` exist yet, since epic 7 is not built — but
    typed now so the shape is pinned and a later story fills it in rather
    than reshaping the response.
    """

    model_config = ConfigDict(extra="forbid")

    kept: int = 0
    partial: int = 0
    broken: int = 0
    no_opportunity: int = 0
    absent: int = 0
    not_testable: int = 0


class CandidateSummaryCard(BaseModel):
    """A shareable summary of one candidacy, candidate data only.

    Every field here is public campaign/candidate information or a
    not-yet-computed placeholder (`None`) — never a voter's data. See the
    module docstring for why that is structural (`extra="forbid"`, no
    identity-shaped field exists to populate) rather than a promise kept by
    convention.

    `verdict_counts`, `say_do_gap` and `positions_summary` are `None` today
    for every candidacy, honestly: they are datamodel.md's documented
    `candidate_cards` fields, rebuilt from `verdict.published` and
    `positions.recomputed` events that epics 6/7/11 have not yet published.
    Returning `None` rather than `0`, `{}` or an invented number is
    deliberate — `0` would read as "zero promises kept", which is a claim
    about the candidate's record this release has no evidence for, and
    prd.md's "evidence over opinion" rule forbids asserting what has not
    been computed.
    """

    model_config = ConfigDict(extra="forbid")

    candidacy_id: str = Field(description="The `candidacies` row this card summarizes")
    person_name: str = Field(description="The candidate's name, as filed")
    party: str | None = Field(default=None, description="Party as recorded on the candidacy")
    district: str = Field(description="Human-readable district/seat name")
    photo_url: str | None = Field(default=None, description="The candidate's photo, if known")
    verdict_counts: VerdictCounts | None = Field(
        default=None,
        description=(
            "Counts of this candidate's promises by verdict. None until "
            "epic 7's verdicts exist — never a fabricated zero."
        ),
    )
    say_do_gap: float | None = Field(
        default=None,
        description=(
            "How often this candidate's votes matched their stated promises. "
            "None until epic 11 computes it."
        ),
    )
    positions_summary: str | None = Field(
        default=None,
        description=(
            "A short, evidence-linked summary of this candidate's computed "
            "positions. None until epic 6's candidate_positions exist."
        ),
    )
    updated_at: str = Field(description="When this card's data was last refreshed, ISO-8601 UTC")


__all__ = ["CandidateSummaryCard", "VerdictCounts"]
