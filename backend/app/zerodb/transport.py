"""The transport seam between the typed ZeroDB client and the wire (story 1.2).

THIS IS THE ONE FILE THE MCP SWAP TOUCHES. `ZeroDBClient` builds requests as
`(method, path, json, params)` and reads back `ZeroDBResponse`; it never imports
httpx. To move Informed onto the ZeroDB MCP server, add an
`McpZeroDBTransport` here that maps each `(method, path)` onto the matching
`zerodb_*` MCP tool call and returns the tool's result in a `ZeroDBResponse`.
No client method, repository or test above this boundary changes.

See `client.py`'s module docstring for why REST is in use today.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

USER_AGENT = "informed-api/zerodb-client"


@dataclass(frozen=True)
class ZeroDBResponse:
    """A transport-neutral answer from ZeroDB.

    Attributes:
        status_code: HTTP status, or the nearest equivalent an MCP transport can
            synthesise (200 for a successful tool call, 404 when the tool
            reports the target is absent, and so on).
        json_body: The parsed response body, or None when it was not JSON.
        text: The raw body, kept for error messages when parsing failed.
    """

    status_code: int
    json_body: Any | None
    text: str = ""

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


@runtime_checkable
class ZeroDBTransport(Protocol):
    """What the client needs from whatever carries its requests.

    Implementations may raise their own connection or timeout exceptions; the
    client catches `httpx.TransportError` and anything deriving from
    `ZeroDBTransportError` and maps both to `ZeroDBConnectionError`. They must
    not raise for an error *status* — a 404 is returned as a `ZeroDBResponse`,
    because only the client decides what a status means.
    """

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ZeroDBResponse:
        """Send one request and return the response. One attempt, no retrying.

        Retrying is the client's job: it owns the policy about which failures
        are worth repeating, so every transport behaves identically under load.
        """
        ...


class ZeroDBTransportError(Exception):
    """Base for transport-level failures that are not HTTP statuses.

    An MCP transport raises a subclass of this for a dead server or a timed-out
    tool call, and the client maps it to `ZeroDBConnectionError` exactly as it
    does an httpx transport error.
    """


class HttpxZeroDBTransport:
    """Calls the ZeroDB REST API over HTTPS with httpx.

    Temporary: see `client.py` for the tracked deviation from CLAUDE.md's
    "ZeroDB is reached only through the ZeroDB MCP server" rule.

    The bearer token is held in the httpx client's default headers rather than
    passed per call, so no client method handles the credential and it cannot
    reach a log line through a request argument.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ZeroDBResponse:
        response = self._client.request(method, path, json=json, params=params)
        try:
            body: Any | None = response.json()
        except ValueError:
            body = None
        return ZeroDBResponse(status_code=response.status_code, json_body=body, text=response.text)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HttpxZeroDBTransport":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@dataclass
class RecordedCall:
    """One request a transport was asked to make.

    The fake transport collects these so contract tests can assert on the exact
    path, payload and headers the client built, which is the half of the client
    that an in-memory fake can still verify faithfully.
    """

    method: str
    path: str
    json: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


__all__ = [
    "USER_AGENT",
    "HttpxZeroDBTransport",
    "RecordedCall",
    "ZeroDBResponse",
    "ZeroDBTransport",
    "ZeroDBTransportError",
]
