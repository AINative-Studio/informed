"""Wire shapes for the public methodology endpoints (story 12.2).

Separate from `app.profiles.methodology.MethodologyVersion` (story 4.2) for
the same reason `app.stances.models` is separate from the stored `Stance`:
the stored row is an internal record and this is the published document. The
two differ in three deliberate ways.

1. **`id` becomes `version`.** Every other id in this codebase is an opaque
   natural key a client passes back; this one is a *methodology version
   number* that a voter reads, a journalist quotes and a verdict cites. Naming
   it `version` on the wire says that out loud.
2. **`path` is added.** prd.md requires "every verdict links to the
   methodology version that produced it", so the link is published alongside
   the version rather than string-built by each caller. See
   `app.methodology.router` for the honest scope note on what part of that
   requirement is demonstrable today.
3. **`weights_not_yet_published` is added.** datamodel.md documents three
   weight groups — `tiers`, `evidence_blend`, `vote_types` — and only `tiers`
   is in force. A published page that silently omitted the other two would let
   a reader think it was the whole methodology.

Nothing here can carry voter data, and `test_the_public_response_model_has_no
_voter_field` pins that: this is the API's first uncredentialed read, and
prd.md's privacy rule is absolute.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, computed_field

#: The weight groups published on `methodology_versions.weights`.
#:
#: The first three are datamodel.md's own: `tiers` (prd.md's Feature 1 table,
#: seeded by story 4.2) and `evidence_blend` / `vote_types` (prd.md's Feature 3
#: and Feature 2, given real numbers by story 6.1).
#:
#: `badge_thresholds` is story 6.5's addition, and it is an EXTENSION of
#: datamodel.md's documented three rather than one of them. The justification
#: is the same shape as story 6.1's: backlog.md 6.5's acceptance criterion is
#: "Badges are High, Medium, Low or Insufficient, with thresholds **from the
#: methodology**", and datamodel.md's rule that live values "come from
#: `methodology_versions` and are never hard-coded" applies to the ">=10
#: relevant votes" boundary prd.md publishes just as it applies to a tier
#: weight. The stored model deliberately keeps `weights` a free-form dict so a
#: story can add a group without editing a schema (see
#: `app.profiles.methodology.MethodologyVersion`), and naming it here is what
#: makes the new group appear on the public `/v1/methodology` page rather than
#: becoming a private side channel — including being reported in
#: `weights_not_yet_published` for the already-published `2026.1` row that
#: predates it. See `app.scoring.badges` for the numbers and their sourcing.
DOCUMENTED_WEIGHT_GROUPS: tuple[str, ...] = (
    "tiers",
    "evidence_blend",
    "vote_types",
    "badge_thresholds",
)

#: Where a version's public page lives. One definition, used by the router to
#: declare the route and by this model to publish the link, so the two cannot
#: drift — which is the entire value of publishing the link at all.
VERSION_PATH_TEMPLATE = "/v1/methodology/{version}"

#: datamodel.md's id pattern for `methodology_versions`: `2026.1` — a four
#: digit year, a dot, then a sequence number. Used to validate the path
#: parameter on the one endpoint that takes input from the open internet, and
#: to keep this story's router from reading ZeroDB on behalf of junk.
VERSION_ID_PATTERN = r"^\d{4}\.\d+$"


def version_path(version: str) -> str:
    """The canonical public path for one methodology version."""
    return VERSION_PATH_TEMPLATE.format(version=version)


class PublishedMethodology(BaseModel):
    """One methodology version, as published to anyone who asks.

    `extra="forbid"` is not decorative here. The stored row's `weights` is an
    open dict by design, and this model is what stands between "a future story
    added a field to the row" and "that field is now on a public,
    uncredentialed endpoint".
    """

    model_config = ConfigDict(extra="forbid")

    version: str = Field(
        description=(
            "The methodology version id, e.g. `2026.1` — what a verdict or "
            "score cites, and what `/v1/methodology/{version}` resolves."
        )
    )
    published_at: str = Field(description="When this methodology took effect, ISO-8601 UTC")
    weights: dict[str, dict[str, float]] = Field(
        description=(
            "The published weights, by group. `tiers` carries the interest "
            "tier weights a profile is scored with. Groups named in "
            "`weights_not_yet_published` are absent rather than zeroed."
        )
    )
    changelog_url: str = Field(
        description="Where the plain-language changelog for this version is published"
    )
    is_current: bool = Field(
        description=(
            "Whether this is the methodology in force. A superseded version "
            "stays readable at its own path so that a verdict computed under "
            "it can still show the weights it was actually computed with."
        )
    )
    path: str = Field(
        description=(
            "This version's canonical public path. Published with the version "
            "so a verdict citing it links one place rather than each caller "
            "rebuilding the URL."
        )
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def weights_not_yet_published(self) -> list[str]:
        """Documented weight groups this version does not (yet) carry.

        Computed from the row rather than hard-coded, so when epic 6 publishes
        an `evidence_blend` the page stops claiming it is missing without
        anyone editing this module. An empty group counts as absent: `{}` is
        not a published weighting.
        """
        return [group for group in DOCUMENTED_WEIGHT_GROUPS if not self.weights.get(group)]


class MethodologyVersionsResponse(BaseModel):
    """The changelog list: every published version, newest first.

    Newest first because this is read as a chronology — "what changed, and
    when" — and because the current version is the one most readers want.
    Ordered by `published_at`, never by the id as a string: `2026.10` sorts
    before `2026.9` lexicographically and after it chronologically.
    """

    model_config = ConfigDict(extra="forbid")

    versions: list[PublishedMethodology] = Field(
        description=(
            "Every published methodology version, newest first. Empty for a "
            "project that has published none — an honest answer about history "
            "rather than a row written as a side effect of a page load."
        )
    )


__all__ = [
    "DOCUMENTED_WEIGHT_GROUPS",
    "VERSION_ID_PATTERN",
    "VERSION_PATH_TEMPLATE",
    "MethodologyVersionsResponse",
    "PublishedMethodology",
    "version_path",
]
