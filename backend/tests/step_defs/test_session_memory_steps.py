"""Step definitions for tests/features/session_memory.feature (story 13.5)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.agent_memory import (
    RETENTION,
    SessionMemoryService,
    sweep_expired_session_memory,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/session_memory.feature")

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


@dataclass
class MemoryWorld:
    transport: FakeZeroDBTransport
    client: ZeroDBClient
    service: SessionMemoryService
    sessions: list[str] = field(default_factory=list)
    anonymous: bool = False
    result: Any = None
    sweeps: list[Any] = field(default_factory=list)
    recalled: list[Any] = field(default_factory=list)
    stale_session: str | None = None
    fresh_session: str | None = None


@pytest.fixture
def world() -> MemoryWorld:
    transport = FakeZeroDBTransport()
    client = ZeroDBClient(project_id=FakeZeroDBTransport.PROJECT_ID, transport=transport)
    return MemoryWorld(transport=transport, client=client, service=SessionMemoryService(client))


@given("an identified Q&A session")
def identified_session(world: MemoryWorld) -> None:
    world.anonymous = False
    world.sessions.append(str(uuid.uuid4()))


@given("an anonymous Q&A session")
def anonymous_session(world: MemoryWorld) -> None:
    world.anonymous = True
    world.sessions.append(str(uuid.uuid4()))


@given("an identified Q&A session with two remembered turns")
def session_with_two_turns(world: MemoryWorld) -> None:
    session = str(uuid.uuid4())
    world.sessions.append(session)
    for content in ("first turn", "second turn"):
        world.service.remember(
            session_id=session, role="user", content=content, anonymous=False, now=NOW
        )


@given("a second identified Q&A session with one remembered turn")
def second_session_with_one_turn(world: MemoryWorld) -> None:
    session = str(uuid.uuid4())
    world.sessions.append(session)
    world.service.remember(
        session_id=session, role="user", content="other session", anonymous=False, now=NOW
    )


@given(parsers.parse("an identified Q&A session with a turn remembered {hours:d} hours ago"))
def session_with_a_turn_aged(world: MemoryWorld, hours: int) -> None:
    session = str(uuid.uuid4())
    world.sessions.append(session)
    world.service.remember(
        session_id=session,
        role="user",
        content=f"turn from {hours}h ago",
        anonymous=False,
        now=NOW - timedelta(hours=hours),
    )
    if hours > 24:
        world.stale_session = session
    else:
        world.fresh_session = session


@given(parsers.parse("an identified Q&A session with a turn remembered {hours:d} hour ago"))
def session_with_a_turn_aged_singular(world: MemoryWorld, hours: int) -> None:
    session_with_a_turn_aged(world, hours)


@when("the voter's turn is remembered")
def remember_the_turn(world: MemoryWorld) -> None:
    world.result = world.service.remember(
        session_id=world.sessions[-1],
        role="user",
        content="how did my representative vote?",
        anonymous=world.anonymous,
        now=NOW,
    )


@when("the first session's memory is read")
def read_the_first_session(world: MemoryWorld) -> None:
    world.recalled = world.service.recall(session_id=world.sessions[0], anonymous=False, now=NOW)


@when("the session's memory is read")
def read_the_session(world: MemoryWorld) -> None:
    world.recalled = world.service.recall(session_id=world.sessions[-1], anonymous=False, now=NOW)


@when("the session-memory retention sweep runs")
@when("the session-memory retention sweep runs again")
def run_the_sweep(world: MemoryWorld) -> None:
    world.sweeps.append(sweep_expired_session_memory(world.client, now=NOW))


@when("the scheduled job registry is listed")
def list_the_registry(world: MemoryWorld) -> None:
    from app.scheduling.jobs import default_jobs

    world.result = {job.name: job for job in default_jobs()}


@then(parsers.parse("the stored turn's retention is {days:d} day"))
def stored_retention_is_one_day(world: MemoryWorld, days: int) -> None:
    assert timedelta(days=days) == RETENTION
    metadata = (world.transport.calls[-1].json or {})["memory_metadata"]
    assert metadata["retention_days"] == days


@then(parsers.parse("the stored turn expires exactly {days:d} day after it was written"))
def stored_expiry_is_one_day_out(world: MemoryWorld, days: int) -> None:
    metadata = (world.transport.calls[-1].json or {})["memory_metadata"]
    expected = (NOW + timedelta(days=days)).isoformat(timespec="microseconds")
    assert metadata["expires_at"] == expected.replace("+00:00", "Z")


@then("nothing was written to ZeroDB")
@then("no ZeroDB request was made at all")
def nothing_written(world: MemoryWorld) -> None:
    assert world.transport.calls == []


@then("the service reports the turn as not stored because the session is anonymous")
def reported_not_stored(world: MemoryWorld) -> None:
    assert world.result.stored is False
    assert world.result.reason == "anonymous mode"


@then("only the first session's two turns come back")
def only_the_first_sessions_turns(world: MemoryWorld) -> None:
    assert [turn.content for turn in world.recalled] == ["first turn", "second turn"]


@then("no turns come back")
def no_turns_come_back(world: MemoryWorld) -> None:
    assert world.recalled == []


@then(parsers.parse("the turn from {hours:d} hours ago is deleted"))
def stale_turn_deleted(world: MemoryWorld, hours: int) -> None:
    assert world.stale_session is not None
    assert world.client.list_memories(session_id=world.stale_session).total == 0


@then(parsers.parse("the turn from {hours:d} hour ago is kept"))
def fresh_turn_kept(world: MemoryWorld, hours: int) -> None:
    assert world.fresh_session is not None
    assert world.client.list_memories(session_id=world.fresh_session).total == 1


@then("the second run deletes nothing")
def second_run_deletes_nothing(world: MemoryWorld) -> None:
    assert world.sweeps[0].deleted >= 1
    assert world.sweeps[1].deleted == 0


@then(parsers.parse('it contains a job named "{name}"'))
def registry_contains_job(world: MemoryWorld, name: str) -> None:
    assert name in world.result


@then(parsers.parse("that job's interval is at most {days:d} day"))
def job_interval_at_most(world: MemoryWorld, days: int) -> None:
    assert world.result["sweep_qa_session_memory"].interval <= timedelta(days=days)


@then("the stored payload holds no interest, stance, tier or dealbreaker field")
def payload_holds_no_profile(world: MemoryWorld) -> None:
    flattened = str(world.transport.calls[-1].json or {}).lower()
    for banned in ("interest", "stance", "tier", "dealbreaker"):
        assert banned not in flattened, banned
