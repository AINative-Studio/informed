"""Reading and writing one Q&A session's context (story 13.5).

===============================================================================
THE TWO RULES, AND WHY EACH NEEDED ITS OWN MECHANISM
===============================================================================

datamodel.md gives this story two rules: "retention set to 1 day" and "in
anonymous mode memory is not written at all". They are enforced by different
mechanisms because they fail in different ways.

**"Nothing in anonymous mode" is a refusal at the door.** `remember()` checks
`anonymous` before it builds a request, so an anonymous session makes no ZeroDB
call whatsoever. A write that happened and was then deleted would still have
been a write — the content would have crossed the network, landed in a
database, and appeared in whatever logs the server keeps. The scenario
"Anonymous mode is refused before the request is built, not filtered
afterwards" is what pins this, and `anonymous` is a REQUIRED keyword argument
on every method rather than a defaulted one: a caller that forgets it gets a
`TypeError`, not the writing path.

**"1 day" needed two mechanisms, because ZeroDB has no TTL.** Probed live
during this story against `POST /projects/{id}/database/memory`:

  * there is no `ttl`, `ttl_seconds`, `expires_at` or `retention` field on
    `MemoryRecordCreate` — the full request schema is
    `{agent_id, session_id, role, content, memory_metadata, project_id,
    embedding}`;
  * sending `ttl_seconds: 86400` and a top-level `expires_at` anyway returns
    200 and SILENTLY DROPS both — neither comes back in the response, and
    nothing expires;
  * `/api/v1/memory/decay-config` is a decay-RATE and LRU-eviction-LIMIT knob
    (`default_decay_rate`, `eviction_limit`), not a time-based retention one,
    and answered 500 on a live GET;
  * `memory_metadata` DOES round-trip verbatim.

So "retention is 1 day" is this codebase's promise to keep, not ZeroDB's. It is
kept twice over, deliberately:

  1. **A read guard.** `recall()` never returns a turn past its window, even
     while ZeroDB still holds the row. This is the mechanism that makes the
     promise true to the voter at every instant: a sweep that runs late, or
     fails, or has not been wired to a scheduler yet, cannot cause an expired
     question to be served back into a conversation.
  2. **A sweep.** `app.agent_memory.retention.sweep_expired_session_memory`
     deletes the rows, registered in `app.scheduling.jobs.default_jobs()` as
     `sweep_qa_session_memory` on a 6-hour cadence. This is the mechanism that
     makes the promise true to the DATABASE: the row stops existing, which is
     what a retention claim actually means.

Either alone would be a weaker promise than datamodel.md states. The read guard
alone leaves the data sitting in ZeroDB forever; the sweep alone means a turn is
retained for up to one sweep interval past its day. Both is the honest reading.

**Why `expires_at` is written into `memory_metadata` at all**, given the read
guard could compute it from `created_at`: because the window is then a property
of the row rather than of whichever code happens to read it. A later service, a
support query, or an auditor reading the table directly can see when each turn
was due to go without knowing this module's constant — and if the constant ever
changes, rows already written keep the window they were written under rather
than being retroactively extended. The read guard falls back to `created_at`
for a row that has no marker (one written before this story, or by another
client), so no row is ever immortal because its metadata was incomplete.

===============================================================================
WHAT IS NEVER WRITTEN
===============================================================================

The turn's text and nothing else. No profile id, no interest, no tier, no
stance, no dealbreaker — prd.md's rule is that a voter's priorities never leave
`voter_profiles`, and a Q&A transcript is a tempting place to denormalise them
into. `test_the_stored_payload_holds_no_voter_profile_field` is the mechanical
check.

`session_id` is a uuid4, which ZeroDB requires (a readable `"qa:session-1"` is
rejected 422, measured live) and which is also the right privacy choice: an
opaque id cannot carry a user id, a district or an address the way a composed
one could.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.zerodb.client import ZeroDBClient

#: datamodel.md's retention window for Q&A memory: "1 day". One definition,
#: used by the read guard, the written marker and the sweep, and pinned by a
#: test against the documented promise.
RETENTION_DAYS = 1
RETENTION = timedelta(days=RETENTION_DAYS)

#: The codebase's one timestamp spelling, matching
#: `app.corrections.retention._iso` and `app.scheduling.registry._iso`.
_TIMESPEC = "microseconds"

#: Counts, rule names and session ids never travel to the log from here. A
#: voter's question is not profile data, but it is a political-opinion signal
#: and prd.md's rule is "never log" — so this module logs what HAPPENED, never
#: what was asked. Same posture as `app.corrections.retention`, which erases
#: emails without ever logging one.
logger = logging.getLogger(__name__)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec=_TIMESPEC).replace("+00:00", "Z")


def parse_iso(value: str) -> datetime | None:
    """Parse a timestamp from ZeroDB, tolerating what it actually sends.

    Live `created_at` comes back as `2026-10-17T09:05:59.050155+00:00` (offset
    form), while this module writes `...Z`. Both parse; anything unparseable
    returns None so a caller can treat it as "no usable clock" rather than
    crashing a sweep on one malformed row.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def expiry_of(memory_metadata: dict[str, object] | None, created_at: str | None) -> datetime | None:
    """When one stored turn falls out of its retention window.

    The row's own `expires_at` marker if it has one (so a row keeps the window
    it was written under), else `created_at + RETENTION`. None only when the
    row carries neither a usable marker nor a usable creation time, which the
    sweep treats as "cannot age this safely, leave it and report it" rather
    than as either extreme.

    Shared by the read guard and the sweep so there is exactly one definition
    of "expired" in this codebase.
    """
    marker = (memory_metadata or {}).get("expires_at")
    if isinstance(marker, str):
        parsed = parse_iso(marker)
        if parsed is not None:
            return parsed
    if created_at:
        created = parse_iso(created_at)
        if created is not None:
            return created + RETENTION
    return None


