"""Short-lived Q&A session memory (story 13.5).

datamodel.md, "Files, events, memory and RLHF":

    **Memory** (`zerodb_store_memory`) keeps the Q&A agent's session context,
    scoped by `session_id`, with retention set to 1 day. In anonymous mode
    memory is not written at all.

and the retention table: `Q&A memory | 1 day | Never written in anonymous
mode`.

Two things live here: `SessionMemoryService`, the only way the Q&A agent is
allowed to read or write a conversation, and `sweep_expired_session_memory`,
the retention sweep that actually deletes expired turns. See
`app.agent_memory.service` for why the 1-day window needed both a read guard
and a sweep rather than a TTL parameter.
"""

from app.agent_memory.retention import SessionMemorySweepResult, sweep_expired_session_memory
from app.agent_memory.service import (
    RETENTION,
    RETENTION_DAYS,
    RememberResult,
    SessionMemoryService,
    SessionTurn,
)

__all__ = [
    "RETENTION",
    "RETENTION_DAYS",
    "RememberResult",
    "SessionMemoryService",
    "SessionMemorySweepResult",
    "SessionTurn",
    "sweep_expired_session_memory",
]
