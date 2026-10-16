"""Unit tests for `TopicService.suggest` (story 3.5).

The Gherkin feature (`tests/features/free_text_interest_mapping.feature`) and
its step definitions cover the HTTP endpoint end to end against the fake
transport; this module is the narrower, faster check on `suggest`'s own
filtering/dedup/limit logic, the same split story 3.4's unit tests use
against its feature file.
"""

from __future__ import annotations

import pytest

from app.embed_taxonomy import TOPICS_NAMESPACE
from app.repositories import Repository
from app.repositories.models import IssueTopic
from app.topics.service import SUGGESTION_LIMIT, TopicService
from app.zerodb import ZeroDBClient
from app.zerodb.models import SearchHit, SearchResults
from app.zerodb.testing import FakeZeroDBTransport


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    zdb = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    zdb.create_table("issue_topics", {"id": "string"})
    return zdb


@pytest.fixture
def topics(client: ZeroDBClient) -> Repository[IssueTopic]:
    return Repository(client=client, model=IssueTopic)


def _topic(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "id": "en-US:healthcare:v1",
        "parent_id": None,
        "name": "Healthcare",
        "locale": "en-US",
        "taxonomy_version": 1,
    }
    defaults.update(overrides)
    return defaults


def _seed_healthcare_tree(topics: Repository[IssueTopic]) -> None:
    topics.upsert(_topic(id="en-US:healthcare:v1", name="Healthcare"))
    topics.upsert(
        _topic(
            id="en-US:prescription-drug-pricing:v1",
            parent_id="en-US:healthcare:v1",
            name="Prescription drug pricing",
        )
    )


def test_suggest_returns_at_most_the_suggestion_limit(
    client: ZeroDBClient, topics: Repository[IssueTopic]
) -> None:
    _seed_healthcare_tree(topics)
    client.embed_and_store(
        texts=["Healthcare"],
        namespace=TOPICS_NAMESPACE,
        metadata_list=[
            {"topic_id": "en-US:healthcare:v1", "locale": "en-US", "taxonomy_version": 1}
        ],
    )
    client.embed_and_store(
        texts=["Prescription drug pricing — Healthcare"],
        namespace=TOPICS_NAMESPACE,
        metadata_list=[
            {
                "topic_id": "en-US:prescription-drug-pricing:v1",
                "locale": "en-US",
                "taxonomy_version": 1,
            }
        ],
    )
    service = TopicService(client)

    suggestions = service.suggest(text="prescription drug prices", locale="en-US")

    assert len(suggestions) <= SUGGESTION_LIMIT


def test_a_subtopic_suggestion_carries_its_parent(
    client: ZeroDBClient, topics: Repository[IssueTopic]
) -> None:
    _seed_healthcare_tree(topics)
    client.embed_and_store(
        texts=["Prescription drug pricing — Healthcare"],
        namespace=TOPICS_NAMESPACE,
        metadata_list=[
            {
                "topic_id": "en-US:prescription-drug-pricing:v1",
                "locale": "en-US",
                "taxonomy_version": 1,
            }
        ],
    )
    service = TopicService(client)

    suggestions = service.suggest(text="prescription drug pricing", locale="en-US")

    assert len(suggestions) == 1
    only = suggestions[0]
    assert only.topic_id == "en-US:prescription-drug-pricing:v1"
    assert only.is_subtopic is True
    assert only.parent_id == "en-US:healthcare:v1"
    assert only.parent_name == "Healthcare"


def test_a_top_level_suggestion_carries_no_parent(
    client: ZeroDBClient, topics: Repository[IssueTopic]
) -> None:
    _seed_healthcare_tree(topics)
    client.embed_and_store(
        texts=["Healthcare"],
        namespace=TOPICS_NAMESPACE,
        metadata_list=[
            {"topic_id": "en-US:healthcare:v1", "locale": "en-US", "taxonomy_version": 1}
        ],
    )
    service = TopicService(client)

    suggestions = service.suggest(text="healthcare", locale="en-US")

    assert len(suggestions) == 1
    only = suggestions[0]
    assert only.is_subtopic is False
    assert only.parent_id is None
    assert only.parent_name is None


