"""Tests for the shared BDD step library itself (story 1.7).

Two things need proving, and they pull in opposite directions:

* a **pending** step must not fail the build — otherwise the PRD's acceptance
  criteria could not be committed before the code that satisfies them;
* an **actually broken** step must still fail the build — otherwise the pending
  idiom would be a hole in CI.

The runner tests below use pytest's own ``pytester`` fixture to run a throwaway
suite in a subprocess and assert on its outcome, which is the only way to
observe xfail versus failure without the outer suite swallowing it.
"""

import pathlib

import pytest

from tests.step_defs.common_steps import (
    TIER_WEIGHTS,
    CandidateBuilder,
    ScenarioContext,
    VoterProfileBuilder,
)

pytest_plugins = ["pytester"]


# --- builders -------------------------------------------------------------


def test_tier_weights_match_the_prd() -> None:
    assert TIER_WEIGHTS == {"primary": 3.0, "secondary": 1.5, "nice-to-have": 0.5}


def test_voter_profile_builder_reports_tier_weights_in_order() -> None:
    voter = (
        VoterProfileBuilder()
        .add("Prescription drug pricing", "primary", "lower out-of-pocket costs")
        .add("Broadband access", "secondary")
        .add("Public lands", "nice-to-have")
    )
    assert voter.weights == [3.0, 1.5, 0.5]
    assert voter.has_primary


def test_voter_profile_without_a_primary_interest_is_flagged() -> None:
    assert not VoterProfileBuilder().add("Public lands", "nice-to-have").has_primary


def test_mirrored_voter_profile_flips_every_stance() -> None:
    mirror = VoterProfileBuilder().add("Topic", "primary", "for").mirrored()
    assert [interest.stance for interest in mirror.interests] == ["not for"]
    # Tiers are preserved; only stances flip, so weights stay comparable.
    assert mirror.weights == [3.0]


def test_mirrored_candidate_flips_every_position_and_keeps_vote_types() -> None:
    candidate = CandidateBuilder(name="A")
    candidate.voted("bill-1", "Yea").voted("bill-2", "Nay", vote_type="procedural")
    mirror = candidate.mirrored()
    assert [(v.position, v.vote_type) for v in mirror.votes] == [
        ("Nay", "final-passage"),
        ("Yea", "procedural"),
    ]


def test_candidate_final_passage_votes_exclude_procedural_votes() -> None:
    candidate = CandidateBuilder(name="B")
    candidate.voted("bill", "Nay", vote_type="procedural").voted("bill", "Yea")
    assert [vote.vote_type for vote in candidate.final_passage_votes()] == ["final-passage"]


def test_scenario_context_returns_the_same_candidate_for_a_repeated_name() -> None:
    context = ScenarioContext()
    assert context.candidate("A") is context.candidate("A")


# --- the pending idiom ----------------------------------------------------

_FEATURE = """\
Feature: Pending idiom
  Scenario: A step that is not implemented yet
    Given a shared-library scenario
    Then the unimplemented behaviour holds
"""

_PENDING_STEPS = """\
import pytest
from pytest_bdd import given, scenarios, then
from tests.step_defs.common_steps import pending

scenarios("pending_idiom.feature")

@given("a shared-library scenario")
def _given():
    pass

@then("the unimplemented behaviour holds")
def _then(request):
    pending(request, "9.9", "a behaviour from a later story")
"""

_BROKEN_STEPS = """\
from pytest_bdd import given, scenarios, then

scenarios("pending_idiom.feature")

@given("a shared-library scenario")
def _given():
    pass

@then("the unimplemented behaviour holds")
def _then():
    raise AssertionError("this step is genuinely broken, not pending")
"""


#: The backend package root, so a pytester subprocess can import ``tests.*``.
_BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def bdd_pytester(pytester: pytest.Pytester) -> pytest.Pytester:
    """A throwaway pytest project that can import the real step library.

    The subprocess runs in pytester's temp directory, so ``pythonpath`` has to
    name the real backend root explicitly for ``tests.step_defs.common_steps``
    to resolve.
    """
    pytester.makefile(".feature", pending_idiom=_FEATURE)
    pytester.makeini(
        f"""
        [pytest]
        pythonpath = . {_BACKEND_ROOT}
        """
    )
    return pytester


def test_a_pending_step_reports_as_xfail_and_exits_zero(bdd_pytester: pytest.Pytester) -> None:
    bdd_pytester.makepyfile(test_pending=_PENDING_STEPS)
    result = bdd_pytester.runpytest_subprocess("-rxX")

    result.assert_outcomes(xfailed=1, failed=0, passed=0)
    assert result.ret == 0, "a pending scenario must not fail the build"
    # The reason names the story, so CI output says where the step is going.
    result.stdout.fnmatch_lines(["*XFAIL*pending: a behaviour from a later story*story 9.9*"])


def test_a_genuinely_broken_step_still_fails_the_build(bdd_pytester: pytest.Pytester) -> None:
    bdd_pytester.makepyfile(test_broken=_BROKEN_STEPS)
    result = bdd_pytester.runpytest_subprocess()

    result.assert_outcomes(failed=1, xfailed=0)
    assert result.ret != 0, "a broken step must still fail the build"


def test_an_undefined_step_still_fails_the_build(bdd_pytester: pytest.Pytester) -> None:
    """A feature whose steps nobody wrote is an error, not a silent pass."""
    bdd_pytester.makepyfile(
        test_undefined='from pytest_bdd import scenarios\nscenarios("pending_idiom.feature")\n'
    )
    result = bdd_pytester.runpytest_subprocess()

    result.assert_outcomes(failed=1, xfailed=0)
    assert result.ret != 0
