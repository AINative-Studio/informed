"""Response models for `GET /v1/ballot` (stories 1.5, 2.4).

prd.md's Core API table describes this endpoint as returning "districts, races
and candidates for an address". Story 1.5 returned the first of those and the
officeholders sitting in them. Story 2.4 adds the other two — `races`, each
with its `candidates` and an explicit incumbent flag — because that is this
story's acceptance criterion and because ui-prd.md 2.3 lays the ballot view out
per race, so a client given a flat list would have to invent the grouping.

What a "candidate" honestly is right now
========================================

datamodel.md models a candidate as a `candidacies` row:

    candidacies | {election_id}:{district_id}:{person_id}
                | election_id, district_id, person_id, person_name, party,
                  is_incumbent, status

No `elections` and no `candidacies` rows exist yet — story 2.6 (iteration 2)
ingests them, and story 2.5's spike is still choosing the challenger source. So
every candidate this endpoint can name is somebody with a **current term** in
the seat, which is to say an incumbent. Three consequences, each deliberate:

  * `is_incumbent` is `True` for everyone returned today, and it is a *fact
    about the row* rather than a default: the service only ever puts a sitting
    officeholder in a race. When 2.6 adds challengers the flag starts
    discriminating without its meaning changing.
  * `data_state` says so out loud. A race carrying one candidate is otherwise
    indistinguishable from an uncontested race, and ui-prd.md 2.1 requires
    "no candidates found" to be explained rather than rendered as a silently
    short ballot. Telling the client "incumbents only" is the difference
    between an incomplete answer and a wrong one.
  * `Race.election_id` and `BallotCandidate.candidacy_id` exist and are
    **null**. They are the fields 2.6 fills; leaving them out would make 2.6 a
    reshape, and filling them with a plausible-looking id for a row that does
    not exist would be the kind of fabrication prd.md's evidence-over-opinion
    rule forbids — a later story would join against it and get nothing.

Why a race id is the seat and not the election
==============================================

`Race.id` is `{body_id}:{district_id}` — `us-house:ocd-division/country:us/
state:ca/cd:19`. The seat is the thing that exists today and is stable across
elections; a candidacy id cannot be built without an `election_id`. The pair is
also what keeps an at-large state correct: in WY, VT, DE, AK, ND and SD the
House and Senate divisions are the **same string**, so a race keyed on the
division alone would merge two chambers into one race and lose a seat — the
same collision story 2.3 fixed in the flat list, which this grouping could have
reintroduced.

What is deliberately absent, and must stay absent
=================================================

  * **The address.** Not a field, not an echo, not a `resolved_from`. prd.md's
    privacy rule and story 2.3's "the address is never stored" both apply, and
    the cheapest way to honour them is for the response type to have nowhere to
    put it.
  * **Anything that reads as a recommendation.** No `recommended`, no `best
    match`, no ordering that implies a preference — prd.md: the app never
    recommends who to vote for. Races and candidates come back in a stable,
    explainable order (chamber, then name), never a ranked one, and ui-prd.md
    2.3 makes sorting by score a user-initiated action rather than a default.
  * **An alignment score.** It is per-voter data, computed per request and
    never stored (CLAUDE.md), and it belongs to
    `GET /v1/candidates/{id}/alignment?profile_id=`. A ballot lookup takes no
    profile and must not look as though it could carry one.

Every field carries a description, enforced by a test against the generated
OpenAPI schema: this is the contract the React client (ui-prd.md 2.1 and 2.3)
is typed against, and `is_incumbent` in particular is a claim about a real
person that a client must not have to guess the meaning of.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DataState(StrEnum):
    """How complete the candidate list in this ballot is.

    An enum rather than a bool, because "is this ballot complete" has more than
    two honest answers over the life of the product and a client that switched
    on a bool would have to be rewritten for the third one.

    Attributes:
        INCUMBENTS_ONLY: every candidate listed is a sitting officeholder.
            Challengers are not loaded yet (story 2.6), so a race showing one
            candidate is not an uncontested race. This is the only value story
            2.4 can return.
        FULL: candidacies for an upcoming election have been loaded, so the
            races list everyone known to be on the ballot. Story 2.6 starts
            returning this; declared here so the client's handling of it is
            written once.
    """

    INCUMBENTS_ONLY = "incumbents_only"
    FULL = "full"


class DistrictSummary(BaseModel):
    """The district an address resolved to, as the voter sees it."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="OCD-ID, e.g. ocd-division/country:us/state:ca/cd:19")
    name: str = Field(
        description=(
            "Human-readable district name, as the geocoder labels it — "
            '"Congressional District 7", "Delegate District (at Large)"'
        )
    )
    state: str = Field(description="Two-letter state code, upper case")


