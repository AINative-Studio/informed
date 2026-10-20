"""Step definitions for tests/features/promise_embedding.feature (9.4, 9.5).

Runs entirely against story 1.2's in-memory fake transport: no network, no
real embedding call, no LLM. What these scenarios pin is the contract around
the embedding -- the `vector_id` written back onto the row, the metadata
datamodel.md's `promises` namespace requires, the once-per-promise
`promise.extracted` event, and the two "never report what did not happen"
failure paths.

The promises embedded here come from story 9.3's explicitly-labeled
`RuleBasedStubExtractor`. That is deliberate and harmless for these
scenarios: nothing below asserts anything about extraction quality, only
about what happens to a `promises` row once one exists.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.promise_embedding import (
    PROMISE_EXTRACTED_TOPIC,
    PROMISES_NAMESPACE,
    PromiseEmbedder,
    PromiseEmbeddingReport,
)
from app.ingest.promise_extraction import (
    STUB_EXTRACTOR_VERSION,
    PromiseExtractionPipeline,
    RuleBasedStubExtractor,
)
from app.repositories import Repository
from app.repositories.models import Candidacy, Promise, PromiseSource
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError
from app.zerodb.testing import FakeZeroDBTransport, ZeroDBResponse

scenarios("../features/promise_embedding.feature")

CANDIDACY_ID = "us-2026-11-03-general:ocd-division/country:us/state:ga:person:bioguide-O000175"
PERSON_ID = "person:bioguide-O000175"
SOURCE_ID = "src:1009ac74455bc9baaa8ec0bebf79549e0f805478542ab56ee318854f92092bca"
SOURCE_URL = "https://electjon.com/"

#: One testable commitment the stub extractor does recognise. Short and
#: specific on purpose: these scenarios are about embedding, not extraction.
TESTABLE_TEXT = "I will vote to cap insulin co-pays at $35 per month."
MIXED_TEXT = (
    "I will vote to cap insulin co-pays at $35 per month. "
    "I'll fight for working families every single day."
)


class _SelectivelyFailingTransport:
    """The fake, with one route broken.

    `FakeZeroDBTransport.always_fail` fails EVERY request, which cannot
    express "the embedding call fails but the row write still works" -- the
    exact shape both of this story's honesty scenarios need. This wraps the
    fake and fails only the paths matching `break_paths`, leaving everything
    else to behave normally.

    Deliberately a local test double rather than a new feature on the shared
    fake: per-route failure injection is this story's need, and widening the
    shared fake's API for one feature file is how a fake grows a second,
    less-tested life of its own.
    """

    def __init__(self, inner: FakeZeroDBTransport, *, break_paths: tuple[str, ...]) -> None:
        self._inner = inner
        self._break_paths = break_paths

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def request(self, method: str, path: str, **kwargs: Any) -> ZeroDBResponse:
        if any(fragment in path for fragment in self._break_paths):
            body = {
                "detail": "injected failure for this route",
                "error_code": "SERVER_ERROR",
                "next_action": "retry",
            }
            return ZeroDBResponse(status_code=500, json_body=body, text=str(body))
        return self._inner.request(method, path, **kwargs)


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def transport(fake: FakeZeroDBTransport) -> dict[str, Any]:
    """Mutable holder so a Given step can swap in a broken transport."""
    return {"transport": fake}


@pytest.fixture
def result() -> dict[str, Any]:
    return {}


def _client(transport: dict[str, Any]) -> ZeroDBClient:
    return ZeroDBClient(
        transport=transport["transport"],
        project_id="proj-test",
        backoff_base=0.0,
        sleep=lambda _: None,
    )


# --- background --------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _given_client(transport: dict[str, Any]) -> None:
    # The client is rebuilt per use from `transport`, so a later Given that
    # breaks one route still affects every call this feature makes.
    return None


@given("the reference tables exist")
def _reference_tables(transport: dict[str, Any]) -> None:
    Bootstrapper(_client(transport), pace=lambda: None).run()


@given("a candidacy and an archived promise source exist")
def _candidacy_and_source(transport: dict[str, Any]) -> None:
    from app.repositories.models import District, Election, Person

    client = _client(transport)
    Repository(client=client, model=Election).upsert(
        {
            "id": "us-2026-11-03-general",
            "jurisdiction_id": "us-federal",
            "name": "2026 federal general election",
            "election_date": "2026-11-03",
            "kind": "general",
        }
    )
    Repository(client=client, model=District).upsert(
        {
            "id": "ocd-division/country:us/state:ga",
            "body_id": "us-senate",
            "name": "Georgia",
            "state": "GA",
            "valid_from": "2025-01-03",
        }
    )
    Repository(client=client, model=Person).upsert(
        {
            "id": PERSON_ID,
            "canonical_name": "Ossoff, Jon",
            "external_ids": {"bioguide": "O000175"},
            "photo_url": None,
            "terms": [],
        }
    )
    Repository(client=client, model=Candidacy).upsert(
        {
            "id": CANDIDACY_ID,
            "election_id": "us-2026-11-03-general",
            "district_id": "ocd-division/country:us/state:ga",
            "person_id": PERSON_ID,
            "person_name": "Ossoff, Jon",
            "party": "DEM",
            "is_incumbent": True,
            "status": "filed",
        }
    )
    Repository(client=client, model=PromiseSource).upsert(
        {
            "id": SOURCE_ID,
            "candidacy_id": CANDIDACY_ID,
            "source_type": "campaign_website",
            "url": SOURCE_URL,
            "archived_file_id": "file-1c765687",
            "captured_at": "2026-10-16T22:40:21.023804+00:00",
        }
    )


def _extract(transport: dict[str, Any], text: str) -> None:
    pipeline = PromiseExtractionPipeline(
        _client(transport),
        extractor=RuleBasedStubExtractor(),
        extractor_version=STUB_EXTRACTOR_VERSION,
        pace=lambda: None,
    )
    pipeline.extract_from_source(SOURCE_ID, source_text=text)


@given("promises have been extracted from that source")
def _promises_extracted(transport: dict[str, Any]) -> None:
    _extract(transport, TESTABLE_TEXT)


@given("the source text contains one testable and one non-testable commitment")
def _mixed_source(transport: dict[str, Any], fake: FakeZeroDBTransport) -> None:
    # Replace the Background's single promise with the mixed pair, so the
    # scenario's vector count is exactly what its own text describes.
    fake.rows.pop(Promise.table_name, None)
    _extract(transport, MIXED_TEXT)


# --- failure injection -------------------------------------------------------


@given("the vector store refuses to store embeddings")
def _embedding_store_broken(transport: dict[str, Any], fake: FakeZeroDBTransport) -> None:
    transport["transport"] = _SelectivelyFailingTransport(
        fake, break_paths=("/embeddings/embed-and-store",)
    )


@given("the vector store stores embeddings but will not list them back")
def _vector_listing_broken(transport: dict[str, Any], fake: FakeZeroDBTransport) -> None:
    transport["transport"] = _SelectivelyFailingTransport(fake, break_paths=("/database/vectors",))


@given("the event log refuses to publish events")
def _events_broken(transport: dict[str, Any], fake: FakeZeroDBTransport) -> None:
    transport["transport"] = _SelectivelyFailingTransport(fake, break_paths=("/database/events",))


# --- embedding ---------------------------------------------------------------


def _embed(transport: dict[str, Any], result: dict[str, Any]) -> None:
    embedder = PromiseEmbedder(_client(transport), pace=lambda: None)
    result["report"] = embedder.embed_promises_for_source(SOURCE_ID)


@when("I embed the promises for that source")
@when("I embed the promises for that source again")
def _when_embed(transport: dict[str, Any], result: dict[str, Any]) -> None:
    _embed(transport, result)


@given("I have already embedded the promises for that source")
def _already_embedded(transport: dict[str, Any], result: dict[str, Any]) -> None:
    _embed(transport, result)
    result.pop("report", None)


@when("I try to embed the promises for that source")
def _try_embed(transport: dict[str, Any], result: dict[str, Any]) -> None:
    try:
        _embed(transport, result)
    except ZeroDBError as exc:
        result["error"] = exc


@given("a stored promise's vector was built from different text")
def _vector_text_drifted(fake: FakeZeroDBTransport) -> None:
    """Simulate a promise whose quote changed after it was embedded.

    Rewrites the stored vector's document so it no longer matches the row's
    `quote_span`, which is exactly the state a re-extraction with a better
    extractor produces. The row keeps its `vector_id`, so a naive "has a
    vector_id, skip it" check would leave the stale vector in place -- which
    is the bug this scenario exists to catch.
    """
    bucket = fake.vectors.get(PROMISES_NAMESPACE, [])
    embedded = [v for v in bucket if v["metadata"].get("promise_id")]
    assert embedded, "no promise vectors were stored to drift"
    embedded[0]["document"] = "something this promise never said"


# --- assertions: promise rows ------------------------------------------------


def _promise_rows(fake: FakeZeroDBTransport) -> list[dict[str, Any]]:
    stored = fake.rows.get(Promise.table_name, {})
    return [
        row["row_data"] for row in stored.values() if row["row_data"].get("source_id") == SOURCE_ID
    ]


def _promise_vectors(fake: FakeZeroDBTransport) -> list[dict[str, Any]]:
    return [
        vector
        for vector in fake.vectors.get(PROMISES_NAMESPACE, [])
        if vector["metadata"].get("promise_id")
    ]


@then("every promise row has a vector_id")
def _every_row_has_vector_id(fake: FakeZeroDBTransport) -> None:
    rows = _promise_rows(fake)
    assert rows, "no promise rows exist to embed"
    for row in rows:
        assert row.get("vector_id"), f"promise {row['id']} has no vector_id"


@then("no promise row has a vector_id")
def _no_row_has_vector_id(fake: FakeZeroDBTransport) -> None:
    rows = _promise_rows(fake)
    assert rows, "no promise rows exist"
    for row in rows:
        assert not row.get("vector_id"), (
            f"promise {row['id']} carries vector_id {row['vector_id']!r} after a failed "
            "embedding; a vector_id that names nothing is worse than none"
        )


@then("every promise row still has a vector_id")
def _rows_still_have_vector_id(fake: FakeZeroDBTransport) -> None:
    _every_row_has_vector_id(fake)


@then(
    parsers.parse('the vector_id names a vector that really exists in the "{namespace}" namespace')
)
def _vector_id_resolves(fake: FakeZeroDBTransport, namespace: str) -> None:
    stored_ids = {vector["vector_id"] for vector in fake.vectors.get(namespace, [])}
    for row in _promise_rows(fake):
        assert row["vector_id"] in stored_ids, (
            f"promise {row['id']} names vector {row['vector_id']!r}, which is not in "
            f"the {namespace!r} namespace"
        )


# --- assertions: vectors -----------------------------------------------------


@then("every stored vector's document is the quote span of the promise it belongs to")
def _document_is_quote_span(fake: FakeZeroDBTransport) -> None:
    by_id = {row["id"]: row for row in _promise_rows(fake)}
    vectors = _promise_vectors(fake)
    assert vectors, "no promise vectors were stored"
    for vector in vectors:
        promise = by_id[vector["metadata"]["promise_id"]]
        assert vector["document"] == promise["quote_span"], (
            f"vector document {vector['document']!r} is not the promise's own "
            f"verbatim quote {promise['quote_span']!r}"
        )


@then("every stored vector's metadata carries the promise_id it was built from")
def _metadata_has_promise_id(fake: FakeZeroDBTransport) -> None:
    vectors = _promise_vectors(fake)
    assert vectors, "no promise vectors were stored"
    for vector in vectors:
        assert vector["metadata"].get("promise_id"), "vector metadata has no promise_id"


@then("every stored vector's metadata carries that promise's person_id")
def _metadata_has_person_id(fake: FakeZeroDBTransport) -> None:
    by_id = {row["id"]: row for row in _promise_rows(fake)}
    for vector in _promise_vectors(fake):
        promise = by_id[vector["metadata"]["promise_id"]]
        assert vector["metadata"].get("person_id") == promise["person_id"], (
            f"vector metadata person_id is {vector['metadata'].get('person_id')!r}, "
            f"expected {promise['person_id']!r}"
        )


@then("every stored vector's metadata carries topic_id, cycle and testable")
def _metadata_has_filter_fields(fake: FakeZeroDBTransport) -> None:
    vectors = _promise_vectors(fake)
    assert vectors, "no promise vectors were stored"
    for vector in vectors:
        metadata = vector["metadata"]
        # datamodel.md's `promises` namespace metadata: promise_id, person_id,
        # topic_id, cycle, testable. `topic_id` is legitimately None until
        # story 10.1 classifies, so the KEY must be present rather than truthy.
        assert "topic_id" in metadata, "vector metadata is missing the topic_id key"
        assert metadata.get("cycle"), "vector metadata has no cycle"
        assert isinstance(metadata.get("testable"), bool), (
            f"vector metadata testable is {metadata.get('testable')!r}, not a bool"
        )


@then("the promise_id in each vector's metadata resolves to a stored promise row")
def _metadata_promise_resolves(transport: dict[str, Any], fake: FakeZeroDBTransport) -> None:
    repo: Repository[Promise] = Repository(client=_client(transport), model=Promise)
    vectors = _promise_vectors(fake)
    assert vectors, "no promise vectors were stored"
    for vector in vectors:
        promise_id = vector["metadata"]["promise_id"]
        assert repo.get(promise_id) is not None, (
            f"vector names promise {promise_id!r}, which does not resolve to a row"
        )


@then(parsers.parse('exactly {count:d} vectors exist in the "{namespace}" namespace'))
def _exact_vector_count(fake: FakeZeroDBTransport, count: int, namespace: str) -> None:
    vectors = [v for v in fake.vectors.get(namespace, []) if v["metadata"].get("promise_id")]
    assert len(vectors) == count, f"found {len(vectors)} promise vector(s), expected {count}"


@then(parsers.parse('exactly 1 vector exists in the "{namespace}" namespace per promise row'))
def _one_vector_per_row(fake: FakeZeroDBTransport, namespace: str) -> None:
    rows = _promise_rows(fake)
    assert rows, "no promise rows exist"
    vectors = [v for v in fake.vectors.get(namespace, []) if v["metadata"].get("promise_id")]
    counts: dict[str, int] = {}
    for vector in vectors:
        promise_id = vector["metadata"]["promise_id"]
        counts[promise_id] = counts.get(promise_id, 0) + 1
    for row in rows:
        found = counts.get(row["id"], 0)
        assert found == 1, f"promise {row['id']} has {found} vector(s), expected exactly 1"
    assert len(vectors) == len(rows), (
        f"{len(vectors)} promise vector(s) for {len(rows)} promise row(s)"
    )


@then(parsers.parse("{count:d} of those vectors is marked testable in its metadata"))
def _n_vectors_testable(fake: FakeZeroDBTransport, count: int) -> None:
    testable = [v for v in _promise_vectors(fake) if v["metadata"].get("testable") is True]
    assert len(testable) == count, f"{len(testable)} testable vector(s), expected {count}"


@then(parsers.parse("{count:d} of those vectors is marked not testable in its metadata"))
def _n_vectors_not_testable(fake: FakeZeroDBTransport, count: int) -> None:
    rows = [v for v in _promise_vectors(fake) if v["metadata"].get("testable") is False]
    assert len(rows) == count, f"{len(rows)} non-testable vector(s), expected {count}"


@then("that promise is re-embedded")
def _promise_rebuilt(result: dict[str, Any]) -> None:
    report: PromiseEmbeddingReport = result["report"]
    assert report.rebuilt, "the report lists no re-embedded promise"


# --- assertions: the report --------------------------------------------------


@then(parsers.parse("the embedding report says {count:d} promises were embedded"))
def _report_embedded_count(result: dict[str, Any], count: int) -> None:
    report: PromiseEmbeddingReport = result["report"]
    assert len(report.embedded) == count, (
        f"report says {len(report.embedded)} promise(s) embedded, expected {count}"
    )


@then("the embedding report says every promise was already current")
def _report_all_skipped(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    report: PromiseEmbeddingReport = result["report"]
    assert len(report.skipped) == len(_promise_rows(fake)), (
        f"report skipped {len(report.skipped)} of {len(_promise_rows(fake))} promise(s)"
    )


@then(parsers.parse("the embedding report says {count:d} events were emitted"))
def _report_event_count(result: dict[str, Any], count: int) -> None:
    report: PromiseEmbeddingReport = result["report"]
    assert len(report.events_emitted) == count, (
        f"report says {len(report.events_emitted)} event(s) emitted, expected {count}"
    )


@then("the embedding report records the failure")
def _report_records_failure(result: dict[str, Any]) -> None:
    report: PromiseEmbeddingReport = result["report"]
    assert report.failures, "the report records no failure after a broken call"


# --- assertions: events ------------------------------------------------------


def _events(fake: FakeZeroDBTransport, topic: str) -> list[dict[str, Any]]:
    return [event for event in fake.events if event["topic"] == topic]


@then(parsers.parse('a "{topic}" event exists for every promise row'))
def _event_per_row(fake: FakeZeroDBTransport, topic: str) -> None:
    rows = _promise_rows(fake)
    assert rows, "no promise rows exist"
    announced = {event["event_payload"].get("promise_id") for event in _events(fake, topic)}
    for row in rows:
        assert row["id"] in announced, f"no {topic} event for promise {row['id']}"


@then(parsers.parse('every "{topic}" event payload carries a promise_id and a person_id'))
def _event_payload_fields(fake: FakeZeroDBTransport, topic: str) -> None:
    events = _events(fake, topic)
    assert events, f"no {topic} events were published"
    for event in events:
        payload = event["event_payload"]
        assert payload.get("promise_id"), f"{topic} payload has no promise_id"
        assert payload.get("person_id"), f"{topic} payload has no person_id"


@then("each event's promise_id resolves to a stored promise row")
def _event_promise_resolves(transport: dict[str, Any], fake: FakeZeroDBTransport) -> None:
    repo: Repository[Promise] = Repository(client=_client(transport), model=Promise)
    events = _events(fake, PROMISE_EXTRACTED_TOPIC)
    assert events, "no promise.extracted events were published"
    for event in events:
        promise_id = event["event_payload"]["promise_id"]
        assert repo.get(promise_id) is not None, (
            f"event names promise {promise_id!r}, which does not resolve to a row"
        )


@then(parsers.parse('exactly 1 "{topic}" event exists per promise row'))
def _one_event_per_row(fake: FakeZeroDBTransport, topic: str) -> None:
    rows = _promise_rows(fake)
    assert rows, "no promise rows exist"
    counts: dict[str, int] = {}
    for event in _events(fake, topic):
        promise_id = event["event_payload"].get("promise_id")
        counts[promise_id] = counts.get(promise_id, 0) + 1
    for row in rows:
        found = counts.get(row["id"], 0)
        assert found == 1, f"promise {row['id']} has {found} {topic} event(s), expected 1"


@then(parsers.parse('no "{topic}" event exists'))
def _no_event(fake: FakeZeroDBTransport, topic: str) -> None:
    events = _events(fake, topic)
    assert not events, (
        f"{len(events)} {topic} event(s) were published for promises that were never "
        "embedded; the mapping agent would wake to nothing searchable"
    )
