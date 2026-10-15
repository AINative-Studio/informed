"""In-memory fake ZeroDB transport for contract tests (story 1.2).

Story 1.2's acceptance criterion is that "contract tests run against an
in-memory fake". This is that fake: it implements `ZeroDBTransport`, holds
tables, rows, vectors, graph entities, edges, files and events in dicts, and
answers in the same envelope shapes the live "informed" project returns (each
one checked against the real API before this was written).

It ships in `app/` rather than `tests/` on purpose: stories 1.3, 1.4 and 1.5 all
need it, and later stories' fixtures should import one fake rather than grow
their own drifting copies.

What it is for:
  * asserting the exact request the client built — path, payload, headers;
  * driving failure paths (`fail_next`, `always_fail`) so retry and error
    mapping are tested without waiting on a real outage;
  * giving repositories (story 1.4) a ZeroDB to run against in CI with no
    credentials.

What it is NOT: a ZeroDB emulator. Semantic search returns stored documents
ranked by a crude token overlap, not real embeddings, and the graph does no
inference. A behaviour that depends on real ranking or real graph reasoning must
be checked against live ZeroDB in a story of its own, never asserted here.
"""

import json as json_module
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.zerodb.transport import RecordedCall, ZeroDBResponse


@dataclass
class _Failure:
    """A queued or standing failure the fake should produce."""

    status: int | None = None
    exception: BaseException | None = None
    #: Overrides the synthetic error body, for pinning a real payload shape
    #: (the rate limiter's, say) against the client's error mapping.
    body: dict[str, Any] | None = None


