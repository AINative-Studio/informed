"""Embed each promise, and announce it (stories 9.4 and 9.5).

What this is, and why the two stories are one module
=====================================================

Story 9.3 turned archived source text into `promises` rows and left two
fields explicitly for later: `vector_id` ("story 9.4 embeds and fills this")
and no event at all. This module closes both.

  * **Story 9.4** — "As the system, I want each promise embedded, so that it
    can be matched to bills." Acceptance: *every promise has a `vector_id`;
    its metadata includes promise_id and person_id.*
  * **Story 9.5** — "As the mapping agent, I want a `promise.extracted`
    event, so that mapping starts automatically." Acceptance: *emitted once
    per promise.*

They are one module and one feature file because **9.5's event is the
completion signal of 9.4's embedding step, not an independent write**. A
`promise.extracted` event tells story 10.1's mapping agent that a promise is
ready to be matched to bills; a promise is not ready to be matched until it
has a vector to be matched WITH, because 10.1's whole mechanism is a semantic
search of this namespace (datamodel.md: "Mapping agent: promise → candidate
bills"). Emitting first would wake a consumer that then finds nothing
searchable, so the order is embed-then-announce and the two are verified as
one transition.

What is embedded, and what is NOT
==================================

**The document is the promise's own `quote_span`, verbatim, and nothing
else.** datamodel.md's `promises` namespace document text is "Atomic
commitment text", and the quote span is that text — it is the only wording in
the row the source actually said.

Deliberately NOT embedded alongside it: `actor`, `action` and `object`.
Those are the *extractor's reading* of the sentence, not the source's words.
Appending them would make a bill match a promise partly because of wording
nobody said — and with story 9.3's stand-in extractor still wired, partly
because of regex output. The quote is the citation prd.md's
evidence-over-opinion rule rests on; the embedding is built from the citation.

The metadata contract
=====================

datamodel.md's `promises` namespace metadata column names exactly five
fields: `promise_id, person_id, topic_id, cycle, testable`. All five are
written, and two deserve a note:

  * **`topic_id` is present and null**, not absent. Story 10.1 classifies a
    promise's topic; until then every row's is None. The KEY still has to be
    in the metadata, because a `filter_metadata={"topic_id": …}` search
    against vectors that never declared the field cannot match it — a gap
    that would only surface once 10.1 shipped and would look like a mapping
    bug rather than an embedding one.
  * **`cycle` is derived, because no model carries it.** `Election` has
    `election_date` and its id embeds the same date (`us-2026-11-03-general`);
    `Candidacy` and `Promise` have no cycle field at all. `cycle_for_election_id`
    reads the four-digit year out of the candidacy's `election_id`, which
    needs no second read. When it cannot find one it returns **None rather
    than a guess**: a wrong cycle filters the mapping agent to the wrong
    election's bills, which is worse than no filter.

`testable` is in the metadata because story 10.1 maps only testable promises
(prd.md shows vague rhetoric to the voter but never scores it), and a
metadata filter is how it excludes the rest without re-reading every row.
**Non-testable promises are still embedded** — story 9.6's challenger view
and the Q&A agent both search promise text, and a promise with no checkable
object still has words a voter may ask about.

Idempotency, and the two live facts it is built on
===================================================

Same two constraints story 3.4's `app.embed_taxonomy` documented, re-confirmed
live against the real "informed" project during this story (2026-10-20):

  * **`embed_and_store` returns no vector ids.** The live response is
    `{success, vectors_stored, embeddings_generated, model: "bge-m3",
    dimensions: 1024, target_column, namespace, project_id,
    processing_time_ms}` — measured, not inferred. So the `vector_id` story
    9.4's acceptance criterion requires **cannot be read off the write**; it
    has to be recovered by listing the namespace afterwards and matching on
    the metadata this module itself wrote. That is why `_embed_one` does a
    read after every write, and why a promise whose id cannot be found that
    way is reported as a FAILURE rather than as embedded.
  * **There is no vector upsert and no in-place update.** A vector is created
    or deleted, never edited. So re-embedding is delete-then-insert, and
    "don't embed twice" has to be decided by this module from what is
    actually stored.

The staleness check is **the stored document versus the row's current
`quote_span`**, not the row's `vector_id` being non-null. The difference
matters: story 9.3's ids are content-addressed over
`(source_id, quote_span, actor, action, object, direction)`, so a *changed
quote* produces a *new row*, but a re-extraction that revises only
`confidence` or `testable` updates the SAME row in place — and a better
extractor trimming a quote span to the exact committing words would leave a
row whose `vector_id` points at a vector of the old, longer text. Checking
"does the stored vector's document still equal this row's quote_span" catches
that; checking "does the row have a vector_id" does not.

Never reporting what did not happen
====================================

Both of this module's reports are earned, not assumed — the lesson PR #166
applied to `rollcall.ingested` in story 5.7, applied here before it could
become the same bug:

  * a promise is in `report.embedded` only once its vector has been stored
    **and** its id read back **and** written onto the row;
  * a promise is in `report.events_emitted` only once `create_event` has
    actually returned;
  * anything that fails lands in `report.failures` with the reason.

A failure does not abort the run: the remaining promises are independent, and
one unavailable route should not leave a source half-embedded with no record
of which half. The run returns a report a caller can act on, and a re-run
mends whatever failed, because every step is idempotent.

**The cost of a lost event, stated plainly.** The event is emitted only when
the promise is newly embedded, so a publish outage means that promise's event
is never retried: a later run finds its vector current, skips it, and emits
nothing. The promise is stored, embedded and searchable — the mapping it
should have triggered just does not happen until something calls story 10.1
directly. Same accepted trade, and same missing durable outbox, as
`HouseRollCallLoader._emit_ingested` documents; `report.failures` is what
makes it visible rather than silent.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from app.repositories import Repository
from app.repositories.models import Candidacy, Promise, PromiseSource
from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBError
from app.zerodb.models import VectorRecord

logger = logging.getLogger(__name__)

#: datamodel.md's "Vector namespaces" table names this one; nothing else
#: writes into it. Story 1.3's bootstrap left one marker vector in it
#: (metadata `{"bootstrap_marker": true, …}`), which carries no `promise_id`
#: and so is never matched to a row, skipped, rebuilt or deleted here —
#: exactly the posture `app.embed_taxonomy` takes to the same marker.
PROMISES_NAMESPACE = "promises"

#: datamodel.md's Events table: `promise.extracted`, payload
#: `promise_id, person_id`, consumers "Mapping agent; QA queue".
PROMISE_EXTRACTED_TOPIC = "promise.extracted"

#: A page of `list_vectors`/`query` read at once. Larger than the
#: repository's 100-row default because one real campaign site yields tens of
#: promises and a namespace accumulates every one of them across every
#: candidate — the same reason `app.embed_taxonomy` pages at 200.
PAGE_SIZE = 200

#: Mirrors every other loader's pacing constant against the ~60 req/min
#: ZeroDB ceiling datamodel.md records. This module makes up to three calls
#: per promise (embed, list, row update) plus one event, so it reaches that
#: ceiling faster than most.
DEFAULT_MIN_INTERVAL_SECONDS = 1.1

#: A four-digit year in an election id (`us-2026-11-03-general`). Anchored to
#: a `-` on both sides so a stray number elsewhere in an id cannot be read as
#: a cycle. `19|20` rather than `\d{4}` so `us-0000-…` and `us-99-…` have no
#: cycle rather than a nonsensical one — see `cycle_for_election_id`.
_CYCLE_IN_ELECTION_ID = re.compile(r"-((?:19|20)\d{2})-")


class PromiseEmbeddingError(Exception):
    """Embedding could not be attempted for a source."""


@dataclass
class PromiseEmbeddingReport:
    """What one embedding run actually did, per promise.

    Every list holds `promises.id` values, never vector ids: a caller's
    summary, the live verifier and a human reading a log all want the id they
    recognise.

    `rebuilt` is a SUBSET of `embedded` rather than a separate bucket, so
    `embedded` answers "which promises have a current vector because of this
    run" without a caller having to add two lists together.

    `failures` is why this report exists at all. See the module docstring:
    nothing lands in `embedded` or `events_emitted` unless it really
    happened, so a run with a broken route reports the truth rather than an
    optimistic count.
    """

    source_id: str
    embedded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    rebuilt: list[str] = field(default_factory=list)
    events_emitted: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def promise_count(self) -> int:
        """How many promise rows this run accounted for, embedded or skipped."""
        return len(self.embedded) + len(self.skipped)


def promise_embedding_text(promise: Promise) -> str:
    """The text embedded for one promise: its own verbatim quote span.

    Not a summary, and deliberately not the extracted parts rejoined — see
    the module docstring, "What is embedded, and what is NOT".
    """
    return promise.quote_span


def cycle_for_election_id(election_id: str) -> int | None:
    """datamodel.md's `cycle` metadata field, read out of the election id.

    `us-2026-11-03-general` -> 2026. None when the id carries no plausible
    four-digit year, because a wrong cycle filters story 10.1's mapping agent
    to the wrong election's bills — worse than no filter at all. See the
    module docstring.
    """
    match = _CYCLE_IN_ELECTION_ID.search(election_id or "")
    return int(match.group(1)) if match else None


def promise_vector_metadata(promise: Promise, *, cycle: int | None) -> dict[str, object]:
    """The exact five-field metadata datamodel.md's `promises` namespace names.

    `promise_id, person_id, topic_id, cycle, testable` — no more (the
    document already holds the quote; duplicating it, or adding the
    extractor's actor/action/object reading, would make an interpretation
    searchable as if it were the source's words) and no fewer (`topic_id` is
    present-and-null rather than omitted; see the module docstring).
    """
    return {
        "promise_id": promise.id,
        "person_id": promise.person_id,
        "topic_id": promise.topic_id,
        "cycle": cycle,
        "testable": promise.testable,
    }


class PromiseEmbedder:
    """Embeds `promises` rows and emits `promise.extracted` for each.

    Reads the live rows through `Repository` and the live vector state through
    `ZeroDBClient.list_vectors`, so idempotency is decided from what is
    actually stored rather than from a prior run's log — the same posture
    `app.embed_taxonomy.TaxonomyEmbedder` takes, for the same two live
    reasons (no vector upsert, no in-place vector update).

    `pace` defaults to None (no pacing) and the CLI supplies the real pacer,
    matching `PromiseExtractionPipeline` rather than `TaxonomyEmbedder`: this
    class's callers are a CLI and a verification script that both configure
    their own, and tests pass `pace=lambda: None`.
    """

    def __init__(self, client: ZeroDBClient, *, pace: Callable[[], None] | None = None) -> None:
        self._client = client
        self._pace = pace if pace is not None else (lambda: None)
        self._promises: Repository[Promise] = Repository(client=client, model=Promise)
        self._sources: Repository[PromiseSource] = Repository(client=client, model=PromiseSource)
        self._candidacies: Repository[Candidacy] = Repository(client=client, model=Candidacy)

    def embed_promises_for_source(self, source_id: str) -> PromiseEmbeddingReport:
        """Embed every promise extracted from one source, and announce each.

        Scoped to one source because that is the unit story 9.3's extraction
        runs in, so a caller can embed what it just extracted without
        re-walking the whole table.

        Raises:
            PromiseEmbeddingError: `source_id` does not resolve to a
                `promise_sources` row. A promise's citability flows through
                that row, and embedding against a source that is not on file
                would make a vector resolvable to an uncitable promise.
        """
        source = self._sources.get(source_id)
        if source is None:
            raise PromiseEmbeddingError(
                f"no promise_sources row {source_id!r}; a promise's citability flows "
                "through its source row, so there is nothing to embed against"
            )

        report = PromiseEmbeddingReport(source_id=source_id)
        promises = self._promises_for_source(source_id)
        if not promises:
            logger.info("promise_embedding: no promises extracted from %s yet", source_id)
            return report

        cycle = self._cycle_for(source)
        try:
            existing = self._existing_vectors()
        except ZeroDBError as exc:
            # The namespace could not be read, so this run cannot tell which
            # promises are already embedded. Refusing to proceed is the right
            # answer rather than embedding blindly: without the listing,
            # every promise would look unembedded and the run would store a
            # SECOND vector for each one -- a duplicate the mapping agent
            # matches twice. Reported, not raised, so the caller sees which
            # source failed and why, and a re-run mends it.
            report.failures.append(
                f"the {PROMISES_NAMESPACE} namespace could not be read, so this run "
                f"cannot tell which promises are already embedded: {exc}"
            )
            logger.error(
                "promise_embedding: could not list the %s namespace for %s; embedding "
                "nothing rather than risking a duplicate vector per promise: %s",
                PROMISES_NAMESPACE,
                source_id,
                exc,
            )
            return report

        for promise in promises:
            assert promise.id is not None  # noqa: S101 - stored rows always carry an id
            self._embed_one(
                promise, cycle=cycle, existing=existing.get(promise.id, []), report=report
            )

        logger.info(
            "promise_embedding: %s -> %d embedded (%d rebuilt), %d already current, "
            "%d event(s) emitted, %d failure(s)",
            source_id,
            len(report.embedded),
            len(report.rebuilt),
            len(report.skipped),
            len(report.events_emitted),
            len(report.failures),
        )
        return report

    # --- one promise ------------------------------------------------------

    def _embed_one(
        self,
        promise: Promise,
        *,
        cycle: int | None,
        existing: list[VectorRecord],
        report: PromiseEmbeddingReport,
    ) -> None:
        """Skip, rebuild or freshly embed one promise, then announce it.

        The staleness test is the stored document against the row's current
        `quote_span`, not the row's `vector_id` being set — see the module
        docstring for the re-extraction case that distinction catches.
        """
        assert promise.id is not None  # noqa: S101 - caller checked
        text = promise_embedding_text(promise)
        current = [record for record in existing if record.document == text]

        if current and promise.vector_id == current[0].vector_id:
            # Stored, current, and the row already points at it. Nothing to
            # do, and -- per story 9.5's "once per promise" -- nothing to
            # announce: this promise was announced when it was embedded.
            report.skipped.append(promise.id)
            return

        stale = [record for record in existing if record.document != text]
        if stale:
            # Delete first: a failed delete leaves two vectors for one
            # promise, which is a duplicate the mapping agent would match
            # twice. Reported as a failure rather than embedded over.
            if not self._delete_stale(promise.id, stale, report=report):
                return
            report.rebuilt.append(promise.id)

        vector_id = self._store_vector(promise, text=text, cycle=cycle, report=report)
        if vector_id is None:
            return

        if not self._record_vector_id(promise, vector_id=vector_id, report=report):
            return

        report.embedded.append(promise.id)
        self._announce(promise, report=report)

    def _delete_stale(
        self, promise_id: str, stale: list[VectorRecord], *, report: PromiseEmbeddingReport
    ) -> bool:
        """Delete every stale vector for one promise. False if any delete failed."""
        for record in stale:
            self._pace()
            try:
                self._client.delete_vector(vector_id=record.vector_id, namespace=PROMISES_NAMESPACE)
            except ZeroDBError as exc:
                report.failures.append(
                    f"{promise_id}: could not delete the stale vector {record.vector_id}: {exc}"
                )
                logger.error(
                    "promise_embedding: %s has a stale vector %s that could not be "
                    "deleted; leaving it rather than storing a second one: %s",
                    promise_id,
                    record.vector_id,
                    exc,
                )
                return False
        return True

    def _store_vector(
        self,
        promise: Promise,
        *,
        text: str,
        cycle: int | None,
        report: PromiseEmbeddingReport,
    ) -> str | None:
        """Embed one promise and recover the stored vector's id, or None.

        Two calls, because `embed_and_store` returns no ids (measured live;
        see the module docstring). The read is matched on the `promise_id`
        this module just wrote into the metadata, which is the only handle
        ZeroDB keeps.
        """
        assert promise.id is not None  # noqa: S101 - caller checked
        self._pace()
        try:
            self._client.embed_and_store(
                texts=[text],
                namespace=PROMISES_NAMESPACE,
                metadata_list=[promise_vector_metadata(promise, cycle=cycle)],
            )
        except ZeroDBError as exc:
            report.failures.append(f"{promise.id}: could not store the embedding: {exc}")
            logger.error("promise_embedding: could not embed %s: %s", promise.id, exc)
            return None

        try:
            stored = self._stored_vector_for(promise.id, text=text)
        except ZeroDBError as exc:
            report.failures.append(
                f"{promise.id}: embedding stored but the namespace could not be read "
                f"back, so its vector_id is unknown: {exc}"
            )
            logger.error(
                "promise_embedding: embedded %s but could not read the namespace back "
                "to recover its vector_id: %s",
                promise.id,
                exc,
            )
            return None

        if stored is None:
            # The write reported success and the read found nothing matching.
            # Not reported as embedded: story 9.4's criterion is a vector_id,
            # and there is none to write.
            report.failures.append(
                f"{promise.id}: the embedding was stored but no vector carrying its "
                "promise_id could be found, so there is no vector_id to record"
            )
            logger.error(
                "promise_embedding: embedded %s but found no vector carrying its "
                "promise_id; the row keeps no vector_id",
                promise.id,
            )
            return None
        return stored.vector_id

    def _record_vector_id(
        self, promise: Promise, *, vector_id: str, report: PromiseEmbeddingReport
    ) -> bool:
        """Write the vector's id back onto the promise row. False on failure.

        This is story 9.4's acceptance criterion itself — "every promise has
        a `vector_id`" is a property of the ROW, not of the namespace.
        """
        assert promise.id is not None  # noqa: S101 - caller checked
        self._pace()
        try:
            self._promises.upsert(
                {**promise.model_dump(), "id": promise.id, "vector_id": vector_id}
            )
        except Exception as exc:  # noqa: BLE001 - any write failure is the same gap
            report.failures.append(
                f"{promise.id}: embedded as vector {vector_id} but the row's vector_id "
                f"could not be written: {exc}"
            )
            logger.error(
                "promise_embedding: stored vector %s for %s but could not write the "
                "row's vector_id: %s",
                vector_id,
                promise.id,
                exc,
            )
            return False
        return True

    def _announce(self, promise: Promise, *, report: PromiseEmbeddingReport) -> None:
        """Emit story 9.5's `promise.extracted`, counting it only if it sent.

        Called only after the promise is embedded AND its row carries the
        `vector_id` — see the module docstring for why the order is fixed.

        A failed publish does not fail the run: the promise is stored,
        embedded and searchable, and raising here would abandon the remaining
        promises over an event the consumer polls for anyway.
        """
        assert promise.id is not None  # noqa: S101 - caller checked
        self._pace()
        try:
            self._client.create_event(
                topic=PROMISE_EXTRACTED_TOPIC,
                # datamodel.md's payload for this topic: promise_id,
                # person_id. Nothing else -- not the quote, not the candidacy.
                # A consumer that needs more reads the row by this id, which
                # is also the only way it sees a correction.
                payload={"promise_id": promise.id, "person_id": promise.person_id},
            )
        except ZeroDBError as exc:
            report.failures.append(
                f"{promise.id}: embedded, but the {PROMISE_EXTRACTED_TOPIC} event could "
                f"not be published, so mapping will not start automatically: {exc}"
            )
            logger.error(
                "promise_embedding: embedded %s but could not emit %s: %s",
                promise.id,
                PROMISE_EXTRACTED_TOPIC,
                exc,
            )
            return
        report.events_emitted.append(promise.id)

    # --- reads ------------------------------------------------------------

    def _cycle_for(self, source: PromiseSource) -> int | None:
        """The election cycle this source's candidacy belongs to, or None.

        One read of the candidacy per source rather than per promise: every
        promise from one source shares its candidacy.

        A candidacy that does not resolve yields None rather than raising,
        but **be clear about what actually happens next in that case, because
        it is not what it looks like**: this method returning None does not
        mean the promise is embedded with a null cycle. `Promise.references`
        names `candidacy_id`, so the `vector_id` write-back in
        `_record_vector_id` fails its own reference check and that promise is
        reported as a failure, not as embedded — verified by
        `test_a_candidacy_that_does_not_resolve_is_reported_as_a_failure`.
        The vector is stored but orphaned until the candidacy is restored and
        the run repeated, at which point the stale vector is rebuilt.

        That is the right outcome (a promise whose candidate is not on file
        cannot be cited, so it must not quietly become searchable evidence),
        and it is left to the reference check rather than duplicated here —
        but it means a null cycle in this method is never a *survivable*
        degradation in practice. The None return exists for the narrower real
        case: a candidacy that resolves but whose `election_id` carries no
        readable year.
        """
        candidacy = self._candidacies.get(source.candidacy_id)
        if candidacy is None:
            logger.warning(
                "promise_embedding: candidacy %s does not resolve, so these vectors carry no cycle",
                source.candidacy_id,
            )
            return None
        return cycle_for_election_id(candidacy.election_id)

    def _promises_for_source(self, source_id: str) -> list[Promise]:
        """Every promise row extracted from one source, paged."""
        rows: list[Promise] = []
        skip = 0
        while True:
            self._pace()
            page = self._promises.query(filter={"source_id": source_id}, limit=PAGE_SIZE, skip=skip)
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
        # The live server-side filter is observed to drop matching rows
        # (datamodel.md "Open questions"), and the client's fallback filters
        # client-side within a page -- so narrow again here rather than trust
        # either layer to have done it.
        return [row for row in rows if row.source_id == source_id]

    def _existing_vectors(self) -> dict[str, list[VectorRecord]]:
        """Every promise vector currently stored, grouped by `promise_id`.

        A list per id, not a single record, because a failed delete or a
        crashed run can leave more than one — and the rebuild path has to
        remove every stale one rather than just the first. Vectors with no
        `promise_id` (story 1.3's bootstrap marker) are excluded: this module
        did not create them and has no mandate to touch them.
        """
        index: dict[str, list[VectorRecord]] = {}
        for record in self._list_all_vectors():
            promise_id = record.vector_metadata.get("promise_id")
            if not promise_id:
                continue
            index.setdefault(str(promise_id), []).append(record)
        return index

    def _stored_vector_for(self, promise_id: str, *, text: str) -> VectorRecord | None:
        """The stored vector carrying this `promise_id` and this document.

        Matched on both, not just the id: a rebuild whose delete was reported
        as succeeding but did not take would otherwise hand back the stale
        vector's id, and the row would point at a vector of the old text.
        """
        for record in self._list_all_vectors():
            if record.vector_metadata.get("promise_id") == promise_id and record.document == text:
                return record
        return None

    def _list_all_vectors(self) -> list[VectorRecord]:
        """Page through every vector in the `promises` namespace."""
        records: list[VectorRecord] = []
        offset = 0
        while True:
            self._pace()
            page = self._client.list_vectors(
                namespace=PROMISES_NAMESPACE, limit=PAGE_SIZE, offset=offset
            )
            records.extend(page.vectors)
            if not page.vectors or len(page.vectors) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return records


__all__ = [
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "PAGE_SIZE",
    "PROMISES_NAMESPACE",
    "PROMISE_EXTRACTED_TOPIC",
    "PromiseEmbedder",
    "PromiseEmbeddingError",
    "PromiseEmbeddingReport",
    "cycle_for_election_id",
    "promise_embedding_text",
    "promise_vector_metadata",
]
