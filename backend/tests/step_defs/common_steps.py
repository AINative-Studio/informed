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

from app.agent_policy import classify
from app.main import create_app
from app.profiles.methodology import DEFAULT_TIER_WEIGHTS

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

#: Tier weights, read from a PUBLISHED methodology version rather than
#: hard-coded here (story 6.1).
#:
#: This used to be a literal `{"primary": 3.0, ...}` in this file, which is the
#: duplication datamodel.md forbids in production code ("the live values come
#: from `methodology_versions` and are never hard-coded") and which made
#: `profile_saved_with_weights` a tautology: it asserted that a test constant
#: equalled the numbers written into the same test. Now the numbers come from
#: story 6.1's published defaults — the exact dict a `methodology_versions`
#: row carries — so prd.md's table, the published row and this assertion
#: cannot drift apart silently.
#:
#: The keys are re-spelled because the two vocabularies differ by a hyphen:
#: prd.md's prose and these Gherkin scenarios say "nice-to-have", while the
#: stored row and `app.profiles.models` use the identifier `nice_to_have`.
TIER_WEIGHTS: dict[str, float] = {
    tier.replace("_", "-"): weight for tier, weight in DEFAULT_TIER_WEIGHTS.items()
}


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
    context: ScenarioContext,
    first: float,
    second: float,
    third: float,
) -> None:
    """prd.md: a tiered profile's three interests carry 3.0, 1.5 and 0.5.

    No longer pending (story 6.1). The history is worth keeping, because the
    pointer moved twice and the second move is the one that mattered:

      * it first said "story 3.3", which was stale — story 14.1 built the
        encrypted profile persistence and 4.2/4.4 wired it to HTTP;
      * it was then re-pointed at 6.1, because the real gap was never
        persistence. Weights are deliberately NOT stored on a profile
        (datamodel.md: "the live values come from `methodology_versions` and
        are never hard-coded"; `app.profiles.models` has no weight field, by
        design and with a test pinning its absence). So the only honest way to
        assert this scenario is to read the weights back from a published
        methodology version.

    Which is what it now does. `VoterProfileBuilder.weights` resolves through
    `TIER_WEIGHTS`, which story 6.1 re-derived from the published defaults a
    `methodology_versions` row actually carries, so this assertion passes only
    while prd.md's table, the published row and the profile model agree. The
    round trip through a live ZeroDB row is covered by story 6.1's own feature
    file and its live verification script; what this step pins is the number
    a voter's profile is scored with.
    """
    assert context.voter is not None
    assert context.voter.weights == [first, second, third]


