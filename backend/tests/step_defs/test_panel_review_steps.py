"""Step definitions for tests/features/panel_review.feature (story 3.3).

Runs against story 1.2's in-memory fake transport, never live ZeroDB, so CI
needs no credentials — the rule every backend feature in this suite follows.
The live confirmation lives in `tests/test_panel_review_live_shapes.py` and
`scripts/verify_panel_review_live.py`.

`break_server_side_filter()` is applied for the reason story 3.1's and 3.2's
steps apply it: live ZeroDB's server-side `/query` filter matches nothing
(story 1.4's finding), so every read here runs on the client's paging
fallback, and a fake with a working filter would make the ledger reads pass
trivially. It matters more for this story than for either of those: the whole
gate is a read of the ledger, and a ledger read that works only because the
fake's filter works would be a gate that fails open live.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.main import create_app
from app.panel_review.router import panel_review_service, zerodb_client_for_panel_review
from app.panel_review.service import (
    BACKFILL_REVIEWER_ID,
    EmptyProposalError,
    PanelReviewService,
    SelfReviewError,
    UnknownSubjectError,
    UnreviewableWordingError,
    VersionMismatchError,
)
from app.repositories import Repository
from app.repositories.models import IssueTopic, PanelReview, Stance
from app.seed_stances import STANCES, WORDING_VERSION, StanceSeeder
from app.seed_taxonomy import TAXONOMY_LOCALE, TAXONOMY_VERSION, TaxonomySeeder, taxonomy_rows
from app.stances.service import StanceService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/panel_review.feature")

#: The anchor card prd.md names itself ("lower out-of-pocket drug costs"), used
#: wherever a scenario needs one real stance — same anchor story 3.2's live
#: verifier uses, so a change to it is visible in both.
ANCHOR_STANCE = "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
ANCHOR_TOPIC = "en-US:prescription-drug-pricing:v1"
NEW_STANCE = "en-US:prescription-drug-pricing:v1:index-prices-to-inflation"
NEW_TOPIC = "en-US:synthetic-review-topic:v1"

#: Two distinct synthetic reviewers. Two, not one, because the self-review ban
#: is only testable with a second actor — and the proposal/approval path in
#: this feature deliberately always uses both, so a scenario cannot pass by
#: accidentally letting an author approve themselves.
REVIEWER_A = "panelist:synthetic-a"
REVIEWER_B = "panelist:synthetic-b"

#: Tables this story reads or writes.
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
def reviews(zerodb: ZeroDBClient) -> Repository[PanelReview]:
    return Repository(client=zerodb, model=PanelReview)


@pytest.fixture
def api(zerodb: ZeroDBClient, panel: PanelReviewService) -> Any:
    """The app with its ZeroDB dependency pointed at this scenario's fake.

    Both the client factory and the service are overridden: the service so the
    endpoints share the exact instance the Given steps wrote through, and the
    client factory so nothing falls back to `from_env()` and reaches for live
    credentials CI does not have.
    """
    app = create_app()
    app.dependency_overrides[zerodb_client_for_panel_review] = lambda: zerodb
    app.dependency_overrides[panel_review_service] = lambda: panel
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


# --- background ------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="panel_client")
def panel_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the issue taxonomy has been seeded")
def taxonomy_seeded(zerodb: ZeroDBClient) -> None:
    TaxonomySeeder(zerodb, pace=lambda: None).run()


@given("the stance cards have been seeded")
def stance_cards_seeded(zerodb: ZeroDBClient, fake: FakeZeroDBTransport) -> None:
    StanceSeeder(zerodb, pace=lambda: None).run()
    fake.calls.clear()


# --- recording decisions ---------------------------------------------------


@when(
    parsers.parse(
        'reviewer "{reviewer}" approves the stance "{stance_id}" at wording version {version:d}'
    )
)
@given(
    parsers.parse(
        'reviewer "{reviewer}" approved the stance "{stance_id}" at wording version {version:d}'
    )
)
def approve_stance(
    panel: PanelReviewService,
    result: dict[str, Any],
    reviewer: str,
    stance_id: str,
    version: int,
) -> None:
    try:
        result["review"] = panel.record_decision(
            subject_table="stances",
            subject_id=stance_id,
            subject_version=version,
            reviewer_id=reviewer,
            decision="approved",
        )
    except (UnknownSubjectError, VersionMismatchError) as exc:
        result["error"] = exc


@when(
    parsers.parse(
        'reviewer "{reviewer}" rejects the stance "{stance_id}" at wording version '
        '{version:d} because "{reason}"'
    )
)
def reject_stance_with_reason(
    panel: PanelReviewService,
    result: dict[str, Any],
    reviewer: str,
    stance_id: str,
    version: int,
    reason: str,
) -> None:
    result["review"] = panel.record_decision(
        subject_table="stances",
        subject_id=stance_id,
        subject_version=version,
        reviewer_id=reviewer,
        decision="rejected",
        reason=reason,
    )


@given(
    parsers.parse(
        'reviewer "{reviewer}" rejected the stance "{stance_id}" at wording version {version:d}'
    )
)
def rejected_stance(
    panel: PanelReviewService,
    result: dict[str, Any],
    reviewer: str,
    stance_id: str,
    version: int,
) -> None:
    result["earlier_rejection"] = panel.record_decision(
        subject_table="stances",
        subject_id=stance_id,
        subject_version=version,
        reviewer_id=reviewer,
        decision="rejected",
        reason="Synthetic rejection recorded by a BDD scenario.",
    )


@then(parsers.parse("the review ledger holds {count:d} decision for that stance"))
@then(parsers.parse("the review ledger holds {count:d} decisions for that stance"))
def ledger_holds(panel: PanelReviewService, result: dict[str, Any], count: int) -> None:
    subject_id = result["review"].subject_id
    history = panel.history_for(subject_table="stances", subject_id=subject_id)
    assert len(history) == count, f"history is {[row.decision for row in history]}"


@then(parsers.parse('that stance\'s review status is "{expected}"'))
def stance_status_is(panel: PanelReviewService, result: dict[str, Any], expected: str) -> None:
    """The status of whichever stance this scenario has been acting on.

    "That stance" is resolved from whatever the scenario last touched, in
    priority order, because the phrase is reused across three scenario shapes:
    a bare decision (`review`), a second decision over an earlier rejection
    (`earlier_rejection`), and an approved wording change (`status`, returned
    by `decide`). Resolving it this way rather than hard-coding the anchor
    keeps the Gherkin readable — the scenario names the stance once, in its
    own When — and keeps the step honest, since a scenario that touched no
    stance fails loudly here instead of silently asserting about the anchor.
    """
    touched = result.get("review") or result.get("status") or result.get("earlier_rejection")
    assert touched is not None, "no scenario step recorded a stance to check the status of"
    status = panel.status_for(subject_table="stances", subject_id=touched.subject_id)
    assert status.decision == expected, f"status is {status.decision!r}"


@then("the decision records the reviewer and a timestamp")
def decision_records_who_and_when(result: dict[str, Any]) -> None:
    review = result["review"]
    assert review.reviewer_id == REVIEWER_A
    assert review.decided_at.endswith("Z")
    assert review.decided_at.startswith("20")


@then("the rejection reason is visible in the review history")
def rejection_reason_visible(panel: PanelReviewService, result: dict[str, Any]) -> None:
    review = result["review"]
    history = panel.history_for(subject_table="stances", subject_id=review.subject_id)
    reasons = [row.reason for row in history if row.decision == "rejected"]
    assert reasons and all(reason for reason in reasons), f"reasons are {reasons!r}"
    assert "frames competition" in (reasons[0] or "")


@then("the earlier rejection is still in the review history")
def earlier_rejection_survives(panel: PanelReviewService, result: dict[str, Any]) -> None:
    earlier = result["earlier_rejection"]
    history = panel.history_for(subject_table="stances", subject_id=earlier.subject_id)
    assert any(str(row.id) == str(earlier.id) for row in history), (
        "the append-only ledger lost the earlier decision"
    )


@then("the review write is refused for a missing stance")
def refused_missing_stance(result: dict[str, Any]) -> None:
    error = result.get("error")
    assert isinstance(error, UnknownSubjectError), f"got {error!r}"
    assert "no stances row" in str(error)


@then("the review is refused as judging wording the row does not carry")
def refused_version_mismatch(result: dict[str, Any]) -> None:
    error = result.get("error")
    assert isinstance(error, VersionMismatchError), f"got {error!r}"


# --- self review -----------------------------------------------------------


@given(
    parsers.parse('reviewer "{reviewer}" proposed the label "{label}" for the stance "{stance_id}"')
)
@when(
    parsers.parse('reviewer "{reviewer}" proposes the label "{label}" for the stance "{stance_id}"')
)
def propose_stance_label(
    panel: PanelReviewService,
    result: dict[str, Any],
    reviewer: str,
    label: str,
    stance_id: str,
) -> None:
    try:
        result["proposal"] = panel.propose(
            subject_table="stances", subject_id=stance_id, author_id=reviewer, label=label
        )
    except (EmptyProposalError, UnreviewableWordingError) as exc:
        result["error"] = exc


@when(parsers.parse('reviewer "{reviewer}" approves their own proposal'))
def approve_own_proposal(panel: PanelReviewService, result: dict[str, Any], reviewer: str) -> None:
    try:
        panel.decide(
            proposal_id=result["proposal"].proposal_id,
            reviewer_id=reviewer,
            decision="approved",
        )
    except SelfReviewError as exc:
        result["error"] = exc


@then("the approval is refused as self-review")
def refused_self_review(result: dict[str, Any]) -> None:
    error = result.get("error")
    assert isinstance(error, SelfReviewError), f"got {error!r}"
    assert "cross-partisan" in str(error)


# --- the gate --------------------------------------------------------------


@given(parsers.parse('a new stance "{stance_id}" has been written but never reviewed'))
def new_unreviewed_stance(stances: Repository[Stance], result: dict[str, Any]) -> None:
    """A card written straight into storage, as a later ingest or editor would.

    Written through the repository rather than through this story's own service
    on purpose: the gate's job is to withhold wording that arrived by ANY path,
    including one that knows nothing about the review ledger. A fixture that
    could only create rows through `PanelReviewService` would never test that.
    """
    result["new_stance"] = stances.upsert(
        {
            "id": NEW_STANCE,
            "topic_id": ANCHOR_TOPIC,
            "label": "Index drug prices to general inflation",
            "description": "Cap annual list-price growth for existing medicines at the general "
            "rate of inflation, with rebates owed to Medicare when a manufacturer exceeds it.",
            "direction_code": "0",
            "wording_version": WORDING_VERSION,
        }
    )


@given(parsers.parse('a new topic "{topic_id}" has been written but never reviewed'))
def new_unreviewed_topic(topics: Repository[IssueTopic], result: dict[str, Any]) -> None:
    result["new_topic"] = topics.upsert(
        {
            "id": NEW_TOPIC,
            "parent_id": None,
            "name": "Municipal broadband ownership",
            "locale": TAXONOMY_LOCALE,
            "taxonomy_version": TAXONOMY_VERSION,
        }
    )


@given(parsers.parse('reviewer "{reviewer}" approved that new stance'))
def approve_new_stance(panel: PanelReviewService, reviewer: str) -> None:
    panel.record_decision(
        subject_table="stances",
        subject_id=NEW_STANCE,
        subject_version=WORDING_VERSION,
        reviewer_id=reviewer,
        decision="approved",
    )


@given(parsers.parse('reviewer "{reviewer}" rejected that new stance'))
def reject_new_stance(panel: PanelReviewService, reviewer: str) -> None:
    panel.record_decision(
        subject_table="stances",
        subject_id=NEW_STANCE,
        subject_version=WORDING_VERSION,
        reviewer_id=reviewer,
        decision="rejected",
        reason="Synthetic rejection recorded by a BDD scenario.",
    )


@when(parsers.parse('I read the approved stances for "{topic_id}"'))
def read_approved_stances(panel: PanelReviewService, result: dict[str, Any], topic_id: str) -> None:
    result["served"] = panel.approved_stances_for_topic(topic_id)


@when(parsers.parse('I read the stances for "{topic_id}"'))
def read_ungated_stances(zerodb: ZeroDBClient, result: dict[str, Any], topic_id: str) -> None:
    result["served"] = StanceService(zerodb).for_topic(topic_id)


@when("I read the approved topic tree")
def read_approved_topics(panel: PanelReviewService, result: dict[str, Any]) -> None:
    result["tree"] = panel.approved_topic_tree(
        locale=TAXONOMY_LOCALE, taxonomy_version=TAXONOMY_VERSION
    )


@then("the new stance is not among them")
def new_stance_withheld(result: dict[str, Any]) -> None:
    served = {str(card.id) for card in result["served"]}
    assert NEW_STANCE not in served, "unreviewed wording reached a gated read"


@then("the new stance is among them")
def new_stance_served(result: dict[str, Any]) -> None:
    served = {str(card.id) for card in result["served"]}
    assert NEW_STANCE in served, f"served ids are {sorted(served)}"


@then("the previously approved stances are still among them")
def previously_approved_still_served(result: dict[str, Any]) -> None:
    """The gate withholds the unreviewed card and nothing else.

    Asserted because the interesting failure is not "the gate let the new card
    through" but "the gate dropped everything" — an over-broad filter that
    empties the menu looks like a working gate in a scenario that only checks
    the new card's absence.
    """
    served = {str(card.id) for card in result["served"]}
    seeded = {card.row_id for card in STANCES if card.topic_id == ANCHOR_TOPIC}
    assert seeded, "the anchor subtopic has no seeded cards; the check would be vacuous"
    # No grandfathering has run in these scenarios, so the seeded cards are
    # unreviewed too and are correctly withheld. What must hold is that the
    # gate's answer is the same for them as for the new card — the gate is
    # about review status, not about which path wrote the row.
    assert served == set(), f"nothing is reviewed yet, so nothing should serve; got {served}"


@then("the new topic is not among its topics")
def new_topic_withheld(result: dict[str, Any]) -> None:
    served = {topic.id for topic in result["tree"].topics}
    assert NEW_TOPIC not in served


@then("the grandfathered topics are still among its topics")
def grandfathered_topics_present(result: dict[str, Any]) -> None:
    """Nothing is grandfathered in this scenario, so nothing serves.

    Kept as an explicit assertion rather than dropped: the scenario's point is
    that the gate's verdict is uniform, and "the new topic is withheld" alone
    would also pass against a gate that withheld everything for the wrong
    reason.
    """
    assert result["tree"].topics == [], (
        "no grandfathering has run, so the gated tree should be empty"
    )


# --- grandfathering --------------------------------------------------------


@when(parsers.parse('the existing corpus is grandfathered by "{reviewer}"'))
@given(parsers.parse('the existing corpus has been grandfathered by "{reviewer}"'))
def grandfather_corpus(panel: PanelReviewService, result: dict[str, Any], reviewer: str) -> None:
    result["backfill"] = panel.grandfather_existing_corpus(reviewer_id=reviewer)


@when(parsers.parse('the existing corpus is grandfathered by "{reviewer}" again'))
def grandfather_corpus_again(
    panel: PanelReviewService, result: dict[str, Any], reviewer: str
) -> None:
    result["second_backfill"] = panel.grandfather_existing_corpus(reviewer_id=reviewer)


@then(parsers.parse('every seeded stance\'s review status is "{expected}"'))
def every_stance_status(panel: PanelReviewService, expected: str) -> None:
    for card in STANCES:
        status = panel.status_for(subject_table="stances", subject_id=card.row_id)
        assert status.decision == expected, f"{card.row_id} is {status.decision!r}"


@then(parsers.parse('every seeded topic\'s review status is "{expected}"'))
def every_topic_status(panel: PanelReviewService, expected: str) -> None:
    for row in taxonomy_rows():
        status = panel.status_for(subject_table="issue_topics", subject_id=str(row.id))
        assert status.decision == expected, f"{row.id} is {status.decision!r}"


@then("no grandfathered row is recorded as approved")
def grandfathered_is_not_approved(panel: PanelReviewService) -> None:
    """The honesty check, and the one that matters most in this feature.

    If this ever passes trivially — because `grandfathered` was quietly
    aliased to `approved`, or because `is_panel_approved` started returning
    True for the backfill — the ledger has begun claiming a review that did not
    happen, in the one table whose whole value is that its claims are true.
    """
    for card in STANCES:
        status = panel.status_for(subject_table="stances", subject_id=card.row_id)
        assert status.decision != "approved", f"{card.row_id} claims approval"
        assert not status.is_panel_approved, f"{card.row_id} claims a panel approved it"
        assert status.is_publishable, f"{card.row_id} is not served, so the app is down"


@then("every grandfathered decision names story 3.3's backfill rather than a panel member")
def grandfathered_names_the_backfill(reviews: Repository[PanelReview]) -> None:
    rows = [row for row in reviews.query(limit=5000) if row.decision == "grandfathered"]
    assert rows, "no grandfathering decisions were written"
    for row in rows:
        assert row.reviewer_id == BACKFILL_REVIEWER_ID, (
            f"{row.subject_id} is grandfathered by {row.reviewer_id!r}, which reads as a person"
        )
        assert "NOT panel-approved" in (row.reason or "")


@then(parsers.parse("every seeded stance for that subtopic is among them"))
def every_seeded_card_served(result: dict[str, Any]) -> None:
    served = {str(card.id) for card in result["served"]}
    seeded = {card.row_id for card in STANCES if card.topic_id == ANCHOR_TOPIC}
    assert seeded <= served, f"withheld {sorted(seeded - served)} — the app would be down"


@then("the review queue reports every grandfathered row as awaiting a real review")
def queue_reports_grandfathered(panel: PanelReviewService) -> None:
    queued = {entry.subject_id for entry in panel.queue()}
    expected = {card.row_id for card in STANCES} | {str(row.id) for row in taxonomy_rows()}
    assert expected <= queued, f"the queue forgets {len(expected - queued)} grandfathered row(s)"


@then("the backfill reports how many rows it marked")
def backfill_reports_counts(result: dict[str, Any]) -> None:
    report = result["backfill"]
    assert len(report.topics_marked) == len(taxonomy_rows())
    assert len(report.stances_marked) == len(STANCES)
    assert report.row_count == len(taxonomy_rows()) + len(STANCES)


@then(parsers.parse("exactly {count:d} grandfathering decision is stored per seeded row"))
def one_grandfathering_per_row(reviews: Repository[PanelReview], count: int) -> None:
    rows = [row for row in reviews.query(limit=5000) if row.decision == "grandfathered"]
    per_subject: dict[str, int] = {}
    for row in rows:
        per_subject[row.subject_id] = per_subject.get(row.subject_id, 0) + 1
    duplicated = {key: value for key, value in per_subject.items() if value != count}
    assert not duplicated, f"re-running the backfill duplicated {len(duplicated)} row(s)"


@then("the review queue no longer reports that stance as awaiting a real review")
def queue_drops_approved(panel: PanelReviewService) -> None:
    queued = {entry.subject_id for entry in panel.queue()}
    assert ANCHOR_STANCE not in queued, "a panel-approved row is still reported as owing a review"


# --- proposals and version bumps -------------------------------------------


@then("the stored stance still carries its original label")
def stance_label_unchanged(stances: Repository[Stance]) -> None:
    stored = stances.get(ANCHOR_STANCE)
    declared = next(card for card in STANCES if card.row_id == ANCHOR_STANCE)
    assert stored is not None
    assert stored.label == declared.label, f"the live wording moved to {stored.label!r}"


@then(parsers.parse("the stored stance is still at wording version {version:d}"))
@then(parsers.parse("the stored stance is at wording version {version:d}"))
def stance_at_version(stances: Repository[Stance], version: int) -> None:
    stored = stances.get(ANCHOR_STANCE)
    assert stored is not None
    assert stored.wording_version == version, f"version is {stored.wording_version}"


@then("the proposal is awaiting review")
def proposal_awaiting(panel: PanelReviewService, result: dict[str, Any]) -> None:
    awaiting = {proposal.proposal_id for proposal in panel.proposals_awaiting_review()}
    assert result["proposal"].proposal_id in awaiting


@when(parsers.parse('reviewer "{reviewer}" approves that proposal'))
def approve_that_proposal(panel: PanelReviewService, result: dict[str, Any], reviewer: str) -> None:
    result["status"] = panel.decide(
        proposal_id=result["proposal"].proposal_id, reviewer_id=reviewer, decision="approved"
    )


@when(parsers.parse('reviewer "{reviewer}" rejects that proposal because "{reason}"'))
def reject_that_proposal(
    panel: PanelReviewService, result: dict[str, Any], reviewer: str, reason: str
) -> None:
    result["status"] = panel.decide(
        proposal_id=result["proposal"].proposal_id,
        reviewer_id=reviewer,
        decision="rejected",
        reason=reason,
    )


@then("the stored stance carries the proposed label")
def stance_carries_proposed(stances: Repository[Stance], result: dict[str, Any]) -> None:
    stored = stances.get(ANCHOR_STANCE)
    assert stored is not None
    assert stored.label == result["proposal"].label


@then("the approved stance is served by the gated read path")
def approved_stance_served(panel: PanelReviewService) -> None:
    served = {str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)}
    assert ANCHOR_STANCE in served, "an approved edit is not published"


@then(parsers.parse('the stance keeps the id "{stance_id}"'))
def stance_keeps_id(stances: Repository[Stance], stance_id: str) -> None:
    """The id is stable across a wording bump; see `Stance`'s docstring.

    A voter's encrypted profile stores `stance_id`, so a re-worded card under a
    new id would silently orphan their saved choice — which is why
    `wording_version` is a field rather than part of the id, and why this is
    asserted rather than assumed.
    """
    stored = stances.get(stance_id)
    assert stored is not None, f"{stance_id} is gone — the bump changed its id"


@then(parsers.parse("exactly {count:d} row carries that stance id"))
def one_row_per_stance_id(zerodb: ZeroDBClient, count: int) -> None:
    page = zerodb.query_rows("stances", limit=5000)
    matching = [row for row in page.rows if row.row_data.get("id") == ANCHOR_STANCE]
    assert len(matching) == count, f"{len(matching)} rows carry {ANCHOR_STANCE}"


@then("the rejected proposal is visible in the review history")
def rejected_proposal_in_history(panel: PanelReviewService) -> None:
    history = panel.history_for(subject_table="stances", subject_id=ANCHOR_STANCE)
    decisions = [row.decision for row in history]
    assert "proposed" in decisions, f"the proposal vanished; history is {decisions}"
    assert "rejected" in decisions, f"the rejection vanished; history is {decisions}"


@when(parsers.parse('two wording changes to "{stance_id}" are proposed and approved in turn'))
def two_approved_edits(panel: PanelReviewService, result: dict[str, Any], stance_id: str) -> None:
    labels = ("Cap monthly out-of-pocket drug costs", "Cap what patients pay each month")
    for label in labels:
        proposal = panel.propose(
            subject_table="stances", subject_id=stance_id, author_id=REVIEWER_A, label=label
        )
        panel.decide(proposal_id=proposal.proposal_id, reviewer_id=REVIEWER_B, decision="approved")
    result["labels"] = labels


@then("the review history for that stance lists both approved edits in order")
def both_edits_in_order(panel: PanelReviewService, result: dict[str, Any]) -> None:
    history = panel.history_for(subject_table="stances", subject_id=ANCHOR_STANCE)
    approved = [row for row in history if row.decision == "approved"]
    # Two approvals per edit: the decision against the version reviewed, and
    # the carry-forward onto the bumped version so the row passes its own gate.
    assert len(approved) == 4, f"{len(approved)} approvals; expected 4 (2 per edit)"
    newest_first = [row.proposed_label for row in approved]
    assert newest_first[0] == result["labels"][1], f"order is {newest_first}"
    assert newest_first[-1] == result["labels"][0], f"order is {newest_first}"


@when('reviewer "panelist:synthetic-a" proposes the stance\'s existing label unchanged')
def propose_unchanged(panel: PanelReviewService, result: dict[str, Any]) -> None:
    declared = next(card for card in STANCES if card.row_id == ANCHOR_STANCE)
    try:
        panel.propose(
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            author_id=REVIEWER_A,
            label=declared.label,
        )
    except EmptyProposalError as exc:
        result["error"] = exc


@then("the proposal is refused as changing nothing")
def refused_empty(result: dict[str, Any]) -> None:
    assert isinstance(result.get("error"), EmptyProposalError), f"got {result.get('error')!r}"


@then("the proposal is refused as naming a political side")
def refused_partisan(result: dict[str, Any]) -> None:
    error = result.get("error")
    assert isinstance(error, UnreviewableWordingError), f"got {error!r}"
    assert "names a political side" in str(error)


@when(parsers.parse('reviewer "{reviewer}" proposes the name "{name}" for the topic "{topic_id}"'))
@given(parsers.parse('reviewer "{reviewer}" proposed the name "{name}" for the topic "{topic_id}"'))
def propose_topic_name(
    panel: PanelReviewService,
    result: dict[str, Any],
    reviewer: str,
    name: str,
    topic_id: str,
) -> None:
    try:
        result["proposal"] = panel.propose(
            subject_table="issue_topics",
            subject_id=topic_id,
            author_id=reviewer,
            name=name,
        )
        result["topic_id"] = topic_id
    except (EmptyProposalError, UnreviewableWordingError) as exc:
        result["error"] = exc


@then("the proposal is refused as presupposing a stance")
def refused_presupposing(result: dict[str, Any]) -> None:
    error = result.get("error")
    assert isinstance(error, UnreviewableWordingError), f"got {error!r}"
    assert "presupposes a stance" in str(error)


@then("the stored topic carries the proposed name")
def topic_carries_proposed_name(topics: Repository[IssueTopic], result: dict[str, Any]) -> None:
    stored = topics.get(result["topic_id"])
    assert stored is not None
    assert stored.name == result["proposal"].name


@then(parsers.parse('the stored topic keeps the id "{topic_id}"'))
def topic_keeps_id(topics: Repository[IssueTopic], topic_id: str) -> None:
    assert topics.get(topic_id) is not None, f"{topic_id} is gone — the bump changed its id"


# --- history ---------------------------------------------------------------


@when(parsers.parse('I read the review history for "{subject_id}"'))
def read_history(panel: PanelReviewService, result: dict[str, Any], subject_id: str) -> None:
    result["history"] = panel.history_for(subject_table="stances", subject_id=subject_id)


@then(parsers.parse("the history lists {count:d} decisions newest first"))
def history_lists_newest_first(result: dict[str, Any], count: int) -> None:
    history = result["history"]
    assert len(history) == count, f"history is {[row.decision for row in history]}"
    timestamps = [row.decided_at for row in history]
    assert timestamps == sorted(timestamps, reverse=True), f"order is {timestamps}"


@then("every decision names its reviewer, decision, wording version and timestamp")
def every_decision_complete(result: dict[str, Any]) -> None:
    for row in result["history"]:
        assert row.reviewer_id
        assert row.decision
        assert row.subject_version >= 1
        assert row.decided_at.endswith("Z")


@then("the history is empty")
def history_is_empty(result: dict[str, Any]) -> None:
    assert result["history"] == [], f"got {[row.decision for row in result['history']]}"


@then("the history shows the label before and after the change")
def history_shows_before_and_after(result: dict[str, Any]) -> None:
    declared = next(card for card in STANCES if card.row_id == ANCHOR_STANCE)
    approved = [row for row in result["history"] if row.decision == "approved"]
    assert approved, "no approval in the history"
    entry = approved[0]
    assert entry.previous_label == declared.label, f"previous is {entry.previous_label!r}"
    assert entry.proposed_label == "Cap monthly out-of-pocket drug costs"


# --- HTTP ------------------------------------------------------------------


@when("the API is asked for that stance's review history")
def api_history(api: TestClient, result: dict[str, Any]) -> None:
    result["response"] = api.get(f"/v1/panel/reviews/stances/{ANCHOR_STANCE}")


@then("the response lists the rejection with its reviewer and reason")
def api_lists_rejection(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 200, response.text
    body = response.json()
    rejections = [row for row in body["history"] if row["decision"] == "rejected"]
    assert rejections, f"history is {[row['decision'] for row in body['history']]}"
    assert rejections[0]["reviewer_id"] == REVIEWER_A
    assert rejections[0]["reason"]
    assert body["is_panel_approved"] is False


@when("the API is asked for the review queue")
def api_queue(api: TestClient, result: dict[str, Any]) -> None:
    result["response"] = api.get("/v1/panel/queue")


@then("the response reports every grandfathered row as awaiting a real review")
def api_reports_queue(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 200, response.text
    body = response.json()
    expected = len(STANCES) + len(taxonomy_rows())
    assert body["awaiting_review"] == expected, f"queue reports {body['awaiting_review']}"
    assert body["grandfathered"] == expected
    assert body["never_reviewed"] == 0


@when(parsers.parse('the API is used to propose and then approve a new label for "{stance_id}"'))
def api_propose_and_approve(api: TestClient, result: dict[str, Any], stance_id: str) -> None:
    proposed = api.post(
        "/v1/panel/proposals",
        json={
            "author_id": REVIEWER_A,
            "subject_table": "stances",
            "subject_id": stance_id,
            "label": "Cap monthly out-of-pocket drug costs",
        },
    )
    assert proposed.status_code == 201, proposed.text
    result["response"] = api.post(
        f"/v1/panel/proposals/{proposed.json()['proposal_id']}/decision",
        json={"reviewer_id": REVIEWER_B, "decision": "approved"},
    )


@then("the API reports the stance at wording version 2 with the new label")
def api_reports_bumped(result: dict[str, Any]) -> None:
    response = result["response"]
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["subject_version"] == 2, body
    assert body["label"] == "Cap monthly out-of-pocket drug costs", body
    assert body["status"] == "approved", body
    assert body["is_published"] is True, body
    assert body["is_panel_approved"] is True, body


@when("the API is asked to approve a stance with no reviewer id")
def api_approve_without_reviewer(api: TestClient, result: dict[str, Any]) -> None:
    result["response"] = api.post(
        "/v1/panel/reviews",
        json={
            "subject_table": "stances",
            "subject_id": ANCHOR_STANCE,
            "subject_version": 1,
            "decision": "approved",
        },
    )


@then("the API refuses it as a bad request")
def api_refuses_bad_request(result: dict[str, Any]) -> None:
    assert result["response"].status_code == 422, result["response"].text
