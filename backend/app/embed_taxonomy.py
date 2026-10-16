"""Embed topics and stances into their vector namespaces (story 3.4).

What this is
============

Story 3.1 seeded ~80 `issue_topics` rows and story 3.2 seeded 201 `stances`
rows as NoSQL reference data. Neither is searchable by meaning until it also
exists as a vector: free-text interest matching (story 3.5) needs every topic
embedded in the `topics` namespace, and classifying a promise's or vote's
direction (epic 5/6) needs every stance embedded in the `stances` namespace.
This module is the pipeline that reads the live NoSQL rows and writes the
corresponding vectors, via `ZeroDBClient.embed_and_store`.

It is deliberately **read-only against `issue_topics` and `stances`** — it
never writes a NoSQL row, only vectors — and it is a separate command from
`scripts/seed_taxonomy.py` / `scripts/seed_stances.py`, run after both, because
every topic and stance it embeds must already be stored (there is nothing to
embed otherwise).

What gets embedded, and why (datamodel.md's "Document text" column)
=====================================================================

datamodel.md documents the `topics` namespace's text as "Topic name +
definition, per locale". **`IssueTopic` (story 3.1) has no `definition` field
separate from `name`** — the model was built with `name` as the only wording a
voter reads (see `app.repositories.models.IssueTopic` and
`app.seed_taxonomy.Topic`). There is no hidden definition text to concatenate.

Given that, embedding just `name` would lose a subtopic's context ("Carry
permits and state preemption" reads differently once you know it sits under
"Firearms policy" rather than, say, "Civil liberties and discrimination law"),
so this module embeds **`"{topic name} — {parent name}"` for a subtopic, and
just `"{topic name}"` for a top-level topic** (which has no parent to add).
That is the most honest stand-in for "name + definition" available from the
data that actually exists: it is still just the taxonomy's own wording, no
invented definition text, and it gives the embedding the same disambiguating
context a voter reading the UI would have (the subtopic is always shown under
its parent). If a later story adds a real `definition` field to `IssueTopic`,
this is the one place that needs to change.

For `stances`, datamodel.md says "Stance card wording", and `Stance` (story
3.2) has exactly two wording fields: `label` and `description`. Both are
embedded, joined as `"{label}. {description}"` — the label is the short
version a voter scans, the description is what disambiguates it from a
neighboring card on the same subtopic, and a classifier matching free promise
or roll-call text benefits from both.

Idempotency and rebuilds (the acceptance criterion)
====================================================

"Embeddings are rebuilt when a version changes" is this module's whole second
job, and it rests on two live-API facts confirmed during this story (see
`ZeroDBClient.list_vectors` and `delete_vector`, added here):

  * **There is no idempotent upsert for vectors.** `embed_and_store` always
    creates a new vector; calling it twice for the same topic with the same
    text would store the text twice. So this module tracks what is already
    embedded *itself*, by reading each namespace's existing vectors and their
    metadata, the only record ZeroDB keeps.
  * **Vectors cannot be updated in place.** There is no PATCH/PUT for a
    vector's embedding; a vector is created or deleted, never edited. So "never
    patched in place" (datamodel.md) is not a style choice here, it is the only
    thing the live API offers — a version bump is delete-then-insert.

The algorithm, per namespace, per row:

  1. List the namespace's existing vectors (paged) and index them by the id
     in their metadata (`topic_id` or `stance_id`) and by the version field
     stored alongside it (`taxonomy_version` or `wording_version`).
  2. For each live NoSQL row:
     - no existing vector for that id -> embed and store (a genuine first
       write);
     - an existing vector whose stored version equals the row's current
       version -> skip; re-embedding unchanged wording is pure waste against
       a project throttled at 60 req/min (datamodel.md "Open questions");
     - an existing vector whose stored version differs -> delete the stale
       vector(s) for that id, then embed and store the current wording. This
       is the rebuild path; it is never an update, because there is nothing to
       update.
  3. A stray vector whose metadata names an id that no longer exists in
     `issue_topics`/`stances` is left alone. Detecting and removing an
     orphaned embedding would need a full reverse scan of the NoSQL table for
     every vector, against a project this module cannot assume is small
     forever, and nothing in story 3.4's acceptance criteria asks for it. It is
     a known gap, flagged rather than silently handled.

One more live wrinkle this module works around: story 1.3's bootstrap left one
"schema bootstrap marker" vector in each of the four namespaces (metadata
`{"bootstrap_marker": true, ...}`), confirmed present in both `topics` and
`stances` during this story's live verification. That marker carries no
`topic_id`/`stance_id`, so the indexing step above simply never matches it to
a row — it is neither skipped nor rebuilt nor deleted, just ignored, which is
the right outcome for a marker this module did not create and has no mandate
to remove.

Why it lives beside `seed_taxonomy.py` and `seed_stances.py`, as its own CLI
=============================================================================

Same reasoning as those two modules' own docstrings: this is a separate
operational step with its own cost profile (up to ~80 + 201 embed calls, each
possibly preceded by a list-and-maybe-delete), run after both seeds, and not
worth folding into `Bootstrapper.run()` or either seed's own CLI. It gets its
own: `python scripts/embed_taxonomy.py`.

Permanence
==========

Same rule as every other seed in this file's family: the vectors this module
writes against the live "informed" project are production reference data, not
test debris, and `scripts/verify_embed_taxonomy_live.py` does not delete them
after checking they exist.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.bootstrap import default_pacer
from app.repositories import Repository
from app.repositories.models import IssueTopic, Stance
from app.zerodb.client import ZeroDBClient
from app.zerodb.models import VectorRecord

logger = logging.getLogger(__name__)

#: The two namespaces this module owns. datamodel.md's "Vector namespaces"
#: table names both; nothing else writes into either.
TOPICS_NAMESPACE = "topics"
STANCES_NAMESPACE = "stances"

#: A page of `query`/`list_vectors` reads at once. `stances` runs to 201 rows,
#: comfortably past the repository's 100-row default, so every read in this
#: module pages explicitly rather than assuming one page is everything.
PAGE_SIZE = 200

#: Vectors are read and written one at a time rather than in one batch call,
#: paced the same way `TaxonomySeeder`/`StanceSeeder` pace their upserts,
#: because the live project is observed to throttle at 60 requests per minute
#: (datamodel.md "Open questions") and this module's live listing step adds
#: reads on top of the seeds' writes.


@dataclass(frozen=True)
class _ExistingVector:
    """One namespace's existing vector for a topic/stance id, as found live."""

    vector_id: str
    version: int | None


