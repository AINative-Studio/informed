"""Domain exceptions for ZeroDB access (story 1.2).

Callers above the client — the repository layer (story 1.4), the ingest agents,
the API handlers — catch these and never `httpx.HTTPError`. That is what lets
the transport underneath swap from REST to MCP without touching callers: an
MCP transport raises its own library's errors, and the client maps them into
this same hierarchy.

Every error carries the HTTP status and ZeroDB's own `error_code` where one was
returned, so a caller can branch on the cause without re-parsing a body.
"""


class ZeroDBError(Exception):
    """Base class for every ZeroDB failure the client reports.

    A caller that wants to treat all ZeroDB trouble alike catches this one.
    """

    #: True when retrying the same request could plausibly succeed.
    transient: bool = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        next_action: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.next_action = next_action
        #: Seconds ZeroDB asked us to wait, when it said. The client prefers
        #: this over its own backoff, because the server knows when the
        #: throttle window actually clears.
        self.retry_after = retry_after

    def __str__(self) -> str:
        parts = [self.message]
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.error_code:
            parts.append(f"error_code={self.error_code}")
        return " ".join(parts)


class ZeroDBValidationError(ZeroDBError):
    """The request was malformed or rejected by ZeroDB's schema (400, 422).

    Also raised by the client itself for arguments that cannot produce a valid
    request, so a caller never spends a round trip on a request we know is bad.
    """


class ZeroDBAuthError(ZeroDBError):
    """The API key is missing, wrong, or not permitted here (401, 403)."""


class ZeroDBNotFoundError(ZeroDBError):
    """The addressed table, row, file or entity does not exist (404)."""


class ZeroDBConflictError(ZeroDBError):
    """The write collided with existing state (409).

    Append-only tables (`verdicts`, `candidate_positions`, `provenance_log`)
    rely on this rather than overwriting history in place.
    """


class ZeroDBRateLimitError(ZeroDBError):
    """ZeroDB is throttling us (429). Retried, then surfaced."""

    transient = True


class ZeroDBServerError(ZeroDBError):
    """ZeroDB failed on its side (5xx). Retried, then surfaced."""

    transient = True


class ZeroDBConnectionError(ZeroDBError):
    """The request never got a usable answer: DNS, TCP, TLS or timeout.

    Retried, then surfaced. The original transport exception is kept as
    `__cause__` for debugging without leaking it into the caller's except
    clauses.
    """

    transient = True


class ZeroDBProtocolError(ZeroDBError):
    """ZeroDB answered, but not in a shape this client understands.

    Raised for a non-JSON body and for a response missing fields the typed
    model requires. Deliberately not transient: retrying a shape change just
    wastes attempts, and failing loudly is what catches an upstream API change
    before bad data reaches the evidence graph.
    """


__all__ = [
    "ZeroDBAuthError",
    "ZeroDBConflictError",
    "ZeroDBConnectionError",
    "ZeroDBError",
    "ZeroDBNotFoundError",
    "ZeroDBProtocolError",
    "ZeroDBRateLimitError",
    "ZeroDBServerError",
    "ZeroDBValidationError",
]
