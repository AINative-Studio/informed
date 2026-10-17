"""Structured JSON logging, standardized across the whole app (story 16.5).

## What was there before this story

Every module that logs (`app.ingest.*`, `app.corrections.retention`,
`app.integrity.*`, `app.scheduling.*`) already does the right THING —
`logging.getLogger(__name__)` plus `%s`-style messages, never an f-string
with voter data interpolated in. What none of them do is agree on a FORMAT:
each is free text with its own shape, which is fine to read in a terminal and
useless to a log aggregator that wants to filter by `operation` or compute a
p95 by `duration_ms`. This module is the one place that gets fixed, once,
rather than twenty call sites each inventing their own structure.

## The format: one JSON object per line

`StructuredFormatter` renders every `LogRecord` as a single-line JSON object
with a fixed set of top-level keys plus whatever `extra={...}` the call site
supplied, merged in. Fixed keys:

    timestamp   ISO-8601 UTC, matching this codebase's one timestamp
                spelling (`app.scheduling.registry._iso`)
    level       "INFO", "WARNING", etc.
    logger      the `__name__` the record came from
    message     the formatted message, AFTER the leak guard below has run
    story       optional; a call site passes it via extra={"story": "16.2"}
                when it is worth tagging which story's code logged this
    operation   optional; a short verb phrase, e.g. "load_house_rollcalls"
    duration_ms optional; wall-clock cost of the operation, for latency
                dashboards built off logs rather than off `/metrics`

`story`, `operation` and `duration_ms` are conventions, not requirements —
most log lines in this codebase are fine without them. They exist so a line
that DOES want to say "this took 340ms" says it the same way everywhere,
which is what makes "p95 latency from the logs" a query instead of a parsing
project.

## The leak guard: the actual point of this module

Story 14.1 built one feature-level test
(`test_encrypted_profile_storage_steps.py`'s "Plaintext is never logged")
that captures real log output during a save/read cycle and asserts specific
known-sensitive strings are absent. That is correct but narrow: it only
catches what that one test happens to check for, and it runs nowhere near
every other router that logs.

`PlaintextLeakGuard` generalises it into something that runs on EVERY log
record, in production and in tests, not just in one feature file:

  * it redacts anything that LOOKS like key material, a bearer token, or a
    long opaque secret by structural pattern (base64/hex runs above a length
    no human-readable word reaches, `Authorization: Bearer ...`-shaped
    substrings) — independent of knowing the specific secret value in
    advance;
  * it is paired with (not a replacement for) the existing discipline of
    "never pass voter data as a log argument" — no regex can reliably
    recognise an arbitrary topic id or free-text interest as sensitive by
    its shape alone, because topic ids are readable strings
    (`en-US:rx-drug-pricing:v1`) indistinguishable in form from any other
    identifier this codebase logs on purpose (`en-US:some-public-topic:v1`
    is fine to log; a voter's own choice of it is not). That half of the
    guarantee is enforced the way 14.1 already enforces it: by the code
    paths that touch `InterestProfile` simply never passing it, or any field
    of it, to a logger — see `app.profiles.service`'s log lines, which log
    `row.key_ref` and a version number and nothing else.

The guard is therefore a belt to that codebase's existing suspenders: a
structural net that catches secret-shaped material even if a future call
site slips, PLUS the repo-wide test in
`test_monitoring_and_tracing_steps.py` that replays a full HTTP request
cycle through profile save and asserts the specific topic/stance ids used in
that request never appear in the captured log text — the generalised,
automated version of 14.1's check, now exercised at the HTTP layer rather
than only at the service layer, and runnable against ANY future feature's
log output the same way.

## Why a `logging.Filter`, not a `Formatter` subclass alone

A `Filter` runs on the record before formatting and can mutate
`record.msg`/`record.args` in place, so the redaction happens once per
record regardless of which handler/formatter eventually renders it (JSON in
production, bare text in a developer's terminal). Attaching it as a filter
on the root logger means every logger in the process is covered the moment
`configure_logging()` runs, with no per-module opt-in to forget.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

#: The codebase's one timestamp spelling (see `app.scheduling.registry._iso`).
_TIMESPEC = "milliseconds"

#: `LogRecord` attributes that are part of the stdlib's own shape and must
#: never be re-emitted as "extra" fields (they would either duplicate a fixed
#: key above or leak internals like the raw `args` tuple, which can hold
#: exactly the unredacted values this module exists to protect against).
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)

#: Structural patterns for secret-shaped substrings, independent of knowing
#: any specific secret value in advance:
#:
#:   * an HTTP Authorization header value, any scheme;
#:   * a long contiguous run of base64/hex-alphabet characters — the shape of
#:     an API key, a bearer token, a wrapped data key or a cipher digest.
#:     32 chars is the floor: it is already longer than any human-chosen
#:     identifier this codebase logs on purpose (the longest is a FEC
#:     candidate id or a ZeroDB UUID, both well under 32 contiguous
#:     alphanumerics without a separator), and it is deliberately well under
#:     a real secret's length (an AES-256 key base64-encodes to 44
#:     characters; a JWT's segments are each far longer) — a false positive
#:     here redacts something that was not a secret, which costs nothing; a
#:     false negative lets a real secret through, which is the failure mode
#:     this guard exists to prevent, so the threshold leans toward catching
#:     more rather than less.
_AUTH_HEADER_PATTERN = re.compile(r"(?i)\b(authorization|api[_-]?key)\s*[:=]\s*(bearer\s+)?\S+")
_OPAQUE_SECRET_PATTERN = re.compile(r"\b[A-Za-z0-9+/_=-]{32,}\b")

#: What a redacted secret is replaced with. Says that something was removed
#: rather than leaving a gap that reads as "this log line was just short".
_REDACTED = "[REDACTED]"


def _redact(text: str) -> str:
    """Replace anything secret-shaped in `text` with `_REDACTED`."""
    text = _AUTH_HEADER_PATTERN.sub(_REDACTED, text)
    text = _OPAQUE_SECRET_PATTERN.sub(_REDACTED, text)
    return text


class PlaintextLeakGuard(logging.Filter):
    """A logging filter that redacts secret-shaped text from every record.

    Mutates `record.msg` in place to the fully-formatted, redacted string and
    clears `record.args`, so downstream formatting (`%`-style or otherwise)
    cannot reintroduce the original unformatted arguments. This is safe to do
    unconditionally: by the time a filter runs, nothing downstream needs the
    original `args` tuple, and `record.getMessage()` is what every formatter
    (including the stdlib default) actually renders.

    `violations()` is a classmethod-free static helper used by the repo-wide
    test and documented as the "plaintext-in-logs guard" acceptance scenario:
    given a list of already-captured log lines (or raw `LogRecord`s), report
    which ones still contain a given forbidden substring. The guard filter
    above stops secret-SHAPED text; this check is what a test uses to assert
    a SPECIFIC known value (a topic id used in that test's own request) never
    reached the log, which is the complementary half of the guarantee — see
    the module docstring's explanation of why shape-based redaction alone
    cannot catch an arbitrary voter interest.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = _redact(message)
        record.args = None
        return True

    @staticmethod
    def contains_forbidden(log_text: str, forbidden: list[str]) -> list[str]:
        """Which of `forbidden` substrings appear anywhere in `log_text`.

        Used by `test_monitoring_and_tracing_steps.py` to turn "no captured
        log line contains any interest topic id" into a concrete assertion
        against the real text `caplog` captured over a real request cycle.
        """
        return [value for value in forbidden if value and value in log_text]


class StructuredFormatter(logging.Formatter):
    """Renders one `LogRecord` as a single-line JSON object.

    See the module docstring for the fixed keys and the `story`/`operation`/
    `duration_ms` conventions. Any other `extra={...}` key a call site passes
    is merged in as-is, so this formatter never has to be told about a new
    convention before a module can use one.
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = (
            datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec=_TIMESPEC)
            .replace("+00:00", "Z")
        )
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # default=str: an extra field holding something non-JSON-native (a
        # dataclass, a Path) must not crash logging itself — logging a
        # malformed record is worse than logging an approximate one.
        return json.dumps(payload, default=str)


_CONFIGURED = False


def configure_logging(*, level: str | int = "INFO", stream: Any = None) -> None:
    """Install the structured formatter and leak guard on the root logger.

    Idempotent: calling this more than once (the app factory, a script, and a
    test fixture may all want to call it) does not stack up duplicate
    handlers — it clears and re-installs rather than appending.

    Args:
        level: root logger level, name or numeric. Defaults to INFO; set
            `LOG_LEVEL` in the environment (see `.env.example`) and pass
            `os.getenv("LOG_LEVEL", "INFO").upper()` from the call site —
            this function takes the resolved value rather than reading the
            environment itself, for the same reason `ZeroDBSettings` reads
            the environment and nothing else: one seam, testable without
            environment mutation.
        stream: where the handler writes. Defaults to `sys.stdout` — logs are
            scraped from process output by whatever runs this (a Railway
            service, a GitHub Actions job, a developer's terminal), never
            from a file this app manages itself.
    """
    global _CONFIGURED
    root = logging.getLogger()
    root.setLevel(level)

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(StructuredFormatter())
    handler.addFilter(PlaintextLeakGuard())

    root.handlers.clear()
    root.addHandler(handler)
    _CONFIGURED = True


def is_configured() -> bool:
    """Whether `configure_logging` has run in this process. For tests that
    need to know whether they are observing the structured format or
    pytest's own default capture handler.
    """
    return _CONFIGURED


__all__ = [
    "PlaintextLeakGuard",
    "StructuredFormatter",
    "configure_logging",
    "is_configured",
]
