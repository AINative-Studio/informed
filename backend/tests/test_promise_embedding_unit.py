"""Unit tests for story 9.4/9.5's embedding pieces.

The BDD scenarios in `tests/features/promise_embedding.feature` cover the
end-to-end behaviour. These cover the decision functions and the error paths
whose edge cases do not read well as Gherkin:

  * `promise_embedding_text` -- what text is actually embedded. The whole
    value of story 10.1's bill matching rests on this being the promise's own
    words and nothing else.
  * `cycle_for_election_id` / `cycle_for` -- deriving datamodel.md's `cycle`
    metadata field, which no model carries as a column.
  * `promise_vector_metadata` -- the exact metadata contract datamodel.md's
    `promises` namespace names, including the fields that are legitimately
    null today.
  * `PromiseEmbedder`'s failure and boundary paths -- an unknown source, a
    source with nothing extracted yet, a stale vector that will not delete,
    an embedding that stores but cannot be found, a row whose `vector_id`
    will not write, a candidacy that does not resolve, and paging past one
    page. Each one is a claim this module's docstring makes, so each one is
    held by a test rather than by the docstring alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.bootstrap import Bootstrapper
from app.ingest.promise_embedding import (
    PAGE_SIZE,
    PROMISE_EXTRACTED_TOPIC,
    PROMISES_NAMESPACE,
    PromiseEmbedder,
    PromiseEmbeddingError,
    PromiseEmbeddingReport,
    cycle_for_election_id,
    promise_embedding_text,
    promise_vector_metadata,
)
from app.repositories import Repository
from app.repositories.models import (
    Candidacy,
    District,
    Election,
    Person,
    Promise,
    PromiseSource,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport, ZeroDBResponse

TESTABLE_PROMISE = Promise(
    id="prm:deadbeef",
    candidacy_id="us-2026-11-03-general:ocd-division/country:us/state:ga:person:bioguide-O000175",
    person_id="person:bioguide-O000175",
    source_id="src:abc",
    quote_span="I will vote to cap insulin co-pays at $35 per month.",
    actor="the speaker",
    action="vote to cap",
    object="insulin co-pays at $35 per month",
    direction="support",
    testable=True,
    extractor_version="stub-rule-based-v0",
    confidence=0.6,
)

NON_TESTABLE_PROMISE = Promise(
    id="prm:cafebabe",
    candidacy_id="us-2026-11-03-general:ocd-division/country:us/state:ga:person:bioguide-O000175",
    person_id="person:bioguide-O000175",
    source_id="src:abc",
    quote_span="I'll fight for working families every single day.",
    actor="the speaker",
    action="fight for",
    object=None,
    direction="unclear",
    testable=False,
    extractor_version="stub-rule-based-v0",
    confidence=0.3,
)


# --- the embedded text --------------------------------------------------------


def test_the_embedded_text_is_the_promises_own_verbatim_quote() -> None:
    """Not a summary, not the four parts rejoined -- the quote itself.

    datamodel.md's `promises` namespace document text is "Atomic commitment
    text". The quote span IS that text, and it is the only wording in the row
    that the source actually said.
    """
    assert promise_embedding_text(TESTABLE_PROMISE) == TESTABLE_PROMISE.quote_span


def test_a_non_testable_promise_is_embedded_from_its_quote_too() -> None:
    """A promise with no object still has words, and still gets embedded."""
    assert promise_embedding_text(NON_TESTABLE_PROMISE) == NON_TESTABLE_PROMISE.quote_span


def test_the_embedded_text_is_not_padded_with_the_extracted_parts() -> None:
    """actor/action/object are the extractor's reading, not the source's words.

    Appending them would embed the extractor's interpretation alongside the
    quote, so a bill would match a promise partly because of wording nobody
    said. With a stand-in extractor still wired (story 9.3), that would mean
    matching against regex output.
    """
    text = promise_embedding_text(TESTABLE_PROMISE)
    assert "vote to cap" in text  # because the QUOTE contains it
    assert text.count("vote to cap") == 1, "the action was appended as well as quoted"
    assert "the speaker" not in text


# --- the cycle ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("election_id", "expected"),
    [
        ("us-2026-11-03-general", 2026),
        ("us-2024-11-05-general", 2024),
        ("us-2026-05-19-primary", 2026),
    ],
)
def test_the_cycle_is_the_election_year(election_id: str, expected: int) -> None:
    """datamodel.md's `cycle` metadata field, derived from the election id.

    No model carries `cycle` as a column -- `Election` has `election_date`
    and the id embeds the same date. The year is read from the id rather than
    from `election_date` so the derivation needs no second read.
    """
    assert cycle_for_election_id(election_id) == expected


@pytest.mark.parametrize(
    "election_id",
    ["", "us-general", "not-an-election-id", "us-99-11-03-general", "us-0000-11-03-general"],
)
def test_an_election_id_with_no_year_has_no_cycle(election_id: str) -> None:
    """None rather than a guess: a wrong cycle filters the mapping agent to
    the wrong election's bills, which is worse than no filter at all."""
    assert cycle_for_election_id(election_id) is None


