"""Reading `issue_topics` back as a tree (story 4.1).

Why the whole table is read every call
=======================================

`issue_topics` is ~80 rows (story 3.1: 20 top-level, 60 subtopics) and is
permanent reference data that changes only when a panel publishes a new
`taxonomy_version` — never per request. One unfiltered page, grouped in
Python, is the same trade `StanceService._read_all` already makes for the
~190-row `stances` table and for the same reason: live ZeroDB's server-side
`/query` filter was observed to drop matching rows (story 1.4's finding,
datamodel.md "Open questions"), so a `filter={"locale": ...}` here would not
reliably narrow the page server-side, and reading everything and filtering
client-side is both correct and, at this size, cheap.

Why alphabetical, and not insertion order or a stored rank
============================================================

`issue_topics` has no rank or display-order column (datamodel.md's documented
fields are `parent_id, name, locale, taxonomy_version` — nothing else), and
for the same reason `stances` has none: a stored order is a ranking, and
`app.seed_taxonomy`'s own module docstring is explicit that no topic is to
read as more prominent than another. Returning rows in whatever order the
table happens to hand them back would be an *arbitrary but stable* order,
which in practice reads to a voter as "the first ones are the default ones" —
exactly what `app.stances.service` was written to avoid for stance cards.
Sorting by name is the one order this module can produce that is both stable
across calls (needed so "2 of 5 primary picked" style UI does not reshuffle
under a voter's finger) and visibly arbitrary-by-construction (alphabetical
order carries no implied endorsement).

This is a *difference* from `StanceService`, not an oversight: stance order is
randomized per request because the choice IS the opinion (prd.md: "presented
in randomized order"), so any stable order would bias it. A *topic* is not an
opinion — picking "Healthcare" over "Elections" is not a position on either —
so there is nothing for a stable, explainable order to bias. The hard
constraint in ui-prd.md 2.2 is about stance order, not topic order.
"""

from __future__ import annotations

import logging