@dataclass
class EmbedReport:
    """What one embedding run did, split by namespace and by action.

    `written` holds every id that now has a current embedding, whether that
    meant a fresh insert or a version-triggered rebuild; `rebuilt` is the
    subset of `written` where a stale vector was deleted first; `skipped` is
    every id whose stored embedding was already current.

    All three lists are ids (`topic_id`/`stance_id`), not vector ids — the
    CLI's summary and the live verifier both want to report on taxonomy/stance
    ids, which are the ones a human recognizes.
    """

    written: list[str] = field(default_factory=list)
    rebuilt: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.written) + len(self.skipped)


def topic_embedding_text(topic: IssueTopic, *, parent: IssueTopic | None) -> str:
    """The text embedded for one topic row.

    `"{name} — {parent name}"` for a subtopic, bare `name` for a top-level
    topic. See the module docstring for why: `IssueTopic` has no `definition`
    field distinct from `name`, so the parent's name is the honest stand-in for
    the disambiguating context datamodel.md's "definition" implies.
    """
    if parent is None:
        return topic.name
    return f"{topic.name} — {parent.name}"


def stance_embedding_text(stance: Stance) -> str:
    """The text embedded for one stance row: `"{label}. {description}"`.

    Both of `Stance`'s wording fields (story 3.2), joined with a period so the
    label reads as a sentence before the description's detail follows it.
    """
    label = stance.label.rstrip(".")
    return f"{label}. {stance.description}"


