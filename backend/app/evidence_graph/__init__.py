"""Writing the evidence graph (story 13.1).

datamodel.md, "Knowledge graph ontology":

    The evidence graph mirrors the NoSQL records the grounded agent must cite.
    It is registered once with `zerodb_graph_ontology_upsert`, written by the
    ingest and verdict agents, and queried with `zerodb_graph_rag_search` and
    `zerodb_graph_traverse`. Each entity's `canonical_name` is its NoSQL id, so
    every answer resolves back to a citable row.

This package is the "written by the ingest and verdict agents" half. Nothing in
Informed should call `ZeroDBClient.graph_upsert_entity` or `graph_create_edge`
directly from an ingest module: those are the raw transport, and they will
happily write an edge the ontology never registered, or one whose endpoints are
the wrong way round. `EvidenceGraphWriter` is the one typed door, and it refuses
both before spending a request.

See `app.evidence_graph.writer` for the design argument and for what this does
NOT do yet (the `voted` edges that need real roll-call data).
"""

from app.evidence_graph.writer import (
    DOCUMENTED_PREDICATE_TABLE,
    EvidenceGraphError,
    EvidenceGraphWriter,
    OntologyReport,
    UnknownPredicateError,
    VoteEdgeResult,
    WriteReport,
    WrongEndpointTypeError,
)

__all__ = [
    "DOCUMENTED_PREDICATE_TABLE",
    "EvidenceGraphError",
    "EvidenceGraphWriter",
    "OntologyReport",
    "UnknownPredicateError",
    "VoteEdgeResult",
    "WriteReport",
    "WrongEndpointTypeError",
]
