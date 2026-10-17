"""Unit tests for story 14.7's analytics service, k-anonymity gate and the
no-third-party-tracker audit.
"""

from __future__ import annotations

import inspect

import pytest

from app.analytics.models import AggregateResult, RecordedEvent
from app.analytics.service import K_ANONYMITY_THRESHOLD, AnalyticsService
from app.analytics.tracker_audit import (
    find_disallowed_dependencies,
    find_third_party_tracker_scripts,
)
from app.zerodb.client import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport


@pytest.fixture
def client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    return ZeroDBClient(transport=transport, project_id="proj-test", sleep=lambda _: None)


@pytest.fixture
def service(client: ZeroDBClient) -> AnalyticsService:
    return AnalyticsService(client=client)


def test_k_anonymity_threshold_is_fifty() -> None:
    """Pinned as a named constant, not a magic number buried in a method, so
    backlog.md's "k >= 50" is checkable by reading one line."""
    assert K_ANONYMITY_THRESHOLD == 50


def test_record_takes_no_identity_parameter() -> None:
    """`record`'s only parameters are the event type and an optional coarse
    bucket — no user_id, ip or address parameter exists for a caller to
    (mis)use, checked against the live signature rather than by convention.
    """
    signature = inspect.signature(AnalyticsService.record)
    params = [name for name in signature.parameters if name != "self"]
    assert params == ["event_type", "bucket"]
    forbidden = {"user_id", "ip", "ip_address", "address", "voter_id", "profile_id"}
    assert forbidden.isdisjoint(params)


def test_recorded_event_carries_no_identifying_field(service: AnalyticsService) -> None:
    recorded = service.record("ballot_viewed", bucket="WA")
    assert set(recorded.payload) <= {"bucket"}
    assert recorded.payload.get("bucket") == "WA"
    assert recorded.recorded_at


def test_recording_with_no_bucket_omits_it(service: AnalyticsService) -> None:
    recorded = service.record("onboarding_completed")
    assert recorded.payload == {}


def test_aggregate_below_threshold_withholds_the_count(service: AnalyticsService) -> None:
    for _ in range(K_ANONYMITY_THRESHOLD - 1):
        service.record("onboarding_completed", bucket="NV")

    result = service.aggregate("onboarding_completed", bucket="NV")
    assert result.sufficient is False
    assert result.count is None


def test_aggregate_at_exactly_the_threshold_reveals_the_count(service: AnalyticsService) -> None:
    for _ in range(K_ANONYMITY_THRESHOLD):
        service.record("onboarding_completed", bucket="OR")

    result = service.aggregate("onboarding_completed", bucket="OR")
    assert result.sufficient is True
    assert result.count == K_ANONYMITY_THRESHOLD


def test_aggregate_only_counts_the_matching_event_type_and_bucket(
    service: AnalyticsService,
) -> None:
    for _ in range(K_ANONYMITY_THRESHOLD):
        service.record("onboarding_completed", bucket="OR")
    for _ in range(K_ANONYMITY_THRESHOLD):
        service.record("ballot_viewed", bucket="OR")
    for _ in range(K_ANONYMITY_THRESHOLD):
        service.record("onboarding_completed", bucket="WA")

    result = service.aggregate("onboarding_completed", bucket="OR")
    assert result.count == K_ANONYMITY_THRESHOLD


def test_aggregate_result_never_carries_a_count_field_when_insufficient() -> None:
    """Structural, not just a convention the service happens to follow:
    `AggregateResult(sufficient=False, count=5)` is nonsensical and the model
    itself refuses it."""
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic ValidationError
        AggregateResult(event_type="x", sufficient=False, count=5, threshold=K_ANONYMITY_THRESHOLD)


def test_aggregate_result_refuses_sufficient_with_no_count() -> None:
    """The other half of the same guarantee: a result cannot claim
    `sufficient=True` while omitting the number — that would be a different
    kind of inconsistent response (a caller cannot tell "sufficient, but we
    forgot to say what" from a bug)."""
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic ValidationError
        AggregateResult(
            event_type="x", sufficient=True, count=None, threshold=K_ANONYMITY_THRESHOLD
        )


def test_recorded_event_model_forbids_extra_fields() -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011
        RecordedEvent(
            event_type="x",
            recorded_at="2026-01-01T00:00:00Z",
            payload={},
            user_id="nope",  # type: ignore[call-arg]
        )


# --- tracker audit -----------------------------------------------------


def test_find_disallowed_dependencies_flags_a_known_tracker(tmp_path) -> None:
    manifest = tmp_path / "package.json"
    manifest.write_text('{"dependencies": {"react": "^18.0.0", "mixpanel-browser": "^2.0.0"}}')
    findings = find_disallowed_dependencies(manifest)
    assert any("mixpanel-browser" in finding for finding in findings)


def test_find_disallowed_dependencies_is_clean_for_an_innocuous_manifest(tmp_path) -> None:
    manifest = tmp_path / "package.json"
    manifest.write_text('{"dependencies": {"react": "^18.0.0"}, "devDependencies": {}}')
    assert find_disallowed_dependencies(manifest) == []


def test_find_disallowed_dependencies_checks_dev_dependencies_too(tmp_path) -> None:
    manifest = tmp_path / "package.json"
    manifest.write_text('{"dependencies": {}, "devDependencies": {"@segment/analytics-next": "1"}}')
    findings = find_disallowed_dependencies(manifest)
    assert any("@segment/analytics-next" in finding for finding in findings)


def test_find_third_party_tracker_scripts_flags_a_known_domain(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.html").write_text(
        '<script src="https://www.google-analytics.com/analytics.js"></script>'
    )
    findings = find_third_party_tracker_scripts(tmp_path)
    assert any("google-analytics.com" in finding for finding in findings)


def test_find_third_party_tracker_scripts_is_clean_for_the_real_frontend_source() -> None:
    """Confirms the ACTUAL frontend/ in this repo carries no third-party
    tracker script — the live assertion this story's acceptance criterion
    names, not just a synthetic example."""
    from pathlib import Path

    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    assert find_third_party_tracker_scripts(frontend_dir) == []


def test_the_real_frontend_package_json_has_no_disallowed_dependency() -> None:
    from pathlib import Path

    manifest = Path(__file__).resolve().parents[2] / "frontend" / "package.json"
    assert find_disallowed_dependencies(manifest) == []
