"""Live-confirmed behaviour free-text interest mapping depends on (story 3.5).

Same purpose as `test_stance_cards_live_shapes.py` and
`test_embed_taxonomy_live_shapes.py`: each test here pins something measured
against the LIVE "informed" project during story 3.5, not inferred from the
docs or from the fake. The measurement output is in the story's PR.

What was measured, on 2026-10-16, with
`scripts/verify_free_text_interest_mapping_live.py` against the project that
already held story 3.4's live `topics` embeddings:

  * **The live semantic search response carries NO numeric score field at
    all.** `POST /projects/{id}/embeddings/search`'s raw JSON body has no
    `score` key on any result — not `0`, not `null` spelled out, simply
    absent — even though results arrive already ranked server-side, closest
    match first. `SearchHit.score` (story 1.2) defaults to `None` for an
    absent field, so this round-trips as `None`, not `0.0`. This matters
    because an earlier version of `TopicService.suggest` coerced a missing
    score to `0.0`, which would have shown every live suggestion as "0%
    confident" to a client that renders the score — worse than honestly
    reporting no score at all.
  * **Results are still genuinely, usefully ranked.** Five independent,
    synthetic free-text queries (never a real voter's words — see the verify
    script) each returned a taxonomy topic a human reader would call
    relevant as its #1 suggestion: a cost-of-medication worry surfaced
    "Prescription drug pricing" first; "can't find a doctor" surfaced
    "Healthcare" first; a slow/expensive home internet complaint surfaced
    "Technology and data privacy" (with "Broadband access" at #3); a
    worry about school funding surfaced "Higher education and student
    debt" then "School funding formulas"; a gas/groceries cost complaint
    surfaced "Electricity prices and grid capacity" then "Prescription drug
    pricing". None of the 5 came back empty or obviously random.
  * **No row was written to any of the live project's 24 tables, and no
    vector was added to the live `topics` namespace**, confirmed by
    comparing `ZeroDBClient.query_rows(..., limit=1).total` and
    `ZeroDBClient.get_vector_stats(namespace="topics")` before and after the
    whole run.
  * **No log line emitted by `app.*` during the run contained any of the 5
    sample free-text query strings.** Checked by attaching a `logging.Handler`
    to the `app` logger (not the root logger — see the verify script's own
    docstring for why) around every `TopicService.suggest(...)` call.

Nothing here makes a live call. The payload shape is the live one, replayed
through a monkeypatched `semantic_search`, which is what makes this runnable
in CI without credentials.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.repositories import Repository
from app.repositories.models import IssueTopic
from app.topics.service import TopicService
from app.zerodb import ZeroDBClient
from app.zerodb.models import SearchHit, SearchResults
from app.zerodb.testing import FakeZeroDBTransport

#: Copied (fields only, vector/project ids redacted) from a live
#: `POST /projects/{id}/embeddings/search` response against the real
#: "informed" project, querying the `topics` namespace with
#: "I'm worried about how much I pay for my medications every month". Note
#: what is ABSENT: no `score` key anywhere in the result object.
LIVE_SEARCH_HIT: dict[str, Any] = {
    "namespace": "topics",
    "vector_metadata": {
        "model": "bge-m3",
        "locale": "en-US",
        "topic_id": "en-US:prescription-drug-pricing:v1",
        "dimensions": 1024,
        "vector_column": "vector_embedding",
        "taxonomy_version": 1,
    },
    "document": "Prescription drug pricing — Healthcare",
    "source": None,
    "vector_id": "a7d353ec-d4ad-44fd-866d-af1fe20da320",
    "project_id": "REDACTED",
    "created_at": "2026-10-16T20:27:56.083099Z",
    "metadata": {
        "model": "bge-m3",
        "locale": "en-US",
        "topic_id": "en-US:prescription-drug-pricing:v1",
        "dimensions": 1024,
        "vector_column": "vector_embedding",
        "taxonomy_version": 1,
    },
}


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


def test_the_live_hit_shape_has_no_score_field() -> None:
    """The raw live payload itself, before any parsing — the primary source."""
    assert "score" not in LIVE_SEARCH_HIT


def test_the_live_hit_parses_with_a_none_score() -> None:
    """`SearchHit.score` (story 1.2) must default to `None`, not raise or 0."""
    hit = SearchHit.model_validate(LIVE_SEARCH_HIT)
    assert hit.score is None
    assert hit.metadata["topic_id"] == "en-US:prescription-drug-pricing:v1"


def test_suggest_reports_the_live_score_as_none_not_zero(
    client: ZeroDBClient, topics: Repository[IssueTopic], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this story's live finding guards against.

    `0.0` and "no score reported" are different facts — the first says
    "zero similarity", the second says "ZeroDB did not tell us". Coercing
    one into the other would make every live suggestion look maximally
    dissimilar to a client that renders the score, which is worse than
    simply not showing one.
    """
    topics.upsert(
        {
            "id": "en-US:healthcare:v1",
            "parent_id": None,
            "name": "Healthcare",
            "locale": "en-US",
            "taxonomy_version": 1,
        }
    )
    topics.upsert(
        {
            "id": "en-US:prescription-drug-pricing:v1",
            "parent_id": "en-US:healthcare:v1",
            "name": "Prescription drug pricing",
            "locale": "en-US",
            "taxonomy_version": 1,
        }
    )
    service = TopicService(client)

    def live_shaped_search(**_: object) -> SearchResults:
        return SearchResults(
            results=[SearchHit.model_validate(LIVE_SEARCH_HIT)],
            query="I'm worried about how much I pay for my medications every month",
            total_results=1,
        )

    monkeypatch.setattr(client, "semantic_search", live_shaped_search)

    suggestions = service.suggest(
        text="I'm worried about how much I pay for my medications every month", locale="en-US"
    )

    assert len(suggestions) == 1
    assert suggestions[0].topic_id == "en-US:prescription-drug-pricing:v1"
    assert suggestions[0].score is None, "a missing live score must stay None, never become 0.0"


def test_suggest_still_resolves_the_parent_from_live_metadata(
    client: ZeroDBClient, topics: Repository[IssueTopic], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live metadata shape (`topic_id` nested under `vector_metadata`,
    read by `SearchHit.metadata`) still carries enough to resolve a
    subtopic's parent for display, exactly as the fake's shape does.
    """
    topics.upsert(
        {
            "id": "en-US:healthcare:v1",
            "parent_id": None,
            "name": "Healthcare",
            "locale": "en-US",
            "taxonomy_version": 1,
        }
    )
    topics.upsert(
        {
            "id": "en-US:prescription-drug-pricing:v1",
            "parent_id": "en-US:healthcare:v1",
            "name": "Prescription drug pricing",
            "locale": "en-US",
            "taxonomy_version": 1,
        }
    )
    service = TopicService(client)

    def live_shaped_search(**_: object) -> SearchResults:
        return SearchResults(results=[SearchHit.model_validate(LIVE_SEARCH_HIT)])

    monkeypatch.setattr(client, "semantic_search", live_shaped_search)

    suggestions = service.suggest(text="prescription costs", locale="en-US")

    assert suggestions[0].is_subtopic is True
    assert suggestions[0].parent_id == "en-US:healthcare:v1"
    assert suggestions[0].parent_name == "Healthcare"
