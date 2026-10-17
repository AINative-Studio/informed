"""The no-recommendations guardrail (story 13.3).

prd.md, agent guardrails: "It never says who to vote for, never predicts
outcomes, never generates persuasive content for or against a candidate." And
the first principle: "The app never tells anyone who to vote for."

This package is where that rule is enforced, and it exists ahead of the agent
it guards (story 13.2) on purpose — see `app.agent_policy.policy`'s module
docstring for the argument, and for exactly what a rule-based classifier does
and does not buy.

Typical use, once 13.2's agent exists::

    decision = classify(question)
    if not decision.allowed:
        return AgentAnswer(text=decision.refusal, citations=[])
    ...  # answer from the evidence graph

`app.agent_policy.red_team` holds the 50 adversarial prompts backlog.md 13.3
requires, as data a test can parametrise over.
"""

from app.agent_policy.policy import (
    RECOMMENDATION_REFUSAL,
    REFUSAL_REASON,
    PolicyDecision,
    classify,
    is_recommendation_request,
    refusal_response,
)

__all__ = [
    "RECOMMENDATION_REFUSAL",
    "REFUSAL_REASON",
    "PolicyDecision",
    "classify",
    "is_recommendation_request",
    "refusal_response",
]
