"""Response models for `GET /v1/ballot` (story 1.5).

prd.md's Core API table describes this endpoint as returning "districts, races
and candidates for an address". Story 1.5 returns the first of those three and
the officeholders sitting in them; `races` and `candidates` need `elections` and
`candidacies` rows, which epic 2 ingests. The field names here are chosen so
those two arrive as *additions* to this payload rather than a reshape of it.

What is deliberately absent, and must stay absent:

  * **The address.** Not a field, not an echo, not a `resolved_from`. prd.md's
    privacy rule and story 2.3's "the address is never stored" both apply, and
    the cheapest way to honour them is for the response type to have nowhere to
    put it.
  * **Anything that reads as a recommendation.** No `recommended`, no `best
    match`, no ordering that implies a preference — prd.md: the app never
    recommends who to vote for. Officeholders come back in a stable,
    explainable order (chamber, then name), not a ranked one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DistrictSummary(BaseModel):
    """The district an address resolved to, as the voter sees it."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="OCD-ID, e.g. ocd-division/country:us/state:ca/cd:19")
    name: str = Field(description="Human-readable district name")
    state: str = Field(description="Two-letter state code")


class Officeholder(BaseModel):
    """One sitting member of Congress representing this voter.

    `body_id` rather than a free-text chamber label: it is the `bodies` row id
    from datamodel.md, so the client groups by the same key the data is stored
    under and no string like "House" has to be kept in sync in two languages.
    """

    model_config = ConfigDict(extra="forbid")

    person_id: str = Field(description="`people` row id, e.g. person:B000944")
    name: str
    body_id: str = Field(description="`bodies` row id: us-house or us-senate")
    party: str | None = None
    district_id: str | None = Field(default=None, description="The division this term represents")


class BallotResponse(BaseModel):
    """What `GET /v1/ballot?address=` returns.

    Epic 2 adds `races: list[Race]` alongside `officeholders`; the officeholders
    list stays, because the Promise vs. Record view (epic 7) is reached from the
    incumbent, which is an officeholder rather than a candidacy.
    """

    model_config = ConfigDict(extra="forbid")

    district: DistrictSummary
    officeholders: list[Officeholder] = Field(default_factory=list)


__all__ = ["BallotResponse", "DistrictSummary", "Officeholder"]
