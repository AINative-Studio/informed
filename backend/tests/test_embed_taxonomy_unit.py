"""Unit tests for app.embed_taxonomy's pure functions (story 3.4).

The Gherkin feature (`tests/features/embed_taxonomy.feature`) and its step
definitions cover the pipeline end to end against the fake transport; this
module is the narrower, faster check on the text-building and vector-indexing
logic in isolation, the same split story 3.1/3.2 use between their unit tests
and their feature files.
"""

from __future__ import annotations

from app.embed_taxonomy import (
    STANCES_NAMESPACE,
    TOPICS_NAMESPACE,
    EmbedReport,
    _ExistingVector,
    _index_existing_vectors,
    _version_from_metadata,
    stance_embedding_text,
    topic_embedding_text,
)
from app.repositories.models import IssueTopic, Stance
from app.zerodb.models import VectorRecord


def _topic(**overrides: object) -> IssueTopic:
    defaults: dict[str, object] = {
        "id": "en-US:healthcare:v1",
        "parent_id": None,
        "name": "Healthcare",
        "locale": "en-US",
        "taxonomy_version": 1,
    }
    defaults.update(overrides)
    return IssueTopic.model_validate(defaults)


def _stance(**overrides: object) -> Stance:
    defaults: dict[str, object] = {
        "id": "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs",
        "topic_id": "en-US:prescription-drug-pricing:v1",
        "label": "Cap what patients pay at the pharmacy counter",
        "description": "Set a legal ceiling on monthly out-of-pocket costs.",
        "direction_code": "+1",
        "wording_version": 1,
    }
    defaults.update(overrides)
    return Stance.model_validate(defaults)


# --- topic_embedding_text ----------------------------------------------------


def test_a_top_level_topic_embeds_its_bare_name() -> None:
    topic = _topic(name="Healthcare")
    assert topic_embedding_text(topic, parent=None) == "Healthcare"


def test_a_subtopic_embeds_its_name_and_its_parents_name() -> None:
    parent = _topic(id="en-US:healthcare:v1", name="Healthcare")
    subtopic = _topic(
        id="en-US:prescription-drug-pricing:v1",
        parent_id="en-US:healthcare:v1",
        name="Prescription drug pricing",
    )
    text = topic_embedding_text(subtopic, parent=parent)
    assert "Prescription drug pricing" in text
    assert "Healthcare" in text


# --- stance_embedding_text ---------------------------------------------------


def test_stance_text_includes_label_and_description() -> None:
    stance = _stance(
        label="Cap what patients pay",
        description="Set a legal ceiling on monthly costs.",
    )
    text = stance_embedding_text(stance)
    assert "Cap what patients pay" in text
    assert "Set a legal ceiling on monthly costs." in text


def test_stance_text_does_not_double_the_label_s_own_period() -> None:
    stance = _stance(label="End the program.", description="Stop funding it entirely.")
    text = stance_embedding_text(stance)
    assert ".." not in text


# --- _version_from_metadata ---------------------------------------------------


def test_version_from_metadata_reads_an_int() -> None:
    assert _version_from_metadata({"taxonomy_version": 2}, "taxonomy_version") == 2


def test_version_from_metadata_coerces_a_string_digit() -> None:
    # Metadata survives a round trip through JSON; a version stored by some
    # other writer as a string should still compare sensibly.
    assert _version_from_metadata({"wording_version": "3"}, "wording_version") == 3


def test_version_from_metadata_is_none_when_absent() -> None:
    assert _version_from_metadata({}, "taxonomy_version") is None


def test_version_from_metadata_is_none_for_garbage() -> None:
    assert _version_from_metadata({"taxonomy_version": "not-a-number"}, "taxonomy_version") is None


# --- _index_existing_vectors ---------------------------------------------------


def _record(vector_id: str, metadata: dict[str, object]) -> VectorRecord:
    return VectorRecord.model_validate(
        {
            "vector_id": vector_id,
            "namespace": TOPICS_NAMESPACE,
            "document": "irrelevant for this test",
            "vector_metadata": metadata,
        }
    )


def test_indexes_vectors_by_the_declared_id_key() -> None:
    records = [
        _record("v1", {"topic_id": "en-US:healthcare:v1", "taxonomy_version": 1}),
        _record("v2", {"topic_id": "en-US:housing:v1", "taxonomy_version": 1}),
    ]
    index = _index_existing_vectors(records, id_key="topic_id", version_key="taxonomy_version")
    assert set(index) == {"en-US:healthcare:v1", "en-US:housing:v1"}
    assert index["en-US:healthcare:v1"] == [_ExistingVector(vector_id="v1", version=1)]


def test_a_vector_with_no_id_key_is_excluded() -> None:
    """The bootstrap marker vector has no topic_id/stance_id and must never
    be mistaken for a real topic/stance embedding.
    """
    records = [
        _record("marker", {"bootstrap_marker": True, "model": "bge-m3"}),
        _record("v1", {"topic_id": "en-US:healthcare:v1", "taxonomy_version": 1}),
    ]
    index = _index_existing_vectors(records, id_key="topic_id", version_key="taxonomy_version")
    assert list(index) == ["en-US:healthcare:v1"]


def test_two_vectors_for_the_same_id_are_both_kept() -> None:
    """A rebuild must delete every stale vector for an id, not just the first
    one it finds — so the index groups by id as a list, never collapsing.
    """
    records = [
        _record("old", {"stance_id": "s:1", "wording_version": 1}),
        _record("new", {"stance_id": "s:1", "wording_version": 2}),
    ]
    index = _index_existing_vectors(records, id_key="stance_id", version_key="wording_version")
    assert len(index["s:1"]) == 2


def test_namespaces_are_independent_constants() -> None:
    assert TOPICS_NAMESPACE == "topics"
    assert STANCES_NAMESPACE == "stances"


# --- EmbedReport.row_count ----------------------------------------------------


def test_row_count_is_written_plus_skipped() -> None:
    """`row_count` is the total number of ids now current — a freshly written
    id and a skipped (already-current) one both count; `rebuilt` is a subset
    of `written` and must not be double-counted.
    """
    report = EmbedReport(written=["a", "b"], rebuilt=["b"], skipped=["c"])
    assert report.row_count == 3


def test_row_count_is_zero_for_an_empty_report() -> None:
    assert EmbedReport().row_count == 0
