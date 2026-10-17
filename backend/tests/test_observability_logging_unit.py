"""Unit tests for `app.observability.logging_config` (story 16.5).

The BDD feature's "no plaintext in logs" scenario proves the guarantee holds
across a real HTTP request cycle. These tests pin the formatter's own output
shape and the redaction filter's structural pattern-matching in isolation,
including the false-positive/false-negative boundary cases a request-level
test cannot easily force.
"""

from __future__ import annotations

import json
import logging

from app.observability.logging_config import (
    PlaintextLeakGuard,
    StructuredFormatter,
    configure_logging,
    is_configured,
)


def _make_record(
    message: str, *args: object, level: int = logging.INFO, extra: dict[str, object] | None = None
) -> logging.LogRecord:
    logger = logging.getLogger("tests.observability")
    record = logger.makeRecord(logger.name, level, __file__, 0, message, args, None)
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


# --- StructuredFormatter -----------------------------------------------------


def test_format_renders_one_json_object_with_the_fixed_keys() -> None:
    record = _make_record("hello %s", "world")
    payload = json.loads(StructuredFormatter().format(record))

    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "tests.observability"
    assert "timestamp" in payload
    # ISO-8601 UTC, Z-suffixed, matching the codebase's one timestamp spelling.
    assert payload["timestamp"].endswith("Z")


def test_extra_fields_are_merged_into_the_payload() -> None:
    record = _make_record(
        "job ran", extra={"story": "16.5", "operation": "load_house_rollcalls", "duration_ms": 42}
    )
    payload = json.loads(StructuredFormatter().format(record))

    assert payload["story"] == "16.5"
    assert payload["operation"] == "load_house_rollcalls"
    assert payload["duration_ms"] == 42


def test_stdlib_internal_record_attributes_are_never_echoed_as_extra_fields() -> None:
    record = _make_record("plain message")
    payload = json.loads(StructuredFormatter().format(record))

    # These are the stdlib's own bookkeeping, not this module's convention —
    # leaking `args` in particular would reintroduce the exact unformatted
    # values the leak guard exists to keep out of the rendered message.
    for internal_key in ("args", "msg", "pathname", "funcName", "lineno"):
        assert internal_key not in payload


def test_an_unserializable_extra_value_does_not_crash_formatting() -> None:
    class Unserializable:
        def __repr__(self) -> str:
            return "<Unserializable>"

    record = _make_record("message", extra={"thing": Unserializable()})
    payload = json.loads(StructuredFormatter().format(record))
    assert payload["thing"] == "<Unserializable>"


def test_exception_info_is_included_when_present() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _make_record("failed", level=logging.ERROR)
        record.exc_info = __import__("sys").exc_info()

    payload = json.loads(StructuredFormatter().format(record))
    assert "ValueError" in payload["exc_info"]
    assert "boom" in payload["exc_info"]


# --- PlaintextLeakGuard: structural redaction --------------------------------


def test_an_authorization_header_shaped_substring_is_redacted() -> None:
    record = _make_record("calling upstream with Authorization: Bearer abc123tokenvalue")
    guard = PlaintextLeakGuard()
    guard.filter(record)
    assert "abc123tokenvalue" not in record.msg
    assert "[REDACTED]" in record.msg


def test_a_bare_api_key_assignment_is_redacted() -> None:
    record = _make_record("api_key=sk-practicetestkeyvaluethatislong1234567890")
    guard = PlaintextLeakGuard()
    guard.filter(record)
    assert "sk-practicetestkeyvaluethatislong1234567890" not in record.msg


def test_a_long_opaque_base64_looking_run_is_redacted() -> None:
    secret = "QWxhZGRpbjpvcGVuIHNlc2FtZSBtb3JlIGJ5dGVzIGhlcmU="
    record = _make_record(f"wrapped key is {secret}")
    guard = PlaintextLeakGuard()
    guard.filter(record)
    assert secret not in record.msg
    assert "[REDACTED]" in record.msg


def test_a_short_ordinary_identifier_is_left_alone() -> None:
    record = _make_record("loaded candidacy %s", "us-house:119-2:452")
    guard = PlaintextLeakGuard()
    guard.filter(record)
    assert "us-house:119-2:452" in record.msg


def test_filtering_clears_args_so_downstream_formatting_cannot_reintroduce_them() -> None:
    record = _make_record("value is %s", "some-secret-shaped-value-that-is-long-enough-here")
    guard = PlaintextLeakGuard()
    guard.filter(record)
    assert record.args is None


def test_filter_always_returns_true_so_the_record_is_still_emitted() -> None:
    record = _make_record("ordinary message")
    assert PlaintextLeakGuard().filter(record) is True


# --- PlaintextLeakGuard.contains_forbidden: the known-value check -----------


def test_contains_forbidden_finds_an_exact_known_value() -> None:
    hits = PlaintextLeakGuard.contains_forbidden(
        "voter saved interest en-US:some-topic:v1", ["en-US:some-topic:v1"]
    )
    assert hits == ["en-US:some-topic:v1"]


def test_contains_forbidden_reports_nothing_when_absent() -> None:
    hits = PlaintextLeakGuard.contains_forbidden(
        "voter profile saved at version 1", ["en-US:some-topic:v1"]
    )
    assert hits == []


def test_contains_forbidden_ignores_blank_entries() -> None:
    hits = PlaintextLeakGuard.contains_forbidden("some log text", ["", None])  # type: ignore[list-item]
    assert hits == []


# --- configure_logging --------------------------------------------------------


def test_configure_logging_installs_exactly_one_handler() -> None:
    configure_logging(level="DEBUG")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, StructuredFormatter)


def test_configure_logging_is_idempotent_and_does_not_stack_handlers() -> None:
    configure_logging(level="DEBUG")
    configure_logging(level="DEBUG")
    root = logging.getLogger()
    assert len(root.handlers) == 1


def test_configure_logging_attaches_the_leak_guard() -> None:
    configure_logging(level="DEBUG")
    root = logging.getLogger()
    assert any(isinstance(f, PlaintextLeakGuard) for f in root.handlers[0].filters)


def test_is_configured_reports_true_only_after_configure_logging_runs() -> None:
    configure_logging(level="DEBUG")
    assert is_configured() is True
