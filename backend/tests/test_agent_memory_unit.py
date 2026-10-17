"""Short-lived Q&A session memory (story 13.5).

datamodel.md, "Files, events, memory and RLHF":

    **Memory** (`zerodb_store_memory`) keeps the Q&A agent's session context,
    scoped by `session_id`, with retention set to 1 day. In anonymous mode
    memory is not written at all.

and the retention table: `Q&A memory | 1 day | Never written in anonymous mode`.

Two independent mechanisms, tested separately below:

  1. **Anonymous mode refuses the write.** Checked before the request is
     built, so an anonymous session makes no ZeroDB call at all — not a call
     whose result is discarded.
  2. **A 1-day window, enforced twice.** ZeroDB's memory API has NO TTL field
     (probed live during this story: `ttl_seconds` and a top-level
     `expires_at` are both accepted and silently dropped). So the window is
     enforced on the read — an expired turn is never returned even while the
     row still exists — and swept by a scheduled job that deletes it. Belt and
     braces on purpose: the read guard means a late sweep never leaks a turn,
     and the sweep means the row does not sit in ZeroDB forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agent_memory import (
    RETENTION,
    RETENTION_DAYS,
    SessionMemoryService,
    sweep_expired_session_memory,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
SESSION = "11111111-1111-4111-8111-111111111111"
OTHER_SESSION = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def transport() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(transport: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(project_id=FakeZeroDBTransport.PROJECT_ID, transport=transport)


@pytest.fixture
def service(client: ZeroDBClient) -> SessionMemoryService:
    return SessionMemoryService(client)


# --- the documented retention window -------------------------------------


def test_the_retention_window_is_one_day() -> None:
    """Pinned against datamodel.md's own number so a change to either has to
    be a deliberate change to both.
    """
    assert RETENTION_DAYS == 1
    assert timedelta(days=1) == RETENTION


# --- identified mode: the write happens, with a 1-day marker -------------


def test_remembering_a_turn_writes_it(service: SessionMemoryService) -> None:
    result = service.remember(
        session_id=SESSION, role="user", content="how did she vote?", anonymous=False, now=NOW
    )
    assert result.stored is True
    assert result.memory_id


def test_the_stored_turn_carries_an_expiry_exactly_one_day_out(
    service: SessionMemoryService, transport: FakeZeroDBTransport
) -> None:
    service.remember(session_id=SESSION, role="user", content="q", anonymous=False, now=NOW)
    metadata = (transport.calls[-1].json or {})["memory_metadata"]
    assert metadata["expires_at"] == "2026-10-18T12:00:00.000000Z"
    assert metadata["retention_days"] == 1


def test_the_write_sends_no_server_side_ttl(
    service: SessionMemoryService, transport: FakeZeroDBTransport
) -> None:
    """ZeroDB has no TTL parameter (probed live). Sending a fake one would
    read as "the server enforces this", which it does not.
    """
    service.remember(session_id=SESSION, role="user", content="q", anonymous=False, now=NOW)
    body = transport.calls[-1].json or {}
    assert "ttl_seconds" not in body
    assert "expires_at" not in body


def test_the_stored_payload_holds_no_voter_profile_field(
    service: SessionMemoryService, transport: FakeZeroDBTransport
) -> None:
    """prd.md: a voter's priorities never travel outside `voter_profiles`."""
    service.remember(session_id=SESSION, role="user", content="q", anonymous=False, now=NOW)
    body = transport.calls[-1].json or {}
    flattened = str(body).lower()
    for banned in ("interest", "stance", "tier", "dealbreaker", "primary_", "profile_id"):
        assert banned not in flattened, banned


# --- anonymous mode: nothing is written at all ---------------------------


def test_anonymous_mode_writes_nothing(
    service: SessionMemoryService, transport: FakeZeroDBTransport
) -> None:
    result = service.remember(session_id=SESSION, role="user", content="q", anonymous=True, now=NOW)
    assert result.stored is False
    assert result.memory_id is None
    assert result.reason == "anonymous mode"