@then("no candidate score is shown until at least one primary interest exists")
def no_score_without_primary(context: ScenarioContext) -> None:
    """prd.md's gate on scoring, no longer pending (story 6.4).

    The pointer said "4.2", which was the wrong story: 4.2 edits tier weights
    and has no score to gate. The gate belongs to whatever computes a score,
    which is story 6.4's `/v1/candidates/{id}/alignment` — and it is enforced
    in exactly one place, by exactly one condition.

    What makes that one condition sufficient is a property of the profile
    model, not a second check: `InterestProfile` refuses to validate without at
    least one primary interest ("a profile needs at least 1 primary interest;
    nothing can be scored without one"). So a STORED profile always has one,
    and "has a stored profile" and "is scoreable" are the same question.
    `AlignmentService` asks it once, raises `ProfileNotFoundError`, and the
    router answers 404 — never a score.

    Pinned here against the model's rule (a profile with no primary interest
    cannot exist) and end-to-end over the real endpoint by
    `tests/features/alignment_score.feature`'s "A voter with no stored profile
    gets no score".
    """
    from pydantic import ValidationError

    from app.profiles.models import InterestProfile

    assert context.voter is not None
    assert context.voter.has_primary, "this scenario's voter chose a primary interest"

    # The rule that makes the gate airtight: a profile without a primary
    # interest is unstorable, so there is no stored profile for which a score
    # could be computed and then withheld.
    with pytest.raises(ValidationError, match="primary interest"):
        InterestProfile.model_validate(
            {
                "interests": [
                    {
                        "topic_id": "en-US:test-gate:v1",
                        "stance_id": "en-US:test-gate:v1:for",
                        "tier": "secondary",
                    }
                ]
            }
        )


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
def candidate_shows_dealbreaker_warning(context: ScenarioContext, name: str) -> None:
    """prd.md's "Dealbreaker overrides the average", no longer pending (6.6).

    The pointer said "4.5", which was the wrong story for the same reason the
    sibling step's "4.2" was: epic 4 edits a profile, and this is a claim about
    a SCORE. The behaviour belongs to story 6.6.

    **The real end-to-end version of this scenario lives in
    `tests/features/alignment_score.feature`**, reproduced there verbatim under
    this same name, where it runs over the actual
    `GET /v1/candidates/{id}/alignment` endpoint against stored
    `candidate_positions` and `member_votes`: it asserts the warning appears,
    that it cites a real roll call, and — the part that is the requirement —
    that the score is byte-identical with the dealbreaker flag cleared.

    This copy is kept because prd.md's BDD section is collected here in full
    (story 1.7) and a scenario quietly deleted from that collection is a
    requirement quietly dropped. What it asserts is the half that is checkable
    from this file's own synthetic builders — that the scenario's premise holds
    and that the behaviour is implemented rather than pending:
    `DealbreakerWarning` exists, it cannot be built without a citation, and it
    cannot be built for a candidate who did not act against the stance.
    """
    from pydantic import ValidationError

    from app.scoring.models import DealbreakerWarning

    candidate = context.candidate(name)
    assert candidate.final_passage_votes(), "the scenario's premise is final-passage votes"
    assert context.voter is not None
    assert any(interest.dealbreaker for interest in context.voter.interests)

    # The warning is a real, citation-bound shape rather than a pending idea.
    with pytest.raises(ValidationError, match="citations"):
        DealbreakerWarning(
            stance_id="en-US:test-gate:v1:for",
            s_value=-1.0,
            confidence=1.0,
            evidence_count=len(candidate.final_passage_votes()),
            citations=[],
        )


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
def verdict_with_procedural_note(
    request: pytest.FixtureRequest, context: ScenarioContext, verdict: str, note: str
) -> None:
    """Half of this is now real; the other half is story 11.1's.

    The pending marker here used to name story "5.4", which was wrong twice
    over: 5.4 is "Archive bill text", and the two halves of this scenario
    belong to two different stories.

    **The classification half is done (story 8.3), and is asserted here
    rather than deferred.** The scenario's whole premise is that a Nay on
    cloture and a Yea on final passage of the same bill are DIFFERENT KINDS
    of vote, weighted differently -- without which no verdict engine could
    reach "Kept" on these two votes however it was written.

    **The verdict half is story 11.1's** (the verdict engine, iteration 7):
    turning those two typed votes into a stored "Kept" verdict with a
    rendered "procedural vote" note. Nothing in iteration 5 writes a
    `verdicts` row at all.
    """
    from app.ingest.senate_vote_typing import VoteSignals, classify
    from app.methodology.publish import DEFAULT_VOTE_TYPE_WEIGHTS

    cloture = classify(VoteSignals.from_source_fields(question="On the Cloture Motion"))
    final = classify(VoteSignals.from_source_fields(question="On Passage of the Bill"))
    assert cloture.vote_type == "cloture", cloture
    assert final.vote_type == "final_passage", final
    assert (
        DEFAULT_VOTE_TYPE_WEIGHTS[cloture.vote_type] < DEFAULT_VOTE_TYPE_WEIGHTS[final.vote_type]
    ), "a cloture vote must weigh less than final passage for this scenario to be reachable"
    assert len(context.candidate("B").votes) == 2, (
        "the scenario needs both votes on the record for a verdict to weigh them"
    )
    pending(
        request,
        "11.1",
        f"the verdict engine turning a cloture Nay plus a final-passage Yea into "
        f'"{verdict}" with a "{note}" note (8.3 types and weights the two votes)',
    )


@given("a promise maps only to a bill passed by voice vote")
def promise_maps_to_voice_vote(context: ScenarioContext) -> None:
    # A voice vote leaves no roll call, so there is no recorded position to score.
    context.promises.append(PromiseBuilder(text="synthetic promise", roll_call_url=None))


@then(parsers.parse('the verdict is "{verdict}" and the UI says "{message}"'))
def verdict_no_opportunity(
    request: pytest.FixtureRequest, context: ScenarioContext, verdict: str, message: str
) -> None:
    """The "no recorded vote" half is now real (story 8.5); the stored
    verdict is still story 11.1's.

    The marker here named story 7.3 ("Candidate detail"), which renders a
    verdict but does not decide one. Corrected to 11.1, the verdict engine.

    What 8.5 settles, and what this step now asserts rather than deferring:
    the UI message is prd.md's exact wording, the verdict such an outcome
    implies is `no_opportunity`, and the outcome is marked unscorable -- so
    "silence is never treated as support" is true at the layer that decides
    it, independently of whether any `verdicts` row exists yet.
    """
    from app.ingest.senate_vote_typing import (
        NO_RECORDED_VOTE_MESSAGE,
        NO_RECORDED_VOTE_VERDICT,
        unrecorded_outcome,
    )

    assert context.promises[-1].roll_call_url is None
    outcome = unrecorded_outcome("Passed Senate by Voice Vote")
    assert outcome is not None, "a voice vote must be recognized as an unrecorded outcome"
    assert outcome.message == message == NO_RECORDED_VOTE_MESSAGE
    assert outcome.verdict == NO_RECORDED_VOTE_VERDICT
    assert outcome.scorable is False, "an unrecorded outcome must never be scorable"
    pending(
        request,
        "11.1",
        f'the verdict engine storing a "{verdict}" verdict row '
        f"(8.5 settles the message, the implied verdict and unscorability)",
    )


