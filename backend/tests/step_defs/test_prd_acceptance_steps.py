"""Binds the PRD's own Gherkin acceptance criteria to pytest-bdd (story 1.7).

The scenarios in prd.md -> "BDD acceptance criteria" are the executable
definition of done for the MVP. They are collected here from day one so the
feature set is visible in every CI run, and they report as *pending* until the
stories that implement them land (epics 3, 4, 7, 11, 14).

The steps themselves come from tests/step_defs/common_steps.py, which is
registered as a pytest plugin in tests/conftest.py, so a future feature-specific
step file can import or override individual steps without restating the rest.
"""

from pytest_bdd import scenarios

scenarios(
    "../features/interest_profile.feature",
    "../features/promise_vs_record.feature",
    "../features/neutrality.feature",
    "../features/agent_grounded.feature",
)
