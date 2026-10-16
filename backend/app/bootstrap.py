"""Idempotent ZeroDB bootstrap for Informed (story 1.3).

One command creates every NoSQL table, vector namespace + HNSW index, and the
knowledge graph ontology that datamodel.md specifies, through the typed
`ZeroDBClient` from story 1.2. Running it twice must change nothing: this
module's whole job is checking what already exists before creating anything,
because ZeroDB itself has no upsert-by-natural-key (`POST /rows` always
appends — see datamodel.md's "Open questions").

It also **seeds** the fixed reference rows story 2.1 specifies, by calling
`app.seed.Seeder` once the tables exist. That is rows rather than structure, so
it lives in its own module — see `app/seed.py` for the split, and for why those
three rows are permanent rather than cleaned up like a verify script's.

What "idempotent" means for each primitive here:

  * Tables — `ZeroDBClient.list_tables()` is checked first; a table already
    present is skipped. `create_table` itself is not idempotent on the live
    API (calling it twice is not documented as a safe no-op), so the
    check-before-create is this module's responsibility, not the client's.
  * Vector namespaces/indexes — **confirmed live during this story**:
    `create_vector_index` is NOT idempotent (a second call for an
    already-indexed namespace silently creates a second, independent index
    rather than erroring or no-op'ing), and there is no live "list existing
    indexes" endpoint to check against first (the one that once existed is
    deprecated/410; see `ZeroDBClient.create_vector_index`'s docstring).
    So this module tracks which namespaces it has indexed in the
    `schema_versions` row itself — the only idempotency guard available —
    and reads that row FIRST, before writing anything, so it knows what to
    skip. It is also confirmed live that a namespace must already hold at
    least one vector before it can be indexed, so each new namespace is
    seeded with one small marker vector before its index is created.
  * The ontology — `graph_upsert_ontology` is a true upsert on ZeroDB's side
    (the name says so, and the fake transport models it that way), so it is
    safe to call every run; it never duplicates an edge type even though it
    is always called (there is no cheap "already has this ontology" check
    to skip it on).
  * The schema version — written with `ZeroDBClient.upsert_row`, which story
    1.2 already made idempotent (find-then-write by natural-key id).

Rate limiting: the live "informed" project was observed to throttle at 60
requests/minute (datamodel.md "Open questions"). A cold bootstrap makes
roughly 35 calls (23 tables incl. `schema_versions` + 4 namespaces x 2 calls
each [seed vector, then index] + 1 ontology read + 1 ontology write +
1 schema-version write + 3 reference-data seed writes, each of which is
itself a find-then-write and so costs more than one request — see
`app.seed`), close enough to that ceiling that this module paces
every call through an injectable `pace` callback rather than relying on the
client's retry-on-429 as the only defense. The default pacer sleeps enough to
keep calls at least `DEFAULT_MIN_INTERVAL_SECONDS` apart; tests inject a
no-op or a recording pacer instead of waiting.

Rate limiting observed live during this story: a cold bootstrap against the
real "informed" project completed in full (every table, namespace and the
ontology) in about 39 seconds with zero 429s at the pacing below, so the
60 req/min assumption was not contradicted and no pacing change was needed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.seed import Seeder, SeedReport
from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBNotFoundError
from app.zerodb.models import Table

logger = logging.getLogger(__name__)

#: Current schema version this module bootstraps. Bump when a table, field,
#: namespace or ontology predicate changes shape; the bootstrap script does
#: not infer this from the tables it creates.
SCHEMA_VERSION = "1.4.0"

#: The row id `schema_versions` is written and read under. One row, always
#: this id: the table is a meta-table about the schema itself, not a history
#: of every version ever bootstrapped (append-only history is for the
#: evidence tables, not this one).
SCHEMA_VERSION_ROW_ID = "current"

#: Default: a little more than one second between calls, keeping a 27-call
#: bootstrap comfortably under 60 req/min even with network jitter, without
#: making a cold bootstrap take unreasonably long.
DEFAULT_MIN_INTERVAL_SECONDS = 1.1

#: ZeroDB's live `bge-m3` model emits 1024-dimension vectors (datamodel.md,
#: corrected from an earlier 768-dimension assumption during story 1.2;
#: re-confirmed live during this story via `embed_and_store`'s own response).
#: Documentation only: the live `create_vector_index` endpoint takes no
#: dimension parameter at all — the dimension is a property of whatever
#: embedding model wrote the namespace's vectors, not of the index.
VECTOR_DIMENSIONS = 1024

#: The four namespaces datamodel.md specifies, each indexed with HNSW.
VECTOR_NAMESPACES: tuple[str, ...] = ("promises", "bill_summaries", "topics", "stances")


# ---------------------------------------------------------------------------
# Table specs — datamodel.md "NoSQL table specs", 22 tables across 4 groups.
# ---------------------------------------------------------------------------
#
# Each schema is a flat field-name -> type-hint dict, matching how
# `ZeroDBClient.create_table` and the live API accept `schema_definition`
# (see `zerodb_client.feature`'s "fields" steps for the shape already in
# use). Compound fields (`terms[]`, `sponsors[]`, `mappings[]`, `tally`,
# `weights`, `payload`) are typed "array" or "object": datamodel.md documents
# their internal shape, but ZeroDB's schema is not a nested validator — the
# repository layer (story 1.4) is where that structure is actually enforced
# with Pydantic before a write, per datamodel.md rule 5.
#
# The `id` field is included on every table on purpose: it is Informed's
# deterministic natural-key id, carried inside `row_data`, which is where
# `ZeroDBClient.upsert_row`/`get_row` always look for it (story 1.2).

TABLE_SCHEMAS: dict[str, dict[str, str]] = {
    # --- Reference and people ---------------------------------------------
    "jurisdictions": {
        "id": "string",
        "name": "string",
        "country_code": "string",
        "level": "string",
        "adapter_name": "string",
        "active": "boolean",
    },
    "bodies": {
        "id": "string",
        "jurisdiction_id": "string",
        "name": "string",
        "chamber": "string",
        "seats": "number",
    },
    "districts": {
        "id": "string",
        "body_id": "string",
        "name": "string",
        "state": "string",
        "valid_from": "string",
        "valid_to": "string",
    },
    "people": {
        "id": "string",
        "canonical_name": "string",
        "external_ids": "object",
        "photo_url": "string",
        "terms": "array",
    },
    "elections": {
        "id": "string",
        "jurisdiction_id": "string",
        "name": "string",
        "election_date": "string",
        "kind": "string",
    },
    "candidacies": {
        "id": "string",
        "election_id": "string",
        "district_id": "string",
        "person_id": "string",
        "person_name": "string",
        "party": "string",
        "is_incumbent": "boolean",
        "status": "string",
    },
    "issue_topics": {
        "id": "string",
        "parent_id": "string",
        "name": "string",
        "locale": "string",
        "taxonomy_version": "number",
    },
    "stances": {
        "id": "string",
        "topic_id": "string",
        "label": "string",
        "description": "string",
        "direction_code": "string",
        "wording_version": "number",
    },
    # --- Legislative record -------------------------------------------------
    "bills": {
        "id": "string",
        "title": "string",
        "summary": "string",
        "status": "string",
        "introduced_on": "string",
        "subjects": "array",
        "topic_ids": "array",
        "sponsors": "array",
        "text_file_id": "string",
        "source_url": "string",
    },
    "roll_calls": {
        "id": "string",
        "voted_at": "string",
        "question": "string",
        "vote_type": "string",
        "bill_id": "string",
        "amendment_ref": "string",
        "result": "string",
        "tally": "object",
        "is_bundled": "boolean",
        "topic_ids": "array",
        "source_url": "string",
        "content_hash": "string",
    },
    "member_votes": {
        "id": "string",
        "roll_call_id": "string",
        "person_id": "string",
        "position": "string",
        "voted_at": "string",
        "vote_type": "string",
        "bill_id": "string",
        "topic_ids": "array",
        "is_bundled": "boolean",
    },
    # --- Promises, verdicts and positions -----------------------------------
    "promise_sources": {
        "id": "string",
        "candidacy_id": "string",
        "source_type": "string",
        "url": "string",
        "archived_file_id": "string",
        "captured_at": "string",
    },
    "promises": {
        "id": "string",
        "candidacy_id": "string",
        "person_id": "string",
        "source_id": "string",
        "quote_span": "string",
        "actor": "string",
        "action": "string",
        "object": "string",
        "direction": "string",
        "topic_id": "string",
        "stance_id": "string",
        "testable": "boolean",
        "extractor_version": "string",
        "confidence": "number",
        "vector_id": "string",
        "mappings": "array",
        "qa_pending": "boolean",
    },
    "verdicts": {
        "id": "string",
        "promise_id": "string",
        "person_id": "string",
        "verdict": "string",
        "confidence": "number",
        "methodology_version": "string",
        "qa_status": "string",
        "reviewed_by": "string",
        "reviewed_at": "string",
        "is_current": "boolean",
        "created_at": "string",
    },
    "candidate_positions": {
        "id": "string",
        "person_id": "string",
        "stance_id": "string",
        "s_value": "number",
        "confidence": "number",
        "evidence_count": "number",
        "basis": "string",
        "is_current": "boolean",
        "computed_at": "string",
    },
    "candidate_cards": {
        "id": "string",
        "person_name": "string",
        "party": "string",
        "district": "string",
        "photo_url": "string",
        "verdict_counts": "object",
        "say_do_gap": "number",
        "positions_summary": "array",
        "updated_at": "string",
    },
    "candidate_statements": {
        "id": "string",
        "person_id": "string",
        "verdict_id": "string",
        "body": "string",
        "verified_by": "string",
        "posted_at": "string",
    },
    # --- Governance, provenance and users -----------------------------------
    "methodology_versions": {
        "id": "string",
        "published_at": "string",
        "weights": "object",
        "changelog_url": "string",
        "is_current": "boolean",
    },
    "corrections": {
        "id": "string",
        "target_table": "string",
        "target_id": "string",
        "body": "string",
        "contact_email": "string",
        "status": "string",
        "sla_due_at": "string",
        "resolved_at": "string",
        # Story 12.4 adds these two to datamodel.md's documented field list,
        # and `app/corrections/models.py` argues both: `created_at` is what
        # `sla_due_at` is defined relative to (without it, "was this triaged
        # late" is unanswerable after the fact and the SLA is unauditable),
        # and `tracking_token_hash` is how a reporter with no account checks
        # their own status — a sha256 digest, never a token.
        "created_at": "string",
        "tracking_token_hash": "string",
    },
    "audit_runs": {
        "id": "string",
        "run_at": "string",
        "methodology_version": "string",
        "max_delta": "number",
        "passed": "boolean",
        "report_file_id": "string",
    },
    "provenance_log": {
        "id": "string",
        "entity_table": "string",
        "entity_id": "string",
        "source_url": "string",
        "fetched_at": "string",
        "adapter_version": "string",
    },
    "voter_profiles": {
        "id": "string",
        "version": "number",
        "locale": "string",
        # Story 14.1: both documented in datamodel.md's "Voter profile
        # document" example row and missing from this schema until now.
        # `jurisdiction_ids` and `key_ref` are outer-row fields on purpose —
        # they are needed to pick a taxonomy and to know which key scheme
        # sealed `payload`, both BEFORE anything is decrypted.
        "jurisdiction_ids": "array",
        "updated_at": "string",
        "key_ref": "string",
        "payload": "string",
    },
    # --- Infrastructure tables, not among datamodel.md's 22 -----------------
    #
    # Story 14.1: the wrapped per-user data keys behind `voter_profiles`.
    # A separate table rather than a column on the profile row for two
    # reasons. First, datamodel.md fixes `voter_profiles` at exactly seven
    # fields and a key column would contradict it. Second and decisively,
    # "delete shreds the key" has to be a write the profile row's own
    # lifecycle cannot undo: the key must be destroyable on its own, and a
    # restored `voter_profiles` backup must not restore the key with it.
    #
    # Nothing here is a secret in the clear: the row id is the same HMAC the
    # profile row uses (so the table names no accounts) and `wrapped_key` is
    # sealed under a KEK derived from the root key, which lives outside the
    # database. See app/profiles/crypto.py.
    "voter_profile_keys": {
        "id": "string",
        "wrapped_key": "string",
        "salt": "string",
        "key_ref": "string",
        "created_at": "string",
    },
    # Story 3.3: the cross-partisan advisory panel's review ledger over the
    # wording in `issue_topics` and `stances`.
    #
    # Infrastructure rather than one of datamodel.md's 22 for the same reason
    # `voter_profile_keys` is: datamodel.md describes no table for it, and both
    # of the tables it judges are fixed there at their documented field lists.
    # prd.md, not datamodel.md, is what requires it — "stance cards and issue
    # taxonomy reviewed by a cross-partisan advisory panel before each release;
    # wording changes are versioned and public" — and a documented rule that
    # cannot be implemented is the same situation story 14.1 was in.
    #
    # A separate table rather than status columns on the reviewed rows, which
    # is argued at length in `app.repositories.models.PanelReview`: the
    # seeders restate every row on each run and would wipe a status field, a
    # decision is history rather than state, and 281 live rows are read by
    # four endpoints that must not be touched to add a review workflow.
    #
    # Nothing here is voter data: the rows name editorial wording and the
    # reviewer who judged it, never a voter, a profile or a score.
    "panel_reviews": {
        "id": "string",
        "subject_table": "string",
        "subject_id": "string",
        "subject_version": "number",
        "decision": "string",
        "reviewer_id": "string",
        "decided_at": "string",
        "reason": "string",
        "proposal_id": "string",
        "proposed_label": "string",
        "proposed_description": "string",
        "proposed_name": "string",
        "previous_label": "string",
        "previous_description": "string",
        "previous_name": "string",
    },
    # --- Bootstrap's own meta-table -----------------------------------------
    "schema_versions": {
        "id": "string",
        "version": "string",
        "tables": "array",
        "vector_namespaces": "array",
        "ontology_registered": "boolean",
        "bootstrapped_at": "string",
    },
}

#: datamodel.md counts 22 tables. The rest are infrastructure that no section
#: of datamodel.md describes as a data table, counted separately so a reader
#: (or a test) can see at a glance that the two numbers differ deliberately
#: rather than by miscount.
DATAMODEL_TABLE_COUNT = 22

#: `schema_versions` (story 1.3's own bookkeeping), `voter_profile_keys`
#: (story 14.1's wrapped data keys) and `panel_reviews` (story 3.3's wording
#: review ledger). None holds product data in datamodel.md's sense; all three
#: exist so a documented rule — idempotent bootstrap, "key shredded on
#: delete", "reviewed by a cross-partisan advisory panel before each release" —
#: can actually be implemented rather than asserted in a release note.
INFRASTRUCTURE_TABLES = ("schema_versions", "voter_profile_keys", "panel_reviews")

assert len(TABLE_SCHEMAS) == DATAMODEL_TABLE_COUNT + len(INFRASTRUCTURE_TABLES), (
    f"expected {DATAMODEL_TABLE_COUNT} datamodel.md tables plus "
    f"{len(INFRASTRUCTURE_TABLES)} infrastructure tables, got {len(TABLE_SCHEMAS)}"
)
assert all(name in TABLE_SCHEMAS for name in INFRASTRUCTURE_TABLES)


# ---------------------------------------------------------------------------
# Ontology — datamodel.md "Knowledge graph ontology" predicate table.
# ---------------------------------------------------------------------------

#: Every entity type that appears as a source or target in the predicate
#: table, in the order datamodel.md lists the predicates.
ONTOLOGY_ENTITY_TYPES: tuple[str, ...] = (
    "person",
    "candidacy",
    "promise",
    "stance",
    "roll_call",
    "bill",
    "verdict",
    "topic",
)

#: predicate -> (source_type, target_type), one entry per row of
#: datamodel.md's predicate table. `advances`/`opposes` are two predicates
#: sharing one row in the doc; both are registered here since the ontology
#: has no way to express "one of two predicates" on a single edge type.
ONTOLOGY_PREDICATES: dict[str, dict[str, Any]] = {
    "ran_in": {"source": "person", "target": "candidacy"},
    "made_promise": {"source": "candidacy", "target": "promise"},
    "about": {"source": "promise", "target": "stance"},
    "tested_by": {"source": "promise", "target": "roll_call"},
    "voted": {"source": "person", "target": "roll_call"},
    "sponsored": {"source": "person", "target": "bill"},
    "on": {"source": "roll_call", "target": "bill"},
    "advances": {"source": "bill", "target": "stance"},
    "opposes": {"source": "bill", "target": "stance"},
    "has_verdict": {"source": "promise", "target": "verdict"},
    "under": {"source": "stance", "target": "topic"},
}


@dataclass
class BootstrapReport:
    """What one bootstrap run did, for logging and for the CLI's exit summary.

    Every list below holds only what THIS run actually created — a fully
    idempotent re-run against an already-bootstrapped project reports empty
    lists everywhere and `schema_version_changed = False`, which is exactly
    what the "running it twice changes nothing" acceptance criterion checks.
    """

    tables_created: list[str] = field(default_factory=list)
    tables_skipped: list[str] = field(default_factory=list)
    namespaces_indexed: list[str] = field(default_factory=list)
    namespaces_skipped: list[str] = field(default_factory=list)
    ontology_registered: bool = False
    ontology_already_current: bool = False
    #: What the reference-data seed wrote (story 2.1). Always populated, and
    #: deliberately NOT part of `is_noop`: the seed restates its three fixed
    #: rows on every run, so "it wrote three rows" says nothing about whether
    #: anything changed. See `app.seed`'s module docstring.
    seed: SeedReport = field(default_factory=SeedReport)
    schema_version: str = SCHEMA_VERSION
    schema_version_changed: bool = False

    @property
    def is_noop(self) -> bool:
        """True when this run changed nothing observable.

        The ontology upsert always runs (there is no cheap "already has this
        ontology" check to skip it on, per `Bootstrapper._bootstrap_ontology`'s
        docstring), so `ontology_registered` alone does not mean anything
        changed — `ontology_already_current` is what distinguishes a true
        first registration from a repeat call that reaffirmed the same state.

        The seed is excluded for the same reason, one step further: it has no
        "already current" signal at all, because it restates its rows
        unconditionally rather than reading them first. A second bootstrap
        whose only writes were the seed's three restatements is still a no-op
        in the sense this property means — nothing about the environment
        differs afterwards.
        """
        return (
            not self.tables_created
            and not self.namespaces_indexed
            and self.ontology_already_current
            and not self.schema_version_changed
        )


class Bootstrapper:
    """Creates (once) everything datamodel.md specifies, through one client.

    Every public method checks what already exists before writing, so
    `run()` called twice against the same project is safe. Pass a `pace`
    callback to control the delay between calls (see module docstring);
    the default paces for a live 60 req/min ceiling, and tests pass a
    no-op or recording pacer so the suite does not actually sleep.
    """

    def __init__(
        self,
        client: ZeroDBClient,
        *,
        pace: Callable[[], None] | None = None,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._client = client
        self._pace = pace if pace is not None else default_pacer(min_interval_seconds)

    # --- the whole job ------------------------------------------------------

    def run(self) -> BootstrapReport:
        """Create every table, namespace/index and the ontology; record the version.

        Idempotent: each step below checks before it writes, so calling this
        twice against the same project produces an unchanged second
        `BootstrapReport` (every list empty, `schema_version_changed = False`).

        Tables use `list_tables` as their existence check, because that
        primitive is real and current on the live API. Vector namespaces
        cannot: there is no live "list existing indexes" endpoint any more
        (confirmed during this story — see `create_vector_index`'s
        docstring), so the *only* record of which namespaces this bootstrap
        already indexed is the `indexed_namespaces` list this module itself
        writes into the `schema_versions` row. That row is therefore read
        FIRST, before any writing happens, so `_bootstrap_vector_namespaces`
        has something to check against.

        The reference-data seed (story 2.1) runs immediately after the tables,
        before the vector and graph work, for two reasons: its rows have to go
        into tables that already exist, and its three writes are the cheapest
        step here — so a project whose ontology call later fails still has a
        usable reference tree rather than a schema with nothing in it. Row
        seeding lives in `app.seed`, not in this module; see that module's
        docstring for the schema-versus-data split and for why those rows are
        permanent.
        """
        report = BootstrapReport()
        previously_indexed = self._previously_indexed_namespaces()
        self._bootstrap_tables(report)
        self._seed_reference_data(report)
        self._bootstrap_vector_namespaces(report, previously_indexed)
        self._bootstrap_ontology(report)
        self._record_schema_version(report, previously_indexed)
        return report

    def _previously_indexed_namespaces(self) -> set[str]:
        """Namespaces a prior run of this bootstrap already indexed.

        Returns the empty set on a project's first-ever bootstrap (the
        `schema_versions` table does not exist yet, so the lookup is wrapped
        exactly like `get_schema_version`'s own tolerance of that).
        """
        try:
            row = self._client.get_row("schema_versions", SCHEMA_VERSION_ROW_ID)
        except ZeroDBNotFoundError:
            return set()
        if row is None:
            return set()
        indexed = row.row_data.get("indexed_namespaces") or []
        return set(indexed)

    # --- tables ---------------------------------------------------------

    def _bootstrap_tables(self, report: BootstrapReport) -> None:
        existing = {table.table_name for table in self._list_tables()}
        for table_name, schema in TABLE_SCHEMAS.items():
            if table_name in existing:
                report.tables_skipped.append(table_name)
                logger.info("bootstrap: table %r already exists, skipping", table_name)
                continue
            self._pace()
            self._client.create_table(table_name, schema)
            report.tables_created.append(table_name)
            logger.info("bootstrap: created table %r", table_name)

    def _list_tables(self) -> list[Table]:
        """All existing tables, paged until exhausted.

        A freshly bootstrapped project has 23; this still pages defensively
        so a project that somehow holds more than one page's worth of tables
        is not silently under-counted and re-created.
        """
        tables: list[Table] = []
        skip = 0
        limit = 100
        while True:
            page = self._client.list_tables(limit=limit, skip=skip)
            tables.extend(page)
            if len(page) < limit:
                break
            skip += limit
        return tables

    # --- fixed reference data (story 2.1) ---------------------------------

    def _seed_reference_data(self, report: BootstrapReport) -> None:
        """Seed the US federal jurisdiction and its two chambers.

        Delegated whole to `app.seed.Seeder`, sharing this bootstrap's pacer so
        the seed's writes count against the same 60 req/min budget as
        everything else here. This method exists only to connect the two and to
        carry the seed's report into the bootstrap's.
        """
        report.seed = Seeder(self._client, pace=self._pace).run()

    # --- vector namespaces + HNSW indexes --------------------------------

    def _bootstrap_vector_namespaces(
        self, report: BootstrapReport, previously_indexed: set[str]
    ) -> None:
        """Seed one marker vector per namespace, then index it.

        Two live-confirmed facts drive this, both documented on
        `ZeroDBClient.create_vector_index`:

          1. `create_vector_index` 404s on a namespace with no vectors in it
             yet, so a namespace must be seeded with at least one vector
             before it can be indexed. The marker text embedded here is
             schema bootstrap metadata, not voter or candidate data — it
             never touches a table and carries no PII, so it is not subject
             to the privacy rules in datamodel.md/prd.md.
          2. `create_vector_index` is NOT idempotent — a second call for an
             already-indexed namespace creates a second, independent index
             rather than erroring or no-op'ing. So `previously_indexed`
             (read from `schema_versions` before this method runs, per
             `run()`'s docstring) is checked first and is the only guard
             against ever calling this twice for the same namespace.
        """
        for namespace in VECTOR_NAMESPACES:
            if namespace in previously_indexed:
                report.namespaces_skipped.append(namespace)
                logger.info("bootstrap: namespace %r already indexed, skipping", namespace)
                continue
            self._pace()
            self._client.embed_and_store(
                texts=[f"informed schema bootstrap marker for namespace {namespace}"],
                namespace=namespace,
                metadata_list=[{"bootstrap_marker": True, "schema_version": SCHEMA_VERSION}],
            )
            self._pace()
            self._client.create_vector_index(namespace=namespace, index_type="hnsw")
            report.namespaces_indexed.append(namespace)
            logger.info("bootstrap: created HNSW index for namespace %r", namespace)

    # --- ontology ---------------------------------------------------------

    def _bootstrap_ontology(self, report: BootstrapReport) -> None:
        """Register the ontology.

        Calling `graph_upsert_ontology` every run is safe in the sense that
        it never creates a second ontology or a duplicate predicate — but
        **confirmed live during this story, it is a REPLACE, not a merge**
        (see that method's docstring for the live incident this caused), so
        unlike tables and namespaces this step cannot use "skip if it
        already exists" as its idempotency guard; it always has to call
        `graph_upsert_ontology` with the complete, fixed entity/predicate
        sets to be safe, even on a repeat run.

        What `report.ontology_already_current` means, and why it has to be
        computed from a READ taken *before* this method writes anything:
        `graph_upsert_ontology`'s own response always echoes back exactly
        what was just sent, so a project with nothing registered yet and a
        project that already had everything produce an identical-looking
        response. Checking the write's response can therefore never tell
        "first registration" apart from "re-affirmed, nothing new" — only a
        read taken first can. That is exactly the bug this method had until
        this story's live run surfaced it (the write-response check was
        tautologically true on every call), fixed here the same way
        `_previously_indexed_namespaces` already reads before writing.
        """
        self._pace()
        previous = self._client.graph_get_ontology()
        already_had_all = previous is not None and (
            set(ONTOLOGY_ENTITY_TYPES) <= set(previous.entity_types)
            and set(ONTOLOGY_PREDICATES) <= set(previous.predicates)
        )
        self._pace()
        self._client.graph_upsert_ontology(
            entity_types=list(ONTOLOGY_ENTITY_TYPES),
            predicates=ONTOLOGY_PREDICATES,
        )
        report.ontology_registered = True
        report.ontology_already_current = already_had_all

    # --- schema version -----------------------------------------------------

    def _record_schema_version(self, report: BootstrapReport, previously_indexed: set[str]) -> None:
        """Write (or leave alone) the one `schema_versions` row.

        `indexed_namespaces` is this row's record of which vector namespaces
        have been indexed across every run, because there is nowhere else to
        ask (see `_bootstrap_vector_namespaces`). That makes this write
        necessary not just when the version string changes, but whenever
        THIS run indexed a namespace that a prior run had not — otherwise
        the next run would re-read an empty/stale list and try to index an
        already-indexed namespace again, which is exactly the non-idempotent
        call this module exists to prevent.
        """
        indexed_namespaces = sorted(previously_indexed | set(report.namespaces_indexed))
        existing = self._client.get_row("schema_versions", SCHEMA_VERSION_ROW_ID)
        payload = {
            "id": SCHEMA_VERSION_ROW_ID,
            "version": SCHEMA_VERSION,
            "tables": sorted(TABLE_SCHEMAS),
            "vector_namespaces": list(VECTOR_NAMESPACES),
            "indexed_namespaces": indexed_namespaces,
            "ontology_registered": True,
            "bootstrapped_at": _now_iso(),
        }
        unchanged = (
            existing is not None
            and existing.row_data.get("version") == SCHEMA_VERSION
            and set(existing.row_data.get("indexed_namespaces") or []) == set(indexed_namespaces)
        )
        if unchanged:
            report.schema_version_changed = False
            logger.info(
                "bootstrap: schema_versions already at %r, leaving it alone", SCHEMA_VERSION
            )
            return
        self._pace()
        self._client.upsert_row("schema_versions", SCHEMA_VERSION_ROW_ID, payload)
        report.schema_version_changed = True
        logger.info("bootstrap: recorded schema version %r", SCHEMA_VERSION)


def default_pacer(min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS) -> Callable[[], None]:
    """Sleep at least `min_interval_seconds` since the pacer's last call.

    Deliberately measures from the last call rather than always sleeping the
    full interval, so a client whose own request time already ate some of the
    budget doesn't needlessly double-pace. The first call never sleeps.

    Public because the 60 req/min ceiling (datamodel.md "Open questions") is a
    property of the live project, not of the bootstrap: story 3.1's taxonomy
    seeder makes ~80 paced writes of its own and uses the same pacer rather
    than inventing a second rate-limit policy that could drift from this one.
    """
    state = {"last": None}

    def pace() -> None:
        now = time.monotonic()
        last = state["last"]
        if last is not None:
            elapsed = now - last
            remaining = min_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        state["last"] = time.monotonic()

    return pace


def _now_iso() -> str:
    """UTC timestamp for `bootstrapped_at`, isolated for easy test injection."""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def get_schema_version(client: ZeroDBClient) -> str | None:
    """Read back the recorded schema version, or None if bootstrap never ran.

    Used by tests and by operators checking "is this environment bootstrapped
    and current" without re-running the whole thing. Deliberately tolerant of
    a `schema_versions` table that does not exist yet: that is exactly the
    "bootstrap never ran here" case, not an error, so `ZeroDBNotFoundError`
    (which `query_rows` raises for a missing *table*, as opposed to `get_row`'s
    own None for a missing *row*) is caught rather than left to propagate.
    """
    try:
        row = client.get_row("schema_versions", SCHEMA_VERSION_ROW_ID)
    except ZeroDBNotFoundError:
        return None
    if row is None:
        return None
    version = row.row_data.get("version")
    return str(version) if version is not None else None


__all__ = [
    "DATAMODEL_TABLE_COUNT",
    "ONTOLOGY_ENTITY_TYPES",
    "ONTOLOGY_PREDICATES",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_ROW_ID",
    "TABLE_SCHEMAS",
    "VECTOR_DIMENSIONS",
    "VECTOR_NAMESPACES",
    "Bootstrapper",
    "BootstrapReport",
    "default_pacer",
    "get_schema_version",
]
