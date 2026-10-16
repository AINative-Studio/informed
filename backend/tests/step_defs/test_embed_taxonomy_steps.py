"""Step definitions for tests/features/embed_taxonomy.feature (story 3.4).

Runs against story 1.2's in-memory fake transport, never live ZeroDB, so CI
needs no credentials — the rule every backend feature in this suite follows.
The live confirmation of the same behaviour lives in
`tests/test_embed_taxonomy_live_shapes.py` and
`scripts/verify_embed_taxonomy_live.py`.

The taxonomy and stance seeds run first in the Background, through the real
`TaxonomySeeder`/`StanceSeeder`, for the same reason `test_stance_cards_steps.py`
seeds the taxonomy before the stances: the embedder reads every live row, so a
handful of fixture rows would under-exercise the "one embedding per row"
assertions the whole feature is about.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.embed_taxonomy import (
    STANCES_NAMESPACE,
    TOPICS_NAMESPACE,
    TaxonomyEmbedder,
)
from app.repositories import Repository
from app.repositories.models import IssueTopic, Stance
from app.seed_stances import StanceSeeder, stance_row_ids
from app.seed_taxonomy import TaxonomySeeder, taxonomy_row_ids
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/embed_taxonomy.feature")


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table("issue_topics", {"id": "string"})
    client.create_table("stances", {"id": "string"})
    return client


@pytest.fixture
def topics_repo(zerodb: ZeroDBClient) -> Repository[IssueTopic]:
    return Repository(client=zerodb, model=IssueTopic)


@pytest.fixture
def stances_repo(zerodb: ZeroDBClient) -> Repository[Stance]:
    return Repository(client=zerodb, model=Stance)


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


# --- Background -------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="embed_client")
def embed_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the issue taxonomy has been seeded")
def taxonomy_seeded(zerodb: ZeroDBClient, fake: FakeZeroDBTransport) -> None:
    TaxonomySeeder(zerodb, pace=lambda: None).run()
    fake.calls.clear()


@given("the stance cards have been seeded")
def stances_seeded(zerodb: ZeroDBClient, fake: FakeZeroDBTransport) -> None:
    StanceSeeder(zerodb, pace=lambda: None).run()
    fake.calls.clear()


# --- running the pipeline ----------------------------------------------------


@when("I run the embedding pipeline")
def run_pipeline(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    topics_report, stances_report = TaxonomyEmbedder(zerodb, pace=lambda: None).run()
    result["topics_report"] = topics_report
    result["stances_report"] = stances_report


@given("I have run the embedding pipeline once")
def pipeline_already_run(zerodb: ZeroDBClient, fake: FakeZeroDBTransport) -> None:
    TaxonomyEmbedder(zerodb, pace=lambda: None).run()
    fake.calls.clear()


@when("I run the embedding pipeline again")
def run_pipeline_again(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    topics_report, stances_report = TaxonomyEmbedder(zerodb, pace=lambda: None).run()
    result["topics_report"] = topics_report
    result["stances_report"] = stances_report


@when("a topic's taxonomy_version changes")
def bump_a_topic_version(
    zerodb: ZeroDBClient, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    """Hand-edit one stored `issue_topics` row's version, as a panel edit would.

    Mutates the row directly in the fake's own store rather than through
    `Repository.upsert`, because an upsert would also need a valid full row —
    this isolates the one field the rebuild logic cares about. The id is
    recorded so later Then steps can check it specifically.
    """
    topic_id = "en-US:prescription-drug-pricing:v1"
    for envelope in fake.rows["issue_topics"].values():
        if envelope["row_data"].get("id") == topic_id:
            envelope["row_data"]["taxonomy_version"] = 2
            break
    else:
        raise AssertionError(f"{topic_id} not found in the fake's issue_topics store")
    result["bumped_topic_id"] = topic_id


@when("a stance's wording_version changes")
def bump_a_stance_version(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    stance_id = "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    for envelope in fake.rows["stances"].values():
        if envelope["row_data"].get("id") == stance_id:
            envelope["row_data"]["wording_version"] = 2
            break
    else:
        raise AssertionError(f"{stance_id} not found in the fake's stances store")
    result["bumped_stance_id"] = stance_id


@given("the topics namespace already holds a bootstrap marker vector")
def seed_bootstrap_marker(fake: FakeZeroDBTransport) -> None:
    """Mirrors story 1.3's bootstrap, which left exactly this shape of vector
    live in every namespace — confirmed during this story's live verification.
    """
    fake.vectors.setdefault(TOPICS_NAMESPACE, []).append(
        {
            "vector_id": "bootstrap-marker-vector",
            "document": "informed schema bootstrap marker for namespace topics",
            "namespace": TOPICS_NAMESPACE,
            "metadata": {
                "model": "bge-m3",
                "dimensions": 1024,
                "bootstrap_marker": True,
            },
        }
    )


# --- assertions --------------------------------------------------------------


def _vectors_for(fake: FakeZeroDBTransport, namespace: str) -> list[dict[str, Any]]:
    return fake.vectors.get(namespace, [])


def _real_vectors(fake: FakeZeroDBTransport, namespace: str, id_key: str) -> list[dict[str, Any]]:
    """Every vector in `namespace` that carries `id_key` — excludes the
    bootstrap marker, which carries neither `topic_id` nor `stance_id`.
    """
    return [v for v in _vectors_for(fake, namespace) if v["metadata"].get(id_key)]


@then("the topics namespace has one embedding per stored topic")
def topics_namespace_fully_populated(fake: FakeZeroDBTransport) -> None:
    real = _real_vectors(fake, TOPICS_NAMESPACE, "topic_id")
    ids = {v["metadata"]["topic_id"] for v in real}
    assert len(real) == len(ids) == len(taxonomy_row_ids())
    assert ids == taxonomy_row_ids()


@then("the topics namespace still has one embedding per stored topic")
def topics_namespace_still_fully_populated(fake: FakeZeroDBTransport) -> None:
    topics_namespace_fully_populated(fake)


@then("the stances namespace has one embedding per stored stance")
def stances_namespace_fully_populated(fake: FakeZeroDBTransport) -> None:
    real = _real_vectors(fake, STANCES_NAMESPACE, "stance_id")
    ids = {v["metadata"]["stance_id"] for v in real}
    assert len(real) == len(ids) == len(stance_row_ids())
    assert ids == stance_row_ids()


@then("the stances namespace still has one embedding per stored stance")
def stances_namespace_still_fully_populated(fake: FakeZeroDBTransport) -> None:
    stances_namespace_fully_populated(fake)


@then("every topic embedding's metadata has topic_id, locale and taxonomy_version")
def topic_metadata_shape(fake: FakeZeroDBTransport) -> None:
    for vector in _real_vectors(fake, TOPICS_NAMESPACE, "topic_id"):
        metadata = vector["metadata"]
        assert {"topic_id", "locale", "taxonomy_version"} <= set(metadata)
        assert metadata["locale"] == "en-US"
        assert isinstance(metadata["taxonomy_version"], int)


@then("every stance embedding's metadata has stance_id, topic_id and wording_version")
def stance_metadata_shape(fake: FakeZeroDBTransport) -> None:
    for vector in _real_vectors(fake, STANCES_NAMESPACE, "stance_id"):
        metadata = vector["metadata"]
        assert {"stance_id", "topic_id", "wording_version"} <= set(metadata)
        assert isinstance(metadata["wording_version"], int)


@then(
    parsers.parse('the embedded text for "{topic_id}" includes its own name and its parent\'s name')
)
def subtopic_text_includes_parent(
    fake: FakeZeroDBTransport, topics_repo: Any, topic_id: str
) -> None:
    topic = topics_repo.get(topic_id)
    assert topic is not None
    parent = topics_repo.get(topic.parent_id)
    assert parent is not None

    [vector] = [
        v
        for v in _real_vectors(fake, TOPICS_NAMESPACE, "topic_id")
        if v["metadata"]["topic_id"] == topic_id
    ]
    assert topic.name in vector["document"]
    assert parent.name in vector["document"]


@then(
    parsers.parse(
        'the embedded text for stance "{stance_id}" includes its label and its description'
    )
)
def stance_text_includes_both_fields(
    fake: FakeZeroDBTransport, stances_repo: Any, stance_id: str
) -> None:
    stance = stances_repo.get(stance_id)
    assert stance is not None

    [vector] = [
        v
        for v in _real_vectors(fake, STANCES_NAMESPACE, "stance_id")
        if v["metadata"]["stance_id"] == stance_id
    ]
    assert stance.label.rstrip(".") in vector["document"]
    assert stance.description in vector["document"]


@then("the second run embedded no topics")
def second_run_embedded_no_topics(result: dict[str, Any]) -> None:
    assert result["topics_report"].written == []


@then("the second run embedded no stances")
def second_run_embedded_no_stances(result: dict[str, Any]) -> None:
    assert result["stances_report"].written == []


@then("that topic's old embedding is gone")
def old_topic_embedding_gone(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    topic_id = result["bumped_topic_id"]
    matching = [
        v
        for v in _real_vectors(fake, TOPICS_NAMESPACE, "topic_id")
        if v["metadata"]["topic_id"] == topic_id and v["metadata"]["taxonomy_version"] == 1
    ]
    assert matching == []


@then("that topic has exactly one embedding, at the new version")
def topic_has_one_embedding_at_new_version(
    fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    topic_id = result["bumped_topic_id"]
    matching = [
        v
        for v in _real_vectors(fake, TOPICS_NAMESPACE, "topic_id")
        if v["metadata"]["topic_id"] == topic_id
    ]
    assert len(matching) == 1
    assert matching[0]["metadata"]["taxonomy_version"] == 2
    assert result["topics_report"].rebuilt == [topic_id]


@then("every other topic's embedding is unchanged")
def other_topics_unchanged(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    bumped = result["bumped_topic_id"]
    real = _real_vectors(fake, TOPICS_NAMESPACE, "topic_id")
    others = [v for v in real if v["metadata"]["topic_id"] != bumped]
    assert len(others) == len(taxonomy_row_ids()) - 1
    assert all(v["metadata"]["taxonomy_version"] == 1 for v in others)
    assert bumped not in result["topics_report"].skipped


@then("that stance's old embedding is gone")
def old_stance_embedding_gone(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    stance_id = result["bumped_stance_id"]
    matching = [
        v
        for v in _real_vectors(fake, STANCES_NAMESPACE, "stance_id")
        if v["metadata"]["stance_id"] == stance_id and v["metadata"]["wording_version"] == 1
    ]
    assert matching == []


@then("that stance has exactly one embedding, at the new version")
def stance_has_one_embedding_at_new_version(
    fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    stance_id = result["bumped_stance_id"]
    matching = [
        v
        for v in _real_vectors(fake, STANCES_NAMESPACE, "stance_id")
        if v["metadata"]["stance_id"] == stance_id
    ]
    assert len(matching) == 1
    assert matching[0]["metadata"]["wording_version"] == 2
    assert result["stances_report"].rebuilt == [stance_id]


@then("every other stance's embedding is unchanged")
def other_stances_unchanged(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    bumped = result["bumped_stance_id"]
    real = _real_vectors(fake, STANCES_NAMESPACE, "stance_id")
    others = [v for v in real if v["metadata"]["stance_id"] != bumped]
    assert len(others) == len(stance_row_ids()) - 1
    assert all(v["metadata"]["wording_version"] == 1 for v in others)
    assert bumped not in result["stances_report"].skipped


@then("the bootstrap marker vector is still present and unchanged")
def bootstrap_marker_untouched(fake: FakeZeroDBTransport) -> None:
    markers = [
        v for v in _vectors_for(fake, TOPICS_NAMESPACE) if v["metadata"].get("bootstrap_marker")
    ]
    assert len(markers) == 1
    assert markers[0]["vector_id"] == "bootstrap-marker-vector"
    assert markers[0]["document"] == "informed schema bootstrap marker for namespace topics"
