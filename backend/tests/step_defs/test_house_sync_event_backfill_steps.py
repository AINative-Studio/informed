"""Step definitions for tests/features/house_sync_event_backfill.feature (5.6/5.7/5.8).

Runs entirely against story 1.2's in-memory fake transport and the fixture at
tests/fixtures/house_rollcalls_sample.json -- never the live network, so CI
needs no `CONGRESS_GOV_API_KEY` even though one finally exists in this
environment (see `scripts/verify_house_sync_event_backfill_live.py` for the
live half, and this branch's PR for its real numbers).

Like `test_house_rollcalls_steps.py`, the fake has `break_server_side_filter()`
applied, because that is what live ZeroDB does and the idempotency claims
these scenarios make rest on the client's paging fallback working under it.

The list-endpoint paging scenarios (5.8) stub `httpx.get` with a recorded
shape rather than hitting the network: the SHAPE is what was confirmed live
(`pagination.count`, a hard 250-row page cap) and is what the loader has to
handle; re-confirming it from CI would make the suite depend on
Congress.gov being reachable, which CLAUDE.md forbids.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.house_rollcalls import (
    BACKFILL_CONGRESS_SESSIONS,
    HouseRollCallLoader,
    LoadReport,
    fetch_roll_call_numbers,
    fetch_source_total,
    reconcile_congress_session,
    transform_roll_call,
    transform_roll_calls,
)
from app.repositories import Repository
from app.repositories.models import RollCall
from app.scheduling.jobs import HOUSE_PUBLICATION_LAG_PROMISE, default_jobs
from app.scheduling.registry import JobStatus, ScheduledJobRunner
from app.scoring.positions import ROLLCALL_INGESTED_TOPIC, PositionRecomputeConsumer
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/house_sync_event_backfill.feature")

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "house_rollcalls_sample.json"

#: A fixed "now" so "last succeeded N minutes ago" is deterministic.
_NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


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


def _fixture_envelopes() -> dict[str, Any]:
    data = json.loads(FIXTURE_PATH.read_text())
    return {key: value for key, value in data.items() if key != "_comment"}


# --- background --------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="sync_client")
def sync_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def reference_tables(zerodb: ZeroDBClient) -> None:
    Bootstrapper(zerodb, pace=lambda: None).run()
    zerodb.create_table("scheduled_jobs", {"id": "string", "last_run_at": "string"})


# --- story 5.6: the registry -------------------------------------------------


@given("no CONGRESS_GOV_API_KEY is set in the environment")
def no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONGRESS_GOV_API_KEY", raising=False)


@when("I read the default job registry")
def read_registry(result: dict[str, Any]) -> None:
    result["jobs"] = {job.name: job for job in default_jobs()}


@then(parsers.parse('the job "{name}" is registered'))
def job_is_registered(result: dict[str, Any], name: str) -> None:
    assert name in result["jobs"], f"{name!r} is not in the default registry"


@then(parsers.parse('the job "{name}" runs every {hours:d} hour'))
def job_runs_every(result: dict[str, Any], name: str, hours: int) -> None:
    assert result["jobs"][name].interval == timedelta(hours=hours)


@then(parsers.parse('the job "{name}" meets the 2-hour publication promise'))
def job_meets_promise(result: dict[str, Any], name: str) -> None:
    """Story 5.6's criterion is a LATENCY bound, so what has to hold is that
    the registered cadence leaves room for it: a roll call published just
    after one run is visible after the next, so worst-case lag is one
    interval plus the run's own duration. Asserting `interval <= promise / 2`
    keeps that true with the margin the run itself needs, rather than
    pinning `interval == 1 hour` twice.
    """
    interval = result["jobs"][name].interval
    assert interval <= HOUSE_PUBLICATION_LAG_PROMISE / 2, (
        f"{name} runs every {interval}, which cannot guarantee a roll call is "
        f"visible within {HOUSE_PUBLICATION_LAG_PROMISE}"
    )


@then(parsers.parse('the job "{name}" is blocked with a reason naming {env_var}'))
def job_blocked_for_env(result: dict[str, Any], name: str, env_var: str) -> None:
    job = result["jobs"][name]
    assert job.precondition is not None, f"{name} has no precondition"
    reason = job.precondition()
    assert reason is not None, f"{name} is not blocked"
    assert env_var in reason, f"blocked reason {reason!r} does not name {env_var}"


@given(parsers.parse("the hourly sync last succeeded {minutes:d} minutes ago"))
def sync_last_succeeded(zerodb: ZeroDBClient, minutes: int) -> None:
    last = _NOW - timedelta(minutes=minutes)
    zerodb.upsert_row(
        "scheduled_jobs",
        "load_house_rollcalls",
        {
            "id": "load_house_rollcalls",
            "last_run_at": last.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        },
    )


@when("the scheduled job runner runs")
def runner_runs(
    zerodb: ZeroDBClient, result: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs the REAL registry, but with no credentials in the environment, so
    every credential-gated job reports `blocked` rather than reaching out to
    a live source from inside the test suite. What this scenario asserts is
    the due/not-due decision, which is made before the precondition check.
    """
    monkeypatch.delenv("CONGRESS_GOV_API_KEY", raising=False)
    monkeypatch.delenv("FEC_API_KEY", raising=False)
    jobs = [job for job in default_jobs() if job.name == "load_house_rollcalls"]
    report = ScheduledJobRunner(zerodb, jobs).run(now=_NOW)
    result["report"] = report


