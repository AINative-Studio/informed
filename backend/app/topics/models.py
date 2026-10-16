"""Response shapes for `GET /v1/topics` (story 4.1).

`issue_topics` is stored flat (datamodel.md: `parent_id`, no nesting), because
a flat table is what `Repository.upsert` and the self-reference check in
`IssueTopic` work against. Every onboarding path, though, wants the tree: the
quick picker shows 20 top-level topics and drills into one's subtopics, the
advanced browser (story 4.7) walks the same tree with a search box over it.
Nesting it once, here, means neither path re-implements the walk.

Nothing in this module is a vote on which topics a voter should pick. The
order is whatever `IssueTopicService.tree()` reads back (alphabetical by name,
the only explainable order a tree with no stored rank can offer — see that
module for why there is no rank column to sort by instead), and no field here
carries a "popular" or "suggested" flag. prd.md: "the app may ask clarifying
questions but never suggests what a user's interests should be."
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TopicNode(BaseModel):
    """One subtopic: a leaf of the tree, and the level a voter's interest cites.

    `Interest.topic_id` (story 14.1's `app.profiles.models`) always names a
    node at this level — datamodel.md's stance cards hang off subtopics, never
    off a top-level topic (story 3.2's own rule, enforced by
    `StanceSeeder`) — so this model carries nothing a voter could pick *as* an
    interest and then find has no stances to show.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="`issue_topics` row id, e.g. en-US:prescription-drug-pricing:v1")
    name: str = Field(description="Voter-facing label, reviewed for neutral framing")


class TopicTree(BaseModel):
    """One top-level topic and the subtopics nested under it.

    `id` and `name` describe the top-level node itself; `subtopics` is where a
    voter's actual pick lives, because `issue_topics` carries no stance cards
    on a top-level row (see `TopicNode`). A client renders this as one
    expandable group — exactly what the quick onboarding picker and the
    advanced browser (story 4.7) both need, each at its own depth.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="`issue_topics` row id, e.g. en-US:healthcare:v1")
    name: str = Field(description="Voter-facing label, reviewed for neutral framing")
    subtopics: list[TopicNode] = Field(
        default_factory=list,
        description=(
            "This topic's subtopics, alphabetical by name — the taxonomy "
            "carries no rank to sort by, and an explainable order beats an "
            "arbitrary one that happens to be stable. Never implies a "
            "suggested or most-common choice."
        ),
    )


class TopicTreeResponse(BaseModel):
    """What `GET /v1/topics` returns: the whole tree, one locale."""

    model_config = ConfigDict(extra="forbid")

    locale: str = Field(description='The tree\'s locale, e.g. "en-US"')
    taxonomy_version: int = Field(description="The published taxonomy_version these rows carry")
    topics: list[TopicTree] = Field(
        description="Every top-level topic for this locale and version, alphabetical by name"
    )


__all__ = ["TopicNode", "TopicTree", "TopicTreeResponse"]
