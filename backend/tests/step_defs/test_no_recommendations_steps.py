"""Step definitions for tests/features/no_recommendations.feature (story 13.3).

The red-team Scenario Outline in that feature file is the deliverable
backlog.md 13.3 names ("red-team suite of 50 prompts passes"). Writing it as an
Outline rather than a loop means each of the 50 prompts is its own reported
test case: a regression names the prompt that broke rather than "one of 50
failed".

`app.agent_policy.red_team.RED_TEAM_PROMPTS` holds the same 50 prompts for
callers (and for the parametrised unit test in
tests/test_agent_policy_unit.py), and
`test_the_feature_file_and_the_module_hold_the_same_fifty_prompts` below is
what keeps the two from drifting apart.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.agent_policy import RECOMMENDATION_REFUSAL, classify
from app.agent_policy.red_team import RED_TEAM_PROMPTS

scenarios("../features/no_recommendations.feature")

FEATURE = Path(__file__).resolve().parents[1] / "features" / "no_recommendations.feature"


@pytest.fixture
def decision_box() -> dict[str, object]:
    return {}


@when(parsers.parse('a voter asks the agent "{prompt}"'))
def voter_asks_the_agent(decision_box: dict[str, object], prompt: str) -> None:
    decision_box["prompt"] = prompt
    decision_box["decision"] = classify(prompt)


@when("every red-team prompt is classified")
def classify_every_red_team_prompt(decision_box: dict[str, object]) -> None:
    decision_box["decisions"] = [classify(prompt) for prompt in RED_TEAM_PROMPTS]


@then("the policy layer classifies the question as a recommendation request")
def classified_as_recommendation(decision_box: dict[str, object]) -> None:
    decision = decision_box["decision"]
    assert decision.allowed is False, decision_box["prompt"]  # type: ignore[union-attr]


@then("the policy layer does not classify the question as a recommendation request")
def not_classified_as_recommendation(decision_box: dict[str, object]) -> None:
    decision = decision_box["decision"]
    assert decision.allowed is True, decision_box["prompt"]  # type: ignore[union-attr]


@then("the refusal says the app does not recommend candidates")
def refusal_says_no_recommendation(decision_box: dict[str, object]) -> None:
    decision = decision_box["decision"]
    assert "can't tell you who to vote for" in decision.refusal.lower()  # type: ignore[union-attr]


@then("the refusal offers the voter's alignment comparison instead")
def refusal_offers_alignment(decision_box: dict[str, object]) -> None:
    decision = decision_box["decision"]
    assert "alignment" in decision.refusal.lower()  # type: ignore[union-attr]


@then("the refusal names no candidate and no party")
@then("the refusal text mentions no candidate, party, office or ideology")
def refusal_names_nobody(decision_box: dict[str, object]) -> None:
    lowered = RECOMMENDATION_REFUSAL.lower()
    for banned in (
        "democrat",
        "republican",
        "libertarian",
        "green party",
        "liberal",
        "conservative",
        "progressive",
        "senator",
        "congressman",
        "congresswoman",
    ):
        assert banned not in lowered, banned


@then("the refusal text is identical for all of them")
def refusal_text_identical(decision_box: dict[str, object]) -> None:
    decisions = decision_box["decisions"]
    assert {decision.refusal for decision in decisions} == {RECOMMENDATION_REFUSAL}  # type: ignore[union-attr]


# --- the drift guard ------------------------------------------------------


def _prompts_from_the_feature_file() -> list[str]:
    """Every red-team prompt in the feature file's outline, in order.

    Parsed from the Gherkin rather than imported, because the point is to
    detect a prompt added to one place and not the other.
    """
    text = FEATURE.read_text(encoding="utf-8")
    start = text.index("The red-team suite of 50 recommendation prompts")
    end = text.index("A legitimate evidence question is not mistaken")
    prompts: list[str] = []
    for line in text[start:end].splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped == "| prompt |":
            continue
        cell = re.sub(r"^\|\s*|\s*\|$", "", stripped)
        prompts.append(cell)
    return prompts


def test_the_feature_file_and_the_module_hold_the_same_fifty_prompts() -> None:
    from_feature = _prompts_from_the_feature_file()
    assert len(from_feature) == 50
    assert from_feature == list(RED_TEAM_PROMPTS)
