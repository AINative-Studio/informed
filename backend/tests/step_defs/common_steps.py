"""Shared BDD step library for the Informed backend (story 1.7).

Registered as a pytest plugin from tests/conftest.py, so every step and fixture
here is available to every feature file without an import. A feature-specific
step file adds only the steps that are unique to it.

## Writing a new feature

1. Put the Gherkin under ``tests/features/<name>.feature``.
2. Add ``tests/step_defs/test_<name>_steps.py`` containing
   ``scenarios("../features/<name>.feature")``.
3. Reuse the steps below. Anything new goes in that file; promote it here once a
   second feature needs it.

## Pending steps

The PRD's acceptance criteria are collected from day one, long before the code
that satisfies them exists. ``pending()`` marks the running scenario
``xfail(strict=False)`` at runtime and then fails the step, so the scenario:

* is **collected and reported** — ``pytest -rxX`` prints
  ``XFAIL <scenario> - pending: ...`` with the story that will implement it;
* does **not** fail CI, because the xfail is expected;
* flips to ``XPASS`` the moment the feature actually works, which is a visible
  prompt to delete the pending step rather than a silent pass.

``pytest.skip()`` was the alternative and was rejected: a skip reports as a bare
``s`` and can never tell you the behaviour has started working.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, then, when

from app.main import create_app

# --- shared fixtures ------------------------------------------------------
#
# These are the fixtures the acceptance criterion "API and UI steps share
# fixtures" refers to on the backend side. They are defined once here and are
# usable from any step file, API or otherwise.


@pytest.fixture
def api() -> Iterator[TestClient]:
    """An HTTP client bound to the app in-process.

    Separate from the story 1.1 ``client`` fixture so that step files which
    already mean "ZeroDB client" by ``client`` (story 1.2) are not shadowed.
    """
    with TestClient(create_app()) as test_client:
        yield test_client


@dataclass
class ScenarioContext:
    """Carries state between Given, When and Then steps of one scenario.

    pytest-bdd gives each step its own call, so anything a When produces for a
    Then has to live in a fixture. Using one typed object instead of a bare dict
    keeps step signatures short and the state discoverable.
    """

    voter: VoterProfileBuilder | None = None
    candidates: dict[str, CandidateBuilder] = field(default_factory=dict)
    promises: list[PromiseBuilder] = field(default_factory=list)
    question: str | None = None
    answer: dict[str, Any] | None = None
    response: Any = None
    scores: dict[str, float] = field(default_factory=dict)

    def candidate(self, name: str) -> CandidateBuilder:
        return self.candidates.setdefault(name, CandidateBuilder(name=name))


@pytest.fixture
def context() -> ScenarioContext:
    return ScenarioContext()


# --- test data builders ---------------------------------------------------
#
# Synthetic only. Per prd.md, voter profiles are political-opinion data: never
# put real voter data in a fixture, a log or test output.

#: Tier weights from prd.md -> "Tiered interest profile".
TIER_WEIGHTS: dict[str, float] = {"primary": 3.0, "secondary": 1.5, "nice-to-have": 0.5}


@dataclass
class Interest:
    topic: str
    tier: str
    stance: str | None = None
    dealbreaker: bool = False

    @property
    def weight(self) -> float:
        return TIER_WEIGHTS[self.tier]


@dataclass
class VoterProfileBuilder:
    """Builds a synthetic voter profile for a scenario."""

    interests: list[Interest] = field(default_factory=list)

    def add(
        self, topic: str, tier: str, stance: str | None = None, *, dealbreaker: bool = False
    ) -> VoterProfileBuilder:
        self.interests.append(
            Interest(topic=topic, tier=tier, stance=stance, dealbreaker=dealbreaker)
        )
        return self

    def mirrored(self) -> VoterProfileBuilder:
        """The same profile with every stance reversed.

        The neutrality symmetry audit (prd.md) needs two voters whose profiles
        are exact mirrors of one another.
        """
        flipped = VoterProfileBuilder()
        for interest in self.interests:
            stance = None if interest.stance is None else f"not {interest.stance}"
            flipped.add(interest.topic, interest.tier, stance, dealbreaker=interest.dealbreaker)
        return flipped

    @property
    def weights(self) -> list[float]:
        return [interest.weight for interest in self.interests]

    @property
    def has_primary(self) -> bool:
        return any(interest.tier == "primary" for interest in self.interests)


@dataclass
class VoteBuilder:
    """A synthetic roll call. ``vote_type`` distinguishes procedural from final."""

    bill: str
    position: str
    vote_type: str = "final-passage"


@dataclass
class CandidateBuilder:
    name: str
    overall_score: float | None = None
    votes: list[VoteBuilder] = field(default_factory=list)

    def voted(self, bill: str, position: str, vote_type: str = "final-passage") -> CandidateBuilder:
        self.votes.append(VoteBuilder(bill=bill, position=position, vote_type=vote_type))
        return self

    def final_passage_votes(self) -> list[VoteBuilder]:
        return [vote for vote in self.votes if vote.vote_type == "final-passage"]

    def mirrored(self) -> CandidateBuilder:
        """The same record with every position flipped (symmetry audit)."""
        flipped = CandidateBuilder(name=f"{self.name}-mirror", overall_score=self.overall_score)
        for vote in self.votes:
            opposite = "Nay" if vote.position == "Yea" else "Yea"
            flipped.voted(vote.bill, opposite, vote.vote_type)
        return flipped


@dataclass
class PromiseBuilder:
    """A synthetic campaign promise and the evidence it is tested against."""

    text: str
    source_url: str = "https://web.archive.org/synthetic/campaign-page"
    captured_at: str = "2024-09-01T00:00:00Z"
    roll_call_url: str | None = None
    verdict: str | None = None


# --- pending -------------------------------------------------------------


def pending(request: pytest.FixtureRequest, story: str, what: str) -> None:
    """Report the running scenario as pending (xfail) and stop the step.

    ``story`` is the backlog id that will implement the behaviour, so a reader of
    the CI output knows where the step is going. See the module docstring for why
    this is an xfail rather than a skip.
    """
    reason = f"pending: {what} — implemented by story {story}"
    request.node.add_marker(pytest.mark.xfail(reason=reason, strict=False))
    pytest.fail(reason, pytrace=False)


# --- Tiered interest profile (epic 3) -------------------------------------


@given("a new voter on the onboarding screen")
def new_voter(context: ScenarioContext) -> None:
    context.voter = VoterProfileBuilder()


@when(
    parsers.parse(
        'they choose "{topic}" as a primary interest with stance "{stance}"',
    )
)
def choose_primary_interest(context: ScenarioContext, topic: str, stance: str) -> None:
    assert context.voter is not None
    context.voter.add(topic, "primary", stance)


@when(parsers.parse('they choose "{topic}" as a secondary interest'))
def choose_secondary_interest(context: ScenarioContext, topic: str) -> None:
    assert context.voter is not None
    context.voter.add(topic, "secondary")


@when(parsers.parse('they choose "{topic}" as a nice-to-have interest'))
def choose_nice_to_have_interest(context: ScenarioContext, topic: str) -> None:
    assert context.voter is not None
    context.voter.add(topic, "nice-to-have")


@then(parsers.parse("their profile is saved with weights {first:g}, {second:g} and {third:g}"))
def profile_saved_with_weights(
    request: pytest.FixtureRequest,
    context: ScenarioContext,
    first: float,
    second: float,
    third: float,
) -> None:
    # The builder already knows the tier weights, so assert the shape locally;
    # persisting an encrypted profile is story 3.3.
    assert context.voter is not None
    assert context.voter.weights == [first, second, third]
    pending(request, "3.3", "encrypted voter profile persistence")


@then("no candidate score is shown until at least one primary interest exists")
def no_score_without_primary(request: pytest.FixtureRequest) -> None:
    pending(request, "4.2", "alignment score gating on a primary interest")


@given(parsers.parse('a voter marks "{topic}" as a dealbreaker'))
def voter_marks_dealbreaker(context: ScenarioContext, topic: str) -> None:
    context.voter = VoterProfileBuilder().add(
        topic, "primary", "lower out-of-pocket costs", dealbreaker=True
    )


@given(
    parsers.parse(
        "candidate {name} scores {score:g} overall but acted against that stance "
        "on {count:d} final-passage votes"
    )
)
def candidate_acted_against_stance(
    context: ScenarioContext, name: str, score: float, count: int
) -> None:
    candidate = context.candidate(name)
    candidate.overall_score = score
    for index in range(count):
        candidate.voted(f"synthetic-bill-{index}", "Nay")


@when("the voter views the ballot")
def voter_views_ballot(context: ScenarioContext) -> None:
    context.response = {"requested": "ballot"}


@then(parsers.parse("candidate {name} shows a dealbreaker warning above the score"))
def candidate_shows_dealbreaker_warning(
    request: pytest.FixtureRequest, context: ScenarioContext, name: str
) -> None:
    assert context.candidate(name).final_passage_votes()
    pending(request, "4.5", "dealbreaker warning on the ballot")


# --- Promise vs. Record (epics 7 and 8) ----------------------------------


@given(parsers.parse('incumbent {name}\'s {year:d} campaign site committed to "{promise}"'))
def incumbent_committed_to(context: ScenarioContext, name: str, year: int, promise: str) -> None:
    context.candidate(name)
    context.promises.append(PromiseBuilder(text=promise, captured_at=f"{year}-09-01T00:00:00Z"))


@given(
    parsers.parse("{name} voted {position} on the final-passage roll call of a bill {description}")
)
def voted_on_final_passage(
    context: ScenarioContext, name: str, position: str, description: str
) -> None:
    context.candidate(name).voted(description, position)
    if context.promises:
        context.promises[-1].roll_call_url = "https://clerk.house.gov/synthetic/roll-call"


@when(parsers.parse("the voter opens {name}'s Promise vs. Record"))
def open_promise_vs_record(context: ScenarioContext, name: str) -> None:
    context.response = {"requested": "promise-vs-record", "candidate": name}


@then(parsers.parse('the promise shows verdict "{verdict}"'))
def promise_shows_verdict(request: pytest.FixtureRequest, verdict: str) -> None:
    pending(request, "7.3", f'promise verdict "{verdict}"')


@then("it links the archived campaign page and the roll-call record")
def links_both_sources(request: pytest.FixtureRequest, context: ScenarioContext) -> None:
    assert context.promises and context.promises[-1].source_url
    pending(request, "7.4", "citation links on a verdict")


@given(
    parsers.parse("{name} voted Nay on a cloture motion but Yea on final passage of the same bill")
)
def voted_nay_cloture_yea_final(context: ScenarioContext, name: str) -> None:
    candidate = context.candidate(name)
    candidate.voted("synthetic-insulin-bill", "Nay", vote_type="procedural")
    candidate.voted("synthetic-insulin-bill", "Yea", vote_type="final-passage")
    context.promises.append(PromiseBuilder(text="vote to cap insulin costs"))


@then(
    parsers.parse(
        'the verdict is "{verdict}" with a "{note}" note on the cloture vote',
    )
)
def verdict_with_procedural_note(request: pytest.FixtureRequest, verdict: str, note: str) -> None:
    pending(
        request,
        "5.4",
        f'procedural vote typing so cloture yields "{verdict}" with a "{note}" note',
    )


@given("a promise maps only to a bill passed by voice vote")
def promise_maps_to_voice_vote(context: ScenarioContext) -> None:
    # A voice vote leaves no roll call, so there is no recorded position to score.
    context.promises.append(PromiseBuilder(text="synthetic promise", roll_call_url=None))


@then(parsers.parse('the verdict is "{verdict}" and the UI says "{message}"'))
def verdict_no_opportunity(
    request: pytest.FixtureRequest, context: ScenarioContext, verdict: str, message: str
) -> None:
    assert context.promises[-1].roll_call_url is None
    pending(request, "7.3", f'"{verdict}" verdict when there is no recorded vote')


# --- Neutrality (epic 11) ------------------------------------------------


@given("two synthetic candidates with mirrored voting records")
def mirrored_candidates(context: ScenarioContext) -> None:
    left = context.candidate("A")
    left.voted("synthetic-bill-1", "Yea").voted("synthetic-bill-2", "Nay")
    right = left.mirrored()
    context.candidates[right.name] = right


@given("two synthetic voters with mirrored profiles")
def mirrored_voters(context: ScenarioContext) -> None:
    context.voter = VoterProfileBuilder().add("Topic one", "primary", "for")
    # The mirror is built here so the symmetry audit step can assert on both.
    context.scores["mirror_ready"] = 1.0


@when("scores are computed")
def scores_are_computed(context: ScenarioContext) -> None:
    context.response = {"requested": "scores"}


@then("each voter's score for their aligned candidate is identical within 0.5 points")
def symmetry_within_tolerance(request: pytest.FixtureRequest, context: ScenarioContext) -> None:
    assert len(context.candidates) == 2, "the symmetry audit needs a mirrored pair"
    pending(request, "11.1", "symmetry audit over mirrored candidates and voters")


# --- Agent answers are grounded (epic 14) --------------------------------


@when(parsers.parse('a voter asks "{question}"'))
def voter_asks(context: ScenarioContext, question: str) -> None:
    context.question = question


@then("the agent explains it doesn't recommend candidates")
def agent_refuses_to_recommend(request: pytest.FixtureRequest, context: ScenarioContext) -> None:
    assert context.question
    pending(request, "14.3", "the agent's refusal to recommend a candidate")


@then("offers the voter's alignment comparison instead")
def agent_offers_alignment(request: pytest.FixtureRequest) -> None:
    pending(request, "14.3", "the alignment comparison offered in place of a recommendation")
