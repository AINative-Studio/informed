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


class TopicSuggestRequest(BaseModel):
    """The body of `POST /v1/topics/suggest` (story 3.5).

    `text` is the voter's own words. It is used ONLY to call ZeroDB's
    embedding/search API for this one request and is never written to any
    ZeroDB table, never logged, and is not an attribute this model's own
    instances are kept around to leak later — `TopicSuggestService.suggest`
    takes it as a plain function argument and this request model is not
    stored or echoed back in the response (see `TopicSuggestResponse`,
    which carries no `text` field). datamodel.md: "Voter free-text notes
    are embedded in memory only and never stored."
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        description=(
            "A free-text description of a voter's concern, in their own "
            "words, e.g. \"I'm worried about how much I pay for my "
            'medications every month". Never stored; used only to find '
            "the closest-matching topics by meaning."
        ),
    )
    locale: str = Field(
        default="en-US", min_length=2, description="Taxonomy locale to suggest topics from"
    )


class TopicSuggestion(BaseModel):
    """One candidate topic match, for the voter to confirm or reject.

    Deliberately named a "suggestion", never a recommendation: prd.md forbids
    the app from suggesting what a voter's interests should be, but mapping
    the voter's OWN stated words to the taxonomy's own names is a translation
    of their input, not an opinion about what they should care about — the
    same distinction `app.topics.service`'s docstring draws for topic order.
    `confirmed` is deliberately absent from this shape: per prd.md, a mapped
    topic is "shown back for confirmation (never silently)", so a client
    MUST render these as choices and let the voter act (typically by then
    calling `POST /v1/profiles` with the chosen `topic_id`) — nothing here is
    pre-selected or auto-applied.
    """

    model_config = ConfigDict(extra="forbid")

    topic_id: str = Field(
        description="`issue_topics` row id, e.g. en-US:prescription-drug-pricing:v1"
    )
    name: str = Field(description="Voter-facing label, reviewed for neutral framing")
    is_subtopic: bool = Field(
        description="Whether this suggestion is a subtopic (has a parent_id) or a top-level topic"
    )
    parent_id: str | None = Field(
        default=None, description="The parent topic's id, when this suggestion is a subtopic"
    )
    parent_name: str | None = Field(
        default=None, description="The parent topic's name, when this suggestion is a subtopic"
    )
    score: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Semantic similarity between the voter's free text and this "
            "topic's embedded wording, when ZeroDB's search reports one. "
            "Higher is closer. **Confirmed live against the real "
            '"informed" project during this story: the live semantic '
            "search endpoint's response carries no numeric score field at "
            "all** (`SearchHit.score` is always `null` there) even though "
            "results arrive correctly ranked server-side, closest match "
            "first — so this is `null` against live ZeroDB today and only "
            "a real number against the in-memory fake, which does report "
            "one. A client MUST treat this as optional, present-order "
            "information, never assume it is always populated. Shown, "
            "when present, so a client can signal confidence — never to "
            "imply ranking is this app's opinion about the topic."
        ),
    )


class TopicSuggestResponse(BaseModel):
    """What `POST /v1/topics/suggest` returns: up to 3 candidate topics.

    Carries no `text` field — the request's free text is not echoed back,
    which is one more place (alongside "never stored", "never logged") this
    story's privacy constraint is kept. `saved` is always `false`: this
    endpoint performs no write of any kind (story 3.5's acceptance
    criterion, "nothing is saved until the voter confirms"); saving a chosen
    suggestion is the existing `POST /v1/profiles` endpoint's job, reached by
    the voter picking one `suggestions[].topic_id` and using the normal
    onboarding save flow.
    """

    model_config = ConfigDict(extra="forbid")

    suggestions: list[TopicSuggestion] = Field(
        description=(
            "Up to 3 candidate topics, highest similarity first. May be fewer than 3, or empty."
        )
    )
    saved: bool = Field(
        default=False,
        description=(
            "Always false. This endpoint never writes anything — it only "
            "suggests. Confirm a suggestion by saving it through "
            "POST /v1/profiles with the chosen topic_id."
        ),
    )
    confirmation_required: bool = Field(
        default=True,
        description=(
            "Always true. A client must show these suggestions back to the "
            "voter and let them confirm (or reject) before any is used — "
            "prd.md: mapped topics are shown back for confirmation, never "
            "applied silently."
        ),
    )


__all__ = [
    "TopicNode",
    "TopicSuggestRequest",
    "TopicSuggestResponse",
    "TopicSuggestion",
    "TopicTree",
    "TopicTreeResponse",
]
