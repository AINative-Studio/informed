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
]