@dataclass(frozen=True)
class SessionTurn:
    """One turn of a conversation, as the agent reads it back.

    Deliberately narrower than `app.zerodb.models.MemoryRecord`: the agent
    needs the role, the text and when it was said, and has no business with
    ZeroDB's internal ids beyond the one needed to delete it.
    """

    memory_id: str
    role: str
    content: str
    created_at: str | None = None


@dataclass(frozen=True)
class RememberResult:
    """Whether a turn was stored, and if not, why not.

    A refusal is an ordinary outcome, not an error: an anonymous session is a
    supported, first-class way to use Informed (story 14.3), not a degraded
    one, so a caller must not have to catch an exception per turn to support
    it.
    """

    stored: bool
    memory_id: str | None = None
    reason: str | None = None
    expires_at: str | None = None


class SessionMemoryService:
    """The only way the Q&A agent touches session memory.

    Stateless; safe to share across requests, like `ZeroDBClient` itself.
    """

    def __init__(self, client: ZeroDBClient) -> None:
        self._client = client

    @staticmethod
    def new_session_id() -> str:
        """Mint a session id.

        A uuid4 because ZeroDB requires a UUID (422 otherwise, measured live)
        and because an opaque random id cannot encode anything about the voter.
        """
        return str(uuid.uuid4())

    def remember(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        anonymous: bool,
        now: datetime | None = None,
    ) -> RememberResult:
        """Store one turn, unless the session is anonymous.

        Args:
            session_id: the conversation's uuid. Not a user id, not derived
                from one.
            role: "user" or "assistant", matching ZeroDB's own vocabulary.
            content: the turn's text. The only voter-supplied value written.
            anonymous: required, not defaulted. `True` writes nothing and makes
                no request — see the module docstring on why the check is at
                the door rather than after the fact.
            now: the moment to date the turn from. Injectable because every
                behaviour here is about elapsed time; production callers omit
                it.

        Returns:
            A `RememberResult`. `stored=False` with `reason="anonymous mode"`
            is a success, not a failure.
        """
        if anonymous:
            # Nothing is built, nothing is sent, nothing is logged about the
            # content. datamodel.md: "In anonymous mode memory is not written
            # at all."
            logger.info("declined to store a Q&A turn: anonymous mode")
            return RememberResult(stored=False, reason="anonymous mode")

        moment = now or datetime.now(UTC)
        expires_at = _iso(moment + RETENTION)
        record = self._client.store_memory(
            session_id=session_id,
            content=content,
            role=role,
            memory_metadata={
                # The window as a property of the row. See the module
                # docstring: this is NOT a server-enforced TTL, and the name is
                # `expires_at` rather than `ttl` precisely so a reader asks
                # "enforced by what?" and finds the sweep.
                "expires_at": expires_at,
                "retention_days": RETENTION_DAYS,
                # What wrote it, so a sweep or an audit can tell this story's
                # rows from any other use of the memory table.
                "kind": "qa_session_turn",
            },
            created_at=_iso(moment) if now is not None else None,
        )
        logger.info("stored a Q&A turn, expiring in %d day(s)", RETENTION_DAYS)
        return RememberResult(stored=True, memory_id=record.memory_id, expires_at=expires_at)

    def recall(
        self,
        *,
        session_id: str,
        anonymous: bool,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[SessionTurn]:
        """Read one session's unexpired turns, oldest first.

        The read guard. A turn past its window is not returned even while
        ZeroDB still holds the row — see the module docstring for why that is
        the mechanism the voter-facing promise rests on, and the sweep is the
        one the database-facing promise rests on.

        An anonymous session has nothing stored, so this makes no request
        either: asking ZeroDB for a session that by construction has no rows
        would be a round trip spent to learn what the flag already said.
        """
        if anonymous:
            return []

        moment = now or datetime.now(UTC)
        page = self._client.list_memories(session_id=session_id, limit=limit)
        turns: list[SessionTurn] = []
        for record in page.memories:
            expiry = expiry_of(record.memory_metadata, record.created_at)
            if expiry is not None and expiry <= moment:
                continue
            turns.append(
                SessionTurn(
                    memory_id=record.memory_id,
                    role=record.role or "user",
                    content=record.content,
                    created_at=record.created_at,
                )
            )
        return turns

    def forget_session(self, *, session_id: str) -> int:
        """Delete every turn of one session; return how many were deleted.

        The voter-facing "forget this conversation" action, and what story
        14.4's consent withdrawal calls for Q&A memory. Takes no `anonymous`
        flag on purpose: deleting is always allowed, and an anonymous session
        simply has nothing to delete.

        Pages rather than assuming one request covers the session, since the
        live `limit` caps at 100 and a long conversation can exceed it.
        """
        deleted = 0
        while True:
            page = self._client.list_memories(session_id=session_id)
            if not page.memories:
                return deleted
            for record in page.memories:
                self._client.delete_memory(memory_id=record.memory_id)
                deleted += 1
            if not page.has_more:
                return deleted


__all__ = [
    "RETENTION",
    "RETENTION_DAYS",
    "RememberResult",
    "SessionMemoryService",
    "SessionTurn",
    "expiry_of",
    "parse_iso",
]