# --- the metadata contract ----------------------------------------------------


def test_the_metadata_carries_every_field_datamodel_names() -> None:
    """datamodel.md: promise_id, person_id, topic_id, cycle, testable."""
    metadata = promise_vector_metadata(TESTABLE_PROMISE, cycle=2026)
    assert metadata["promise_id"] == "prm:deadbeef"
    assert metadata["person_id"] == "person:bioguide-O000175"
    assert metadata["topic_id"] is None
    assert metadata["cycle"] == 2026
    assert metadata["testable"] is True


def test_the_topic_id_key_is_present_even_while_it_is_null() -> None:
    """Present-and-null, not absent.

    `topic_id` is filled by story 10.1, so it is null on every row today.
    The KEY still has to be there: a `filter_metadata={"topic_id": ...}`
    search against vectors that never declared the field cannot match, and
    the gap would only show up once 10.1 shipped.
    """
    metadata = promise_vector_metadata(TESTABLE_PROMISE, cycle=2026)
    assert "topic_id" in metadata


def test_a_promise_with_a_topic_id_carries_it() -> None:
    """When story 10.1 fills `topic_id`, the metadata carries it unchanged."""
    classified = TESTABLE_PROMISE.model_copy(update={"topic_id": "topic:healthcare-costs"})
    assert promise_vector_metadata(classified, cycle=2026)["topic_id"] == ("topic:healthcare-costs")


def test_a_non_testable_promise_is_marked_not_testable_in_its_metadata() -> None:
    """The filter story 10.1 needs: it maps only testable promises.

    prd.md shows non-testable rhetoric to the voter but never scores it, so
    the mapping agent must be able to exclude it with a metadata filter
    rather than by re-reading every row.
    """
    metadata = promise_vector_metadata(NON_TESTABLE_PROMISE, cycle=2026)
    assert metadata["testable"] is False


def test_the_metadata_never_carries_the_quote_or_the_extractors_reading() -> None:
    """Only the ids and filters datamodel.md names.

    The document already holds the quote; duplicating it into metadata (or
    adding actor/action/object) would make the extractor's reading
    searchable as if it were the source's words.
    """
    metadata = promise_vector_metadata(TESTABLE_PROMISE, cycle=2026)
    assert set(metadata) == {"promise_id", "person_id", "topic_id", "cycle", "testable"}


def test_the_metadata_carries_no_cycle_key_value_when_the_cycle_is_unknown() -> None:
    """An unknown cycle is null, never a guessed year."""
    metadata = promise_vector_metadata(TESTABLE_PROMISE, cycle=None)
    assert metadata["cycle"] is None


# --- the report ---------------------------------------------------------------


def test_an_empty_report_counts_nothing() -> None:
    report = PromiseEmbeddingReport(source_id="src:abc")
    assert report.embedded == []
    assert report.skipped == []
    assert report.rebuilt == []
    assert report.events_emitted == []
    assert report.failures == []
    assert report.promise_count == 0


def test_the_report_counts_embedded_and_skipped_promises_together() -> None:
    """`promise_count` is how many rows the run accounted for, either way."""
    report = PromiseEmbeddingReport(source_id="src:abc")
    report.embedded.append("prm:1")
    report.skipped.extend(["prm:2", "prm:3"])
    assert report.promise_count == 3


