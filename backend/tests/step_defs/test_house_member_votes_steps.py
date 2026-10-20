"""Step definitions for tests/features/house_member_votes.feature (story 5.2).

Runs entirely against story 1.2's in-memory fake transport and the fixture at
tests/fixtures/house_member_votes_sample.json -- never the live network, so CI
needs no CONGRESS_GOV_API_KEY, per CLAUDE.md. The fixture itself IS real,
captured live during this story (see its own `_comment`), which is what makes
the `Aye`/`No` and bare-`votePartyTotal` scenarios possible at all: story 5.1
could not have written them from documentation alone.

Like test_house_rollcalls_steps.py and test_senate_rollcalls_steps.py, the fake
has `break_server_side_filter()` applied, for the same reason: the idempotency
claims and `upsert_many`'s reference cache both rest on the client's paging
fallback working under it.

The drift-check scenarios reuse `app.integrity.checks.check_member_votes_drift`
rather than asserting field equality directly. That is deliberate: story 5.2's
acceptance criterion is "the drift check passes", and the only way for that to
mean anything is for the test to run the SAME check the nightly job runs
(story 16.4's `repair_member_votes_denormalization`), not a restatement of it
that could agree with the loader while both disagree with the real check.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.house_member_votes import (
    transform_member_votes_for,
)
from app.ingest.house_rollcalls import (
    HouseRollCallLoader,
    LoadReport,
    transform_roll_call,
)
from app.integrity.checks import check_member_votes_drift
from app.repositories import Repository
from app.repositories.models import MemberVote, Person, RollCall
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/house_member_votes.feature")

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "house_member_votes_sample.json"
)
ROLLCALL_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "house_rollcalls_sample.json"
)


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
def loader(zerodb: ZeroDBClient) -> HouseRollCallLoader:
    return HouseRollCallLoader(zerodb, pace=lambda: None)


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text())


def _members(sample_key: str) -> dict[str, Any]:
    return copy.deepcopy(_fixture()[sample_key])


def _rollcall_envelope(sample_key: str) -> dict[str, Any]:
    """One roll-call envelope, from either fixture file.

    Story 5.1's hand-built fixture still holds the vote_type classification
    cases this story's member rows hang off (`item_final_passage` and friends);
    this story's own fixture holds the real, live-captured `live_item_*`
    envelopes. Looking in both keeps each scenario naming the sample it means.
    """
    own = _fixture()
    if sample_key in own:
        return copy.deepcopy(own[sample_key])
    return copy.deepcopy(json.loads(ROLLCALL_FIXTURE_PATH.read_text())[sample_key])


def _member_bioguides(sample_key: str) -> list[str]:
    results = _members(sample_key)["houseRollCallVoteMemberVotes"]["results"]
    return [str(entry["bioguideID"]) for entry in results if entry.get("bioguideID")]


# --- background --------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="member_votes_client")
def member_votes_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def reference_tables(zerodb: ZeroDBClient) -> None:
    Bootstrapper(zerodb, pace=lambda: None).run()


@given("every representative in the sample members response exists as a people row")
def representatives_exist(zerodb: ZeroDBClient) -> None:
    """`member_votes.person_id` references `people` (datamodel.md rule 5).

    Seeded here for the same reason test_senate_rollcalls_steps.py seeds its
    senators: the real `people` write is story 2.2's `legislators.py`, out of
    scope for this test, while the reference check very much is in scope.
    """
    people: Repository[Person] = Repository(client=zerodb, model=Person)
    seen: set[str] = set()
    for sample_key in ("members_yea_nay", "members_aye_no", "members_present"):
        for bioguide in _member_bioguides(sample_key):
            if bioguide in seen:
                continue
            seen.add(bioguide)
            people.upsert({"id": f"person:{bioguide}", "canonical_name": f"Rep. {bioguide}"})


# --- ingesting ---------------------------------------------------------------


def _ingest(
    loader: HouseRollCallLoader,
    result: dict[str, Any],
    rollcall_key: str,
    members_key: str | None = None,
    *,
    envelope: dict[str, Any] | None = None,
    members_body: dict[str, Any] | None = None,
) -> LoadReport:
    env = envelope if envelope is not None else _rollcall_envelope(rollcall_key)
    row = transform_roll_call(env)
    member_rows: list[dict[str, Any]] = []
    if members_key is not None or members_body is not None:
        body = members_body if members_body is not None else _members(members_key or "")
        member_rows, skipped = transform_member_votes_for(row, body)
        result["member_votes_skipped"] = skipped
    report = loader.load([row], member_vote_rows_by_roll_call={row["id"]: member_rows})
    result["report"] = report
    result["last_row"] = row
    return report


@when(
    parsers.parse(
        'I ingest the sample House roll call "{rollcall_key}" '
        'with member positions "{members_key}"'
    )
)
def ingest_with_members(
    loader: HouseRollCallLoader, result: dict[str, Any], rollcall_key: str, members_key: str
) -> None:
    _ingest(loader, result, rollcall_key, members_key)


@given(
    parsers.parse(
        'I have ingested the sample House roll call "{rollcall_key}" '
        'with member positions "{members_key}"'
    )
)
def given_ingested_with_members(
    loader: HouseRollCallLoader,
    fake: FakeZeroDBTransport,
    result: dict[str, Any],
    rollcall_key: str,
    members_key: str,
) -> None:
    _ingest(loader, result, rollcall_key, members_key)
    fake.calls.clear()


@when(
    parsers.parse(
        'I ingest the sample House roll call "{rollcall_key}" with member positions '
        '"{members_key}" and bioguide "{bioguide}" missing from people'
    )
)
def ingest_with_missing_person(
    zerodb: ZeroDBClient,
    loader: HouseRollCallLoader,
    result: dict[str, Any],
    rollcall_key: str,
    members_key: str,
    bioguide: str,
) -> None:
    """Delete the seeded `people` row, then ingest.

    The reference check lives in the repository layer (story 1.4), so this
    scenario proves the loader lets that check do its job -- it does not
    re-implement a bioguide allowlist of its own.
    """
    zerodb.delete_row("people", f"person:{bioguide}")
    _ingest(loader, result, rollcall_key, members_key)


@when(parsers.parse('I ingest the live-shaped House roll call "{rollcall_key}"'))
def ingest_live_shaped(
    loader: HouseRollCallLoader, result: dict[str, Any], rollcall_key: str
) -> None:
    _ingest(loader, result, rollcall_key)


@when(
    parsers.parse(
        'the source reclassifies roll call "{roll_call_id}" as a procedural vote '
        'and I ingest it again with member positions "{members_key}"'
    )
)
def source_reclassifies(
    loader: HouseRollCallLoader,
    result: dict[str, Any],
    roll_call_id: str,
    members_key: str,
) -> None:
    """A real correction case: the source restates the vote question, so the
    roll call's `vote_type` changes and every member row must be re-stamped.

    This is the scenario that makes the denormalization honest. A loader that
    wrote `member_votes` only on first sight would leave 430 rows claiming the
    old `vote_type` forever, and the drift check would (correctly) fail.
    """
    envelope = _rollcall_envelope("item_final_passage")
    envelope["houseRollCallVote"]["voteQuestion"] = "On Motion to Table"
    _ingest(loader, result, "item_final_passage", members_key, envelope=envelope)
    assert result["last_row"]["id"] == roll_call_id


# --- transform-only steps (no write) ----------------------------------------


@when(parsers.parse('I transform the sample member positions "{members_key}"'))
def transform_positions(result: dict[str, Any], members_key: str) -> None:
    row = transform_roll_call(_rollcall_envelope("item_final_passage"))
    rows, skipped = transform_member_votes_for(row, _members(members_key))
    result["member_rows"] = rows
    result["member_votes_skipped"] = skipped


@when(
    parsers.parse(
        'I transform the sample member positions "{members_key}" '
        'with one voteCast replaced by "{bad_value}"'
    )
)
def transform_with_bad_vote_cast(result: dict[str, Any], members_key: str, bad_value: str) -> None:
    body = _members(members_key)
    body["houseRollCallVoteMemberVotes"]["results"][0]["voteCast"] = bad_value
    row = transform_roll_call(_rollcall_envelope("item_final_passage"))
    rows, skipped = transform_member_votes_for(row, body)
    result["member_rows"] = rows
    result["member_votes_skipped"] = skipped


@when(
    parsers.parse(
        'I transform the sample member positions "{members_key}" with one bioguideID removed'
    )
)
def transform_with_missing_bioguide(result: dict[str, Any], members_key: str) -> None:
    body = _members(members_key)
    body["houseRollCallVoteMemberVotes"]["results"][0].pop("bioguideID", None)
    row = transform_roll_call(_rollcall_envelope("item_final_passage"))
    rows, skipped = transform_member_votes_for(row, body)
    result["member_rows"] = rows
    result["member_votes_skipped"] = skipped


# --- tampering (for the drift check) ----------------------------------------


@when(
    parsers.parse(
        'the {field_name} on member_vote "{member_vote_id}" is changed to "{value}" '
        "behind the loader's back"
    )
)
@given(
    parsers.parse(
        'the {field_name} on member_vote "{member_vote_id}" is changed to "{value}" '
        "behind the loader's back"
    )
)
def tamper_with_member_vote(
    zerodb: ZeroDBClient, field_name: str, member_vote_id: str, value: str
) -> None:
    """Write a drifted value straight through the client, bypassing the loader.

    Patching through `ZeroDBClient.update_row` rather than the repository is the
    point: nothing in the normal write path can produce this state, which is
    exactly why the nightly check exists.
    """
    zerodb.update_row(MemberVote.table_name, member_vote_id, {field_name: value})


# --- assertions --------------------------------------------------------------


def _member_votes_for(zerodb: ZeroDBClient, roll_call_id: str) -> list[dict[str, Any]]:
    repo: Repository[MemberVote] = Repository(client=zerodb, model=MemberVote)
    return [
        vote.model_dump(mode="json")
        for vote in repo.query(limit=1000)
        if vote.roll_call_id == roll_call_id
    ]


@then(parsers.parse('{count:d} member_votes rows exist for roll call "{roll_call_id}"'))
def member_votes_count(zerodb: ZeroDBClient, count: int, roll_call_id: str) -> None:
    assert len(_member_votes_for(zerodb, roll_call_id)) == count


@then(parsers.parse('the member_vote "{member_vote_id}" is stored'))
def member_vote_stored(zerodb: ZeroDBClient, member_vote_id: str) -> None:
    repo: Repository[MemberVote] = Repository(client=zerodb, model=MemberVote)
    assert repo.get(member_vote_id) is not None


@then(parsers.parse('the member_vote "{member_vote_id}" is not stored'))
def member_vote_not_stored(zerodb: ZeroDBClient, member_vote_id: str) -> None:
    repo: Repository[MemberVote] = Repository(client=zerodb, model=MemberVote)
    assert repo.get(member_vote_id) is None


@then(parsers.parse('the member_vote "{member_vote_id}" has position "{position}"'))
def member_vote_position(zerodb: ZeroDBClient, member_vote_id: str, position: str) -> None:
    repo: Repository[MemberVote] = Repository(client=zerodb, model=MemberVote)
    stored = repo.get(member_vote_id)
    assert stored is not None
    assert stored.position == position


@then(parsers.parse('the member_vote "{member_vote_id}" has vote_type "{vote_type}"'))
def member_vote_vote_type(zerodb: ZeroDBClient, member_vote_id: str, vote_type: str) -> None:
    repo: Repository[MemberVote] = Repository(client=zerodb, model=MemberVote)
    stored = repo.get(member_vote_id)
    assert stored is not None
    assert stored.vote_type == vote_type


@then(
    parsers.parse(
        'the member_vote "{member_vote_id}" has {field_name} matching roll call "{roll_call_id}"'
    )
)
def member_vote_field_matches_roll_call(
    zerodb: ZeroDBClient, member_vote_id: str, field_name: str, roll_call_id: str
) -> None:
    """The acceptance criterion, field by field: the copy equals the source."""
    votes: Repository[MemberVote] = Repository(client=zerodb, model=MemberVote)
    rolls: Repository[RollCall] = Repository(client=zerodb, model=RollCall)
    vote = votes.get(member_vote_id)
    roll = rolls.get(roll_call_id)
    assert vote is not None, f"{member_vote_id} not stored"
    assert roll is not None, f"{roll_call_id} not stored"
    assert getattr(vote, field_name) == getattr(roll, field_name)


@then(parsers.parse('exactly 1 row carries the member_vote id "{member_vote_id}"'))
def exactly_one_member_vote_row(zerodb: ZeroDBClient, member_vote_id: str) -> None:
    rows = zerodb.list_rows(MemberVote.table_name, limit=1000).rows
    matching = [row for row in rows if row.row_data.get("id") == member_vote_id]
    assert len(matching) == 1, f"expected 1 row for {member_vote_id}, found {len(matching)}"


@then(parsers.parse('the transformed positions include "{position}"'))
def transformed_positions_include(result: dict[str, Any], position: str) -> None:
    positions = {row["position"] for row in result["member_rows"]}
    assert position in positions, f"expected {position!r} among {sorted(positions)}"


@then(parsers.parse('no transformed position is "{position}"'))
def no_transformed_position(result: dict[str, Any], position: str) -> None:
    positions = {row["position"] for row in result["member_rows"]}
    assert position not in positions, f"{position!r} should not appear, got {sorted(positions)}"


@then(parsers.parse("exactly {count:d} member position is skipped"))
def member_positions_skipped(result: dict[str, Any], count: int) -> None:
    assert len(result["member_votes_skipped"]) == count


@then("the member_votes drift check passes")
def drift_check_passes(zerodb: ZeroDBClient) -> None:
    report = check_member_votes_drift(zerodb)
    assert report.ok, f"drift check failed: {report.summary()}"


@then(parsers.parse('the member_votes drift check fails for member_vote "{member_vote_id}"'))
def drift_check_fails_for(zerodb: ZeroDBClient, member_vote_id: str) -> None:
    report = check_member_votes_drift(zerodb)
    assert not report.ok, "drift check passed, but the row was tampered with"
    assert any(member_vote_id in str(violation) for violation in report.violations), (
        f"{member_vote_id} not named in {report.summary()}"
    )


@then(
    parsers.parse(
        'the roll call "{roll_call_id}" has tally yea {yea:d} nay {nay:d} '
        "present {present:d} not_voting {not_voting:d}"
    )
)
def roll_call_tally(
    zerodb: ZeroDBClient, roll_call_id: str, yea: int, nay: int, present: int, not_voting: int
) -> None:
    repo: Repository[RollCall] = Repository(client=zerodb, model=RollCall)
    stored = repo.get(roll_call_id)
    assert stored is not None
    assert stored.tally.yea == yea
    assert stored.tally.nay == nay
    assert stored.tally.present == present
    assert stored.tally.not_voting == not_voting