def test_anonymous_mode_makes_no_zerodb_request_at_all(
    service: SessionMemoryService, transport: FakeZeroDBTransport
) -> None:
    """Refused before the request is built, not filtered afterwards. A write
    that happened and was then deleted would still have been a write.
    """
    service.remember(session_id=SESSION, role="user", content="q", anonymous=True, now=NOW)
    assert transport.calls == []


def test_anonymous_mode_reading_returns_nothing_and_makes_no_request(
    service: SessionMemoryService, transport: FakeZeroDBTransport
) -> None:
    assert service.recall(session_id=SESSION, anonymous=True, now=NOW) == []
    assert transport.calls == []


def test_anonymous_is_required_rather_than_defaulted(service: SessionMemoryService) -> None:
    """A caller that forgets the flag must not silently get the writing path.
    Every other anonymous-mode-respecting story in this codebase makes the
    same choice: the privacy-preserving branch is never the one you reach by
    omission, but neither is the writing branch — you have to say which.
    """
    with pytest.raises(TypeError):
        service.remember(session_id=SESSION, role="user", content="q", now=NOW)  # type: ignore[call-arg]


# --- reading back ---------------------------------------------------------


def test_recall_returns_only_this_sessions_turns(service: SessionMemoryService) -> None:
    service.remember(session_id=SESSION, role="user", content="one", anonymous=False, now=NOW)
    service.remember(session_id=SESSION, role="assistant", content="two", anonymous=False, now=NOW)
    service.remember(
        session_id=OTHER_SESSION, role="user", content="elsewhere", anonymous=False, now=NOW
    )

    turns = service.recall(session_id=SESSION, anonymous=False, now=NOW)
    assert [turn.content for turn in turns] == ["one", "two"]


def test_recall_never_returns_a_turn_past_its_window(
    service: SessionMemoryService,
) -> None:
    """The read guard: even while ZeroDB still holds the row (because the sweep
    has not run yet), an expired turn is not served.
    """
    stale = NOW - timedelta(hours=25)
    service.remember(session_id=SESSION, role="user", content="stale", anonymous=False, now=stale)
    assert service.recall(session_id=SESSION, anonymous=False, now=NOW) == []


def test_recall_returns_a_turn_still_inside_the_window(service: SessionMemoryService) -> None:
    fresh = NOW - timedelta(hours=23)
    service.remember(session_id=SESSION, role="user", content="fresh", anonymous=False, now=fresh)
    turns = service.recall(session_id=SESSION, anonymous=False, now=NOW)
    assert [turn.content for turn in turns] == ["fresh"]


def test_the_window_boundary_is_exclusive_of_a_turn_exactly_one_day_old(
    service: SessionMemoryService,
) -> None:
    boundary = NOW - timedelta(days=1)
    service.remember(
        session_id=SESSION, role="user", content="boundary", anonymous=False, now=boundary
    )
    assert service.recall(session_id=SESSION, anonymous=False, now=NOW) == []


def test_a_turn_whose_metadata_lost_its_expiry_falls_back_to_created_at(
    service: SessionMemoryService, client: ZeroDBClient
) -> None:
    """A row written before this story (or by another client) has no
    `expires_at` marker. It must still be swept and still be hidden once old:
    the fallback is ZeroDB's own `created_at`, which is always present.
    """
    client.store_memory(
        session_id=SESSION,
        content="no marker",
        role="user",
        memory_metadata={},
        created_at=(NOW - timedelta(hours=30)).isoformat(),
    )
    assert service.recall(session_id=SESSION, anonymous=False, now=NOW) == []


def test_a_new_session_id_is_a_uuid(service: SessionMemoryService) -> None:
    """ZeroDB rejects a non-UUID `session_id` (422, measured live), and an
    opaque uuid is also the privacy-correct choice: a readable session id
    could carry something about the voter.
    """
    import uuid

    minted = service.new_session_id()
    assert uuid.UUID(minted).version == 4