@then(parsers.parse('the job "{name}" is due'))
def job_is_due(result: dict[str, Any], name: str) -> None:
    """Due means "not skipped as not-due". With no API key in the
    environment the job then reports BLOCKED, which still proves the runner
    considered it due -- a not-due job never reaches its precondition.
    """
    outcome = next(o for o in result["report"].outcomes if o.name == name)
    assert outcome.status is not JobStatus.SKIPPED_NOT_DUE, (
        f"{name} was skipped as not due: {outcome.detail}"
    )


# --- syncing (5.6 idempotency, 5.7 events) -----------------------------------


def _sync(loader: HouseRollCallLoader, result: dict[str, Any], *sample_keys: str) -> LoadReport:
    rows = [transform_roll_call(_fixture_envelopes()[key]) for key in sample_keys]
    report = loader.load(rows)
    result["report"] = report
    return report


@when(parsers.parse('I sync the sample roll call "{sample_key}"'))
def sync_sample(loader: HouseRollCallLoader, result: dict[str, Any], sample_key: str) -> None:
    _sync(loader, result, sample_key)


@when(parsers.parse('I sync the sample roll call "{sample_key}" again'))
def sync_sample_again(loader: HouseRollCallLoader, result: dict[str, Any], sample_key: str) -> None:
    _sync(loader, result, sample_key)


@when(parsers.parse('I sync the sample roll calls "{first}" and "{second}"'))
def sync_two_samples(
    loader: HouseRollCallLoader, result: dict[str, Any], first: str, second: str
) -> None:
    _sync(loader, result, first, second)


@given(parsers.parse('I have already synced the sample roll call "{sample_key}" once'))
def already_synced_once(
    loader: HouseRollCallLoader, result: dict[str, Any], sample_key: str
) -> None:
    _sync(loader, result, sample_key)


@when(parsers.parse('the source corrects roll call "{roll_call_id}" and I sync it again'))
def sync_corrected(loader: HouseRollCallLoader, result: dict[str, Any], roll_call_id: str) -> None:
    envelope = copy.deepcopy(_fixture_envelopes()["item_final_passage"])
    envelope["houseRollCallVote"]["result"] = "Agreed to"
    report = loader.load([transform_roll_call(envelope)])
    result["report"] = report


@then(parsers.parse('exactly {count:d} row carries the roll call id "{roll_call_id}"'))
def exact_roll_call_rows(fake: FakeZeroDBTransport, count: int, roll_call_id: str) -> None:
    stored = fake.rows.get("roll_calls", {})
    actual = sum(1 for row in stored.values() if row["row_data"].get("id") == roll_call_id)
    assert actual == count, f"{roll_call_id} has {actual} row(s), expected {count}"


@then(parsers.parse('exactly {count:d} provenance_log row exists for roll_calls "{entity_id}"'))
def exact_provenance_rows(fake: FakeZeroDBTransport, count: int, entity_id: str) -> None:
    rows = [
        row["row_data"]
        for row in fake.rows.get("provenance_log", {}).values()
        if row["row_data"].get("entity_table") == "roll_calls"
        and row["row_data"].get("entity_id") == entity_id
    ]
    assert len(rows) == count, f"found {len(rows)} provenance_log row(s), expected {count}"


# --- story 5.7: the events ---------------------------------------------------


def _events(fake: FakeZeroDBTransport, topic: str) -> list[dict[str, Any]]:
    return [event for event in fake.events if event["topic"] == topic]


@then(parsers.parse('exactly {count:d} "{topic}" event was emitted'))
@then(parsers.parse('exactly {count:d} "{topic}" events were emitted'))
def exact_event_count(fake: FakeZeroDBTransport, count: int, topic: str) -> None:
    events = _events(fake, topic)
    assert len(events) == count, f"found {len(events)} {topic!r} event(s), expected {count}"


