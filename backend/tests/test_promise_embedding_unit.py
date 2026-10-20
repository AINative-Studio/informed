"""Unit tests for story 9.4/9.5's embedding pieces.

The BDD scenarios in `tests/features/promise_embedding.feature` cover the
end-to-end behaviour. These cover the three decision functions whose edge
cases do not read well as Gherkin:

  * `promise_embedding_text` -- what text is actually embedded. The whole
    value of story 10.1's bill matching rests on this being the promise's own
    words and nothing else.
  * `cycle_for_election_id` / `cycle_for` -- deriving datamodel.md's `cycle`
    metadata field, which no model carries as a column.
  * `promise_vector_metadata` -- the exact metadata contract datamodel.md's
    `promises` namespace names, including the fields that are legitimately
    null today.
"""

from __future__ import annotations

import pytest

from app.ingest.promise_embedding import (
    PROMISE_EXTRACTED_TOPIC,
    PROMISES_NAMESPACE,
    PromiseEmbeddingReport,
    cycle_for_election_id,
    promise_embedding_text,
    promise_vector_metadata,
)
from app.repositories.models import Promise

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
    assert promise_vector_metadata(classified, cycle=2026)["topic_id"] == (
        "topic:healthcare-costs"
    )


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