@dataclass
class FakeZeroDBTransport:
    """A ZeroDB stand-in that records calls and serves them from memory.

    Attributes:
        calls: Every request attempt, in order. Retries appear as separate
            entries, which is how the retry tests count attempts.
        tables: Table name to its schema definition.
        rows: Table name to ZeroDB row_id to the stored row envelope.
        vectors: Namespace to the vectors stored in it.
        graph_entities: Canonical name (an Informed NoSQL id) to the entity.
        graph_edges: The set of `(source, predicate, target)` triples created.
        files: File id to the stored file envelope.
        events: Published events, in order.
        subscriptions: Registered event subscriptions.
    """

    #: Detail text every synthetic failure carries, so a test can assert that
    #: ZeroDB's own message survives into the domain exception.
    FAILURE_DETAIL = "fake zerodb failure detail"

    #: The bearer token the fake pretends to hold. Tests assert this never
    #: reaches a log record.
    API_KEY = "fake-api-key-never-logged"

    calls: list[RecordedCall] = field(default_factory=list)
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    rows: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    vectors: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    graph_entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    graph_edges: set[tuple[str, str, str]] = field(default_factory=set)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    subscriptions: list[dict[str, Any]] = field(default_factory=list)
    ontology: dict[str, Any] = field(default_factory=dict)

    _queued_failures: list[_Failure] = field(default_factory=list, repr=False)
    _standing_failure: _Failure | None = field(default=None, repr=False)
    _non_json: bool = field(default=False, repr=False)
    _fixed_json: Any | None = field(default=None, repr=False)

    # --- failure injection ------------------------------------------------

    def fail_next(
        self, count: int, *, status: int | None = None, exception: BaseException | None = None
    ) -> None:
        """Fail the next `count` requests, then behave normally.

        This is how "retries a transient failure then succeeds" is tested
        without a real outage.
        """
        for _ in range(count):
            self._queued_failures.append(_Failure(status=status, exception=exception))

    def always_fail(
        self,
        *,
        status: int | None = None,
        exception: BaseException | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        """Fail every request from now on, to test an exhausted retry budget.

        Pass `body` to serve a captured real error payload instead of the
        synthetic one — the rate limiter's shape differs from every other
        ZeroDB error, and the client's mapping has to handle both.
        """
        self._standing_failure = _Failure(status=status, exception=exception, body=body)

    def always_return_non_json(self) -> None:
        """Answer 200 with a body that is not JSON, to test protocol errors."""
        self._non_json = True

    def always_return_json(self, body: Any) -> None:
        """Answer 200 with a fixed body, to test a response of the wrong shape."""
        self._fixed_json = body

    # --- the transport protocol -------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ZeroDBResponse:
        """Record the call, then either fail it or serve it from memory."""
        self.calls.append(
            RecordedCall(
                method=method,
                path=path,
                json=json,
                params=params,
                headers={
                    "Authorization": f"Bearer {self.API_KEY}",
                    "Content-Type": "application/json",
                },
            )
        )

        failure = self._next_failure()
        if failure is not None:
            if failure.exception is not None:
                raise failure.exception
            status = failure.status or 500
            error_body = failure.body or {
                "detail": self.FAILURE_DETAIL,
                "error_code": _ERROR_CODES.get(status, "ERROR"),
                "next_action": "retry",
            }
            return ZeroDBResponse(
                status_code=status,
                json_body=error_body,
                text=json_module.dumps(error_body),
            )

        if self._non_json:
            return ZeroDBResponse(status_code=200, json_body=None, text="<html>not json</html>")
        if self._fixed_json is not None:
            return ZeroDBResponse(
                status_code=200,
                json_body=self._fixed_json,
                text=json_module.dumps(self._fixed_json),
            )

        return self._route(method, path, json or {}, params or {})

    def _next_failure(self) -> _Failure | None:
        if self._queued_failures:
            return self._queued_failures.pop(0)
        return self._standing_failure

    # --- routing ----------------------------------------------------------

    def _route(
        self, method: str, path: str, body: dict[str, Any], params: dict[str, Any]
    ) -> ZeroDBResponse:
        segments = [segment for segment in path.split("/") if segment]

        if "graph" in segments:
            return self._route_graph(segments[-1], body)
        if "embeddings" in segments:
            return self._route_embeddings(segments[-1], body)
        if "tables" in segments:
            return self._route_tables(method, segments, body, params)
        if "files" in segments:
            return self._route_files(method, segments, body)
        if "events" in segments:
            return self._route_events(method, segments, body, params)

        return _ok({})

    # tables ---------------------------------------------------------------

    def _route_tables(
        self,
        method: str,
        segments: list[str],
        body: dict[str, Any],
        params: dict[str, Any],
    ) -> ZeroDBResponse:
        index = segments.index("tables")
        tail = segments[index + 1 :]

        if not tail:
            if method == "POST":
                name = body["table_name"]
                self.tables[name] = body.get("schema_definition") or {}
                self.rows.setdefault(name, {})
                return _ok(
                    {
                        "table_name": name,
                        "schema_definition": self.tables[name],
                        "table_id": str(uuid.uuid4()),
                    }
                )
            return _ok(
                {
                    "total": len(self.tables),
                    "data": [
                        {"table_name": name, "schema_definition": schema}
                        for name, schema in self.tables.items()
                    ],
                    "has_more": False,
                }
            )

        table = tail[0]
        if method == "DELETE" and len(tail) == 1:
            self.tables.pop(table, None)
            self.rows.pop(table, None)
            return _ok({"message": f"Table {table} deleted successfully"})

        if table not in self.tables:
            return _not_found(f"Table '{table}' not found")

        if tail[-1] == "query" and method == "POST":
            return self._query_rows(table, body)
        if tail[-1] == "rows" and method == "POST":
            return self._insert_row(table, body)
        if tail[-1] == "rows" and method == "GET":
            return self._list_rows(table, params)
        if len(tail) >= 3 and tail[1] == "rows":
            return self._row_by_internal_id(method, table, tail[2], body)

        return _ok({})

    def _insert_row(self, table: str, body: dict[str, Any]) -> ZeroDBResponse:
        """Append a row under a fresh internal row_id.

        ALWAYS APPENDS, because the live API always appends: ZeroDB has no
        upsert-by-natural-key, so two inserts carrying the same Informed `id`
        really do produce two rows. An earlier version of this fake replaced
        instead, which hid a duplicate-row bug in `ZeroDBClient.upsert_row`
        until a live check caught it. Making the upsert idempotent is the
        client's job (find-then-update or insert), not the fake's, and this
        method must keep appending so that job stays tested.
        """
        row_data = body["row_data"]
        store = self.rows.setdefault(table, {})
        row_id = str(uuid.uuid4())
        envelope = {"row_data": dict(row_data), "row_id": row_id, "table_name": table}
        store[row_id] = envelope
        return _ok(envelope)

    def _list_rows(self, table: str, params: dict[str, Any]) -> ZeroDBResponse:
        rows = list(self.rows.get(table, {}).values())
        skip = int(params.get("skip", 0) or 0)
        limit = int(params.get("limit", 100) or 100)
        page = rows[skip : skip + limit]
        return _ok(
            {
                "total": len(rows),
                "skip": skip,
                "limit": limit,
                "has_more": skip + len(page) < len(rows),
                "data": page,
            }
        )

    def _query_rows(self, table: str, body: dict[str, Any]) -> ZeroDBResponse:
        rows = list(self.rows.get(table, {}).values())
        query_filter = body.get("filter") or {}
        matched = [
            row
            for row in rows
            if all(
                row["row_data"].get(key.removeprefix("row_data.")) == value
                for key, value in query_filter.items()
            )
        ]
        skip = int(body.get("skip", 0) or 0)
        limit = int(body.get("limit", 100) or 100)
        page = matched[skip : skip + limit]
        return _ok(
            {
                "total": len(matched),
                "skip": skip,
                "limit": limit,
                "has_more": skip + len(page) < len(matched),
                "data": page,
            }
        )

    def _row_by_internal_id(
        self, method: str, table: str, row_id: str, body: dict[str, Any]
    ) -> ZeroDBResponse:
        """Address one row by ZeroDB's internal UUID.

        The live API rejects anything that is not a UUID here with 422
        "Input should be a valid UUID", so the fake does too: passing Informed's
        natural-key id was the second bug the live check caught, and this is
        what keeps the client honest about resolving the id first.
        """
        try:
            uuid.UUID(row_id)
        except ValueError:
            body_out = {
                "detail": [
                    {
                        "type": "uuid_parsing",
                        "loc": ["path", "row_id"],
                        "msg": "Input should be a valid UUID",
                    }
                ],
                "error_code": "VALIDATION_ERROR",
                "next_action": "fix_request",
            }
            return ZeroDBResponse(
                status_code=422, json_body=body_out, text=json_module.dumps(body_out)
            )

        store = self.rows.setdefault(table, {})
        if row_id not in store:
            return _not_found(f"Row '{row_id}' not found in '{table}'")

        if method == "DELETE":
            store.pop(row_id)
            return _ok({"message": "deleted"})
        if method == "PUT":
            # REPLACES row_data wholesale, as the live API does. It does not
            # merge: a live check showed a PUT of {"name": …} destroying the
            # row's "id" field and orphaning it from every Informed query.
            # Merging is therefore the client's job, and this must keep
            # replacing so that job stays tested.
            store[row_id]["row_data"] = dict(body["row_data"])
            return _ok(store[row_id])
        return _ok(store[row_id])

    # vectors --------------------------------------------------------------

    def _route_embeddings(self, action: str, body: dict[str, Any]) -> ZeroDBResponse:
        if action == "embed-and-store":
            namespace = body.get("namespace", "default")
            texts = body.get("texts", [])
            metadata_list = body.get("metadata_list") or [{} for _ in texts]
            bucket = self.vectors.setdefault(namespace, [])
            for text, metadata in zip(texts, metadata_list, strict=False):
                bucket.append(
                    {
                        "vector_id": str(uuid.uuid4()),
                        "document": text,
                        "namespace": namespace,
                        "metadata": dict(metadata),
                    }
                )
            return _ok(
                {
                    "success": True,
                    "vectors_stored": len(texts),
                    "embeddings_generated": len(texts),
                    "namespace": namespace,
                    "model": "bge-m3",
                    "dimensions": 1024,
                }
            )

        if action == "search":
            namespace = body.get("namespace", "default")
            query = body.get("query", "")
            limit = int(body.get("limit", 10) or 10)
            scored = [
                {**vector, "score": _overlap(query, vector["document"])}
                for vector in self.vectors.get(namespace, [])
            ]
            scored.sort(key=lambda hit: hit["score"], reverse=True)
            return _ok(
                {
                    "results": scored[:limit],
                    "query": query,
                    "total_results": len(scored[:limit]),
                    "model": "bge-m3",
                }
            )

        return _ok({})

    # graph ----------------------------------------------------------------

    def _route_graph(self, action: str, body: dict[str, Any]) -> ZeroDBResponse:
        if action == "ontology":
            self.ontology = {
                "entity_types": body.get("entity_types", []),
                "predicates": body.get("predicates", {}),
                "status": "upserted",
            }
            return _ok(self.ontology)

        if action == "entity":
            name = body["canonical_name"]
            entity = {
                "id": str(uuid.uuid4()),
                "canonical_name": name,
                "entity_type": body["entity_type"],
                "aliases": body.get("aliases") or [],
                "properties": body.get("properties") or {},
                "status": "created" if name not in self.graph_entities else "updated",
            }
            self.graph_entities[name] = entity
            return _ok(entity)

        if action == "edge":
            source = body["source_name"]
            target = body["target_name"]
            predicate = body["predicate"]
            for name in (source, target):
                if name not in self.graph_entities:
                    return _not_found(f"Target entity '{name}' not found")
            self.graph_edges.add((source, predicate, target))
            # Answers with internal entity UUIDs and WITHOUT the canonical names
            # or properties, exactly as the live API does.
            return _ok(
                {
                    "id": str(uuid.uuid4()),
                    "source_id": self.graph_entities[source]["id"],
                    "target_id": self.graph_entities[target]["id"],
                    "predicate": predicate,
                    "confidence": body.get("confidence", 1.0),
                }
            )

        if action == "graphrag":
            query = body.get("query", "")
            return _ok(
                {
                    "query": query,
                    "results": [
                        {"entity": name, "entity_type": entity["entity_type"]}
                        for name, entity in self.graph_entities.items()
                    ][: int(body.get("limit", 10) or 10)],
                }
            )

        if action == "traverse":
            start = body["entity"]
            max_hops = int(body.get("max_hops", 2) or 2)
            reached = self._reachable(start, max_hops)
            # Nodes are {id, label, type, properties} and include the start
            # entity, as the live API returns them. The canonical name is
            # `label`, not `entity`.
            return _ok(
                {
                    "nodes": [
                        {
                            "id": self.graph_entities.get(name, {}).get("id"),
                            "label": name,
                            "type": self.graph_entities.get(name, {}).get("entity_type"),
                            "properties": {},
                        }
                        for name in reached
                    ],
                    "edges": [
                        {
                            "id": str(uuid.uuid4()),
                            "source": self.graph_entities.get(s, {}).get("id"),
                            "target": self.graph_entities.get(t, {}).get("id"),
                            "predicate": p,
                            "depth": 1,
                        }
                        for s, p, t in self.graph_edges
                        if s in reached
                    ],
                    "paths": [],
                }
            )

        return _ok({})

    def _reachable(self, start: str, max_hops: int) -> list[str]:
        """Breadth-first walk out from `start`, bounded by `max_hops`.

        Includes `start` itself, matching the live traversal response.
        """
        reached = [start]
        seen = {start}
        frontier = [(start, 0)]
        while frontier:
            current, hops = frontier.pop(0)
            if hops >= max_hops:
                continue
            for source, _predicate, target in sorted(self.graph_edges):
                if source == current and target not in seen:
                    seen.add(target)
                    reached.append(target)
                    frontier.append((target, hops + 1))
        return reached

    # files ----------------------------------------------------------------

    def _route_files(
        self, method: str, segments: list[str], body: dict[str, Any]
    ) -> ZeroDBResponse:
        if segments[-1] == "presigned-url":
            file_id = segments[-2]
            if file_id not in self.files:
                return _not_found(f"File '{file_id}' not found")
            expiration = int(body.get("expiration", 300) or 300)
            # Field names follow the live API: `presigned_url`, and the
            # lifetime reported as minutes plus an absolute expiry.
            return _ok(
                {
                    "presigned_url": f"https://files.zerodb.test/{file_id}?signature=fake",
                    "file_id": file_id,
                    "file_name": self.files[file_id].get("file_name"),
                    "expires_at": "2026-10-15T00:05:00",
                    "expiry_minutes": max(1, expiration // 60),
                    "operation_type": "download",
                }
            )

        if method == "POST":
            file_id = str(uuid.uuid4())
            envelope = {
                "file_id": file_id,
                "file_key": body["file_key"],
                "file_name": body.get("file_name"),
                "content_type": body.get("content_type"),
                "size_bytes": body.get("size_bytes"),
                # null when none was supplied, as the live API returns it.
                "file_metadata": body.get("file_metadata"),
            }
            self.files[file_id] = envelope
            return _ok(envelope)

        return _ok({"total": len(self.files), "data": list(self.files.values())})

    # events ---------------------------------------------------------------

    def _route_events(
        self,
        method: str,
        segments: list[str],
        body: dict[str, Any],
        params: dict[str, Any],
    ) -> ZeroDBResponse:
        if segments[-1] == "subscriptions":
            if method == "POST":
                subscription = {
                    "subscription_id": str(uuid.uuid4()),
                    "event_types": body.get("event_types", []),
                    "webhook_url": body.get("webhook_url"),
                }
                self.subscriptions.append(subscription)
                return _ok(subscription)
            return _ok({"total": len(self.subscriptions), "data": self.subscriptions})

        if method == "POST":
            event = {
                "event_id": str(uuid.uuid4()),
                "topic": body["topic"],
                "event_payload": body.get("event_payload") or {},
                "published_at": "2026-10-15T00:00:00Z",
            }
            self.events.append(event)
            return _ok(event)

        topic = params.get("topic")
        matched = [e for e in self.events if topic is None or e["topic"] == topic]
        # The live API returns a bare list here, not an envelope.
        return _ok(matched)


_ERROR_CODES = {
    400: "VALIDATION_ERROR",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def _ok(body: Any) -> ZeroDBResponse:
    return ZeroDBResponse(status_code=200, json_body=body, text=json_module.dumps(body))


def _not_found(detail: str) -> ZeroDBResponse:
    body = {"detail": detail, "error_code": "NOT_FOUND", "next_action": "fix_request"}
    return ZeroDBResponse(status_code=404, json_body=body, text=json_module.dumps(body))


def _overlap(query: str, document: str | None) -> float:
    """Crude token-overlap score standing in for a real embedding distance.

    Enough to make "the closest stored text comes back first" testable. Never
    treat this as a stand-in for real ranking quality.
    """
    if not document:
        return 0.0
    query_tokens = set(query.lower().split())
    document_tokens = set(document.lower().split())
    if not query_tokens or not document_tokens:
        return 0.0
    return len(query_tokens & document_tokens) / len(query_tokens | document_tokens)


__all__ = ["FakeZeroDBTransport"]
