"""Steps for story 14.7's privacy-safe analytics feature.

Every event this file writes is synthetic ("WA", "OR", "NV" buckets with no
real geocoded data behind them) — there is no voter here, and nothing in
this feature's data model (datamodel.md: an event type, a timestamp, and an
optional coarse bucket) could identify one even if there were. CLAUDE.md's
"never put real voter data in fixtures" is satisfied by there being no field
here capable of holding voter data at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.analytics.models import AggregateResult, RecordedEvent
from app.analytics.service import AnalyticsService
from app.analytics.tracker_audit import (
    DISALLOWED_ANALYTICS_PACKAGES,
    THIRD_PARTY_ANALYTICS_DOMAINS,
    find_disallowed_dependencies,
    find_third_party_tracker_scripts,
)
from app.zerodb.client import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/privacy_safe_analytics.feature")

#: The repo root, two levels up from backend/tests/step_defs — where
#: frontend/ lives, for the "no third-party tracker" scenario.
_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class AnalyticsContext:
    client: ZeroDBClient | None = None
    service: AnalyticsService | None = None
    last_recorded: RecordedEvent | None = None
    aggregate: AggregateResult | None = None
    dependency_findings: list[str] = field(default_factory=list)
    script_findings: list[str] = field(default_factory=list)


@pytest.fixture
def analytics_ctx() -> AnalyticsContext:
    return AnalyticsContext()


@given("an analytics event store with no events recorded yet", target_fixture="analytics_ctx")
def fresh_store(analytics_ctx: AnalyticsContext) -> AnalyticsContext:
    transport = FakeZeroDBTransport()
    analytics_ctx.client = ZeroDBClient(
        transport=transport, project_id="proj-test", sleep=lambda _: None
    )
    analytics_ctx.service = AnalyticsService(client=analytics_ctx.client)
    return analytics_ctx


@when(parsers.parse('the app records a "{event_type}" usage event in bucket "{bucket}"'))
def record_event(analytics_ctx: AnalyticsContext, event_type: str, bucket: str) -> None:
    assert analytics_ctx.service is not None
    analytics_ctx.last_recorded = analytics_ctx.service.record(event_type, bucket=bucket)


@given(parsers.parse('{count:d} "{event_type}" events have been recorded in bucket "{bucket}"'))
def record_many_events(
    analytics_ctx: AnalyticsContext, count: int, event_type: str, bucket: str
) -> None:
    assert analytics_ctx.service is not None
    for _ in range(count):
        analytics_ctx.service.record(event_type, bucket=bucket)


@when(
    parsers.parse('the team requests the aggregate count for "{event_type}" in bucket "{bucket}"')
)
def request_aggregate(analytics_ctx: AnalyticsContext, event_type: str, bucket: str) -> None:
    assert analytics_ctx.service is not None
    analytics_ctx.aggregate = analytics_ctx.service.aggregate(event_type, bucket=bucket)


@then("the recorded event has no user_id, no ip address and no address field")
def check_no_identifying_fields(analytics_ctx: AnalyticsContext) -> None:
    assert analytics_ctx.last_recorded is not None
    payload_keys = set(analytics_ctx.last_recorded.payload)
    forbidden = {"user_id", "ip", "ip_address", "address", "voter_id", "profile_id"}
    assert forbidden.isdisjoint(payload_keys)


@then("the recorded event has only an event type, a timestamp and the coarse bucket")
def check_only_allowed_fields(analytics_ctx: AnalyticsContext) -> None:
    assert analytics_ctx.last_recorded is not None
    assert analytics_ctx.last_recorded.event_type == "ballot_viewed"
    assert analytics_ctx.last_recorded.recorded_at
    assert set(analytics_ctx.last_recorded.payload) <= {"bucket"}


@then("the response says data is insufficient")
def check_insufficient(analytics_ctx: AnalyticsContext) -> None:
    assert analytics_ctx.aggregate is not None
    assert analytics_ctx.aggregate.sufficient is False
    assert analytics_ctx.aggregate.count is None


@then(parsers.parse("the response never reveals the number {number:d}"))
def check_number_not_revealed(analytics_ctx: AnalyticsContext, number: int) -> None:
    assert analytics_ctx.aggregate is not None
    serialized = str(analytics_ctx.aggregate.model_dump())
    assert str(number) not in serialized


@then(parsers.parse("the response reveals a real count of {number:d}"))
def check_real_count(analytics_ctx: AnalyticsContext, number: int) -> None:
    assert analytics_ctx.aggregate is not None
    assert analytics_ctx.aggregate.sufficient is True
    assert analytics_ctx.aggregate.count == number


@when("the frontend dependency manifest is checked against the disallowed analytics list")
def check_dependency_manifest(analytics_ctx: AnalyticsContext) -> None:
    analytics_ctx.dependency_findings = find_disallowed_dependencies(
        _REPO_ROOT / "frontend" / "package.json"
    )
    analytics_ctx.script_findings = find_third_party_tracker_scripts(_REPO_ROOT / "frontend")


@then("no disallowed analytics package is present")
def check_no_disallowed_packages(analytics_ctx: AnalyticsContext) -> None:
    assert analytics_ctx.dependency_findings == []


@then(
    "no script tag pointing at a known third-party analytics domain exists in the frontend source"
)
def check_no_tracker_scripts(analytics_ctx: AnalyticsContext) -> None:
    assert analytics_ctx.script_findings == []


__all__ = ["DISALLOWED_ANALYTICS_PACKAGES", "THIRD_PARTY_ANALYTICS_DOMAINS"]