# --- Neutrality (story 12.1) ---------------------------------------------
#
# No longer pending. The pending marker here named story "11.1", which was a
# mis-numbering twice over: prd.md's "Symmetry audit" scenario is backlog.md
# story **12.1** (epic 12, trust and neutrality), and 11.1 is the verdict
# engine. Retired rather than re-pointed, because the behaviour now exists —
# `app.neutrality.audit`.
#
# These steps drive the REAL audit rather than the `CandidateBuilder` /
# `VoterProfileBuilder` sketches the rest of this file uses for pending
# scenarios. That is the point of the story: the builders describe a mirrored
# pair, where the auditor writes one into storage, recomputes positions
# through `PositionComputer` and scores both sides through
# `AlignmentService`. A PRD acceptance scenario asserting on the builders
# would be asserting that the test fixture is symmetric, which was never the
# claim prd.md makes.


@given("two synthetic candidates with mirrored voting records", target_fixture="symmetry_stack")
def mirrored_candidates() -> tuple[Any, Any]:
    """The real mirrored fixture's stack: an in-memory ZeroDB and a profile
    service. The candidates themselves are written by the auditor when it
    runs, from `app.neutrality.audit.default_pairs()` — every roll call in
    one candidate's record is the other's with Yea and Nay exchanged.
    """
    from app.bootstrap import Bootstrapper
    from app.methodology.publish import MethodologyPublisher
    from app.profiles.crypto import ProfileKeyring, ProfileRootKey
    from app.profiles.service import VoterProfileService
    from app.zerodb import ZeroDBClient
    from app.zerodb.testing import FakeZeroDBTransport

    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-prd-symmetry", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(client, pace=lambda: None).run()
    MethodologyPublisher(client).publish_new_version("2027.1")
    profiles = VoterProfileService(
        client=client,
        keyring=ProfileKeyring(client=client, root_key=ProfileRootKey(bytes(range(32)))),
    )
    return client, profiles


@given("two synthetic voters with mirrored profiles")
def mirrored_voters() -> None:
    """Also the auditor's, and derived from one declaration rather than
    hand-written: the mirrored voter picks the opposite stance card on the
    same topic, at the same tier and the same intensity. See
    `app.neutrality.audit.SymmetryAuditor._profile_payload`.
    """


@when("scores are computed", target_fixture="symmetry_report")
def scores_are_computed(symmetry_stack: tuple[Any, Any]) -> Any:
    from app.neutrality.audit import SymmetryAuditor

    client, profiles = symmetry_stack
    return SymmetryAuditor(client=client, profiles=profiles).run()


@then("each voter's score for their aligned candidate is identical within 0.5 points")
def symmetry_within_tolerance(symmetry_report: Any) -> None:
    """prd.md's property, asserted on real scores from the real pipeline.

    "Identical" is the right word and not an approximation of "complementary":
    the voter's stance direction and the candidate's record are BOTH mirrored
    here, so `s_value` negates twice and the two scores must be equal. The
    complementary relationship (`left + right == 100`, from mirroring the
    record alone) is the other half and is asserted in
    `tests/features/symmetry_audit.feature`.
    """
    from app.neutrality.audit import SYMMETRY_TOLERANCE

    assert symmetry_report.pairs, "the symmetry audit scored no mirrored pair at all"
    for pair in symmetry_report.pairs:
        assert pair.left_score is not None and pair.mirrored_voter_score is not None, (
            f"{pair.name}: a mirrored pair that cannot be scored is unmeasured, not symmetric"
        )
        delta = abs(pair.left_score - pair.mirrored_voter_score)
        assert delta <= SYMMETRY_TOLERANCE, (
            f"{pair.name}: the voter scored its aligned candidate {pair.left_score} but the "
            f"mirrored voter scored its aligned candidate {pair.mirrored_voter_score} "
            f"(delta {delta} > {SYMMETRY_TOLERANCE})"
        )
    assert symmetry_report.passed, symmetry_report.summary()


# --- Agent answers are grounded (epic 14) --------------------------------


@when(parsers.parse('a voter asks "{question}"'))
def voter_asks(context: ScenarioContext, question: str) -> None:
    context.question = question


@then("the agent explains it doesn't recommend candidates")
def agent_refuses_to_recommend(context: ScenarioContext) -> None:
    """No longer pending — story 13.3 built the guardrail this asserts on.

    The pending marker here named story "14.3", which was a mis-numbering: the
    PRD scenario this step belongs to ("Agent refuses to recommend") is
    backlog.md story **13.3**, and 14.3 is anonymous mode. Retired rather than
    re-pointed, because the behaviour now exists.

    What this asserts is the policy layer's decision, not an agent's HTTP
    response: story 13.2's agent is not built, and `app.agent_policy` is the
    thing that will gate it. When 13.2 lands, this step should assert on the
    agent's own answer text — at which point it will still be asserting the
    same refusal, because the agent returns `decision.refusal` verbatim.
    """
    assert context.question
    decision = classify(context.question)
    assert decision.allowed is False, context.question
    assert "can't tell you who to vote for" in (decision.refusal or "").lower()
    context.answer = {"text": decision.refusal, "citations": []}


@then("offers the voter's alignment comparison instead")
def agent_offers_alignment(context: ScenarioContext) -> None:
    assert context.answer is not None, "the refusal step must run first"
    assert "alignment" in str(context.answer["text"]).lower()
