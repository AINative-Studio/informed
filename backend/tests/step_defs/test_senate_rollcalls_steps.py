"""Step definitions for tests/features/senate_rollcalls.feature (story 8.1).

Runs entirely against story 1.2's in-memory fake transport and the fixture at
tests/fixtures/senate_rollcalls_sample.json -- never the live network, so CI
needs no credential of any kind (unlike house_rollcalls, this story's real
source needs none either, but CI still must not depend on senate.gov being
reachable, per CLAUDE.md).

Like test_house_rollcalls_steps.py, the fake has `break_server_side_filter()`
applied, for the same reason: this loader's idempotency claim, and its
`upsert_many` reference-cache claim for `member_votes`, rest on the client's
paging fallback working under it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.senate_rollcalls import (
    LoadReport,
    SenateRollCallLoader,
    transform_member_votes_for,
    transform_roll_call,
)
from app.repositories import Repository
from app.repositories.models import MemberVote, Person, RollCall
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/senate_rollcalls.feature")

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "senate_rollcalls_sample.json"


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
def loader(zerodb: ZeroDBClient) -> SenateRollCallLoader:
    return SenateRollCallLoader(zerodb, pace=lambda: None)


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text())


def _xml(sample_key: str) -> str:
    data = _fixture()
    return data[sample_key]


def _bioguide_map() -> dict[str, str]:
    return _fixture()["lis_to_bioguide_sample"]


# --- background --------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="rollcalls_client")
def rollcalls_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def reference_tables(zerodb: ZeroDBClient) -> None:
    """`roll_calls`, `member_votes` and `provenance_log` all need to exist as
    tables first, exactly as `test_house_rollcalls_steps.py` does for its own
    two tables. `Bootstrapper.run()` creates every table story 1.3 knows
    about, including all three of this story's.
    """
    Bootstrapper(zerodb, pace=lambda: None).run()


@given("every senator in the sample crosswalk exists as a people row")
def senators_exist(zerodb: ZeroDBClient) -> None:
    """`member_votes.person_id` references `people` (datamodel.md rule 5),
    exactly like the House loader's own compound-reference case. This
    story's own crosswalk resolution (`transform_member_votes_for`) is read-
    only -- it never writes `people` -- so these rows exist for the same
    reason `test_integrity_checks_steps.py`'s own `verdict_appended` step
    seeds a `person:B000944` row before writing something that references
    it: the real write (story 2.2's `legislators.py`, already live) is out
    of scope for this test, and the reference check is not.
    """
    people: Repository[Person] = Repository(client=zerodb, model=Person)
    for bioguide in _bioguide_map().values():
        people.upsert({"id": f"person:{bioguide}", "canonical_name": f"Senator {bioguide}"})


# --- ingesting (roll_calls only) --------------------------------------------


def _ingest_one(
    loader: SenateRollCallLoader, result: dict[str, Any], sample_key: str
) -> LoadReport:
    xml = _xml(sample_key)
    row = transform_roll_call(xml, source_url=f"https://example.test/{sample_key}")
    report = loader.load([row])
    result["report"] = report
    result["last_row"] = row
    return report


@when(parsers.parse('I ingest the sample roll call "{sample_key}"'))
def ingest_sample(loader: SenateRollCallLoader, result: dict[str, Any], sample_key: str) -> None:
    _ingest_one(loader, result, sample_key)


@when(parsers.parse('I ingest the sample roll call "{sample_key}" again'))
def ingest_sample_again(
    loader: SenateRollCallLoader, result: dict[str, Any], sample_key: str
) -> None:
    _ingest_one(loader, result, sample_key)


@given(parsers.parse('I have already ingested the sample roll call "{sample_key}" once'))
def already_ingested_once(
    loader: SenateRollCallLoader,
    fake: FakeZeroDBTransport,
    result: dict[str, Any],
    sample_key: str,
) -> None:
    _ingest_one(loader, result, sample_key)
    fake.calls.clear()


# --- ingesting with member positions ----------------------------------------


def _ingest_with_members(
    loader: SenateRollCallLoader,
    result: dict[str, Any],
    sample_key: str,
    *,
    bioguide_map: dict[str, str] | None = None,
) -> LoadReport:
    xml = _xml(sample_key)
    row = transform_roll_call(xml, source_url=f"https://example.test/{sample_key}")
    member_rows, skipped = transform_member_votes_for(
        row, xml, bioguide_by_lis_id=bioguide_map if bioguide_map is not None else _bioguide_map()
    )
    report = loader.load([row], member_vote_rows_by_roll_call={row["id"]: member_rows})
    result["report"] = report
    result["last_row"] = row
    result["member_votes_skipped"] = skipped
    return report


@when(parsers.parse('I ingest the sample roll call "{sample_key}" with member positions'))
def ingest_with_members(
    loader: SenateRollCallLoader, result: dict[str, Any], sample_key: str
) -> None:
    _ingest_with_members(loader, result, sample_key)


@when(parsers.parse('I ingest the sample roll call "{sample_key}" with member positions again'))
def ingest_with_members_again(
    loader: SenateRollCallLoader, result: dict[str, Any], sample_key: str
) -> None:
    _ingest_with_members(loader, result, sample_key)


@given(
    parsers.parse(
        'I have already ingested the sample roll call "{sample_key}" with member positions once'
    )
)
def already_ingested_with_members_once(
    loader: SenateRollCallLoader,
    fake: FakeZeroDBTransport,
    result: dict[str, Any],
    sample_key: str,
) -> None:
    _ingest_with_members(loader, result, sample_key)
    fake.calls.clear()


@when(
    parsers.parse(
        'I ingest the sample roll call "{sample_key}" with a partial crosswalk missing '
        'bioguide "{missing_lis_id}"'
    )
)
def ingest_with_partial_crosswalk(
    loader: SenateRollCallLoader,
    result: dict[str, Any],
    sample_key: str,
    missing_lis_id: str,
) -> None:
    partial = {k: v for k, v in _bioguide_map().items() if k != missing_lis_id}
    _ingest_with_members(loader, result, sample_key, bioguide_map=partial)


# --- reading roll_calls back -------------------------------------------------


def _roll_call(zerodb: ZeroDBClient, roll_call_id: str) -> RollCall:
    repo: Repository[RollCall] = Repository(client=zerodb, model=RollCall)
    row = repo.get(roll_call_id)
    assert row is not None, f"roll call {roll_call_id!r} is not stored"
    return row


@then(parsers.parse('the roll call "{roll_call_id}" is stored'))
def roll_call_is_stored(zerodb: ZeroDBClient, roll_call_id: str) -> None:
    _roll_call(zerodb, roll_call_id)


@then(parsers.parse('the roll call "{roll_call_id}" has a source_url'))
def roll_call_has_source_url(zerodb: ZeroDBClient, roll_call_id: str) -> None:
    source_url = _roll_call(zerodb, roll_call_id).source_url
    assert source_url, "source_url is empty"


@then(parsers.parse('the roll call "{roll_call_id}" has a content_hash'))
def roll_call_has_content_hash(zerodb: ZeroDBClient, roll_call_id: str) -> None:
    content_hash = _roll_call(zerodb, roll_call_id).content_hash
    assert content_hash, "content_hash is empty"
    assert len(content_hash) == 64, f"expected a sha256 hex digest, got {content_hash!r}"


@then(parsers.parse('the roll call "{roll_call_id}" has vote_type "{vote_type}"'))
def roll_call_has_vote_type(zerodb: ZeroDBClient, roll_call_id: str, vote_type: str) -> None:
    assert _roll_call(zerodb, roll_call_id).vote_type == vote_type


@then(parsers.parse('the roll call "{roll_call_id}" has result "{result_value}"'))
def roll_call_has_result(zerodb: ZeroDBClient, roll_call_id: str, result_value: str) -> None:
    assert _roll_call(zerodb, roll_call_id).result == result_value


@then(parsers.parse('the roll call "{roll_call_id}" has amendment_ref "{amendment_ref}"'))
def roll_call_has_amendment_ref(
    zerodb: ZeroDBClient, roll_call_id: str, amendment_ref: str
) -> None:
    assert _roll_call(zerodb, roll_call_id).amendment_ref == amendment_ref


@then(
    parsers.parse(
        'the roll call "{roll_call_id}" has tally yea {yea:d} nay {nay:d} '
        "present {present:d} not_voting {not_voting:d}"
    )
)
def roll_call_has_tally(
    zerodb: ZeroDBClient,
    roll_call_id: str,
    yea: int,
    nay: int,
    present: int,
    not_voting: int,
) -> None:
    tally = _roll_call(zerodb, roll_call_id).tally
    assert (tally.yea, tally.nay, tally.present, tally.not_voting) == (
        yea,
        nay,
        present,
        not_voting,
    ), tally


# --- provenance ----------------------------------------------------------------


def _provenance_rows_for(fake: FakeZeroDBTransport, entity_id: str) -> list[dict[str, Any]]:
    stored = fake.rows.get("provenance_log", {})
    return [
        row["row_data"]
        for row in stored.values()
        if row["row_data"].get("entity_table") == "roll_calls"
        and row["row_data"].get("entity_id") == entity_id
    ]


@then(parsers.parse('a provenance_log entry exists for roll_calls "{entity_id}"'))
def provenance_entry_exists(
    fake: FakeZeroDBTransport, entity_id: str, result: dict[str, Any]
) -> None:
    rows = _provenance_rows_for(fake, entity_id)
    assert rows, f"no provenance_log entry for roll_calls {entity_id!r}"
    result["provenance_row"] = rows[0]


@then(parsers.parse('that provenance_log entry has adapter_version "{adapter_version}"'))
def provenance_entry_adapter_version(result: dict[str, Any], adapter_version: str) -> None:
    assert result["provenance_row"]["adapter_version"] == adapter_version


@then(parsers.parse('exactly {count:d} provenance_log row exists for roll_calls "{entity_id}"'))
@then(parsers.parse('exactly {count:d} provenance_log rows exist for roll_calls "{entity_id}"'))
def exact_provenance_row_count(fake: FakeZeroDBTransport, count: int, entity_id: str) -> None:
    rows = _provenance_rows_for(fake, entity_id)
    assert len(rows) == count, f"found {len(rows)} provenance_log row(s), expected {count}"


# --- idempotency (roll_calls) ------------------------------------------------


def _rows_with_id(fake: FakeZeroDBTransport, table: str, row_id: str) -> int:
    stored = fake.rows.get(table, {})
    return sum(1 for row in stored.values() if row["row_data"].get("id") == row_id)


@then(parsers.parse('exactly {count:d} row carries the roll call id "{roll_call_id}"'))
def one_roll_call_row(fake: FakeZeroDBTransport, count: int, roll_call_id: str) -> None:
    actual = _rows_with_id(fake, "roll_calls", roll_call_id)
    assert actual == count, f"{roll_call_id} has {actual} row(s), expected {count}"


# --- member_votes -------------------------------------------------------------


def _member_vote_rows_for(fake: FakeZeroDBTransport, roll_call_id: str) -> list[dict[str, Any]]:
    stored = fake.rows.get("member_votes", {})
    return [
        row["row_data"]
        for row in stored.values()
        if row["row_data"].get("roll_call_id") == roll_call_id
    ]


@then(parsers.parse('{count:d} member_votes rows exist for roll call "{roll_call_id}"'))
@then(parsers.parse('exactly {count:d} member_votes rows exist for roll call "{roll_call_id}"'))
def member_votes_row_count(fake: FakeZeroDBTransport, count: int, roll_call_id: str) -> None:
    rows = _member_vote_rows_for(fake, roll_call_id)
    assert len(rows) == count, (
        f"found {len(rows)} member_votes row(s) for {roll_call_id!r}, expected {count}"
    )


def _member_vote(zerodb: ZeroDBClient, roll_call_id: str, bioguide: str) -> MemberVote:
    repo: Repository[MemberVote] = Repository(client=zerodb, model=MemberVote)
    row = repo.get(f"{roll_call_id}:{bioguide.removeprefix('person:')}")
    assert row is not None, f"no member_vote for {roll_call_id!r}/{bioguide!r}"
    return row


@then(
    parsers.parse(
        'the member_vote for roll call "{roll_call_id}" and bioguide "{bioguide}" '
        'has position "{position}"'
    )
)
def member_vote_has_position(
    zerodb: ZeroDBClient, roll_call_id: str, bioguide: str, position: str
) -> None:
    assert _member_vote(zerodb, roll_call_id, bioguide).position == position


@then(
    parsers.parse(
        'the member_vote for roll call "{roll_call_id}" and bioguide "{bioguide}" '
        'has vote_type "{vote_type}"'
    )
)
def member_vote_has_vote_type(
    zerodb: ZeroDBClient, roll_call_id: str, bioguide: str, vote_type: str
) -> None:
    assert _member_vote(zerodb, roll_call_id, bioguide).vote_type == vote_type


@then(parsers.parse('no member_votes row references lis id "{lis_member_id}"'))
def no_member_vote_for_unmapped_lis_id(result: dict[str, Any], lis_member_id: str) -> None:
    """The unmapped senator is recorded in the loader's own skip report, and
    (implicitly, per the companion `exactly N member_votes rows exist` step)
    never reaches a written row at all -- this step asserts the skip was
    recorded with the right reason, which is the only trace of it that
    should exist anywhere.
    """
    skipped = result.get("member_votes_skipped", [])
    matching = [s for s in skipped if s.lis_member_id == lis_member_id]
    assert matching, f"expected {lis_member_id!r} to be skipped, but it was not"
    assert "no bioguide mapping" in matching[0].reason