def test_forgetting_a_session_deletes_every_one_of_its_turns(
    service: SessionMemoryService, client: ZeroDBClient
) -> None:
    """The voter-facing "forget this conversation" path, and what consent
    withdrawal (story 14.4) calls into for Q&A memory.
    """
    service.remember(session_id=SESSION, role="user", content="one", anonymous=False, now=NOW)
    service.remember(session_id=SESSION, role="user", content="two", anonymous=False, now=NOW)
    service.remember(
        session_id=OTHER_SESSION, role="user", content="other", anonymous=False, now=NOW
    )

    deleted = service.forget_session(session_id=SESSION)

    assert deleted == 2
    assert client.list_memories(session_id=SESSION).total == 0
    assert client.list_memories(session_id=OTHER_SESSION).total == 1


# --- the sweep ------------------------------------------------------------


def test_the_sweep_deletes_a_turn_past_the_window_and_keeps_a_fresh_one(
    client: ZeroDBClient, service: SessionMemoryService
) -> None:
    service.remember(
        session_id=SESSION,
        role="user",
        content="stale",
        anonymous=False,
        now=NOW - timedelta(hours=25),
    )
    service.remember(
        session_id=OTHER_SESSION,
        role="user",
        content="fresh",
        anonymous=False,
        now=NOW - timedelta(hours=1),
    )

    result = sweep_expired_session_memory(client, now=NOW)

    assert result.deleted == 1
    assert result.kept == 1
    assert client.list_memories(session_id=SESSION).total == 0
    assert client.list_memories(session_id=OTHER_SESSION).total == 1


def test_the_sweep_is_idempotent(client: ZeroDBClient, service: SessionMemoryService) -> None:
    service.remember(
        session_id=SESSION,
        role="user",
        content="stale",
        anonymous=False,
        now=NOW - timedelta(hours=25),
    )
    first = sweep_expired_session_memory(client, now=NOW)
    second = sweep_expired_session_memory(client, now=NOW)
    assert first.deleted == 1
    assert second.deleted == 0


def test_the_sweep_can_report_without_deleting(
    client: ZeroDBClient, service: SessionMemoryService
) -> None:
    service.remember(
        session_id=SESSION,
        role="user",
        content="stale",
        anonymous=False,
        now=NOW - timedelta(hours=25),
    )
    result = sweep_expired_session_memory(client, now=NOW, dry_run=True)
    assert result.deleted == 1
    assert client.list_memories(session_id=SESSION).total == 1


def test_the_sweep_reports_a_line_safe_to_paste_in_a_ticket(
    client: ZeroDBClient, service: SessionMemoryService
) -> None:
    """Counts and nothing else: never a question, never a session id."""
    service.remember(
        session_id=SESSION,
        role="user",
        content="a question about housing policy",
        anonymous=False,
        now=NOW - timedelta(hours=25),
    )
    summary = sweep_expired_session_memory(client, now=NOW).summary()
    assert "housing" not in summary
    assert SESSION not in summary
    assert "1" in summary