from app.embed_taxonomy import TOPICS_NAMESPACE
from app.repositories import Repository
from app.repositories.models import IssueTopic
from app.topics.models import TopicNode, TopicSuggestion, TopicTree, TopicTreeResponse
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: One page big enough to hold the whole `issue_topics` table (~80 rows at
#: taxonomy_version 1), with headroom for a later version's rows to coexist
#: (datamodel.md: a new taxonomy_version writes new rows rather than mutating
#: these, so two versions' rows are live in the table at once during a
#: transition). Mirrors `app.stances.service.PAGE_SIZE`'s reasoning exactly.
PAGE_SIZE = 500

#: Story 3.5's acceptance criterion: "the top 3 suggestions appear".
SUGGESTION_LIMIT = 3


class TopicService:
    """Reads the issue taxonomy back as a locale's tree of topics.

    Stateless and safe to share across requests, like every other service in
    this codebase built on top of `Repository`.
    """

    def __init__(self, client: ZeroDBClient) -> None:
        self._client = client
        self._topics: Repository[IssueTopic] = Repository(client=client, model=IssueTopic)

    def tree(self, *, locale: str, taxonomy_version: int) -> TopicTreeResponse:
        """One locale and version's tree: top-level topics with their subtopics.

        A top-level topic with zero subtopics is included with an empty
        `subtopics` list rather than dropped — a gap in the taxonomy is a fact
        the picker should be able to show, not one this method should hide by
        pretending the topic does not exist.

        Returns an empty `topics` list for a locale or version with no stored
        rows, rather than raising: "nothing published yet for this locale" is
        a real, renderable answer for a market still being localized (prd.md's
        `JurisdictionAdapter` plan), not a server error.
        """
        rows = self._read_all(locale=locale, taxonomy_version=taxonomy_version)

        top_level = sorted((row for row in rows if row.parent_id is None), key=lambda row: row.name)
        subtopics_by_parent: dict[str, list[IssueTopic]] = {}
        for row in rows:
            if row.parent_id is not None:
                subtopics_by_parent.setdefault(row.parent_id, []).append(row)

        topics = [
            TopicTree(
                id=str(parent.id),
                name=parent.name,
                subtopics=[
                    TopicNode(id=str(child.id), name=child.name)
                    for child in sorted(
                        subtopics_by_parent.get(str(parent.id), []), key=lambda row: row.name
                    )
                ],
            )
            for parent in top_level
        ]
        return TopicTreeResponse(locale=locale, taxonomy_version=taxonomy_version, topics=topics)

    # --- free-text interest mapping (story 3.5) ----------------------------

    def suggest(self, *, text: str, locale: str) -> list[TopicSuggestion]:
        """Map free text to up to `SUGGESTION_LIMIT` candidate topics.

        READ-ONLY, deliberately: this calls `ZeroDBClient.semantic_search`
        against the `topics` namespace (story 3.4's embeddings) and nothing
        else. It writes no row, creates no vector, and does not keep `text`
        anywhere past this call — not in an attribute, not in a log line, not
        in a cache. See `app.topics.models.TopicSuggestRequest`'s docstring
        and this story's feature file for the scenarios that pin that.

        `text` is passed to `semantic_search` purely as a positional value;
        nothing in this method formats it into a log message. The one log
        line below names only the suggestion *count*, never the query.

        Results are filtered to `locale`, because a voter's own words are in
        one language and a cross-locale embedding match would be noise, not
        a suggestion. Within that locale, ZeroDB's own ranking order (highest
        similarity first) is kept — unlike `tree()`'s alphabetical order,
        rank here IS the useful signal: it is a measurement of textual
        closeness to the voter's own words, not an opinion this app is
        injecting about which topic matters more.

        **Confirmed live against the real "informed" project: the live
        search endpoint returns results already ranked (closest match
        first) but with NO numeric score in the response body at all** — see
        `TopicSuggestion.score`'s docstring. `hit.score` is passed through
        as-is (`None` live, a real float only against the in-memory fake),
        so this method never fabricates a number to fill the gap.

        Returns an empty list for a `topics` namespace with nothing embedded,
        or for a query with no hit above ZeroDB's own threshold — "no
        confident match" is a real, renderable answer (prd.md: suggestions
        are shown for confirmation, so showing none is honest when none fit),
        not a server error.
        """
        hits = self._client.semantic_search(
            query=text, namespace=TOPICS_NAMESPACE, limit=SUGGESTION_LIMIT * 4
        )

        by_id = {str(row.id): row for row in self._read_all_versions()}
        suggestions: list[TopicSuggestion] = []
        seen_topic_ids: set[str] = set()
        for hit in hits.results:
            topic_id = hit.metadata.get("topic_id")
            if not topic_id or topic_id in seen_topic_ids:
                continue
            topic = by_id.get(topic_id)
            if topic is None or topic.locale != locale:
                continue
            seen_topic_ids.add(topic_id)
            parent = by_id.get(topic.parent_id) if topic.parent_id else None
            suggestions.append(
                TopicSuggestion(
                    topic_id=topic_id,
                    name=topic.name,
                    is_subtopic=topic.parent_id is not None,
                    parent_id=topic.parent_id,
                    parent_name=parent.name if parent is not None else None,
                    score=hit.score,
                )
            )
            if len(suggestions) >= SUGGESTION_LIMIT:
                break

        logger.info("free-text topic suggestion: %d suggestion(s) returned", len(suggestions))
        return suggestions

    def _read_all_versions(self) -> list[IssueTopic]:
        """Every stored topic, any locale or taxonomy_version.

        Unlike `_read_all`, this is not filtered by version — a suggestion
        needs to resolve whichever `topic_id` the embedded vector's metadata
        names, and a subtopic's parent may be looked up regardless of which
        version is "current" for the tree endpoint.
        """
        rows = self._topics.query(limit=PAGE_SIZE)
        if len(rows) >= PAGE_SIZE:
            logger.warning(
                "the issue_topics read returned a full page of %d rows; the "
                "taxonomy may be larger than PAGE_SIZE now accounts for",
                len(rows),
            )
        return rows

    def _read_all(self, *, locale: str, taxonomy_version: int) -> list[IssueTopic]:
        """Every stored topic for one locale and version, filtered client-side.

        See the module docstring for why the whole table is read rather than a
        server-side filter trusted to narrow it.
        """
        rows = self._topics.query(limit=PAGE_SIZE)
        if len(rows) >= PAGE_SIZE:
            logger.warning(
                "the issue_topics read returned a full page of %d rows; the "
                "taxonomy may be larger than PAGE_SIZE now accounts for",
                len(rows),
            )
        return [
            row for row in rows if row.locale == locale and row.taxonomy_version == taxonomy_version
        ]


__all__ = ["PAGE_SIZE", "SUGGESTION_LIMIT", "TopicService"]
