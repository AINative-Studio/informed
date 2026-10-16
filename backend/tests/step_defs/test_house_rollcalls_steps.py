"""Step definitions for tests/features/house_rollcalls.feature (story 5.1).

Runs entirely against story 1.2's in-memory fake transport and the fixture at
tests/fixtures/house_rollcalls_sample.json -- never the live network, so CI
needs no Congress.gov API key (none is available this session -- see
app/ingest/house_rollcalls.py's module docstring and this story's PR).

Like test_load_legislators_steps.py, the fake has `break_server_side_filter()`
applied, because that is what the live API does and this loader's idempotency
claim rests on the client's paging fallback working under it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.house_rollcalls import (
    HouseRollCallLoader,
    LoadReport,
    transform_roll_call,
)
from app.repositories import Repository
from app.repositories.models import RollCall
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/house_rollcalls.feature")

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "house_rollcalls_sample.json"


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


def _fixture_envelopes() -> dict[str, dict[str, Any]]:
    data = json.loads(FIXTURE_PATH.read_text())
    return {key: value for key, value in data.items() if key != "_comment"}


# --- background --------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="rollcalls_client")
def rollcalls_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def reference_tables(zerodb: ZeroDBClient) -> None:
    """`roll_calls` and `provenance_log` both need to exist as tables first.

    `Bootstrapper.run()` creates every table story 1.3 knows about, including
    both of this story's -- the same approach
    `test_load_legislators_steps.py` takes for `people`/`bodies`.
    """
    Bootstrapper(zerodb, pace=lambda: None).run()


# --- ingesting -----------------------------------------------------------------


def _ingest_one(loader: HouseRollCallLoader, result: dict[str, Any], sample_key: str) -> LoadReport:
    envelope = _fixture_envelopes()[sample_key]
    row = transform_roll_call(envelope)
    report = loader.load([row])
    result["report"] = report
    result["last_row"] = row
    return report


@when(parsers.parse('I ingest the sample roll call "{sample_key}"'))
def ingest_sample(loader: HouseRollCallLoader, result: dict[str, Any], sample_key: str) -> None:
    _ingest_one(loader, result, sample_key)


@when(parsers.parse('I ingest the sample roll call "{sample_key}" again'))
def ingest_sample_again(
    loader: HouseRollCallLoader, result: dict[str, Any], sample_key: str
) -> None:
    _ingest_one(loader, result, sample_key)


@given(parsers.parse('I have already ingested the sample roll call "{sample_key}" once'))
def already_ingested_once(
    loader: HouseRollCallLoader, fake: FakeZeroDBTransport, result: dict[str, Any], sample_key: str
) -> None:
    _ingest_one(loader, result, sample_key)
    fake.calls.clear()


@when(
    'the source updates roll call "us-house:119-2:452" with a corrected result '
    "and I ingest it again"
)
def ingest_corrected_version(loader: HouseRollCallLoader, result: dict[str, Any]) -> None:
    """Simulates a real correction: the source re-publishes the same roll
    call with a changed `result`, which must hash differently and therefore
    write a second, distinct provenance entry -- see the module docstring's
    "Idempotency and provenance" section.
    """
    envelope = copy.deepcopy(_fixture_envelopes()["item_final_passage"])
    envelope["houseRollCallVote"]["result"] = "Agreed to"
    row = transform_roll_call(envelope)
    report = loader.load([row])
    result["report"] = report
    result["last_row"] = row


# --- reading the loaded rows back -------------------------------------------


def _roll_call(zerodb: ZeroDBClient, roll_call_id: str) -> RollCall:
    repo: Repository[RollCall] = Repository(client=zerodb, model=RollCall)
    row = repo.get(roll_call_id)
    assert row is not None, f"roll call {roll_call_id!r} is not stored"
    return row


@then(parsers.parse('the roll call "{roll_call_id}" is stored'))
def roll_call_is_stored(zerodb: ZeroDBClient, roll_call_id: str) -> None:
    _roll_call(zerodb, roll_call_id)


@then(parsers.parse('the roll call "{roll_call_id}" has source_url "{source_url}"'))
def roll_call_has_source_url(zerodb: ZeroDBClient, roll_call_id: str, source_url: str) -> None:
    assert _roll_call(zerodb, roll_call_id).source_url == source_url


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


@then(parsers.parse('that provenance_log entry has source_url "{source_url}"'))
def provenance_entry_source_url(result: dict[str, Any], source_url: str) -> None:
    assert result["provenance_row"]["source_url"] == source_url


@then(parsers.parse('that provenance_log entry has adapter_version "{adapter_version}"'))
def provenance_entry_adapter_version(result: dict[str, Any], adapter_version: str) -> None:
    assert result["provenance_row"]["adapter_version"] == adapter_version


@then(parsers.parse('exactly {count:d} provenance_log row exists for roll_calls "{entity_id}"'))
@then(parsers.parse('exactly {count:d} provenance_log rows exist for roll_calls "{entity_id}"'))
def exact_provenance_row_count(fake: FakeZeroDBTransport, count: int, entity_id: str) -> None:
    rows = _provenance_rows_for(fake, entity_id)
    assert len(rows) == count, f"found {len(rows)} provenance_log row(s), expected {count}"


# --- idempotency -------------------------------------------------------------


def _rows_with_id(fake: FakeZeroDBTransport, table: str, row_id: str) -> int:
    stored = fake.rows.get(table, {})
    return sum(1 for row in stored.values() if row["row_data"].get("id") == row_id)


@then(parsers.parse('exactly {count:d} row carries the roll call id "{roll_call_id}"'))
def one_roll_call_row(fake: FakeZeroDBTransport, count: int, roll_call_id: str) -> None:
    actual = _rows_with_id(fake, "roll_calls", roll_call_id)
    assert actual == count, f"{roll_call_id} has {actual} row(s), expected {count}"
