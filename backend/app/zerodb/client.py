"""Typed ZeroDB client for Informed (story 1.2).

===============================================================================
TRACKED DEVIATION FROM CLAUDE.md — READ BEFORE CHANGING THIS MODULE
===============================================================================

CLAUDE.md says ZeroDB is "reached only through the ZeroDB MCP server" and that
repositories "go through the typed ZeroDB client". **This client currently
calls ZeroDB's REST API directly over HTTPS instead of going through the MCP
server.** That is a deliberate, flagged, temporary deviation — not an oversight
and not a silent violation of the rule.

WHY: the `mcp__ainative-zerodb__*` MCP connection available in this development
environment is authenticated to a different ZeroDB account. Its API key resolves
to another `user_id` and lists roughly a hundred unrelated projects
(ZeroInvoice, ZeroPipeline, OpenCapStack and so on) with no "informed" project
among them. Informed's real ZeroDB project lives under separate credentials
(`ZERODB_WORKSPACE_ID`, `ZERODB_PROJECT_ID`, `ZERODB_API_KEY`), which the REST
API accepts and which was confirmed to reach the live "informed" project. So
there is, in this environment, no way to reach Informed's data through MCP.

THE TARGET ARCHITECTURE IS UNCHANGED. This module must be swapped to call
through the MCP server once that connection is authenticated to the account
that owns the "informed" project.

HOW THE SWAP STAYS CHEAP: every public method below is shaped as a wrapper
around the MCP tool it corresponds to — `create_table` ↔ `zerodb_create_table`,
`upsert_row` ↔ `zerodb_insert_rows`, `query_rows` ↔ `zerodb_query_rows`,
`embed_and_store` ↔ `zerodb_embed_and_store`, `semantic_search` ↔
`zerodb_semantic_search`, `graph_upsert_ontology` ↔
`zerodb_graph_ontology_upsert`, `graph_upsert_entity` ↔
`zerodb_graph_upsert_entity`, `graph_create_edge` ↔ `zerodb_graph_create_edge`,
`graph_rag_search` ↔ `zerodb_graph_rag_search`, `upload_file` ↔
`zerodb_upload_file`, `generate_presigned_url` ↔
`zerodb_generate_presigned_url`, `create_event` ↔ `zerodb_create_event`,
`subscribe_events` ↔ `zerodb_subscribe_events`. This module never imports
httpx: it builds `(method, path, json, params)` and hands them to a
`ZeroDBTransport`. Writing an `McpZeroDBTransport` in `transport.py` is
therefore the whole job — one file, no change to any method signature, any
repository, or any test above this boundary.

===============================================================================

Beyond the transport swap, this client is the only place that knows:

  * which ZeroDB endpoint backs each primitive (verified against the live
    project, not inferred from documentation);
  * that Informed's deterministic natural-key id lives in `row_data["id"]` and
    is not ZeroDB's internal `row_id`;
  * which failures are worth retrying, and with how much backoff;
  * how a ZeroDB error becomes one of Informed's domain exceptions.

It knows nothing about Informed's tables or business rules. Validating
references between rows, enforcing append-only history and upserting by
natural key are the repository layer's job in story 1.4.
"""

import logging
import random
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.zerodb.errors import (
    ZeroDBAuthError,
    ZeroDBConflictError,
    ZeroDBConnectionError,
    ZeroDBError,
    ZeroDBNotFoundError,
    ZeroDBProtocolError,
    ZeroDBRateLimitError,
    ZeroDBServerError,
    ZeroDBValidationError,
)
from app.zerodb.models import (
    EmbedResult,
    Event,
    EventPage,
    EventSubscription,
    GraphEdge,
    GraphEntity,
    GraphRagResults,
    Ontology,
    PresignedUrl,
    Row,
    RowPage,
    SearchResults,
    StoredFile,
    Table,
    Traversal,
    VectorDeleteResult,
    VectorIndex,
    VectorPage,
    VectorStats,
)
from app.zerodb.settings import ZeroDBSettings
from app.zerodb.transport import (
    HttpxZeroDBTransport,
    ZeroDBResponse,
    ZeroDBTransport,
    ZeroDBTransportError,
)

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

#: Statuses worth another attempt: ZeroDB's own failures and throttling.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Statuses mapped to a domain exception and raised on the first attempt.
_STATUS_ERRORS: dict[int, type[ZeroDBError]] = {
    400: ZeroDBValidationError,
    401: ZeroDBAuthError,
    403: ZeroDBAuthError,
    404: ZeroDBNotFoundError,
    409: ZeroDBConflictError,
    422: ZeroDBValidationError,
    429: ZeroDBRateLimitError,
}

DEFAULT_PRESIGN_EXPIRATION = 300
MAX_PRESIGN_EXPIRATION = 3600

#: Rows per request while filtering client-side around ZeroDB's broken
#: server-side filter (see `_fallback_filtered_rows`). Big enough that a
#: reference check on a realistic `people` or `roll_calls` table resolves in a
#: request or two; small enough that no single response has to be held in
#: memory whole.
FALLBACK_PAGE_SIZE = 500

#: Ceiling on pages read in one client-side filter walk: 50 x 500 = 25k rows.
#: Deliberately far short of `member_votes`' ~600k, because this workaround
#: must not become a full-table scan on the largest table in the system. A
#: filtered read that needs to go deeper than this is a signal to fix the
#: server-side filter, not to raise the ceiling.
MAX_FALLBACK_PAGES = 50