class Officeholder(BaseModel):
    """One sitting member of Congress representing this voter.

    Kept alongside `races` rather than replaced by it: `models.py` promised
    story 1.5's callers that races would arrive as an *addition*, and the
    Promise vs. Record view (epic 7, ui-prd.md 2.5) is reached from an
    officeholder — somebody with a voting record — rather than from a candidacy,
    which a challenger also has. The two lists describe the same people today
    and will not once challengers land.

    `body_id` rather than a free-text chamber label: it is the `bodies` row id
    from datamodel.md, so the client groups by the same key the data is stored
    under and no string like "House" has to be kept in sync in two languages.
    """

    model_config = ConfigDict(extra="forbid")

    person_id: str = Field(description="`people` row id, e.g. person:B000944")
    name: str = Field(description="The member's canonical name")
    body_id: str = Field(description="`bodies` row id: us-house or us-senate")
    party: str | None = Field(
        default=None, description="Party as recorded on the member's current term, if known"
    )
    district_id: str | None = Field(
        default=None, description="The OCD division this term represents"
    )


class BallotCandidate(BaseModel):
    """One candidate in one race.

    Today this is always a sitting officeholder and `is_incumbent` is always
    true; see the module docstring for why that is a fact rather than a
    placeholder, and what story 2.6 changes.
    """

    model_config = ConfigDict(extra="forbid")

    person_id: str = Field(description="`people` row id, e.g. person:B000944")
    name: str = Field(description="The candidate's canonical name")
    party: str | None = Field(
        default=None, description="Party as recorded on the current term, if known"
    )
    is_incumbent: bool = Field(
        description=(
            "Whether this candidate currently holds the seat. True for every "
            "candidate while `data_state` is `incumbents_only`, because the "
            "only candidates Informed can name yet are sitting officeholders."
        )
    )
    candidacy_id: str | None = Field(
        default=None,
        description=(
            "`candidacies` row id, once elections and candidacies are loaded "
            "(story 2.6). Null today: no `candidacies` row exists, and an id "
            "for a row that does not exist would be a fabrication."
        ),
    )


class Race(BaseModel):
    """One contest on this voter's ballot: a seat and the candidates for it.

    A House district has one race; a state has one Senate race, which is listed
    whenever the state has sitting senators. DC and the five territories have no
    senators and therefore no Senate race — an empty one would claim a contest
    that does not exist.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description=(
            "Stable id for the seat: `{body_id}:{district_id}`. Names the seat "
            "rather than a candidacy, because no election is loaded yet — and "
            "includes the chamber because an at-large state's House and Senate "
            "divisions are the same OCD-ID."
        )
    )
    body_id: str = Field(description="`bodies` row id of the chamber: us-house or us-senate")
    office_name: str = Field(
        description='Office being contested, e.g. "U.S. House of Representatives"'
    )
    district_id: str = Field(
        description=(
            "OCD division the seat represents: the congressional district for a "
            "House race, the statewide division for a Senate race"
        )
    )
    district_name: str = Field(
        description="Human-readable name of that division, for display beside the office"
    )
    election_id: str | None = Field(
        default=None,
        description=(
            "`elections` row id for the contest, once elections are loaded "
            "(story 2.6). Null today: no `elections` row exists."
        ),
    )
    candidates: list[BallotCandidate] = Field(
        default_factory=list,
        description=(
            "Candidates for this seat, in a stable name order — never a ranked "
            "or recommended one. Empty is a legitimate answer for a seat whose "
            "holder has not been ingested."
        ),
    )


class BallotResponse(BaseModel):
    """What `GET /v1/ballot?address=` returns.

    Carries the voter's district, the races on their ballot, and the sitting
    officeholders — and never the address, a score, or anything that reads as a
    recommendation. See the module docstring.
    """

    model_config = ConfigDict(extra="forbid")

    district: DistrictSummary = Field(description="The district the address resolved to")
    data_state: DataState = Field(
        default=DataState.INCUMBENTS_ONLY,
        description=(
            "How complete the candidate lists are. `incumbents_only` means "
            "challengers are not loaded yet, so a race with one candidate is "
            "not an uncontested race — clients must say so rather than render a "
            "short ballot as a complete one."
        ),
    )
    races: list[Race] = Field(
        default_factory=list,
        description=(
            "The contests on this ballot, chamber order (House then Senate). "
            "Never ordered by any judgement about the candidates."
        ),
    )
    officeholders: list[Officeholder] = Field(
        default_factory=list,
        description=(
            "The sitting members who represent this voter, flat and across "
            "chambers. The same people as `races[].candidates` today; kept "
            "because the Promise vs. Record view starts from an officeholder."
        ),
    )


__all__ = [
    "BallotCandidate",
    "BallotResponse",
    "DataState",
    "DistrictSummary",
    "Officeholder",
    "Race",
]