def test_the_sweep_logs_no_question_text(
    client: ZeroDBClient, service: SessionMemoryService, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    service.remember(
        session_id=SESSION,
        role="user",
        content="a question about housing policy",
        anonymous=False,
        now=NOW - timedelta(hours=25),
    )
    with caplog.at_level(logging.DEBUG, logger="app.agent_memory"):
        sweep_expired_session_memory(client, now=NOW)
    for record in caplog.records:
        assert "housing" not in record.getMessage()


def test_the_sweep_sweeps_a_turn_with_no_expiry_marker(client: ZeroDBClient) -> None:
    client.store_memory(
        session_id=SESSION,
        content="no marker",
        role="user",
        memory_metadata={},
        created_at=(NOW - timedelta(hours=30)).isoformat(),
    )
    result = sweep_expired_session_memory(client, now=NOW)
    assert result.deleted == 1


def test_a_row_whose_age_cannot_be_established_is_reported_not_deleted(
    client: ZeroDBClient,
) -> None:
    """A parse failure must not become a deletion.

    A row with no `expires_at` marker AND no usable `created_at` has no clock
    this sweep can read. Deleting it would destroy data on the strength of a
    bad timestamp; silently keeping it would hide a row that may be
    indefinitely retained. So it is kept and counted as `undateable`, which is
    what puts it in the summary line an operator reads.
    """
    client.store_memory(
        session_id=SESSION,
        content="unreadable clock",
        role="user",
        memory_metadata={"expires_at": "not-a-timestamp"},
        created_at="also-not-a-timestamp",
    )
    result = sweep_expired_session_memory(client, now=NOW)

    assert result.undateable == 1
    assert result.deleted == 0
    assert client.list_memories(session_id=SESSION).total == 1
    assert "undateable" in result.summary()


def test_a_row_deleted_by_someone_else_mid_sweep_counts_as_already_gone(
    client: ZeroDBClient, service: SessionMemoryService, transport: FakeZeroDBTransport
) -> None:
    """The race this sweep has to tolerate: it lists a row, and by the time it
    deletes it, a concurrent sweep or the voter's own `forget_session` has
    removed it. The row is gone, which is this sweep's goal — so it counts as
    `already_gone` and the loop continues, rather than failing the whole run.

    Staged the way the race actually happens: the row is removed from the store
    after the sweep has listed it, by emptying the transport's own state between
    the list and the delete. `delete_memory` then hits the live
    500-that-means-404 path (which the fake reproduces as a plain 404 for a
    missing row) and the sweep treats it as "already gone".
    """
    service.remember(
        session_id=SESSION,
        role="user",
        content="stale",
        anonymous=False,
        now=NOW - timedelta(hours=25),
    )

    listed = transport.memories[0]["memory_id"]
    real_request = transport.request

    def drop_the_row_before_the_delete(method: str, path: str, **kwargs: object) -> object:
        if method == "DELETE" and listed in path:
            transport.memories.clear()
        return real_request(method, path, **kwargs)  # type: ignore[arg-type]

    transport.request = drop_the_row_before_the_delete  # type: ignore[method-assign]

    result = sweep_expired_session_memory(client, now=NOW)

    assert result.already_gone == 1
    assert result.deleted == 0


def test_the_sweep_stops_at_its_page_ceiling_and_says_so(
    client: ZeroDBClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep that has been down for a long time must not become an unbounded
    full-table scan. It stops, reports `truncated`, and the next run picks up
    where it left off — there is no cursor to lose, because "expired" is a
    function of the row and the clock.
    """
    from app.agent_memory import retention as retention_module

    monkeypatch.setattr(retention_module, "MAX_SWEEP_PAGES", 1)
    # Two pages' worth of rows that are all inside their window, so nothing is
    # deleted and `skip` genuinely advances past page one.
    not_yet_expired = (NOW + timedelta(hours=1)).isoformat()
    for index in range(retention_module.DEFAULT_MEMORY_PAGE_SIZE + 5):
        client.store_memory(
            session_id=SESSION,
            content=f"turn {index}",
            role="user",
            memory_metadata={"expires_at": not_yet_expired},
        )

    result = sweep_expired_session_memory(client, now=NOW)

    assert result.truncated is True
    assert "re-run" in result.summary()
    # Everything it did reach was correctly kept.
    assert result.deleted == 0
    assert result.kept == retention_module.DEFAULT_MEMORY_PAGE_SIZE


def test_the_sweep_is_a_registered_scheduled_job() -> None:
    """A retention promise with no scheduler behind it is a documentation
    claim, not a mechanism — the exact gap story 12.4 flagged and story 16.x's
    registry closed. So this one is registered from the start.
    """
    from app.scheduling.jobs import default_jobs

    jobs = {job.name: job for job in default_jobs()}
    assert "sweep_qa_session_memory" in jobs
    assert jobs["sweep_qa_session_memory"].interval <= timedelta(days=1)
    assert jobs["sweep_qa_session_memory"].precondition is None
