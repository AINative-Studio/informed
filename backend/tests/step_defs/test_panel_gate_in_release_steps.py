"""Step definitions for tests/features/panel_gate_in_release.feature (story 12.5).

Runs against story 1.2's in-memory fake transport, never live ZeroDB — the
same rule every backend feature in this suite follows, and the thing that
makes this gate's LOGIC genuinely provable in CI today with zero credentials.
The live confirmation (does the real "informed" project's current corpus
pass, and does it correctly report its grandfathered debt) lives in
`scripts/check_panel_gate.py --live` and is run manually against
`local.env`-sourced credentials; see that script's module docstring.

`break_server_side_filter()` is applied for the same reason
`test_panel_review_steps.py` applies it: live ZeroDB's server-side `/query`
filter matches nothing (story 1.4's finding), so every ledger read here runs
on the client's paging fallback rather than on a fake filter that would make
the gate's logic look sound for a reason that has nothing to do with the real
API.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.panel_review.gate import GateReport, check_panel_gate
from app.panel_review.service import PanelReviewService
from app.repositories import Repository
from app.repositories.models import IssueTopic, Stance
from app.seed_stances import WORDING_VERSION, StanceSeeder
from app.seed_taxonomy import TAXONOMY_LOCALE, TAXONOMY_VERSION, TaxonomySeeder
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/panel_gate_in_release.feature")

#: The anchor subtopic every synthetic sabotage row hangs off, matching story
#: 3.3's own anchor so both suites hang their fixtures off the same real row.
ANCHOR_TOPIC = "en-US:prescription-drug-pricing:v1"

REVIEWER_A = "panelist:synthetic-a"
REVIEWER_B = "panelist:synthetic-b"

TABLES = ("issue_topics", "stances", "panel_reviews")


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    for table in TABLES:
        client.create_table(table, {"id": "string"})
    return client


@pytest.fixture
def panel(zerodb: ZeroDBClient) -> PanelReviewService:
    return PanelReviewService(zerodb)


@pytest.fixture
def topics(zerodb: ZeroDBClient) -> Repository[IssueTopic]:
    return Repository(client=zerodb, model=IssueTopic)


@pytest.fixture
def stances(zerodb: ZeroDBClient) -> Repository[Stance]:
    return Repository(client=zerodb, model=Stance)


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


# --- background -------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="gate_client")
def gate_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the issue taxonomy has been seeded")
def taxonomy_seeded(zerodb: ZeroDBClient) -> None:
    TaxonomySeeder(zerodb, pace=lambda: None).run()


@given("the stance cards have been seeded")
def stance_cards_seeded(zerodb: ZeroDBClient, fake: FakeZeroDBTransport) -> None:
    StanceSeeder(zerodb, pace=lambda: None).run()
    fake.calls.clear()


# --- grandfathering and sabotage fixtures -----------------------------------


@given("the existing corpus has been grandfathered")
def grandfather_corpus(panel: PanelReviewService) -> None:
    panel.grandfather_existing_corpus()


@given(parsers.parse('a new stance "{stance_id}" has been written but never reviewed'))
def new_unreviewed_stance(stances: Repository[Stance], stance_id: str) -> None:
    """A card written straight into storage, exactly as a later ingest, a
    seeder re-run, or a direct edit would — never through
    `PanelReviewService`. The gate's whole job is to catch wording that
    arrived by ANY path with no review attached, so the fixture that proves
    it must not route through the one service that would make it reviewable
    by construction.
    """
    stances.upsert(
        {
            "id": stance_id,
            "topic_id": ANCHOR_TOPIC,
            "label": "Panel gate sabotage card: never reviewed by any panel",
            "description": "A synthetic stance card written directly to storage, bypassing "
            "PanelReviewService entirely, to prove the release gate catches wording that "
            "reaches the database with no panel decision attached at all.",
            "direction_code": "0",
            "wording_version": WORDING_VERSION,
        }
    )


@given(parsers.parse('a new issue topic "{topic_id}" has been written but never reviewed'))
def new_unreviewed_topic(topics: Repository[IssueTopic], topic_id: str) -> None:
    topics.upsert(
        {
            "id": topic_id,
            "parent_id": None,
            "name": "Panel gate sabotage topic: never reviewed by any panel",
            "locale": TAXONOMY_LOCALE,
            "taxonomy_version": TAXONOMY_VERSION,
        }
    )


@given(
    parsers.parse(
        'reviewer "{reviewer}" rejected the stance "{stance_id}" at wording version '
        '{version:d} because "{reason}"'
    )
)
def reviewer_rejected_stance(
    panel: PanelReviewService, reviewer: str, stance_id: str, version: int, reason: str
) -> None:
    panel.record_decision(
        subject_table="stances",
        subject_id=stance_id,
        subject_version=version,
        reviewer_id=reviewer,
        decision="rejected",
        reason=reason,
    )


@given(
    parsers.parse('reviewer "{reviewer}" proposed the label "{label}" for the stance "{stance_id}"')
)
def reviewer_proposed_label(
    panel: PanelReviewService, result: dict[str, Any], reviewer: str, label: str, stance_id: str
) -> None:
    result["proposal"] = panel.propose(
        subject_table="stances", subject_id=stance_id, author_id=reviewer, label=label
    )


@given(parsers.parse('reviewer "{reviewer}" approves that proposal'))
def reviewer_approves_proposal(
    panel: PanelReviewService, result: dict[str, Any], reviewer: str
) -> None:
    panel.decide(
        proposal_id=result["proposal"].proposal_id, reviewer_id=reviewer, decision="approved"
    )


@given(
    parsers.parse(
        'a brand new stance "{stance_id}" exists and the ledger is otherwise empty for it'
    )
)
def brand_new_stance_empty_ledger(stances: Repository[Stance], stance_id: str) -> None:
    """A row written with NOTHING else touched — no seeding, no backfill, no
    other ledger entry. Proves the gate fails closed even when the ledger
    has never been populated at all, not only when a populated ledger is
    missing one row.
    """
    stances.upsert(
        {
            "id": stance_id,
            "topic_id": ANCHOR_TOPIC,
            "label": "Panel gate sabotage card: empty ledger",
            "description": "A synthetic stance card proving the gate fails closed even when "
            "panel_reviews has never been written to at all.",
            "direction_code": "0",
            "wording_version": WORDING_VERSION,
        }
    )


# --- running the gate --------------------------------------------------------


@when("I run the panel gate check", target_fixture="gate_report")
def run_panel_gate(zerodb: ZeroDBClient) -> GateReport:
    return check_panel_gate(zerodb)


# --- assertions ---------------------------------------------------------------


@then("the panel gate check passes")
def gate_passes(gate_report: GateReport) -> None:
    assert gate_report.ok, f"expected the gate to pass; violations: {gate_report.violations}"


@then("the panel gate check fails")
def gate_fails(gate_report: GateReport) -> None:
    assert not gate_report.ok, "expected the gate to fail, but it reported no violations"


@then("the panel gate report counts every topic and stance as grandfathered")
def gate_reports_grandfathered(
    gate_report: GateReport, topics: Repository[IssueTopic], stances: Repository[Stance]
) -> None:
    expected = len(topics.query(limit=5000)) + len(stances.query(limit=5000))
    assert gate_report.grandfathered == expected, (
        f"expected {expected} grandfathered row(s), the report counted {gate_report.grandfathered}"
    )


@then(parsers.parse('the panel gate report flags "{subject_id}" as never reviewed'))
def gate_flags_never_reviewed(gate_report: GateReport, subject_id: str) -> None:
    matching = [v for v in gate_report.violations if v.row_id == subject_id]
    assert matching, f"no violation was reported for {subject_id!r}; got {gate_report.violations}"
    assert "NO panel review of any kind" in matching[0].detail, matching[0].detail


@then(parsers.parse('the panel gate report flags "{subject_id}" as rejected'))
def gate_flags_rejected(gate_report: GateReport, subject_id: str) -> None:
    matching = [v for v in gate_report.violations if v.row_id == subject_id]
    assert matching, f"no violation was reported for {subject_id!r}; got {gate_report.violations}"
    assert "REJECTED" in matching[0].detail, matching[0].detail