@then(parsers.parse('the "{topic}" event carries roll_call_id "{roll_call_id}"'))
def event_carries_roll_call_id(fake: FakeZeroDBTransport, topic: str, roll_call_id: str) -> None:
    payloads = [event["event_payload"] for event in _events(fake, topic)]
    assert any(p.get("roll_call_id") == roll_call_id for p in payloads), payloads


@then(parsers.parse('the "{topic}" event carries a {field} field'))
def event_carries_field(fake: FakeZeroDBTransport, topic: str, field: str) -> None:
    """Presence, not truthiness. datamodel.md's payload names `bill_id` and
    `topic_ids`, and today story 5.1's transform writes `bill_id: None` and
    `topic_ids: []` on every row -- so an assertion that they are non-empty
    would be asserting story 5.3/5.5's work, not this story's. What 5.7 owes
    its consumer is that the documented KEYS are always present, so the
    consumer never has to guess whether an absent key means "no bill" or
    "producer forgot".
    """
    events = _events(fake, topic)
    assert events, f"no {topic!r} event was emitted"
    for event in events:
        assert field in event["event_payload"], (
            f"{topic} payload {event['event_payload']!r} has no {field!r} key"
        )


@then(parsers.parse('a "{topic}" event was emitted for "{roll_call_id}"'))
def event_emitted_for(fake: FakeZeroDBTransport, topic: str, roll_call_id: str) -> None:
    ids = [event["event_payload"].get("roll_call_id") for event in _events(fake, topic)]
    assert roll_call_id in ids, f"{roll_call_id} not among emitted {topic} events: {ids}"


@then("the position recompute consumer handles the emitted event without error")
def consumer_handles_event(zerodb: ZeroDBClient, fake: FakeZeroDBTransport) -> None:
    """The real story 6.2 consumer, against the real emitted event.

    This is the scenario that proves 5.7 actually closed 6.2's documented
    gap rather than emitting a differently-shaped event that happens to
    share a topic name. It asserts no positions are written -- correctly,
    because `topic_ids` is `[]` until story 5.5 classifies -- so what it
    pins is that the consumer reads the payload, finds the roll call, and
    completes.
    """
    events = _events(fake, ROLLCALL_INGESTED_TOPIC)
    assert events, "nothing emitted rollcall.ingested"
    consumer = PositionRecomputeConsumer(zerodb, pace=lambda: None)
    outcome = consumer.run()
    assert outcome.positions_written == 0, (
        "no position can be computed while roll calls carry topic_ids: [] "
        "(story 5.5 classifies); a write here means the consumer invented one"
    )


# --- story 5.8: the backfill -------------------------------------------------


@when("I read the backfill plan")
def read_backfill_plan(result: dict[str, Any]) -> None:
    result["plan"] = list(BACKFILL_CONGRESS_SESSIONS)


@then(parsers.parse("the backfill plan covers congress {congress:d} sessions 1 and 2"))
def plan_covers(result: dict[str, Any], congress: int) -> None:
    plan = result["plan"]
    for session in (1, 2):
        assert (congress, session) in plan, (
            f"congress {congress} session {session} is not in the backfill plan: {plan}"
        )


class _StubListEndpoint:
    """Stands in for `httpx.get` against the list endpoint.

    Reproduces the two behaviours confirmed live on 2026-10-19: the endpoint
    caps a page at 250 rows however large a `limit` is asked for, and it
    reports the true total separately in `pagination.count`.
    """

    def __init__(self, total: int, page_cap: int = 250) -> None:
        self.total = total
        self.page_cap = page_cap
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> Any:
        params = kwargs.get("params") or {}
        self.calls.append(dict(params))
        offset = int(params.get("offset") or 0)
        limit = min(int(params.get("limit") or 20), self.page_cap)
        numbers = list(range(1, self.total + 1))[offset : offset + limit]
        body = {
            "houseRollCallVotes": [
                {"congress": 118, "sessionNumber": 1, "rollCallNumber": n} for n in numbers
            ],
            "pagination": {"count": self.total},
        }
        return _StubResponse(body)


class _StubResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._body


@given(
    parsers.parse(
        "the source lists {total:d} roll calls for congress {congress:d} "
        "session {session:d} in pages of {page_cap:d}"
    )
)
def source_lists_paged(
    result: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    total: int,
    congress: int,
    session: int,
    page_cap: int,
) -> None:
    stub = _StubListEndpoint(total=total, page_cap=page_cap)
    monkeypatch.setattr("app.ingest.house_rollcalls.httpx.get", stub)
    result["stub"] = stub