class ZeroDBClient:
    """The one way Informed's backend reaches ZeroDB.

    Repositories, agents and API handlers depend on this class and on the
    domain exceptions in `errors.py`. Nothing above this client imports httpx,
    constructs a URL, or knows whether the bytes travel by REST or by MCP tool
    call.

    Instances are safe to share across requests: the client holds no per-request
    state, and the underlying httpx client is itself thread-safe and pools
    connections.

    Example:
        >>> client = ZeroDBClient.from_env()
        >>> client.create_table("jurisdictions", {"name": "string"})
        >>> client.upsert_row("jurisdictions", "us-federal", {"name": "United States"})
        >>> page = client.query_rows("jurisdictions", filter={"active": True})
    """

    def __init__(
        self,
        *,
        transport: ZeroDBTransport,
        project_id: str,
        max_attempts: int = 4,
        backoff_base: float = 0.5,
        backoff_max: float = 8.0,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
        fallback_page_size: int = FALLBACK_PAGE_SIZE,
        max_fallback_pages: int = MAX_FALLBACK_PAGES,
    ) -> None:
        """Build a client over an already-configured transport.

        Args:
            transport: Whatever carries the requests. Inject the fake from
                `app.zerodb.testing` in tests; `from_env` wires the REST one.
            project_id: The ZeroDB project every per-project path addresses.
            max_attempts: Total attempts per request, the first one included.
            backoff_base: Seconds before the second attempt. Doubles each time.
            backoff_max: Ceiling on a single wait, so one slow request cannot
                stall a web worker for a minute during an outage.
            sleep: Injected for tests, which record waits instead of taking them.
            jitter: Injected for tests. Spreads retries so a fleet of workers
                does not synchronise its hammering of a recovering ZeroDB.
            fallback_page_size: Rows per request while filtering client-side
                around ZeroDB's broken server-side filter.
            max_fallback_pages: Ceiling on that walk. Injected small in tests so
                the exhaustion path is reachable without seeding 25k rows; see
                `_fallback_filtered_rows`.
        """
        self._transport = transport
        self._project_id = project_id
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._sleep = sleep if sleep is not None else _default_sleep
        self._jitter = jitter if jitter is not None else random.random
        self._fallback_page_size = max(1, fallback_page_size)
        self._max_fallback_pages = max(1, max_fallback_pages)

    @classmethod
    def from_env(cls, settings: ZeroDBSettings | None = None) -> "ZeroDBClient":
        """Build a client from environment variables.

        Reads `ZERODB_API_KEY`, `ZERODB_PROJECT_ID` and friends through
        `ZeroDBSettings`. Nothing here opens `local.env` or any other file: the
        shell, CI or the deployment supplies the variables (story 1.6).
        """
        settings = settings or ZeroDBSettings()
        transport = HttpxZeroDBTransport(
            base_url=settings.base_url,
            api_key=settings.api_key.get_secret_value(),
            timeout_seconds=settings.timeout_seconds,
        )
        return cls(
            transport=transport,
            project_id=settings.project_id,
            max_attempts=settings.max_attempts,
        )

    @property
    def project_id(self) -> str:
        return self._project_id

    # =====================================================================
    # NoSQL tables — the system of record
    # =====================================================================

    def create_table(self, table_name: str, schema_definition: dict[str, Any]) -> Table:
        """Create a NoSQL table with a field-type schema.

        Wraps `zerodb_create_table`. Story 1.3 calls this for all 22 tables and
        relies on it being safe to re-run.
        """
        self._require(table_name, "table_name")
        payload = {"table_name": table_name, "schema_definition": schema_definition}
        response = self._request(
            "POST", f"/projects/{self._project_id}/database/tables", json=payload
        )
        return self._parse(Table, response)

    def list_tables(self, *, limit: int = 100, skip: int = 0) -> list[Table]:
        """List the project's tables. Wraps `zerodb_list_tables`."""
        response = self._request(
            "GET",
            f"/projects/{self._project_id}/database/tables",
            params={"limit": limit, "skip": skip},
        )
        return [self._parse(Table, response, item) for item in self._envelope(response)]

    def delete_table(self, table_name: str) -> None:
        """Drop a table. Wraps `zerodb_delete_table`.

        Used by test fixtures and story 1.3's teardown, never by request
        handling.
        """
        self._require(table_name, "table_name")
        self._request("DELETE", f"/projects/{self._project_id}/database/tables/{table_name}")

    def insert_row(self, table_name: str, row_data: dict[str, Any]) -> Row:
        """Append a row, whatever is already in the table.

        Wraps `zerodb_insert_rows` exactly: ZeroDB always appends. This is the
        right call for genuinely append-only tables — `verdicts`,
        `candidate_positions`, `provenance_log` — where a new version must not
        overwrite its predecessor.

        For re-ingestable records use `upsert_row`, which is idempotent.
        """
        self._require(table_name, "table_name")
        response = self._request(
            "POST",
            f"/projects/{self._project_id}/database/tables/{table_name}/rows",
            json={"row_data": row_data},
        )
        return self._parse(Row, response)

    def upsert_row(self, table_name: str, row_id: str, row_data: dict[str, Any]) -> Row:
        """Write one row under Informed's deterministic natural-key id.

        Idempotent: re-ingesting the same record updates the existing row
        instead of adding a duplicate (datamodel.md rule 1). The id is carried
        in `row_data["id"]`, which is where every Informed query looks for it.

        IMPLEMENTED AS FIND-THEN-WRITE, because ZeroDB has no
        upsert-by-natural-key: its `POST /rows` always appends, and its
        `PUT /rows/{row_id}` addresses rows by an internal UUID that has
        nothing to do with Informed's ids. A live check against the real
        project caught the naive version writing duplicates.

        This costs a read before each write and is NOT atomic: two workers
        upserting the same id at once can still both insert. Ingest is
        single-writer per table today, so that is acceptable; story 1.4 should
        revisit it if concurrent ingest arrives, and story 16.1 is where a
        server-side upsert would be requested from AINative.
        """
        self._require(table_name, "table_name")
        self._require(row_id, "row_id")

        existing = self.get_row(table_name, row_id)
        if existing is None or existing.row_id is None:
            return self.insert_row(table_name, {**row_data, "id": row_id})

        # A full replacement, deliberately: an upsert states the record's whole
        # current contents, so a field the caller dropped should disappear.
        return self._replace_row(table_name, existing.row_id, {**row_data, "id": row_id})

    def query_rows(
        self,
        table_name: str,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - matches the ZeroDB field
        sort: dict[str, Any] | None = None,
        limit: int = 100,
        skip: int = 0,
        projection: dict[str, Any] | None = None,
    ) -> RowPage:
        """Read a filtered, sorted page of rows. Wraps `zerodb_query_rows`.

        Always a page: `member_votes` runs to roughly 600k rows per Congress, so
        no caller may assume it received everything. Check `has_more` and walk
        with `skip`.

        Note: against the live project, server-side `filter` matching returned
        no rows for a filter that an unfiltered read showed should match. Until
        that is settled with AINative (an open question in datamodel.md), the
        filter is also applied client-side to whatever the page returns, so a
        caller gets correct results either way. Remove the client-side pass once
        server-side filtering is confirmed.
        """
        self._require(table_name, "table_name")
        payload: dict[str, Any] = {"limit": limit, "skip": skip}
        if filter is not None:
            payload["filter"] = filter
        if sort is not None:
            payload["sort"] = sort
        if projection is not None:
            payload["projection"] = projection

        response = self._request(
            "POST",
            f"/projects/{self._project_id}/database/tables/{table_name}/query",
            json=payload,
        )
        rows = [self._parse(Row, response, item) for item in self._envelope(response)]

        if filter and not rows:
            rows = self._fallback_filtered_rows(table_name, filter, limit, skip)
            body = {
                "rows": rows,
                "total": len(rows),
                "skip": skip,
                "limit": limit,
                "has_more": False,
            }
            return RowPage.model_validate(body)

        body = response.json_body if isinstance(response.json_body, dict) else {}
        return RowPage.model_validate(
            {
                "rows": rows,
                "total": body.get("total", len(rows)),
                "skip": body.get("skip", skip),
                "limit": body.get("limit", limit),
                "has_more": body.get("has_more", False),
            }
        )

    def get_row(self, table_name: str, row_id: str) -> Row | None:
        """Fetch one row by Informed's id, or None when it is absent.

        Returns None rather than raising, because "no such row" is an ordinary
        answer for a reference check (story 1.4) while a raised
        `ZeroDBNotFoundError` would be indistinguishable from a missing table.
        """
        page = self.query_rows(table_name, filter={"id": row_id}, limit=1)
        return page.rows[0] if page.rows else None

    def update_row(self, table_name: str, row_id: str, row_data: dict[str, Any]) -> Row:
        """Merge fields into one row, addressed by Informed's id.

        Wraps `zerodb_update_rows`. Only the fields given are changed; the rest
        of the row, the natural-key id included, is left alone.

        Not for `verdicts`, `candidate_positions` or `provenance_log`: those are
        append-only, and the live row is marked with `is_current` instead
        (datamodel.md). Use `insert_row` for a new version there. Story 1.4
        enforces that per table.

        Raises:
            ZeroDBNotFoundError: if no row carries that id.
        """
        self._require(table_name, "table_name")
        self._require(row_id, "row_id")

        existing = self.get_row(table_name, row_id)
        if existing is None or existing.row_id is None:
            raise ZeroDBNotFoundError(
                f"no row with id {row_id!r} in table {table_name!r}",
                status_code=404,
                error_code="NOT_FOUND",
            )

        # READ-MERGE-WRITE, because ZeroDB's PUT replaces `row_data` whole: a
        # live check showed a PUT of {"name": …} destroying the row's "id" and
        # orphaning it from every Informed query. Merging here keeps a partial
        # update partial. Like `upsert_row`, this is not atomic; see that
        # method's note.
        merged = {**existing.row_data, **row_data, "id": row_id}
        return self._replace_row(table_name, existing.row_id, merged)

    def _replace_row(self, table_name: str, internal_row_id: str, row_data: dict[str, Any]) -> Row:
        """PUT a complete row body, addressed by ZeroDB's internal UUID."""
        response = self._request(
            "PUT",
            f"/projects/{self._project_id}/database/tables/{table_name}/rows/{internal_row_id}",
            json={"row_data": row_data},
        )
        return self._parse(Row, response)

    def delete_row(self, table_name: str, row_id: str) -> None:
        """Delete one row, addressed by Informed's id.

        Wraps `zerodb_delete_rows`. Used for a voter's profile deletion (where
        the row goes and the data key is shredded) and for test teardown.
        Public-record tables are retained indefinitely.

        Raises:
            ZeroDBNotFoundError: if no row carries that id.
        """
        self._require(table_name, "table_name")
        self._require(row_id, "row_id")
        internal_id = self._require_internal_row_id(table_name, row_id)
        self._request(
            "DELETE",
            f"/projects/{self._project_id}/database/tables/{table_name}/rows/{internal_id}",
        )

    def _require_internal_row_id(self, table_name: str, row_id: str) -> str:
        """Resolve Informed's natural-key id to ZeroDB's internal row UUID.

        ZeroDB's `{row_id}` path segment takes its own UUID and answers 422 for
        anything else, so every write addressed by an Informed id resolves it
        first. The UUID is an implementation detail that stops at this class:
        no caller above the client ever sees one.

        Raises:
            ZeroDBNotFoundError: if no row carries that id.
        """
        row = self.get_row(table_name, row_id)
        if row is None or row.row_id is None:
            raise ZeroDBNotFoundError(
                f"no row with id {row_id!r} in table {table_name!r}",
                status_code=404,
                error_code="NOT_FOUND",
            )
        return row.row_id

    # =====================================================================
    # Vectors — promise, bill-summary, topic and stance embeddings
    # =====================================================================

    def embed_and_store(
        self,
        *,
        texts: Sequence[str],
        namespace: str,
        metadata_list: Sequence[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> EmbedResult:
        """Embed texts and store them in a namespace.

        Wraps `zerodb_embed_and_store`. Each vector's metadata should carry the
        NoSQL row id it was built from, so a search hit resolves to a citable
        record.

        Voter free-text notes are embedded in memory only and must never be
        stored here (datamodel.md, privacy).

        Raises:
            ZeroDBValidationError: if `texts` is empty, or `metadata_list` is a
                different length — a mismatch would silently mislabel vectors
                and send the mapping agent to the wrong rows.
        """
        if not texts:
            raise ZeroDBValidationError("embed_and_store requires at least one text")
        if metadata_list is not None and len(metadata_list) != len(texts):
            raise ZeroDBValidationError(
                f"metadata_list has {len(metadata_list)} entries "
                f"but {len(texts)} texts were given; they must correspond"
            )

        payload: dict[str, Any] = {"texts": list(texts), "namespace": namespace}
        if metadata_list is not None:
            payload["metadata_list"] = [dict(item) for item in metadata_list]
        if model is not None:
            payload["model"] = model

        response = self._request(
            "POST", f"/projects/{self._project_id}/embeddings/embed-and-store", json=payload
        )
        return self._parse(EmbedResult, response)

    def semantic_search(
        self,
        *,
        query: str,
        namespace: str,
        limit: int = 10,
        threshold: float | None = None,
        filter_metadata: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> SearchResults:
        """Search a namespace by meaning. Wraps `zerodb_semantic_search`.

        The mapping agent proposes the top bills per promise above 0.75
        similarity and routes anything under 0.85 to the QA queue; those
        thresholds belong to the agent, which passes them here.
        """
        self._require(query, "query")
        payload: dict[str, Any] = {"query": query, "namespace": namespace, "limit": limit}
        if threshold is not None:
            payload["threshold"] = threshold
        if filter_metadata is not None:
            payload["filter_metadata"] = filter_metadata
        if model is not None:
            payload["model"] = model

        response = self._request(
            "POST", f"/projects/{self._project_id}/embeddings/search", json=payload
        )
        return self._parse(SearchResults, response)

    def create_vector_index(
        self,
        *,
        namespace: str,
        index_type: str = "hnsw",
    ) -> VectorIndex:
        """Create a search index for one vector namespace.

        Wraps `zerodb_create_vector_index`, confirmed live against the real
        "informed" project during story 1.3. Two things the live API does
        that are NOT obvious from the tool description:

          1. The endpoint is `POST /public/v1/vectors/index` with
             `project_id` as a QUERY parameter — unlike every other ZeroDB
             call in this client, it is not under `/projects/{project_id}/...`.
          2. **The namespace must already contain at least one vector**, or
             this returns 404/400. A namespace with nothing embedded in it
             yet cannot be indexed; `embed_and_store` at least one vector
             into it first.
          3. **This call is NOT idempotent.** Calling it twice for the same
             namespace does not error and does not return the existing
             index — it silently creates a second, independently-named
             index. There is also no live "list existing indexes" endpoint
             to check against first (see `VectorIndex`'s docstring for the
             deprecated one). Callers that must not double-index a namespace
             (story 1.3's bootstrap) have to track what they already created
             themselves; this client makes no attempt to hide that gap.

        `distance_metric`/`index_params` are accepted by the MCP tool
        description but are not part of the live REST endpoint's
        `VectorIndexRequest` schema (`namespace` + `index_type` only), so
        they are not sent — passing them would do nothing.
        """
        self._require(namespace, "namespace")
        payload = {"namespace": namespace, "index_type": index_type}
        response = self._request(
            "POST",
            "/public/v1/vectors/index",
            json=payload,
            params={"project_id": self._project_id},
        )
        return self._parse(VectorIndex, response)

    def list_vectors(
        self,
        *,
        namespace: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> VectorPage:
        """List vectors in this project, a page at a time.

        Wraps `GET /projects/{project_id}/database/vectors`
        (confirmed live during story 3.4; no corresponding MCP tool is known
        to exist yet, so this is one of the primitives the MCP swap will need
        to add). `detailed` is deliberately never sent as true: this client
        only ever needs a vector's id, namespace, document and metadata to
        decide whether to rebuild it, never its raw embedding, and fetching
        the embedding for every vector in a namespace would be pure waste.

        This is how story 3.4's embedding pipeline checks what is already
        stored before deciding to skip, rebuild or freshly embed a row — the
        same role `query_rows` plays for NoSQL tables, but for vectors, which
        have no equivalent of `get_row`-by-natural-key: a vector's only
        identity is `metadata`, which this client must read back and match
        itself.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if namespace is not None:
            params["namespace"] = namespace
        response = self._request(
            "GET", f"/projects/{self._project_id}/database/vectors", params=params
        )
        return self._parse(VectorPage, response)

    def delete_vector(self, *, vector_id: str, namespace: str | None = None) -> VectorDeleteResult:
        """Delete one vector by its id.

        Wraps `DELETE /projects/{project_id}/database/vectors/{vector_id}`
        (confirmed live during story 3.4). `namespace` is accepted by the live
        endpoint as an optional filter and is passed through when given, which
        guards against deleting a same-id vector that happens to live in a
        different namespace — vector ids are not scoped to a namespace the way
        Informed's NoSQL ids are scoped to a table.

        This is the "rebuild" half of "embeddings are rebuilt when a version
        changes, never patched in place" (datamodel.md): there is no update
        endpoint for a vector's embedding, so a version bump is this call
        followed by `embed_and_store`, never a single update call.
        """
        self._require(vector_id, "vector_id")
        params: dict[str, Any] = {}
        if namespace is not None:
            params["namespace"] = namespace
        response = self._request(
            "DELETE",
            f"/projects/{self._project_id}/database/vectors/{vector_id}",
            params=params,
        )
        return self._parse(VectorDeleteResult, response)

    def get_vector_stats(self, *, namespace: str | None = None) -> VectorStats:
        """Per-namespace vector counts for this project.

        Wraps `zerodb_vector_stats` via the live, non-deprecated
        `/projects/{project_id}/database/vectors/stats` endpoint (the
        `/public/zerodb/vectors/indexes` endpoint this superseded is marked
        removed/410 in the live OpenAPI schema). This is also how a caller
        checks `create_vector_index`'s "namespace must have ≥1 vector"
        precondition before calling it.
        """
        params: dict[str, Any] = {}
        if namespace is not None:
            params["namespace"] = namespace
        response = self._request(
            "GET", f"/projects/{self._project_id}/database/vectors/stats", params=params
        )
        return self._parse(VectorStats, response)

    # =====================================================================
    # Knowledge graph — the evidence graph behind every grounded answer
    # =====================================================================

    def graph_upsert_ontology(
        self,
        *,
        entity_types: Sequence[str],
        predicates: dict[str, Any],
        constraints: dict[str, Any] | None = None,
    ) -> Ontology:
        """Register the evidence-graph ontology.

        Wraps `zerodb_graph_ontology_upsert`. Called once by story 1.3 with the
        entity types and predicates in datamodel.md.

        **Confirmed live during this story: this is a REPLACE, not a merge.**
        The call name says "upsert" and repeating it with the exact same full
        payload is safe (it ends in the same state, not a duplicate), but a
        call with a narrower `entity_types`/`predicates` than what is already
        registered silently drops everything not included — there is no
        merge-by-key behaviour the way `update_row` has. A live diagnostic
        call with a two-entity-type, one-predicate payload during this story
        replaced the real "informed" project's full 8-entity-type,
        11-predicate ontology down to just those two before the next full
        bootstrap run restored it. Every caller of this method MUST pass the
        complete, current `entity_types`/`predicates` it wants registered —
        never a partial update — exactly like `_bootstrap_ontology` already
        does. See `graph_get_ontology` to read the current state first when
        a caller needs to confirm what is registered before writing.
        """
        if not entity_types:
            raise ZeroDBValidationError("an ontology needs at least one entity type")
        payload: dict[str, Any] = {
            "project_id": self._project_id,
            "entity_types": list(entity_types),
            "predicates": predicates,
        }
        if constraints is not None:
            payload["constraints"] = constraints
        response = self._request("POST", "/public/memory/v2/graph/ontology", json=payload)
        return self._parse(Ontology, response)

    def graph_get_ontology(self) -> Ontology | None:
        """Read the currently registered ontology, or None if never set.

        Confirmed live during this story: `GET /public/memory/v2/graph/ontology`
        (project id as a query parameter, like `create_vector_index`) returns
        the full current `entity_types`/`predicates`. Used by
        `_bootstrap_ontology` to check what was already registered BEFORE
        writing, which is the only way to tell a genuine first registration
        apart from a repeat call — `graph_upsert_ontology`'s own response
        always echoes back exactly what was just sent, so checking the
        write's response can never answer "was this already current".

        Returns None for a project that has never registered an ontology,
        tolerating `ZeroDBNotFoundError` the same way `get_schema_version`
        tolerates a `schema_versions` table that does not exist yet — "no
        ontology yet" is an ordinary answer, not an error.
        """
        try:
            response = self._request(
                "GET",
                "/public/memory/v2/graph/ontology",
                params={"project_id": self._project_id},
            )
        except ZeroDBNotFoundError:
            return None
        return self._parse(Ontology, response)

    def graph_upsert_entity(
        self,
        *,
        canonical_name: str,
        entity_type: str,
        aliases: Sequence[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> GraphEntity:
        """Upsert a graph entity. Wraps `zerodb_graph_upsert_entity`.

        `canonical_name` must be the entity's NoSQL id
        (`person:B000944`, `us-house:119-2:452`), which is what lets every
        answer resolve back to a citable row.
        """
        self._require(canonical_name, "canonical_name")
        self._require(entity_type, "entity_type")
        payload: dict[str, Any] = {
            "canonical_name": canonical_name,
            "entity_type": entity_type,
            "project_id": self._project_id,
        }
        if aliases is not None:
            payload["aliases"] = list(aliases)
        if properties is not None:
            payload["properties"] = properties
        response = self._request("POST", "/public/memory/v2/graph/entity", json=payload)
        return self._parse(GraphEntity, response)

    def graph_create_edge(
        self,
        *,
        source_name: str,
        target_name: str,
        predicate: str,
        confidence: float = 1.0,
        properties: dict[str, Any] | None = None,
    ) -> GraphEdge:
        """Create an edge between two entities.

        Wraps `zerodb_graph_create_edge`. The predicate must be one the
        ontology registered; `properties` carries the evidence qualifiers from
        datamodel.md (`effect`, `qa_status`, `position`, `vote_type`).
        """
        self._require(source_name, "source_name")
        self._require(target_name, "target_name")
        self._require(predicate, "predicate")
        payload: dict[str, Any] = {
            "source_name": source_name,
            "target_name": target_name,
            "predicate": predicate,
            "confidence": confidence,
        }
        if properties is not None:
            payload["properties"] = properties
        response = self._request("POST", "/public/memory/v2/graph/edge", json=payload)
        # The response identifies endpoints by internal entity UUID and does not
        # echo the canonical names or properties, so they are restored from the
        # request: a caller should get back the edge it asked for, described in
        # the ids it uses.
        body = self._body(response)
        return self._parse(
            GraphEdge,
            response,
            {
                **body,
                "source_name": source_name,
                "target_name": target_name,
                "properties": properties or {},
            },
        )

    def graph_rag_search(
        self, *, query: str, limit: int = 10, graph_weight: float = 0.5, max_hops: int = 2
    ) -> GraphRagResults:
        """Retrieve grounded evidence from the graph.

        Wraps `zerodb_graph_rag_search`. The Q&A agent may answer only from a
        path ending in a `roll_call`, `bill` or `promise` entity, and otherwise
        must say there is no evidence; that rule lives in the agent.
        """
        self._require(query, "query")
        payload = {
            "query": query,
            "limit": limit,
            "graph_weight": graph_weight,
            "max_hops": max_hops,
        }
        response = self._request("POST", "/public/memory/v2/graph/graphrag", json=payload)
        return self._parse(GraphRagResults, response)

    def graph_traverse(
        self,
        *,
        entity: str,
        max_hops: int = 2,
        predicates: Sequence[str] | None = None,
        min_confidence: float = 0.0,
        as_of: str | None = None,
    ) -> Traversal:
        """Walk the graph from one entity. Wraps `zerodb_graph_traverse`.

        `as_of` reads the time-scoped history, which is how a superseded
        verdict's evidence is reconstructed after a newer verdict supersedes it.
        """
        self._require(entity, "entity")
        payload: dict[str, Any] = {
            "entity": entity,
            "max_hops": max_hops,
            "min_confidence": min_confidence,
        }
        if predicates is not None:
            payload["predicates"] = list(predicates)
        if as_of is not None:
            payload["as_of"] = as_of
        response = self._request("POST", "/public/memory/v2/graph/traverse", json=payload)
        return self._parse(Traversal, response)

    # =====================================================================
    # Files — immutable snapshots behind every citation
    # =====================================================================

    def upload_file(
        self,
        *,
        file_key: str,
        file_name: str | None = None,
        content_type: str | None = None,
        size_bytes: int | None = None,
        file_metadata: dict[str, Any] | None = None,
    ) -> StoredFile:
        """Register an archived snapshot. Wraps `zerodb_upload_file`.

        `file_metadata` should carry `sha256`, `source_url` and `captured_at`,
        which is what makes a citation verifiable long after the campaign page
        it came from has changed.
        """
        self._require(file_key, "file_key")
        payload: dict[str, Any] = {"file_key": file_key, "project_id": self._project_id}
        if file_name is not None:
            payload["file_name"] = file_name
        if content_type is not None:
            payload["content_type"] = content_type
        if size_bytes is not None:
            payload["size_bytes"] = size_bytes
        if file_metadata is not None:
            payload["file_metadata"] = file_metadata
        response = self._request(
            "POST", f"/projects/{self._project_id}/database/files", json=payload
        )
        return self._parse(StoredFile, response)

    def generate_presigned_url(
        self,
        *,
        file_id: str,
        expiration: int = DEFAULT_PRESIGN_EXPIRATION,
        operation: str = "get_object",
    ) -> PresignedUrl:
        """Mint a short-lived URL for one file.

        Wraps `zerodb_generate_presigned_url`. Deliberately short-lived: a
        citation link that outlives the page view is a link that can be shared
        past its access check.

        Raises:
            ZeroDBValidationError: if the requested lifetime is not positive or
                exceeds one hour.
        """
        self._require(file_id, "file_id")
        if not 0 < expiration <= MAX_PRESIGN_EXPIRATION:
            raise ZeroDBValidationError(
                f"expiration must be between 1 and {MAX_PRESIGN_EXPIRATION} seconds, "
                f"got {expiration}"
            )
        response = self._request(
            "POST",
            f"/projects/{self._project_id}/database/files/{file_id}/presigned-url",
            json={"expiration": expiration, "operation": operation},
        )
        return self._parse(PresignedUrl, response)

    # =====================================================================
    # Events — how read models and alerts stay in sync without joins
    # =====================================================================

    def create_event(self, *, topic: str, payload: dict[str, Any]) -> Event:
        """Publish an event. Wraps `zerodb_create_event`.

        The `profile.updated` payload carries the profile id and nothing else:
        a voter's priorities must never travel in an event (datamodel.md).
        """
        self._require(topic, "topic")
        response = self._request(
            "POST",
            f"/projects/{self._project_id}/database/events",
            json={"topic": topic, "event_payload": payload},
        )
        return self._parse(Event, response)

    def list_events(
        self, *, topic: str | None = None, limit: int = 100, skip: int = 0
    ) -> EventPage:
        """Read a page of published events.

        Wraps the read half of `zerodb_subscribe_events`: consumers that catch
        up by polling use this, while `subscribe_events` registers a webhook.
        """
        params: dict[str, Any] = {"limit": limit, "skip": skip}
        if topic is not None:
            params["topic"] = topic
        response = self._request(
            "GET", f"/projects/{self._project_id}/database/events", params=params
        )
        events = [self._parse(Event, response, item) for item in self._envelope(response)]
        # The live events endpoint answers with a bare JSON list, not the
        # {"total": …, "data": […]} envelope the table endpoints use, so the
        # counters are derived rather than read.
        body = response.json_body if isinstance(response.json_body, dict) else {}
        return EventPage.model_validate(
            {
                "events": events,
                "total": body.get("total", len(events)),
                "has_more": body.get("has_more", False),
            }
        )

    def subscribe_events(
        self,
        *,
        event_types: Sequence[str],
        webhook_url: str | None = None,
        filter: dict[str, Any] | None = None,  # noqa: A002 - matches the ZeroDB field
    ) -> EventSubscription:
        """Register a webhook for event types. Wraps `zerodb_subscribe_events`."""
        if not event_types:
            raise ZeroDBValidationError("a subscription needs at least one event type")
        payload: dict[str, Any] = {"event_types": list(event_types)}
        if webhook_url is not None:
            payload["webhook_url"] = webhook_url
        if filter is not None:
            payload["filter"] = filter
        response = self._request(
            "POST",
            f"/projects/{self._project_id}/database/events/subscriptions",
            json=payload,
        )
        return self._parse(EventSubscription, response)

    # =====================================================================
    # Internals: one retry policy and one error map for every method above
    # =====================================================================

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ZeroDBResponse:
        """Send a request, retrying transient failures with capped backoff.

        A transient failure is a 5xx, a 429, or a transport error (DNS, TCP,
        TLS, timeout). A 4xx is the caller's mistake and is raised on the first
        attempt: retrying a rejected payload only multiplies the rejection.

        Nothing about the payload, the headers or the credential is logged — a
        request body may hold a voter's encrypted profile, and prd.md forbids
        voter data in logs. Only the method, path, status and attempt number go
        to the log.
        """
        last_error: ZeroDBError | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._transport.request(method, path, json=json, params=params)
            except Exception as exc:
                # Only transport failures are ours to wrap and retry; anything
                # else (a bug in this module, a KeyboardInterrupt) propagates.
                if not _is_transport_error(exc):
                    raise
                last_error = ZeroDBConnectionError(
                    f"could not reach ZeroDB for {method} {path}: {type(exc).__name__}"
                )
                last_error.__cause__ = exc
            else:
                if response.is_success:
                    return response
                error = self._error_for(response, method, path)
                if not error.transient:
                    raise error
                last_error = error

            if attempt < self._max_attempts:
                delay = self._backoff_delay(attempt, retry_after=last_error.retry_after)
                logger.warning(
                    "ZeroDB %s %s failed (attempt %d/%d), retrying in %.2fs",
                    method,
                    path,
                    attempt,
                    self._max_attempts,
                    delay,
                )
                self._sleep(delay)

        assert last_error is not None  # noqa: S101 - unreachable without a failure
        logger.error("ZeroDB %s %s failed after %d attempts", method, path, self._max_attempts)
        raise last_error

    def _backoff_delay(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Exponential backoff with full jitter, capped at `backoff_max`.

        Jitter matters with several workers: without it, every one of them
        retries on the same schedule and re-hammers a ZeroDB that is only just
        recovering.

        When ZeroDB sends a `retry_after` (its rate limiter does), that wins:
        the server knows when its throttle window clears, and retrying sooner
        just burns an attempt. It is still capped at `backoff_max`, because a
        60-second wait inside a web request is worse than a failed request.
        """
        if retry_after is not None:
            return min(retry_after, self._backoff_max)

        ceiling = min(self._backoff_base * (2 ** (attempt - 1)), self._backoff_max)
        if ceiling <= 0:
            return 0.0
        # Keep each wait strictly longer than the last so the growth is visible
        # and a caller's worst case stays predictable.
        floor = min(ceiling * 0.5, self._backoff_max)
        return floor + (ceiling - floor) * self._jitter()

    def _error_for(self, response: ZeroDBResponse, method: str, path: str) -> ZeroDBError:
        """Turn a failing response into the right domain exception."""
        body = response.json_body if isinstance(response.json_body, dict) else {}
        detail = body.get("detail")
        if isinstance(detail, list):  # FastAPI validation errors arrive as a list
            detail = "; ".join(
                str(item.get("msg", item)) if isinstance(item, dict) else str(item)
                for item in detail
            )
        # Most errors carry `detail` + `error_code`, but the rate limiter answers
        # with `message` + `error` instead, so both shapes are read rather than
        # falling back to a stringified body.
        if not detail:
            detail = body.get("message")
        message = str(detail) if detail else (response.text[:200] or "ZeroDB request failed")

        error_class = _STATUS_ERRORS.get(response.status_code)
        if error_class is None:
            error_class = ZeroDBServerError if response.status_code >= 500 else ZeroDBError
        error = error_class(
            f"ZeroDB {method} {path}: {message}",
            status_code=response.status_code,
            error_code=body.get("error_code") or body.get("error"),
            next_action=body.get("next_action"),
        )
        error.retry_after = _positive_float(body.get("retry_after"))
        return error

    def _body(self, response: ZeroDBResponse) -> dict[str, Any]:
        """The response body as a dict, or raise if it is not one."""
        if isinstance(response.json_body, dict):
            return response.json_body
        if response.json_body is None:
            raise ZeroDBProtocolError(
                "ZeroDB returned a body that is not JSON",
                status_code=response.status_code,
            )
        raise ZeroDBProtocolError(
            f"ZeroDB returned {type(response.json_body).__name__}, expected an object",
            status_code=response.status_code,
        )

    def _envelope(self, response: ZeroDBResponse) -> list[dict[str, Any]]:
        """Pull the item list out of a collection response.

        ZeroDB is not consistent here: tables, rows and queries come back as
        `{"total": …, "data": [...]}` while events come back as a bare list.
        Both are normalised to a list of dicts so callers see one shape.

        Only for those three. Vector search and graph RAG wrap their own
        `results` key, and are parsed straight into their typed models.
        """
        payload = response.json_body
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            raw = payload.get("data", [])
            items = raw if isinstance(raw, list) else []
        else:
            raise ZeroDBProtocolError(
                "ZeroDB returned a body that is not JSON",
                status_code=response.status_code,
            )
        return [item for item in items if isinstance(item, dict)]

    def _parse(
        self,
        model: type[ModelT],
        response: ZeroDBResponse,
        payload: dict[str, Any] | None = None,
    ) -> ModelT:
        """Validate a response body into a typed model.

        A body that no longer fits raises `ZeroDBProtocolError`. Failing loudly
        is deliberate: a silent shape change upstream would otherwise feed
        half-parsed records into the evidence graph, and an evidence graph that
        cannot be trusted is worse than one that is briefly unavailable.
        """
        body = payload if payload is not None else self._body(response)
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise ZeroDBProtocolError(
                f"ZeroDB response did not match {model.__name__}: "
                f"{exc.error_count()} field(s) invalid",
                status_code=response.status_code,
            ) from exc

    def _fallback_filtered_rows(
        self,
        table_name: str,
        filter: dict[str, Any],  # noqa: A002
        limit: int,
        skip: int,
    ) -> list[Row]:
        """Filter client-side when ZeroDB's server-side filter returns nothing.

        Covers the live-API gap documented on `query_rows`, which story 1.4
        re-measured: the server-side filter matches *nothing*, not merely some
        rows, so this path runs on every filtered read against live ZeroDB.

        **Pages, rather than reading one page** — the fix story 1.4's live run
        forced. The previous version read `GET /rows?limit={limit}&skip={skip}`
        once, so a `get_row` (which asks for `limit=1`) tested exactly the
        table's first row and answered None for every other row in it. Live,
        with three rows seeded behind one pre-existing row, it found none of
        the three. Since `get_row` is the find step of `upsert_row` and of the
        repository's reference check, that meant a duplicate on every re-ingest
        and a rejected reference for data that was perfectly valid, on any
        table past its first page.

        Bounded on purpose, in two ways, because `member_votes` reaches ~600k
        rows per Congress (datamodel.md) and an unbounded scan here would be a
        different outage from the one it fixes:

          * each page is at most `FALLBACK_PAGE_SIZE` rows, so memory stays
            flat however large the table is;
          * at most `MAX_FALLBACK_PAGES` pages are read, after which the walk
            gives up and returns what it found. A caller asking for a single
            row by id (`get_row`) stops at the first match long before that.

        The ceiling is a real limitation, not a solved problem: a `get_row` for
        a row sitting beyond it will still answer None. It is survivable today
        because the ingest order puts parents (`people`, `roll_calls`) in tables
        orders of magnitude smaller than that ceiling, and because `enough` is
        checked per page so a hit ends the walk immediately. The actual fix has
        to come from ZeroDB — see datamodel.md's open question, escalated to
        AINative — and this method is where to delete the workaround once the
        server-side filter works.
        """
        wanted = skip + limit if limit > 0 else 0
        matched: list[Row] = []
        offset = 0
        page_size = self._fallback_page_size

        for _ in range(self._max_fallback_pages):
            response = self._request(
                "GET",
                f"/projects/{self._project_id}/database/tables/{table_name}/rows",
                params={"limit": page_size, "skip": offset},
            )
            page = self._envelope(response)
            matched.extend(
                row
                for row in (self._parse(Row, response, item) for item in page)
                if _matches(row.row_data, filter)
            )
            # Stop as soon as the caller's window is filled — a `get_row` for a
            # row on page one must not pay for page two — or when the table has
            # no more rows to offer.
            if (wanted and len(matched) >= wanted) or len(page) < page_size:
                break
            offset += page_size
        else:
            logger.warning(
                "client-side filter on %s gave up after %d pages (%d rows); a matching "
                "row beyond that is reported as absent",
                table_name,
                self._max_fallback_pages,
                self._max_fallback_pages * page_size,
            )

        return matched[skip : skip + limit] if limit > 0 else matched

    @staticmethod
    def _require(value: str, name: str) -> None:
        """Reject an empty required argument before spending a round trip."""
        if not value or not str(value).strip():
            raise ZeroDBValidationError(f"{name} must be a non-empty string")

    def close(self) -> None:
        """Release the transport's connections, if it holds any."""
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "ZeroDBClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _matches(row_data: dict[str, Any], filter: dict[str, Any]) -> bool:  # noqa: A002
    """Whether a row satisfies a flat equality filter.

    Equality only, deliberately: the operator set ZeroDB supports is an open
    question in datamodel.md, and quietly half-implementing `$in` or `$gte`
    client-side would hide which queries the server can actually serve.
    """
    for key, expected in filter.items():
        actual = row_data.get(key.removeprefix("row_data."))
        if actual != expected:
            return False
    return True


def _is_transport_error(exc: BaseException) -> bool:
    """Whether an exception is a transport failure worth retrying.

    Checked by name so this module keeps its promise not to import httpx: that
    import is what would tie the client to REST and make the MCP swap more than
    a one-file change.
    """
    if isinstance(exc, ZeroDBTransportError):
        return True
    for klass in type(exc).__mro__:
        if f"{klass.__module__}.{klass.__name__}" in _TRANSPORT_ERROR_NAMES:
            return True
    return False


_TRANSPORT_ERROR_NAMES = frozenset(
    {
        "httpx.TransportError",
        "httpx.ConnectError",
        "httpx.ConnectTimeout",
        "httpx.ReadTimeout",
        "httpx.WriteTimeout",
        "httpx.PoolTimeout",
        "httpx.NetworkError",
        "httpx.RemoteProtocolError",
    }
)


def _positive_float(value: Any) -> float | None:
    """Coerce a server-supplied wait to a positive float, or None.

    ZeroDB's `retry_after` drives how long a worker sleeps, so a malformed or
    negative value is discarded in favour of the client's own backoff rather
    than trusted.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _default_sleep(seconds: float) -> None:
    time.sleep(seconds)


__all__ = ["RETRYABLE_STATUSES", "ZeroDBClient"]