def test_rebuilt_promises_are_a_subset_of_embedded_ones() -> None:
    """A rebuild is an embed that deleted a stale vector first.

    Keeping `rebuilt` a subset rather than a separate bucket is what makes
    `embedded` answer "which promises have a current vector because of this
    run" without a caller having to add two lists.
    """
    report = PromiseEmbeddingReport(source_id="src:abc")
    report.embedded.append("prm:1")
    report.rebuilt.append("prm:1")
    assert set(report.rebuilt) <= set(report.embedded)
    assert report.promise_count == 1


# --- the constants ------------------------------------------------------------


def test_the_namespace_is_the_one_datamodel_names() -> None:
    assert PROMISES_NAMESPACE == "promises"


def test_the_event_topic_is_the_one_datamodel_names() -> None:
    assert PROMISE_EXTRACTED_TOPIC == "promise.extracted"


# ===========================================================================
# PromiseEmbedder's failure and boundary paths
# ===========================================================================

CANDIDACY_ID = TESTABLE_PROMISE.candidacy_id
PERSON_ID = TESTABLE_PROMISE.person_id
SOURCE_ID = "src:abc"


class _BrokenRouteTransport:
    """The fake with one route broken. See the step defs' copy for why.

    Duplicated rather than shared because the two files break different
    routes for different reasons, and a shared helper in `tests/support/`
    would be a third place to look for a six-line class.
    """

    def __init__(self, inner: FakeZeroDBTransport, *, break_paths: tuple[str, ...]) -> None:
        self._inner = inner
        self._break_paths = break_paths

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def request(self, method: str, path: str, **kwargs: Any) -> ZeroDBResponse:
        if any(fragment in path for fragment in self._break_paths):
            body = {"detail": "injected", "error_code": "SERVER_ERROR", "next_action": "retry"}
            return ZeroDBResponse(status_code=500, json_body=body, text="injected")
        return self._inner.request(method, path, **kwargs)


def _client(transport: Any) -> ZeroDBClient:
    return ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )


def _seed_reference_rows(client: ZeroDBClient) -> None:
    """The rows a `promise_sources` row and a `promises` row need to validate."""
    Bootstrapper(client, pace=lambda: None).run()
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
            "url": "https://electjon.com/",
            "archived_file_id": "file-1",
            "captured_at": "2026-10-16T22:40:21.023804+00:00",
        }
    )


def _store_promise(client: ZeroDBClient, promise: Promise) -> None:
    Repository(client=client, model=Promise).upsert({**promise.model_dump(), "id": promise.id})


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


def test_embedding_an_unknown_source_is_refused() -> None:
    """A promise's citability flows through its `promise_sources` row.

    Embedding against a source that is not on file would produce a vector
    resolvable to a promise nobody can cite, which is the one thing
    prd.md's evidence-over-opinion rule forbids.
    """
    fake = FakeZeroDBTransport()
    fake.break_server_side_filter()
    client = _client(fake)
    Bootstrapper(client, pace=lambda: None).run()
    embedder = PromiseEmbedder(client, pace=lambda: None)

    with pytest.raises(PromiseEmbeddingError, match="no promise_sources row"):
        embedder.embed_promises_for_source("src:never-archived")


def test_a_source_with_nothing_extracted_yet_is_an_empty_report(
    fake: FakeZeroDBTransport,
) -> None:
    """Not an error: story 9.3 writing zero promises is a real, correct outcome.

    Measured live: the stand-in extractor found zero genuine commitments in
    three real campaign pages and correctly wrote nothing. Embedding that
    source has nothing to do and must say so rather than fail.
    """
    client = _client(fake)
    _seed_reference_rows(client)
    report = PromiseEmbedder(client, pace=lambda: None).embed_promises_for_source(SOURCE_ID)

    assert report.promise_count == 0
    assert report.embedded == []
    assert report.events_emitted == []
    assert report.failures == []