@given(
    parsers.parse(
        "the source reports a pagination count of {total:d} for congress "
        "{congress:d} session {session:d}"
    )
)
def source_reports_count(
    result: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    total: int,
    congress: int,
    session: int,
) -> None:
    stub = _StubListEndpoint(total=total)
    monkeypatch.setattr("app.ingest.house_rollcalls.httpx.get", stub)
    result["stub"] = stub


@when(parsers.parse("I list every roll call number for congress {congress:d} session {session:d}"))
def list_every_number(result: dict[str, Any], congress: int, session: int) -> None:
    result["numbers"] = fetch_roll_call_numbers(
        congress=congress, session=session, api_key="test-key"
    )


@then(parsers.parse("{count:d} roll call numbers are returned"))
def numbers_returned(result: dict[str, Any], count: int) -> None:
    actual = len(result["numbers"])
    assert actual == count, f"listed {actual} roll call number(s), expected {count}"


@then("the numbers are unique")
def numbers_unique(result: dict[str, Any]) -> None:
    numbers = result["numbers"]
    assert len(set(numbers)) == len(numbers), "the listing returned duplicate roll call numbers"


@when(parsers.parse("I read the source total for congress {congress:d} session {session:d}"))
def read_source_total(result: dict[str, Any], congress: int, session: int) -> None:
    result["source_total"] = fetch_source_total(
        congress=congress, session=session, api_key="test-key"
    )


@then(parsers.parse("the source total is {total:d}"))
def source_total_is(result: dict[str, Any], total: int) -> None:
    assert result["source_total"] == total


@given(
    parsers.parse("{count:d} roll calls are stored for congress {congress:d} session {session:d}")
)
def rolls_stored(zerodb: ZeroDBClient, count: int, congress: int, session: int) -> None:
    repo: Repository[RollCall] = Repository(client=zerodb, model=RollCall)
    for n in range(1, count + 1):
        repo.upsert(
            {
                "id": f"us-house:{congress}-{session}:{n}",
                "voted_at": "2026-02-24T14:18:00-05:00",
                "question": "On Agreeing to the Resolution",
                "vote_type": "other",
                "result": "Passed",
                "topic_ids": [],
            }
        )


@when(parsers.parse("I reconcile congress {congress:d} session {session:d}"))
def reconcile(zerodb: ZeroDBClient, result: dict[str, Any], congress: int, session: int) -> None:
    result["reconciliation"] = reconcile_congress_session(
        zerodb, congress=congress, session=session, api_key="test-key"
    )


@then("the reconciliation matches")
def reconciliation_matches(result: dict[str, Any]) -> None:
    rec = result["reconciliation"]
    assert rec.matches, f"reconciliation did not match: {rec}"


@then("the reconciliation does not match")
def reconciliation_does_not_match(result: dict[str, Any]) -> None:
    rec = result["reconciliation"]
    assert not rec.matches, f"reconciliation unexpectedly matched: {rec}"


@then(parsers.parse("the reconciliation reports {stored:d} stored and {expected:d} expected"))
def reconciliation_reports(result: dict[str, Any], stored: int, expected: int) -> None:
    rec = result["reconciliation"]
    assert (rec.stored, rec.expected) == (stored, expected), rec


# --- live-shape regressions --------------------------------------------------


@when("I transform the live-shaped sample roll call")
def transform_live_shaped(result: dict[str, Any]) -> None:
    envelope = _fixture_envelopes()["item_live_shape_119_2_74"]
    result["row"] = transform_roll_call(envelope)


@when("I transform the live-shaped batch")
def transform_live_shaped_batch(result: dict[str, Any]) -> None:
    envelope = _fixture_envelopes()["item_live_shape_119_2_74"]
    rows, skipped = transform_roll_calls([envelope])
    result["rows"] = rows
    result["skipped"] = skipped


@then(
    parsers.parse(
        "the transformed roll call has tally yea {yea:d} nay {nay:d} "
        "present {present:d} not_voting {not_voting:d}"
    )
)
def transformed_tally(
    result: dict[str, Any], yea: int, nay: int, present: int, not_voting: int
) -> None:
    tally = result["row"]["tally"]
    assert (tally.yea, tally.nay, tally.present, tally.not_voting) == (
        yea,
        nay,
        present,
        not_voting,
    ), tally


@then(parsers.parse('the transformed roll call has source_url "{source_url}"'))
def transformed_source_url(result: dict[str, Any], source_url: str) -> None:
    assert result["row"]["source_url"] == source_url


@then("no roll call is skipped")
def nothing_skipped(result: dict[str, Any]) -> None:
    skipped = result["skipped"]
    assert not skipped, f"{len(skipped)} record(s) skipped: {[s.reason for s in skipped]}"
    assert result["rows"], "no rows were produced"
