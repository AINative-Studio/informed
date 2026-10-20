"""The challenger-promises response's wire shapes (story 9.6).

backlog.md 9.6: "As a voter, I want to see a challenger's promises even
though they have no voting record, so that I can still compare them.
Acceptance: promises are grouped by my interests; a prior record in another
office is linked if one exists."

Why this is its own endpoint rather than a widened `/alignment`
=================================================================

`GET /v1/candidates/{id}/alignment` (stories 6.4-6.7) groups by the SAME
key this module uses -- a voter's chosen `stance_id` -- but it is built only
from `candidate_positions`, which story 6.2 computes only from
`member_votes`. A challenger has no voting record by definition, so they are
not invisible there by accident: `AlignmentReason`'s own docstring is explicit
that "no clear record" (an unmeasured stance) is a real, honestly-reported
outcome, and `scored_interest_count` can legitimately be 0 for a challenger.

That is the right answer for a VOTE-based score. It is the wrong answer for
"can I compare this challenger at all", because prd.md's Promise vs. Record
feature put promises in the product for exactly this voter: a challenger has
no votes but can have plenty of promises (story 9.3's extractor), and
`AlignmentService` never reads the `promises` table at all. This module is the
read path for that other table, grouped the same way the voter already
understands grouping (their own `stance_id`), so the UI pattern
`CandidateDetailPage` already established generalizes rather than forking.

What is and is not in this response
====================================

Like `app.scoring.models`, this module carries no interest wording, tier,
intensity or note from the voter's profile -- only the `stance_id` the voter
themselves chose, the same posture `AlignmentReason` takes and for the same
reason (prd.md, privacy first). `extra="forbid"` on every model pins that.

Also like `app.scoring.models`, this response never recommends, endorses or
ranks: there is no score here at all, by construction -- a promise is shown,
quoted and sourced, never scored into a number this module could rank by.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.repositories.models import PromiseDirection


class PromiseItem(BaseModel):
    """One promise, as a voter needs to read and verify it.

    Carries exactly what `app.repositories.models.Promise` stores that a
    voter-facing citation needs: the verbatim quote (prd.md's "evidence over
    opinion"), the commitment's shape (actor/action/object/direction),
    whether it is testable, and the source it was quoted from. Deliberately
    NOT `confidence`, `extractor_version`, `vector_id` or `mappings[]` --
    those are production internals about HOW the row was produced, not
    evidence a voter reads to judge a promise.
    """

    model_config = ConfigDict(extra="forbid")

    promise_id: str = Field(description="The `promises` row id (`prm:{hash}`).")
    quote_span: str = Field(
        min_length=1,
        description="The commitment's own words, verbatim from the source text.",
    )
    actor: str | None = Field(default=None, description="Who the commitment names, if anyone.")
    action: str | None = Field(
        default=None,
        description="The checkable action, when `testable` is true.",
    )
    object: str | None = Field(  # noqa: A003 - mirrors `Promise.object`
        default=None,
        description="What the action is about, when `testable` is true.",
    )
    direction: PromiseDirection = Field(
        description="`support`, `oppose` or `unclear` -- see `Promise.direction`."
    )
    testable: bool = Field(
        description=(
            "Whether this is a checkable commitment (prd.md: 'vague rhetoric "
            "is tagged non-testable and shown but not scored'). This endpoint "
            "shows every promise either way; it never scores one."
        )
    )
    source_url: str = Field(
        min_length=1,
        description="Where this promise was made -- the campaign page, debate or questionnaire.",
    )
    captured_at: str = Field(description="When Informed archived that source, ISO-8601.")


class PromiseGroup(BaseModel):
    """Every promise about one of the voter's own interests.

    Keyed by `stance_id`, exactly as `AlignmentReason` is (story 6.7) -- the
    id a voter chose, never its wording, tier or note. See this module's
    docstring for why that is the grouping key the acceptance criterion
    means by "my interests".
    """

    model_config = ConfigDict(extra="forbid")

    stance_id: str = Field(
        min_length=1,
        description="The `stances` row this group is about -- an id the voter already chose.",
    )
    promises: list[PromiseItem] = Field(
        min_length=1,
        description="Every one of this challenger's promises mapped to this stance.",
    )


class PriorOffice(BaseModel):
    """One elected office this person held before this candidacy.

    Sourced from `people.terms[]` (datamodel.md, embedded on the `people`
    row) -- not a new backend concept: every term that is not this
    candidacy's own current run is, by construction, a PRIOR office. A
    challenger who has one gets it named; one who never held office gets no
    row at all (`ChallengerPromises.prior_offices` is simply empty), rather
    than a fabricated entry -- see `ChallengerPromisesService` for how a term
    is read off the person's own record, never guessed.
    """

    model_config = ConfigDict(extra="forbid")

    body_id: str = Field(min_length=1, description="The legislative body they served in.")
    district_id: str | None = Field(default=None, description="The district they served, if any.")
    party: str | None = Field(default=None, description="The party they served under, if known.")
    start: str | None = Field(default=None, description="When that term began, if known.")
    end: str | None = Field(default=None, description="When that term ended, if known, or null.")


class ChallengerPromises(BaseModel):
    """One challenger's promises, grouped by one voter's own interests.

    **Never stored**, like `CandidateAlignment` -- there is no table linking
    a voter's interests to a candidate, so this is computed fresh per
    request and this model is not a `RowModel`.
    """

    model_config = ConfigDict(extra="forbid")

    candidacy_id: str = Field(description="The `candidacies` row this view is for.")
    person_id: str = Field(description="The `people` row the candidacy names.")

    groups: list[PromiseGroup] = Field(
        default_factory=list,
        description=(
            "One entry per voter interest with at least one matching "
            "promise. Ordered by `stance_id` for a deterministic response -- "
            "the same posture `AlignmentService` documents for its own "
            "ordering."
        ),
    )
    unmatched_count: int = Field(
        default=0,
        ge=0,
        description=(
            "How many of this challenger's promises are NOT about one of "
            "the voter's own interests -- either because they have not yet "
            "been classified to a stance (story 10.1's mapping), or because "
            "their stance is one the voter did not choose. Counted rather "
            "than hidden: a promise a voter did not ask to see is still a "
            "real promise, and the acceptance criterion is 'grouped by my "
            "interests', not 'only my interests are shown to exist'."
        ),
    )
    prior_offices: list[PriorOffice] = Field(
        default_factory=list,
        description=(
            "Elected offices this person held before this candidacy, from "
            "their own `people.terms[]` record. Empty, never fabricated, "
            "when none exists."
        ),
    )


__all__ = [
    "ChallengerPromises",
    "PriorOffice",
    "PromiseGroup",
    "PromiseItem",
]