def test_a_stale_vector_that_cannot_be_deleted_blocks_the_rebuild(
    fake: FakeZeroDBTransport,
) -> None:
    """Never store a second vector for one promise.

    A delete that fails and an embed that proceeds leaves two vectors for one
    promise -- a duplicate story 10.1's mapping agent matches twice, and so
    scores twice. Reporting the failure and leaving the stale vector alone is
    the recoverable state; a re-run mends it.
    """
    client = _client(fake)
    _seed_reference_rows(client)
    _store_promise(client, TESTABLE_PROMISE)

    # First run embeds cleanly.
    PromiseEmbedder(client, pace=lambda: None).embed_promises_for_source(SOURCE_ID)
    stored = fake.vectors[PROMISES_NAMESPACE]
    embedded = [v for v in stored if v["metadata"].get("promise_id")]
    assert len(embedded) == 1
    # Drift the document so the next run must rebuild.
    embedded[0]["document"] = "words this promise never said"

    broken = _BrokenRouteTransport(fake, break_paths=("/database/vectors/",))
    report = PromiseEmbedder(_client(broken), pace=lambda: None).embed_promises_for_source(
        SOURCE_ID
    )

    assert report.embedded == []
    assert report.rebuilt == []
    assert any("stale vector" in failure for failure in report.failures)
    # The stale vector is still the only one: no duplicate was stored.
    assert (
        len([v for v in fake.vectors[PROMISES_NAMESPACE] if v["metadata"].get("promise_id")]) == 1
    )


def test_an_embedding_that_stores_but_cannot_be_found_is_not_reported_as_embedded(
    fake: FakeZeroDBTransport,
) -> None:
    """The live `embed_and_store` returns no vector id, so the id is read back.

    If that read finds nothing carrying the promise's id, there is no
    `vector_id` to write -- and story 9.4's acceptance criterion is a
    `vector_id` on the row. Reported as a failure, not as success.
    """
    client = _client(fake)
    _seed_reference_rows(client)
    _store_promise(client, TESTABLE_PROMISE)

    class _SwallowingTransport(_BrokenRouteTransport):
        """Accepts the embed call and stores nothing, as a silent drop would."""

        def request(self, method: str, path: str, **kwargs: Any) -> ZeroDBResponse:
            if "/embeddings/embed-and-store" in path:
                body = {
                    "success": True,
                    "vectors_stored": 1,
                    "embeddings_generated": 1,
                    "namespace": PROMISES_NAMESPACE,
                    "model": "bge-m3",
                    "dimensions": 1024,
                }
                return ZeroDBResponse(status_code=200, json_body=body, text="ok")
            return self._inner.request(method, path, **kwargs)

    swallowing = _SwallowingTransport(fake, break_paths=())
    report = PromiseEmbedder(_client(swallowing), pace=lambda: None).embed_promises_for_source(
        SOURCE_ID
    )

    assert report.embedded == []
    assert report.events_emitted == []
    assert any("no vector carrying its promise_id" in failure for failure in report.failures)
    stored = Repository(client=client, model=Promise).get(TESTABLE_PROMISE.id)
    assert stored is not None
    assert stored.vector_id is None, "a vector_id that names nothing is worse than none"


def test_a_row_whose_vector_id_cannot_be_written_is_not_reported_as_embedded(
    fake: FakeZeroDBTransport,
) -> None:
    """The criterion is a property of the ROW, so the row write has to land.

    The vector exists at this point, which is why the failure message names
    it: a human repairing this needs to know a vector was stored before the
    row write failed, so a re-run reuses it rather than embedding again.
    """
    client = _client(fake)
    _seed_reference_rows(client)
    _store_promise(client, TESTABLE_PROMISE)

    class _ReadOnlyRowsTransport(_BrokenRouteTransport):
        """Breaks only the promises-table WRITE, leaving reads working."""

        def request(self, method: str, path: str, **kwargs: Any) -> ZeroDBResponse:
            if "/tables/promises/rows" in path and method in {"POST", "PUT"}:
                body = {"detail": "injected", "error_code": "SERVER_ERROR"}
                return ZeroDBResponse(status_code=500, json_body=body, text="injected")
            return self._inner.request(method, path, **kwargs)

    read_only = _ReadOnlyRowsTransport(fake, break_paths=())
    report = PromiseEmbedder(_client(read_only), pace=lambda: None).embed_promises_for_source(
        SOURCE_ID
    )

    assert report.embedded == []
    assert report.events_emitted == [], "a promise with no vector_id is never announced"
    assert any("vector_id could not be written" in failure for failure in report.failures)


