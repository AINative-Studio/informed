"""Step definitions for tests/features/promise_extraction.feature (9.3).

Runs entirely against story 1.2's in-memory fake transport, with the source
text injected and the explicitly-labeled `RuleBasedStubExtractor` as the
extractor -- no network and no LLM call anywhere, per CLAUDE.md.

The stub extractor is NOT the real extractor this story's 90%-precision
acceptance criterion is about; see `app.ingest.promise_extraction`'s module
docstring. What these scenarios verify is the pipeline around the extractor:
the four-part commitment schema, the testable/non-testable split, verbatim
quote spans, idempotent re-extraction, and the exact `promises` row shape
story 10.1 will read.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.promise_extraction import (
    STUB_EXTRACTOR_VERSION,
    ExtractedCommitment,
    ExtractionReport,
    PromiseExtractionPipeline,
    QuoteSpanError,
    RuleBasedStubExtractor,
)
from app.repositories import Repository
from app.repositories.models import Candidacy, Promise, PromiseSource
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/promise_extraction.feature")

CANDIDACY_ID = "us-2026-11-03-general:ocd-division/country:us/state:ga:person:bioguide-O000175"
PERSON_ID = "person:bioguide-O000175"
SOURCE_ID = "src:1009ac74455bc9baaa8ec0bebf79549e0f805478542ab56ee318854f92092bca"
SOURCE_URL = "https://electjon.com/"


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )


@pytest.fixture
def result() -> dict[str, Any]:
    return {}


# --- background --------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def _reference_tables(zerodb: ZeroDBClient) -> None:
    Bootstrapper(zerodb, pace=lambda: None).run()


@given(parsers.parse('a candidacy "{candidacy_id}" exists'))
def _candidacy_exists(zerodb: ZeroDBClient, candidacy_id: str) -> None:
    from app.repositories.models import District, Election, Person

    Repository(client=zerodb, model=Election).upsert(
        {
            "id": "us-2026-11-03-general",
            "jurisdiction_id": "us-federal",
            "name": "2026 federal general election",
            "election_date": "2026-11-03",
            "kind": "general",
        }
    )
    Repository(client=zerodb, model=District).upsert(
        {
            "id": "ocd-division/country:us/state:ga",
            "body_id": "us-senate",
            "name": "Georgia",
            "state": "GA",
            "valid_from": "2025-01-03",
        }
    )
    Repository(client=zerodb, model=Person).upsert(
        {
            "id": PERSON_ID,
            "canonical_name": "Ossoff, Jon",
            "external_ids": {"bioguide": "O000175"},
            "photo_url": None,
            "terms": [],
        }
    )
    Repository(client=zerodb, model=Candidacy).upsert(
        {
            "id": candidacy_id,
            "election_id": "us-2026-11-03-general",
            "district_id": "ocd-division/country:us/state:ga",
            "person_id": PERSON_ID,
            "person_name": "Ossoff, Jon",
            "party": "DEM",
            "is_incumbent": True,
            "status": "filed",
        }
    )


@given("an archived promise source for that candidacy")
def _promise_source_exists(zerodb: ZeroDBClient) -> None:
    Repository(client=zerodb, model=PromiseSource).upsert(
        {
            "id": SOURCE_ID,
            "candidacy_id": CANDIDACY_ID,
            "source_type": "campaign_website",
            "url": SOURCE_URL,
            "archived_file_id": "file-1c765687",
            "captured_at": "2026-10-16T22:40:21.023804+00:00",
        }
    )


@given(parsers.parse('the source text contains "{text}"'))
def _source_text(result: dict[str, Any], text: str) -> None:
    result["text"] = text


@given("an extractor that returns a paraphrase instead of a verbatim quote")
def _paraphrasing_extractor(result: dict[str, Any]) -> None:
    def paraphrase(passage: str, *, context: str) -> list[ExtractedCommitment]:
        return [
            ExtractedCommitment(
                actor="the candidate",
                action="vote to cap",
                object="insulin copays",
                direction="support",
                testable=True,
                confidence=0.9,
                # Deliberately NOT a substring of the source: a cleaned-up,
                # reworded version of what the page actually says.
                quote_span="The candidate promises to cap insulin copays at thirty-five dollars.",
            )
        ]

    result["extractor"] = paraphrase


# --- extracting ----------------------------------------------------------------


def _pipeline(zerodb: ZeroDBClient, result: dict[str, Any]) -> PromiseExtractionPipeline:
    extractor = result.get("extractor") or RuleBasedStubExtractor()
    version = STUB_EXTRACTOR_VERSION if result.get("extractor") is None else "test-paraphrase-v0"
    return PromiseExtractionPipeline(
        zerodb,
        extractor=extractor,
        extractor_version=version,
        pace=lambda: None,
    )


def _extract(zerodb: ZeroDBClient, result: dict[str, Any], *, text: str | None = None) -> None:
    pipeline = _pipeline(zerodb, result)
    report = pipeline.extract_from_source(SOURCE_ID, source_text=text or result["text"])
    result["report"] = report


@when("I extract promises from that source")
@when("I extract promises from that source again")
def _extract_promises(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    _extract(zerodb, result)


@given("I have already extracted promises from that source once")
def _already_extracted(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    _extract(zerodb, result)


@when("I extract promises from a source text with one more commitment")
def _extract_extended(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    extended = result["text"] + " I will vote to repeal the cap on state tax deductions."
    _extract(zerodb, result, text=extended)


@when("I try to extract promises from that source")
def _try_extract(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    try:
        _extract(zerodb, result)
    except QuoteSpanError as exc:
        result["error"] = exc
    else:
        result["error"] = None


@when("I try to build an extraction pipeline with no extractor")
def _try_build_without_extractor(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    try:
        PromiseExtractionPipeline(zerodb, extractor=None, extractor_version="unset")
    except ValueError as exc:
        result["error"] = exc
    else:
        result["error"] = None


# --- assertions ----------------------------------------------------------------


def _promise_rows(fake: FakeZeroDBTransport) -> list[dict[str, Any]]:
    stored = fake.rows.get("promises", {})
    return [
        row["row_data"] for row in stored.values() if row["row_data"].get("source_id") == SOURCE_ID
    ]


def _single_promise(fake: FakeZeroDBTransport) -> dict[str, Any]:
    rows = _promise_rows(fake)
    testable = [row for row in rows if row.get("testable")]
    # Scenarios that assert on "that promise" either produced exactly one row,
    # or are about the one commitment the scenario's own text is about.
    if len(rows) == 1:
        return rows[0]
    assert rows, "no promise rows were written"
    return testable[0] if testable else rows[0]


@then(parsers.parse("exactly {count:d} promise row exists for that source"))
@then(parsers.parse("exactly {count:d} promise rows exist for that source"))
def _exact_promise_count(fake: FakeZeroDBTransport, count: int) -> None:
    rows = _promise_rows(fake)
    assert len(rows) == count, f"found {len(rows)} promise row(s), expected {count}"


@then("that promise has an actor, an action and an object")
def _has_four_parts(fake: FakeZeroDBTransport) -> None:
    row = _single_promise(fake)
    assert row["actor"], "promise has no actor"
    assert row["action"], "promise has no action"
    assert row["object"], "promise has no object"


@then(parsers.parse('that promise has direction "{direction}"'))
def _has_direction(fake: FakeZeroDBTransport, direction: str) -> None:
    row = _single_promise(fake)
    assert row["direction"] == direction, (
        f"direction is {row['direction']!r}, expected {direction!r}"
    )


@then("that promise is marked testable")
def _is_testable(fake: FakeZeroDBTransport) -> None:
    row = _single_promise(fake)
    assert row["testable"] is True, f"promise {row['quote_span']!r} is not marked testable"


@then("that promise is marked not testable")
def _is_not_testable(fake: FakeZeroDBTransport) -> None:
    rows = _promise_rows(fake)
    assert rows, "no promise rows were written"
    row = rows[0] if len(rows) == 1 else next(r for r in rows if not r["testable"])
    assert row["testable"] is False, f"promise {row['quote_span']!r} is marked testable"


@then("that promise references the candidacy")
def _references_candidacy(fake: FakeZeroDBTransport) -> None:
    assert _single_promise(fake)["candidacy_id"] == CANDIDACY_ID


@then("that promise references the same person as the candidacy")
def _references_person(fake: FakeZeroDBTransport) -> None:
    assert _single_promise(fake)["person_id"] == PERSON_ID


@then("that promise references the promise source it came from")
def _references_source(fake: FakeZeroDBTransport) -> None:
    assert _single_promise(fake)["source_id"] == SOURCE_ID


@then(parsers.parse('that promise has extractor_version "{version}"'))
def _has_extractor_version(fake: FakeZeroDBTransport, version: str) -> None:
    assert _single_promise(fake)["extractor_version"] == version


@then("that promise has a confidence between 0 and 1")
def _has_confidence(fake: FakeZeroDBTransport) -> None:
    confidence = _single_promise(fake)["confidence"]
    assert isinstance(confidence, (int, float))
    assert 0.0 <= float(confidence) <= 1.0


@then("that promise has no topic_id and no stance_id")
def _no_topic_or_stance(fake: FakeZeroDBTransport) -> None:
    row = _single_promise(fake)
    assert row["topic_id"] is None, "extraction must not pre-assign a topic (story 10.1 does)"
    assert row["stance_id"] is None, "extraction must not pre-assign a stance (story 10.1 does)"


@then("that promise has no mappings")
def _no_mappings(fake: FakeZeroDBTransport) -> None:
    assert _single_promise(fake)["mappings"] == []


@then("that promise has no vector_id")
def _no_vector_id(fake: FakeZeroDBTransport) -> None:
    assert _single_promise(fake)["vector_id"] is None, "embedding is story 9.4's job, not 9.3's"


@then("that promise is not flagged qa_pending")
def _not_qa_pending(fake: FakeZeroDBTransport) -> None:
    rows = _promise_rows(fake)
    assert rows, "no promise rows were written"
    for row in rows:
        assert row["qa_pending"] is False, "extraction must not flag a promise for mapping QA"


@then(parsers.parse("{count:d} of those promises is testable"))
@then(parsers.parse("{count:d} of those promises are testable"))
def _n_testable(fake: FakeZeroDBTransport, count: int) -> None:
    testable = [row for row in _promise_rows(fake) if row["testable"]]
    assert len(testable) == count, f"{len(testable)} testable promise(s), expected {count}"


@then(parsers.parse("{count:d} of those promises is not testable"))
@then(parsers.parse("{count:d} of those promises are not testable"))
def _n_not_testable(fake: FakeZeroDBTransport, count: int) -> None:
    rows = [row for row in _promise_rows(fake) if not row["testable"]]
    assert len(rows) == count, f"{len(rows)} non-testable promise(s), expected {count}"


@then("every promise quote_span is a verbatim substring of the source text")
def _quote_spans_verbatim(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    source_text = result["text"]
    rows = _promise_rows(fake)
    assert rows, "no promise rows were written"
    for row in rows:
        span = row["quote_span"]
        assert span in source_text, (
            f"quote_span {span!r} is not a verbatim substring of the source text"
        )


@then(parsers.parse('that promise quote_span still contains "{fragment}"'))
def _quote_span_contains(fake: FakeZeroDBTransport, fragment: str) -> None:
    row = _single_promise(fake)
    assert fragment in row["quote_span"], (
        f"quote_span {row['quote_span']!r} lost the verbatim fragment {fragment!r}"
    )


@then(parsers.parse("the extraction report says {count:d} promises were new"))
def _report_new_count(result: dict[str, Any], count: int) -> None:
    report: ExtractionReport = result["report"]
    assert report.new_promises == count, (
        f"report says {report.new_promises} new promise(s), expected {count}"
    )


@then("the extraction is refused for a quote span that is not in the source")
def _refused_for_quote_span(result: dict[str, Any]) -> None:
    error = result["error"]
    assert isinstance(error, QuoteSpanError), f"expected QuoteSpanError, got {error!r}"
    assert "quote_span" in str(error) or "verbatim" in str(error)


@then("the pipeline is refused for having no extractor configured")
def _refused_no_extractor(result: dict[str, Any]) -> None:
    error = result["error"]
    assert isinstance(error, ValueError), f"expected ValueError, got {error!r}"
    assert "extractor" in str(error).lower()


@then("a promise row exists for that source")
def _promise_row_exists(zerodb: ZeroDBClient, fake: FakeZeroDBTransport) -> None:
    rows = _promise_rows(fake)
    assert rows, "no promise rows were written"
    repo: Repository[Promise] = Repository(client=zerodb, model=Promise)
    stored = repo.get(rows[0]["id"])
    assert stored is not None