def test_suggest_never_duplicates_a_topic_id(
    client: ZeroDBClient, topics: Repository[IssueTopic]
) -> None:
    """A topic embedded more than once (e.g. mid-rebuild) must appear once."""
    _seed_healthcare_tree(topics)
    for _ in range(2):
        client.embed_and_store(
            texts=["Healthcare"],
            namespace=TOPICS_NAMESPACE,
            metadata_list=[
                {"topic_id": "en-US:healthcare:v1", "locale": "en-US", "taxonomy_version": 1}
            ],
        )
    service = TopicService(client)

    suggestions = service.suggest(text="healthcare", locale="en-US")

    topic_ids = [s.topic_id for s in suggestions]
    assert len(topic_ids) == len(set(topic_ids)), f"duplicate topic_id in {topic_ids}"


def test_suggest_filters_out_a_different_locale(
    client: ZeroDBClient, topics: Repository[IssueTopic]
) -> None:
    topics.upsert(_topic(id="es-US:healthcare:v1", name="Atención médica", locale="es-US"))
    client.embed_and_store(
        texts=["Atención médica"],
        namespace=TOPICS_NAMESPACE,
        metadata_list=[
            {"topic_id": "es-US:healthcare:v1", "locale": "es-US", "taxonomy_version": 1}
        ],
    )
    service = TopicService(client)

    suggestions = service.suggest(text="healthcare", locale="en-US")

    assert suggestions == []


def test_suggest_returns_empty_for_an_empty_namespace(
    client: ZeroDBClient, topics: Repository[IssueTopic]
) -> None:
    """No embeddings at all is a real, renderable 'no suggestions' answer."""
    _seed_healthcare_tree(topics)
    service = TopicService(client)

    suggestions = service.suggest(text="anything at all", locale="en-US")

    assert suggestions == []


def test_suggest_ignores_the_bootstrap_marker_vector(
    client: ZeroDBClient, topics: Repository[IssueTopic]
) -> None:
    """A story-1.3 bootstrap marker carries no topic_id and must be skipped."""
    _seed_healthcare_tree(topics)
    client.embed_and_store(
        texts=["bootstrap marker"],
        namespace=TOPICS_NAMESPACE,
        metadata_list=[{"bootstrap_marker": True}],
    )
    service = TopicService(client)

    suggestions = service.suggest(text="anything", locale="en-US")

    assert suggestions == []


def test_suggest_never_writes_a_row_or_a_vector(
    client: ZeroDBClient, topics: Repository[IssueTopic], fake: FakeZeroDBTransport
) -> None:
    _seed_healthcare_tree(topics)
    client.embed_and_store(
        texts=["Healthcare"],
        namespace=TOPICS_NAMESPACE,
        metadata_list=[
            {"topic_id": "en-US:healthcare:v1", "locale": "en-US", "taxonomy_version": 1}
        ],
    )
    service = TopicService(client)
    rows_before = {table: len(store) for table, store in fake.rows.items()}
    vectors_before = {ns: len(vs) for ns, vs in fake.vectors.items()}

    service.suggest(text="healthcare", locale="en-US")

    rows_after = {table: len(store) for table, store in fake.rows.items()}
    vectors_after = {ns: len(vs) for ns, vs in fake.vectors.items()}
    assert rows_after == rows_before
    assert vectors_after == vectors_before


def test_suggest_tolerates_a_live_score_of_none(
    client: ZeroDBClient, topics: Repository[IssueTopic], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the live finding: ZeroDB's real search response carries no score.

    Confirmed live against the real "informed" project during this story:
    `POST .../embeddings/search`'s response has no numeric score field at
    all (results still arrive correctly ranked, closest match first), so
    `SearchHit.score` parses as `None` there even though the in-memory fake
    always reports a real float. `semantic_search` is monkeypatched here to
    return that exact live shape — a `SearchHit` with `score=None` — so this
    scenario is reproducible in CI without live credentials, the same
    reasoning `test_stance_cards_live_shapes.py` uses for its own pinned
    live payloads.
    """
    _seed_healthcare_tree(topics)
    service = TopicService(client)

    def fake_search(**_: object) -> SearchResults:
        return SearchResults(
            results=[
                SearchHit(
                    vector_id="v1",
                    document="Healthcare",
                    namespace=TOPICS_NAMESPACE,
                    score=None,
                    metadata={"topic_id": "en-US:healthcare:v1"},
                )
            ],
            query="healthcare",
            total_results=1,
        )

    monkeypatch.setattr(client, "semantic_search", fake_search)

    suggestions = service.suggest(text="healthcare", locale="en-US")

    assert len(suggestions) == 1
    assert suggestions[0].topic_id == "en-US:healthcare:v1"
    assert suggestions[0].score is None
