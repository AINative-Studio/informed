"""Unit tests for the stance read service (story 3.2).

The acceptance criterion is "display order is randomized per session; no stance
is preselected". Those are two different claims that live in two different
layers, and this module is where the split is pinned:

  * **The backend owes "no implied ranking."** It must never hand a caller a
    stable order — not insertion order, not direction order, not alphabetical.
    `StanceService.for_topic` shuffles on every call, which is what these tests
    assert.
  * **The client owes "per session."** A voter must see the SAME order for the
    whole time they are filling in their profile, or the cards jump under their
    cursor between renders. That needs session state, and the backend
    deliberately keeps none — see `app.stances.service`'s module docstring for
    why, and story 3.6 for the frontend that does it.

So "shuffled per request" here is not a half-measure standing in for the real
thing; it is the half of the criterion that belongs on this side of the wire,
and it is the half that makes the other half safe to get wrong — a client that
forgets to shuffle still shows no consistent first option.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from app.repositories import Repository
from app.repositories.models import Stance
from app.seed_stances import StanceSeeder, stances_by_topic
from app.seed_taxonomy import TaxonomySeeder
from app.stances.service import StanceService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

#: A contested subtopic with a full set of cards; used wherever the test needs
#: a real topic id rather than a synthetic one.
CONTESTED_TOPIC = "en-US:background-checks-and-purchase-rules:v1"


@pytest.fixture
def client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table("issue_topics", {"id": "string"})
    client.create_table("stances", {"id": "string"})
    TaxonomySeeder(client, pace=lambda: None).run()
    StanceSeeder(client, pace=lambda: None).run()
    return client


@pytest.fixture
def service(client: ZeroDBClient) -> StanceService:
    """Seeded RNG so the suite cannot flake on a shuffle landing on itself."""
    return StanceService(client, rng=random.Random(20260316))


# --- completeness -----------------------------------------------------------


def test_for_topic_returns_every_stance_stored_for_that_subtopic(service: StanceService) -> None:
    expected = {card.row_id for card in stances_by_topic()[CONTESTED_TOPIC]}
    got = {str(row.id) for row in service.for_topic(CONTESTED_TOPIC)}
    assert got == expected


def test_for_topic_returns_nothing_for_a_topic_with_no_stances(service: StanceService) -> None:
    """A top-level topic, which by design carries no cards.

    An empty list rather than an error: "this topic has no stance cards" is a
    real answer for a parent node, and the caller renders its children instead.
    """
    assert service.for_topic("en-US:healthcare:v1") == []


def test_for_topic_returns_nothing_for_an_unknown_topic(service: StanceService) -> None:
    assert service.for_topic("en-US:no-such-topic:v1") == []


def test_for_topic_never_leaks_another_subtopics_cards(service: StanceService) -> None:
    """The prefix match must be on the whole topic id, not a substring.

    `en-US:social-security-solvency:v1` is a prefix of nothing, but the filter
    is written as an equality on `topic_id` rather than a `startswith` on the
    id for exactly this reason; asserted so a later "optimisation" to prefix
    matching fails here.
    """
    for row in service.for_topic(CONTESTED_TOPIC):
        assert row.topic_id == CONTESTED_TOPIC


# --- no implied ranking -----------------------------------------------------


def test_two_calls_return_different_orders(service: StanceService) -> None:
    first = [str(row.id) for row in service.for_topic(CONTESTED_TOPIC)]
    second = [str(row.id) for row in service.for_topic(CONTESTED_TOPIC)]
    assert set(first) == set(second)
    assert first != second, f"both calls returned the same order: {first}"


def test_no_stance_occupies_the_first_slot_disproportionately(client: ZeroDBClient) -> None:
    """The real question: is any card a de facto default?

    A shuffle that is actually a rotation, or one that pins the first element,
    passes "two calls differ" and still gives one card most of the attention.
    So this samples 600 calls and asserts every card lands first roughly its
    fair share of the time. Seeded, so the bound is a fact about this RNG
    sequence rather than a flake waiting to happen.
    """
    service = StanceService(client, rng=random.Random(7))
    cards = stances_by_topic()[CONTESTED_TOPIC]
    trials = 600
    firsts = Counter(str(service.for_topic(CONTESTED_TOPIC)[0].id) for _ in range(trials))

    assert set(firsts) == {card.row_id for card in cards}, (
        f"only {len(firsts)} of {len(cards)} cards ever appeared first"
    )
    fair_share = trials / len(cards)
    for row_id, count in firsts.items():
        assert 0.6 * fair_share <= count <= 1.4 * fair_share, (
            f"{row_id} came first {count} times in {trials}, fair share is {fair_share:.0f}"
        )


def test_the_returned_order_is_not_the_direction_order(client: ZeroDBClient) -> None:
    """Sorted-by-direction is the specific wrong answer worth naming.

    It is what a well-meaning implementation reaches for ("show them in order
    across the spectrum"), it looks principled, and it silently makes the most
    negative position the first thing every voter reads. Sampled rather than
    checked once, because a single shuffle could land on it by chance.
    """
    service = StanceService(client, rng=random.Random(11))
    sorted_runs = 0
    runs = 200
    for _ in range(runs):
        codes = [int(row.direction_code) for row in service.for_topic(CONTESTED_TOPIC)]
        if codes == sorted(codes) or codes == sorted(codes, reverse=True):
            sorted_runs += 1
    assert sorted_runs < runs * 0.3, (
        f"{sorted_runs}/{runs} reads came back in axis order; the shuffle is not shuffling"
    )


def test_the_service_does_not_sort_by_id_or_label(service: StanceService) -> None:
    """Alphabetical is a ranking too, and a stable one."""
    ids = [str(row.id) for row in service.for_topic(CONTESTED_TOPIC)]
    labels = [row.label for row in service.for_topic(CONTESTED_TOPIC)]
    assert ids != sorted(ids)
    assert labels != sorted(labels)


# --- nothing is preselected -------------------------------------------------


def test_no_returned_stance_carries_a_selection_flag(service: StanceService) -> None:
    forbidden = {"default", "is_default", "preselected", "selected", "recommended"}
    for row in service.for_topic(CONTESTED_TOPIC):
        assert not set(row.row_data()) & forbidden


def test_the_service_exposes_no_method_that_picks_one(service: StanceService) -> None:
    """No `default_for`, no `suggested`, no `top`.

    Pinned as an absence because the absence is the feature: a convenience
    method returning "the usual pick" would be the single most load-bearing
    nudge in the product, and it is the kind of thing added later for a
    plausible reason (an empty-state placeholder, a demo fixture).
    """
    public = {name for name in dir(service) if not name.startswith("_")}
    assert public == {"for_topic", "for_topics"}, f"the service grew a method: {sorted(public)}"


# --- the batch read ---------------------------------------------------------


def test_for_topics_reads_several_subtopics_in_one_pass(service: StanceService) -> None:
    """The onboarding screen needs 3-5 subtopics' cards at once.

    One read for all of them rather than one per subtopic, because live ZeroDB
    throttles at 60 requests per minute (datamodel.md "Open questions") and a
    voter picking five subtopics would otherwise cost five page reads.
    """
    wanted = [
        CONTESTED_TOPIC,
        "en-US:prescription-drug-pricing:v1",
        "en-US:gestational-limits-and-exceptions:v1",
    ]
    got = service.for_topics(wanted)
    assert set(got) == set(wanted)
    for topic_id in wanted:
        assert {str(row.id) for row in got[topic_id]} == {
            card.row_id for card in stances_by_topic()[topic_id]
        }


def test_for_topics_shuffles_each_subtopic_independently(service: StanceService) -> None:
    """One shuffle applied to the whole batch would correlate the orders.

    Not a theoretical concern: shuffling a flat list and then grouping gives
    each subtopic a random order, but a caller showing two subtopics side by
    side would see their orders move together across requests, which over a
    session is a pattern a voter can read.
    """
    wanted = [CONTESTED_TOPIC, "en-US:prescription-drug-pricing:v1"]
    first = service.for_topics(wanted)
    second = service.for_topics(wanted)
    moved = [
        topic_id
        for topic_id in wanted
        if [str(r.id) for r in first[topic_id]] != [str(r.id) for r in second[topic_id]]
    ]
    assert len(moved) == len(wanted), f"only {moved} changed order between calls"


def test_for_topics_includes_an_empty_list_for_a_subtopic_with_no_cards(
    service: StanceService,
) -> None:
    """The caller asked about it, so it gets an answer rather than a hole."""
    got = service.for_topics([CONTESTED_TOPIC, "en-US:healthcare:v1"])
    assert got["en-US:healthcare:v1"] == []
    assert got[CONTESTED_TOPIC]


def test_for_topics_with_no_topics_reads_nothing(client: ZeroDBClient) -> None:
    """An empty request must not fall back to reading the whole table."""
    transport = client._transport  # noqa: SLF001 - asserting no call was made
    service = StanceService(client, rng=random.Random(1))
    before = len(transport.calls)
    assert service.for_topics([]) == {}
    assert len(transport.calls) == before, "an empty batch still hit ZeroDB"


# --- the default RNG --------------------------------------------------------


def test_the_service_uses_the_module_random_by_default(client: ZeroDBClient) -> None:
    """Production takes the system RNG; only tests inject a seeded one.

    `random.SystemRandom` is not used: this is a display order, not a secret,
    and a seeded `Random` is what makes the ordering tests above
    deterministic — a service that reached for `secrets` could not be tested
    for fairness at all.
    """
    service = StanceService(client)
    assert service._rng is random  # noqa: SLF001 - asserting the default


# --- rows still validate ----------------------------------------------------


def test_returned_rows_are_validated_stance_models(client: ZeroDBClient) -> None:
    """The service reads through the repository, so a drifted row raises here.

    Not cosmetic: the stance id reaches a voter's encrypted profile, and a row
    that stopped validating would otherwise be silently dropped from the menu
    rather than failing loudly.
    """
    service = StanceService(client, rng=random.Random(3))
    rows = service.for_topic(CONTESTED_TOPIC)
    assert rows
    for row in rows:
        assert isinstance(row, Stance)
        assert row.wording_version == 1

    repo: Repository[Stance] = Repository(client=client, model=Stance)
    assert repo.get(str(rows[0].id)) == rows[0]