def test_a_candidacy_that_does_not_resolve_is_reported_as_a_failure(
    fake: FakeZeroDBTransport,
) -> None:
    """Not embedded, and never announced -- and this is the RIGHT outcome.

    Written expecting the opposite ("a missing cycle costs a metadata filter,
    not the whole embedding") and corrected to what actually happens, because
    the reason is better than the guess was: `Promise.references` names
    `candidacy_id`, so the `vector_id` write-back fails its own reference
    check. A promise whose candidate is not on file cannot be attributed to
    anyone, so it must not quietly become searchable evidence -- the
    reference check already enforces that, and this test pins it rather than
    letting the embedder paper over it.

    The vector IS stored at this point, orphaned until the candidacy is
    restored and the run repeated -- at which point it is a stale vector and
    gets rebuilt. `report.failures` names it so that state is visible.
    """
    client = _client(fake)
    # Seed everything normally -- the `promise_sources` and `promises` rows
    # both require their candidacy to exist before they will validate -- then
    # remove the candidacy to reach the state this test is about.
    _seed_reference_rows(client)
    _store_promise(client, TESTABLE_PROMISE)
    fake.rows[Candidacy.table_name].clear()

    report = PromiseEmbedder(client, pace=lambda: None).embed_promises_for_source(SOURCE_ID)

    assert report.embedded == []
    assert report.events_emitted == [], (
        "a promise with no candidacy on file must never be announced to the mapping agent"
    )
    assert any("vector_id could not be written" in failure for failure in report.failures)
    # The cycle really was read as None -- the vector that got stored proves
    # the derivation degraded rather than raised.
    vector = next(v for v in fake.vectors[PROMISES_NAMESPACE] if v["metadata"].get("promise_id"))
    assert vector["metadata"]["cycle"] is None


def test_more_promises_than_one_page_are_all_embedded(fake: FakeZeroDBTransport) -> None:
    """Both reads page. A namespace and a source both outgrow one page.

    `PAGE_SIZE + 1` rather than a round number, so a loop that reads exactly
    one page fails rather than passing by coincidence.
    """
    client = _client(fake)
    _seed_reference_rows(client)
    count = PAGE_SIZE + 1
    for index in range(count):
        _store_promise(
            client,
            TESTABLE_PROMISE.model_copy(
                update={
                    "id": f"prm:{index:032x}",
                    "quote_span": f"I will vote to cap insulin co-pays at ${index} per month.",
                }
            ),
        )

    report = PromiseEmbedder(client, pace=lambda: None).embed_promises_for_source(SOURCE_ID)

    assert len(report.embedded) == count, f"embedded {len(report.embedded)} of {count}"
    assert len(report.events_emitted) == count
    assert report.failures == []


def test_an_embedding_stored_before_the_namespace_went_unreadable_is_not_counted(
    fake: FakeZeroDBTransport,
) -> None:
    """The narrow window between the write and the read-back.

    `embed_and_store` returns no vector id (measured live), so recovering one
    takes a second call -- and the namespace can become unreadable BETWEEN
    the two. The embedder has already listed the namespace successfully by
    then, so it cannot short-circuit the way a listing failure up front does;
    this is the only path that reaches the second handler.

    Reported as a failure, because there is still no `vector_id` to put on
    the row, and story 9.4's criterion is a `vector_id` on the row.
    """
    client = _client(fake)
    _seed_reference_rows(client)
    _store_promise(client, TESTABLE_PROMISE)

    class _BreaksAfterFirstListing(_BrokenRouteTransport):
        """Serves the first vector listing, then refuses every later one."""

        listings = 0

        def request(self, method: str, path: str, **kwargs: Any) -> ZeroDBResponse:
            if "/database/vectors" in path and method == "GET":
                self.listings += 1
                if self.listings > 1:
                    body = {"detail": "injected", "error_code": "SERVER_ERROR"}
                    return ZeroDBResponse(status_code=500, json_body=body, text="injected")
            return self._inner.request(method, path, **kwargs)

    flaky = _BreaksAfterFirstListing(fake, break_paths=())
    report = PromiseEmbedder(_client(flaky), pace=lambda: None).embed_promises_for_source(SOURCE_ID)

    assert report.embedded == []
    assert report.events_emitted == []
    assert any("could not be read" in failure for failure in report.failures)
