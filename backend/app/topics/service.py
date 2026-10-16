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

from app.repositories import Repository
from app.repositories.models import IssueTopic
from app.topics.models import TopicNode, TopicTree, TopicTreeResponse
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: One page big enough to hold the whole `issue_topics` table (~80 rows at
#: taxonomy_version 1), with headroom for a later version's rows to coexist
#: (datamodel.md: a new taxonomy_version writes new rows rather than mutating
#: these, so two versions' rows are live in the table at once during a
#: transition). Mirrors `app.stances.service.PAGE_SIZE`'s reasoning exactly.
PAGE_SIZE = 500


class TopicService:
    """Reads the issue taxonomy back as a locale's tree of topics.

    Stateless and safe to share across requests, like every other service in
    this codebase built on top of `Repository`.
    """

    def __init__(self, client: ZeroDBClient) -> None:
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


__all__ = ["PAGE_SIZE", "TopicService"]
