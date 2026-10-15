"""ZeroDB access for Informed (story 1.2).

The one door between Informed's backend and ZeroDB. Repositories (story 1.4),
the bootstrap command (1.3) and the ingest agents import `ZeroDBClient` and the
domain exceptions from here, and never reach for httpx, a URL or an MCP tool
themselves.

    from app.zerodb import ZeroDBClient
    from app.zerodb.errors import ZeroDBNotFoundError

    client = ZeroDBClient.from_env()

NOTE: this client currently speaks ZeroDB's REST API rather than going through
the ZeroDB MCP server that CLAUDE.md mandates. That is a tracked, intentional,
temporary deviation with a one-file migration path — see the module docstring in
`client.py` for why and for how the swap works.

Module map:
    client.py    — the typed client: one method per ZeroDB primitive
    transport.py — the wire boundary; the only file the MCP swap touches
    models.py    — typed response models, verified against the live API
    errors.py    — the domain exceptions callers catch
    settings.py  — credentials and tuning, read from the environment
    testing.py   — the in-memory fake transport for contract tests
"""

from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import (
    ZeroDBAuthError,
    ZeroDBConflictError,
    ZeroDBConnectionError,
    ZeroDBError,
    ZeroDBNotFoundError,
    ZeroDBProtocolError,
    ZeroDBRateLimitError,
    ZeroDBServerError,
    ZeroDBValidationError,
)
from app.zerodb.settings import ZeroDBSettings
from app.zerodb.transport import HttpxZeroDBTransport, ZeroDBTransport

__all__ = [
    "HttpxZeroDBTransport",
    "ZeroDBAuthError",
    "ZeroDBClient",
    "ZeroDBConflictError",
    "ZeroDBConnectionError",
    "ZeroDBError",
    "ZeroDBNotFoundError",
    "ZeroDBProtocolError",
    "ZeroDBRateLimitError",
    "ZeroDBServerError",
    "ZeroDBSettings",
    "ZeroDBTransport",
    "ZeroDBValidationError",
]
