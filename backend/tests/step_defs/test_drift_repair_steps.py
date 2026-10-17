"""Step definitions for tests/features/drift_repair.feature (16.4).

This feature's fixture rows (a roll call, a person, a member vote copying the
roll call's denormalized fields) are the same SHAPE
`test_integrity_checks_steps.py` builds for check 4's detection half — drift
repair operates on exactly the same rows, by design (see
`app.integrity.repair`'s module docstring on why the check and the repair
share `app.integrity.checks.drifted_fields`). The step TEXT below is
deliberately its own wording ("a synced roll call", "a synced member vote")
rather than reusing that other file's phrasing verbatim: pytest-bdd registers
each `@given`/`@when`/`@then` as a fixture named from the step's parsed text
in the step file's own module namespace, and this codebase's test suite has no
per-directory conftest isolating one step file's fixtures from another's — so
two files defining a same-text step is a real, observed collision (one file's
registration silently wins across the whole `tests/step_defs/` collection, not
just within its own file), not merely a style preference. Distinct text avoids
it the same way `test_provenance_enforcement_steps.py` and every other
existing step file in this codebase already does: no two files share a step's
exact wording.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.integrity.repair import RepairResult, repair_member_votes_drift
from app.repositories import Repository
from app.repositories.models import MemberVote, Person, RollCall
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/drift_repair.feature")


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )


@pytest.fixture
def result() -> dict[str, Any]:
    return {}


@given("a ZeroDB client backed by an in-memory fake", target_fixture="integrity_client")
def integrity_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def reference_tables(zerodb: ZeroDBClient) -> None:
    Bootstrapper(zerodb, pace=lambda: None).run()


# --- fixture rows: roll_calls, people, member_votes -------------------------


def _ensure_bill(zerodb: ZeroDBClient, bill_id: str) -> None:
    """A minimal `bills` row so a `roll_calls.bill_id` reference resolves.

    `bills` ingestion is outside this story's scope (no `Bill` model exists
    yet), so the row is written directly through the client — the same
    posture `test_integrity_checks_steps.py`'s identical helper takes.
    """
    if zerodb.get_row("bills", bill_id) is None:
        zerodb.insert_row("bills", {"id": bill_id, "title": "Test Bill", "body_id": "us-house"})


def _roll_call_row(roll_call_id: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": roll_call_id,
        "voted_at": "2026-03-11T14:22:00-04:00",
        "question": "On Motion to Suspend the Rules and Pass",
        "vote_type": "final_passage",
        "result": "Passed",
        "topic_ids": [],
        "is_bundled": False,
    }
    row.update(overrides)
    return row


@given(
    parsers.parse(
        'a synced roll call "{roll_call_id}" with vote_type "{vote_type}" and bill_id "{bill_id}"'
    ),
    target_fixture="synced_roll_call_id",
)
def synced_roll_call(zerodb: ZeroDBClient, roll_call_id: str, vote_type: str, bill_id: str) -> str:
    _ensure_bill(zerodb, bill_id)
    Repository(client=zerodb, model=RollCall).upsert(
        _roll_call_row(roll_call_id, vote_type=vote_type, bill_id=bill_id)
    )
    return roll_call_id


@given(
    parsers.parse('a synced member vote on "{roll_call_id}" for person "{person_id}"')
)
def synced_member_vote(zerodb: ZeroDBClient, roll_call_id: str, person_id: str) -> None:
    if zerodb.get_row(Person.table_name, person_id) is None:
        Repository(client=zerodb, model=Person).upsert(
            {"id": person_id, "canonical_name": "Test Person"}
        )
    roll_call = zerodb.get_row(RollCall.table_name, roll_call_id)
    assert roll_call is not None, f"no roll_calls row {roll_call_id!r} to copy from"
    row_data = roll_call.row_data
    Repository(client=zerodb, model=MemberVote).upsert(
        {
            "roll_call_id": roll_call_id,
            "person_id": person_id,
            "position": "yea",
            "voted_at": row_data["voted_at"],
            "vote_type": row_data["vote_type"],
            "bill_id": row_data.get("bill_id"),
            "topic_ids": row_data.get("topic_ids", []),
            "is_bundled": row_data.get("is_bundled", False),
        }
    )


@given(parsers.parse('the synced roll call is corrected to vote_type "{vote_type}"'))
def correct_synced_roll_call_vote_type(
    zerodb: ZeroDBClient, synced_roll_call_id: str, vote_type: str
) -> None:
    zerodb.update_row(RollCall.table_name, synced_roll_call_id, {"vote_type": vote_type})


@given(
    parsers.parse(
        'the synced roll call is corrected to vote_type "{vote_type}" and bill_id "{bill_id}"'
    )
)
def correct_synced_roll_call_both(
    zerodb: ZeroDBClient, synced_roll_call_id: str, vote_type: str, bill_id: str
) -> None:
    _ensure_bill(zerodb, bill_id)
    zerodb.update_row(
        RollCall.table_name, synced_roll_call_id, {"vote_type": vote_type, "bill_id": bill_id}
    )




@when("I run the drift repair", target_fixture="repair_result")
def run_repair(zerodb: ZeroDBClient) -> RepairResult:
    return repair_member_votes_drift(zerodb)


@when("I run the drift repair again", target_fixture="second_repair_result")
def run_repair_again(zerodb: ZeroDBClient) -> RepairResult:
    return repair_member_votes_drift(zerodb)


@then(parsers.parse("the repair reports {count:d} row(s) repaired"))
def repair_reports_count(repair_result: RepairResult, count: int) -> None:
    assert repair_result.repaired == count, repair_result.summary()


@then(parsers.parse("the second repair reports {count:d} row(s) repaired"))
def second_repair_reports_count(second_repair_result: RepairResult, count: int) -> None:
    assert second_repair_result.repaired == count, second_repair_result.summary()


@then(parsers.parse('the member_votes row "{row_id}" still has vote_type "{vote_type}"'))
@then(parsers.parse('the member_votes row "{row_id}" now has vote_type "{vote_type}"'))
def member_vote_has_vote_type(zerodb: ZeroDBClient, row_id: str, vote_type: str) -> None:
    row = zerodb.get_row(MemberVote.table_name, row_id)
    assert row is not None, f"no member_votes row {row_id!r}"
    assert row.row_data.get("vote_type") == vote_type, row.row_data


@then(parsers.parse('the member_votes row "{row_id}" now has bill_id "{bill_id}"'))
@then(parsers.parse('the member_votes row "{row_id}" still has bill_id "{bill_id}"'))
def member_vote_has_bill_id(zerodb: ZeroDBClient, row_id: str, bill_id: str) -> None:
    row = zerodb.get_row(MemberVote.table_name, row_id)
    assert row is not None, f"no member_votes row {row_id!r}"
    assert row.row_data.get("bill_id") == bill_id, row.row_data


@then(parsers.parse('the member_votes row "{row_id}" still has position "{position}"'))
def member_vote_has_position(zerodb: ZeroDBClient, row_id: str, position: str) -> None:
    row = zerodb.get_row(MemberVote.table_name, row_id)
    assert row is not None, f"no member_votes row {row_id!r}"
    assert row.row_data.get("position") == position, row.row_data