def _version_from_metadata(metadata: dict[str, Any], key: str) -> int | None:
    """Coerce a stored metadata version field to int, or None if absent/bad.

    Metadata survives a round trip through JSON, so an int written by this
    module always comes back as an int — this tolerates a float or string from
    some other writer rather than crashing the rebuild check on it.
    """
    value = metadata.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _index_existing_vectors(
    records: list[VectorRecord], *, id_key: str, version_key: str
) -> dict[str, list[_ExistingVector]]:
    """Group a namespace's vectors by the row id in their metadata.

    A list per id, not a single entry, because a bug or a race could in
    principle leave more than one vector for the same id — the rebuild step
    deletes every entry found for an id whose version does not match, not just
    the first. The bootstrap marker vector (no `id_key` in its metadata) is
    silently excluded, exactly as the module docstring describes.
    """
    index: dict[str, list[_ExistingVector]] = {}
    for record in records:
        row_id = record.vector_metadata.get(id_key)
        if not row_id:
            continue
        version = _version_from_metadata(record.vector_metadata, version_key)
        index.setdefault(row_id, []).append(
            _ExistingVector(vector_id=record.vector_id, version=version)
        )
    return index


class TaxonomyEmbedder:
    """Embeds `issue_topics` and `stances` rows, rebuilding on a version change.

    Reads the live NoSQL rows through `Repository` and the live vector state
    through `ZeroDBClient.list_vectors`, so idempotency is checked against
    what is actually stored rather than assumed from a prior run's log. See the
    module docstring for the full algorithm.

    `pace` defaults to the real pacer for the same reason
    `TaxonomySeeder`/`StanceSeeder` do: this module's only production caller is
    a CLI with nothing above it to supply one, and it runs enough live calls
    (a listing pass plus up to ~280 embed/delete calls) to matter against the
    60 req/min ceiling. Tests pass `pace=lambda: None`.
    """

    def __init__(self, client: ZeroDBClient, *, pace: Callable[[], None] | None = None) -> None:
        self._client = client
        self._pace = pace if pace is not None else default_pacer()
        self._topics: Repository[IssueTopic] = Repository(client=client, model=IssueTopic)
        self._stances: Repository[Stance] = Repository(client=client, model=Stance)

    # --- the whole job --------------------------------------------------

    def run(self) -> tuple[EmbedReport, EmbedReport]:
        """Embed every live topic, then every live stance.

        Returns:
            `(topics_report, stances_report)`.
        """
        topics_report = self.embed_topics()
        stances_report = self.embed_stances()
        return topics_report, stances_report

    # --- topics -----------------------------------------------------------

    def embed_topics(self) -> EmbedReport:
        """Embed every `issue_topics` row into the `topics` namespace."""
        topics = self._all_topics()
        by_id = {topic.id: topic for topic in topics}
        existing = self._existing(
            TOPICS_NAMESPACE, id_key="topic_id", version_key="taxonomy_version"
        )

        report = EmbedReport()
        for topic in topics:
            assert topic.id is not None  # noqa: S101 - stored rows always carry an id
            topic_id = str(topic.id)
            parent = by_id.get(topic.parent_id) if topic.parent_id else None
            text = topic_embedding_text(topic, parent=parent)
            metadata = {
                "topic_id": topic_id,
                "locale": topic.locale,
                "taxonomy_version": topic.taxonomy_version,
            }
            self._embed_one(
                row_id=topic_id,
                current_version=topic.taxonomy_version,
                existing=existing.get(topic_id, []),
                namespace=TOPICS_NAMESPACE,
                text=text,
                metadata=metadata,
                report=report,
            )
        logger.info(
            "topic embeddings: %d written (%d rebuilt), %d already current",
            len(report.written),
            len(report.rebuilt),
            len(report.skipped),
        )
        return report

    # --- stances ------------------------------------------------------------

    def embed_stances(self) -> EmbedReport:
        """Embed every `stances` row into the `stances` namespace."""
        stances = self._all_stances()
        existing = self._existing(
            STANCES_NAMESPACE, id_key="stance_id", version_key="wording_version"
        )

        report = EmbedReport()
        for stance in stances:
            assert stance.id is not None  # noqa: S101 - stored rows always carry an id
            stance_id = str(stance.id)
            text = stance_embedding_text(stance)
            metadata = {
                "stance_id": stance_id,
                "topic_id": stance.topic_id,
                "wording_version": stance.wording_version,
            }
            self._embed_one(
                row_id=stance_id,
                current_version=stance.wording_version,
                existing=existing.get(stance_id, []),
                namespace=STANCES_NAMESPACE,
                text=text,
                metadata=metadata,
                report=report,
            )
        logger.info(
            "stance embeddings: %d written (%d rebuilt), %d already current",
            len(report.written),
            len(report.rebuilt),
            len(report.skipped),
        )
        return report

    # --- shared ------------------------------------------------------------

    def _embed_one(
        self,
        *,
        row_id: str,
        current_version: int,
        existing: list[_ExistingVector],
        namespace: str,
        text: str,
        metadata: dict[str, Any],
        report: EmbedReport,
    ) -> None:
        """Skip, rebuild or freshly embed one row, per the module's algorithm."""
        is_current = any(entry.version == current_version for entry in existing)
        if is_current:
            report.skipped.append(row_id)
            return

        if existing:
            for entry in existing:
                self._pace()
                self._client.delete_vector(vector_id=entry.vector_id, namespace=namespace)
            report.rebuilt.append(row_id)

        self._pace()
        self._client.embed_and_store(texts=[text], namespace=namespace, metadata_list=[metadata])
        report.written.append(row_id)

    def _existing(
        self, namespace: str, *, id_key: str, version_key: str
    ) -> dict[str, list[_ExistingVector]]:
        """Every vector currently stored in `namespace`, indexed by row id."""
        hits = self._list_all_vectors(namespace)
        return _index_existing_vectors(hits, id_key=id_key, version_key=version_key)

    def _list_all_vectors(self, namespace: str) -> list[VectorRecord]:
        """Page through every vector in a namespace.

        `ZeroDBClient.list_vectors` answers a page at a time (`PAGE_SIZE` per
        call), exactly like `query_rows`; this walks every page rather than
        assuming the first one is everything, which matters once a namespace
        holds more than `PAGE_SIZE` vectors.
        """
        records: list[VectorRecord] = []
        offset = 0
        while True:
            self._pace()
            page = self._client.list_vectors(namespace=namespace, limit=PAGE_SIZE, offset=offset)
            records.extend(page.vectors)
            if not page.vectors or len(page.vectors) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return records

    def _all_topics(self) -> list[IssueTopic]:
        """Every `issue_topics` row, paged past the repository's default page."""
        return self._all_rows(self._topics)

    def _all_stances(self) -> list[Stance]:
        """Every `stances` row, paged past the repository's default page."""
        return self._all_rows(self._stances)

    def _all_rows(self, repository: Repository[Any]) -> list[Any]:
        rows: list[Any] = []
        skip = 0
        while True:
            self._pace()
            page = repository.query(limit=PAGE_SIZE, skip=skip)
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
        return rows


__all__ = [
    "EmbedReport",
    "PAGE_SIZE",
    "STANCES_NAMESPACE",
    "TOPICS_NAMESPACE",
    "TaxonomyEmbedder",
    "stance_embedding_text",
    "topic_embedding_text",
]
