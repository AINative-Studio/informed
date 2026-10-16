"""Typed models for ZeroDB responses (story 1.2).

Every field here was checked against the live "informed" project, so the models
describe what ZeroDB actually returns rather than what the docs imply. They are
deliberately tolerant of extra keys (`extra="ignore"`): ZeroDB adds fields over
time, and an unknown field is not a reason to fail an ingest run. Missing
*required* fields are a different matter — those raise `ZeroDBProtocolError` in
the client, because a shape change must not pass silently into the evidence
graph.

These models describe the transport envelope only. Informed's own row schemas
(people, roll_calls, promises …) are the repository layer's business in story
1.4; this client moves opaque `row_data` dicts.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _ZeroDBModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# --- NoSQL tables ---------------------------------------------------------


class Table(_ZeroDBModel):
    """A NoSQL table as ZeroDB reports it."""

    table_name: str
    table_id: str | None = None
    schema_definition: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None


class Row(_ZeroDBModel):
    """One row. `row_data` carries Informed's own fields, id included.

    `row_id` is ZeroDB's internal UUID, which is *not* Informed's id: the
    deterministic natural-key id (`us-house:119-2:452`) lives in
    `row_data["id"]`. Story 1.4 addresses rows by that id, never by `row_id`.
    """

    row_data: dict[str, Any]
    row_id: str | None = None
    table_name: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class RowPage(_ZeroDBModel):
    """A page of rows plus the counters needed to walk a large table.

    `member_votes` reaches roughly 600k rows per Congress, so every read that
    could be large is paged rather than fetched whole (open question in
    datamodel.md; story 16.1 sets the limits).
    """

    rows: list[Row] = Field(default_factory=list)
    total: int = 0
    skip: int = 0
    limit: int = 0
    has_more: bool = False


# --- vectors --------------------------------------------------------------


class EmbedResult(_ZeroDBModel):
    """The outcome of embedding and storing texts in one namespace.

    The live project reports `bge-m3` at 1024 dimensions, not the 768 named in
    datamodel.md. The model and dimension count are read from the response
    rather than assumed, and story 1.3 records whichever the project reports
    when it creates the vector indexes.
    """

    vectors_stored: int = 0
    embeddings_generated: int = 0
    namespace: str | None = None
    model: str | None = None
    dimensions: int | None = None
    success: bool = True


class SearchHit(_ZeroDBModel):
    """One semantic search result.

    `metadata` carries the NoSQL row id the vector was built from, so a hit
    resolves straight back to a citable record.
    """

    vector_id: str | None = None
    document: str | None = None
    namespace: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResults(_ZeroDBModel):
    """A semantic search response."""

    results: list[SearchHit] = Field(default_factory=list)
    query: str | None = None
    total_results: int = 0
    model: str | None = None


class VectorRecord(_ZeroDBModel):
    """One stored vector, as `list_vectors`/`get_vector` return it (story 3.4).

    Field names follow the live API exactly (`VectorResponse` in the live
    OpenAPI schema, confirmed during this story): `vector_metadata`, not
    `metadata` — different from `SearchHit`, which wraps a *search* response
    where the live field really is `metadata`. Two different response shapes
    for "a vector", so two different models; collapsing them would mean one
    of the two field names is a guess rather than a verified one.
    """

    vector_id: str
    namespace: str = "default"
    document: str | None = None
    vector_metadata: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None
    created_at: str | None = None


class VectorPage(_ZeroDBModel):
    """A page of a namespace's vectors. Wraps `list_vectors`.

    Mirrors `RowPage`'s shape for the same reason: a namespace can hold more
    vectors than fit in one page (`stances` already runs to 200+), so every
    caller must check `has_more` and walk with `offset` rather than assume a
    first page is everything.
    """

    vectors: list[VectorRecord] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 0
    has_more: bool = False


class VectorDeleteResult(_ZeroDBModel):
    """The outcome of deleting one vector. Wraps `delete_vector`.

    The live endpoint's response is just `{"message": ...}`
    (`DeleteVectorResponse` in the live OpenAPI schema, confirmed during story
    3.4) — there is nothing else to parse out of it.
    """

    message: str | None = None


class VectorIndex(_ZeroDBModel):
    """A search index created over one vector namespace.

    Field names and shape are exactly what the live API returns (confirmed
    against the real "informed" project during story 1.3, not inferred from
    the OpenAPI summary): `{index_name, index_type, namespace, created_at,
    status}`. There is no `dimensions` or `distance_metric` in the response —
    the live endpoint does not echo either back, unlike `EmbedResult`, which
    does report dimensions.

    IMPORTANT, confirmed live: calling `create_vector_index` twice for the
    same namespace does NOT error and does NOT return the existing index —
    it silently creates a second, differently-named index
    (`idx_{namespace}_{index_type}_{timestamp}`). This endpoint is NOT
    idempotent on ZeroDB's side, and — unlike tables — there is no
    "list existing indexes" endpoint to check against before creating one:
    the only such endpoint (`/public/zerodb/vectors/indexes`) is marked
    deprecated/removed (410) in the live OpenAPI schema, pointing instead at
    `/database/vectors/stats`, which reports per-namespace vector *counts*,
    not configured indexes. Story 1.3's bootstrap therefore tracks which
    namespaces it has already indexed in its own `schema_versions` row rather
    than asking ZeroDB, which is the only idempotency mechanism available
    given this gap.
    """

    namespace: str
    index_name: str | None = None
    index_type: str | None = None
    status: str | None = None
    created_at: str | None = None


class VectorStats(_ZeroDBModel):
    """Vector counts per namespace for one project.

    Wraps the live, non-deprecated `/projects/{project_id}/database/vectors/
    stats` endpoint. `namespaces` maps namespace name to its vector count;
    the create-index endpoint requires at least one vector to already exist
    in a namespace before an index can be created over it, so this is also
    how a caller checks that precondition before calling
    `ZeroDBClient.create_vector_index`.
    """

    total_count: int = 0
    namespaces: dict[str, int] = Field(default_factory=dict)
    avg_dimensions: float | None = None
    total_storage_bytes: int | None = None


# --- knowledge graph ------------------------------------------------------


class Ontology(_ZeroDBModel):
    """The registered evidence-graph ontology."""

    entity_types: list[str] = Field(default_factory=list)
    predicates: dict[str, Any] = Field(default_factory=dict)
    status: str | None = None


class GraphEntity(_ZeroDBModel):
    """A graph entity. `canonical_name` is the NoSQL id it stands for."""

    canonical_name: str
    entity_type: str
    id: str | None = None
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    status: str | None = None


class GraphEdge(_ZeroDBModel):
    """A relationship between two entities.

    The live API answers a create with `{id, source_id, target_id, predicate,
    confidence}`: it identifies the endpoints by their internal entity UUIDs
    and does NOT echo back the canonical names sent, nor the `properties`. So
    `source_name`/`target_name` are filled in by the client from the request it
    made, and are None on an edge parsed from any other response.
    """

    predicate: str
    id: str | None = None
    source_id: str | None = None
    target_id: str | None = None
    confidence: float | None = None
    source_name: str | None = None
    target_name: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphRagResults(_ZeroDBModel):
    """A grounded retrieval over the graph.

    The Q&A agent may answer only from a path ending in a `roll_call`, `bill` or
    `promise` entity; enforcing that is the agent's job, not the client's.
    """

    results: list[dict[str, Any]] = Field(default_factory=list)
    query: str | None = None


class TraversalNode(_ZeroDBModel):
    """One entity reached while traversing from a start entity.

    The live API returns nodes as `{id, label, type, properties}`, where
    `label` is the canonical name — which for Informed is the NoSQL row id.
    `entity` is aliased to it so callers read the id under the name they expect
    rather than ZeroDB's `label`.
    """

    entity: str = Field(validation_alias="label")
    entity_type: str | None = Field(default=None, validation_alias="type")
    id: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class Traversal(_ZeroDBModel):
    """Everything a bounded traversal reached.

    `nodes` includes the start entity itself, so a caller checking "did I reach
    X" should not assume every node is a hop away.
    """

    nodes: list[TraversalNode] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    paths: list[Any] = Field(default_factory=list)


# --- files ----------------------------------------------------------------


class StoredFile(_ZeroDBModel):
    """An archived snapshot behind a citation.

    `file_metadata` carries `sha256`, `source_url` and `captured_at`, which is
    what makes the snapshot verifiable years later. The live API returns it as
    null when none was supplied, so it is normalised to an empty dict rather
    than left as None for every caller to guard.
    """

    file_id: str
    file_key: str | None = None
    file_name: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    file_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None

    @field_validator("file_metadata", mode="before")
    @classmethod
    def _null_metadata_is_empty(cls, value: Any) -> Any:
        return {} if value is None else value


class PresignedUrl(_ZeroDBModel):
    """A short-lived URL the UI hands the browser for one file.

    The live API names the field `presigned_url` and reports the lifetime as
    `expiry_minutes` plus an `expires_at` timestamp. `url` is aliased to the
    former so callers read it under the obvious name.
    """

    url: str = Field(validation_alias="presigned_url")
    file_id: str | None = None
    file_name: str | None = None
    expires_at: str | None = None
    expiry_minutes: int | None = None
    operation_type: str | None = None

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


# --- events ---------------------------------------------------------------


class Event(_ZeroDBModel):
    """A published event. Read models and alerts are driven by these."""

    topic: str
    event_payload: dict[str, Any] = Field(default_factory=dict)
    event_id: str | None = None
    published_at: str | None = None


class EventPage(_ZeroDBModel):
    """A page of events."""

    events: list[Event] = Field(default_factory=list)
    total: int = 0
    has_more: bool = False


class EventSubscription(_ZeroDBModel):
    """A registered interest in one or more event types."""

    event_types: list[str] = Field(default_factory=list)
    subscription_id: str | None = None
    webhook_url: str | None = None


__all__ = [
    "EmbedResult",
    "Event",
    "EventPage",
    "EventSubscription",
    "GraphEdge",
    "GraphEntity",
    "GraphRagResults",
    "Ontology",
    "PresignedUrl",
    "Row",
    "RowPage",
    "SearchHit",
    "SearchResults",
    "StoredFile",
    "Table",
    "Traversal",
    "TraversalNode",
    "VectorDeleteResult",
    "VectorIndex",
    "VectorPage",
    "VectorRecord",
    "VectorStats",
]
