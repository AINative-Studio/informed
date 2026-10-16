"""Unit tests for the panel review workflow (story 3.3).

The feature file covers the acceptance criteria end to end. These cover the
edges underneath them — the ones where a quiet wrong answer is worse than a
loud failure, and which a Gherkin scenario would be a clumsy way to express:

  * the content-addressed id's properties (idempotent, collision-resistant,
    field-order-independent), since the whole ledger's append-only-ness and
    the backfill's re-runnability rest on them;
  * the `_in_force` version and tie-breaking rules, which are the only thing
    standing between a bumped row and a stale approval publishing it;
  * the `grandfathered` / `approved` distinction, asserted from several angles
    because collapsing them is the one failure that would make this story's
    ledger dishonest rather than merely wrong;
  * the model's own refusals, which stop a malformed decision reaching storage.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.bootstrap import INFRASTRUCTURE_TABLES, TABLE_SCHEMAS
from app.panel_review.service import (
    BACKFILL_REVIEWER_ID,
    PUBLISHABLE_DECISIONS,
    EmptyProposalError,
    PanelReviewService,
    ReviewStatus,
    SelfReviewError,
    UnknownProposalError,
    UnknownSubjectError,
    UnreviewableWordingError,
    VersionMismatchError,
    review_id,
)
from app.repositories import Repository
from app.repositories.models import IssueTopic, PanelReview, Stance
from app.seed_stances import STANCES, WORDING_VERSION, StanceSeeder
from app.seed_taxonomy import TAXONOMY_LOCALE, TAXONOMY_VERSION, TaxonomySeeder
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

ANCHOR_STANCE = "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
ANCHOR_TOPIC = "en-US:prescription-drug-pricing:v1"
REVIEWER_A = "panelist:synthetic-a"
REVIEWER_B = "panelist:synthetic-b"


@pytest.fixture
def client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    zerodb = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    for table in ("issue_topics", "stances", "panel_reviews"):
        zerodb.create_table(table, {"id": "string"})
    TaxonomySeeder(zerodb, pace=lambda: None).run()
    StanceSeeder(zerodb, pace=lambda: None).run()
    return zerodb


@pytest.fixture
def panel(client: ZeroDBClient) -> PanelReviewService:
    return PanelReviewService(client)


# --- the content-addressed id ---------------------------------------------


def test_the_same_decision_hashes_to_the_same_id() -> None:
    """Idempotency's whole mechanism. See `review_id`.

    If this ever stops holding, the grandfathering backfill appends 281 fresh
    rows per run instead of landing on the ones already stored.
    """
    kwargs = {
        "subject_table": "stances",
        "subject_id": ANCHOR_STANCE,
        "subject_version": 1,
        "reviewer_id": REVIEWER_A,
        "decision": "approved",
        "reason": None,
    }
    assert review_id(**kwargs) == review_id(**kwargs)  # type: ignore[arg-type]


def test_a_different_decision_about_the_same_row_hashes_differently() -> None:
    """Two real decisions must both persist, which needs distinct ids."""
    base = {
        "subject_table": "stances",
        "subject_id": ANCHOR_STANCE,
        "subject_version": 1,
        "reason": None,
    }
    approved = review_id(reviewer_id=REVIEWER_A, decision="approved", **base)  # type: ignore[arg-type]
    rejected = review_id(reviewer_id=REVIEWER_A, decision="rejected", **base)  # type: ignore[arg-type]
    other_reviewer = review_id(reviewer_id=REVIEWER_B, decision="approved", **base)  # type: ignore[arg-type]
    later_version = review_id(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=2,
        reviewer_id=REVIEWER_A,
        decision="approved",
        reason=None,
    )
    assert len({approved, rejected, other_reviewer, later_version}) == 4


def test_the_id_does_not_depend_on_field_order() -> None:
    """`json.dumps(sort_keys=True)`'s purpose, pinned.

    A hash that moved when a field was added in a different position would
    make every previously stored decision unfindable by re-derivation — and
    the backfill would then duplicate the entire corpus exactly once, which is
    the kind of bug that looks like a one-off glitch.
    """
    first = review_id(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=1,
        reviewer_id=REVIEWER_A,
        decision="approved",
        reason=None,
        proposed={"proposed_label": "A", "previous_label": "B"},
    )
    second = review_id(
        reason=None,
        decision="approved",
        reviewer_id=REVIEWER_A,
        subject_version=1,
        subject_id=ANCHOR_STANCE,
        subject_table="stances",
        proposed={"previous_label": "B", "proposed_label": "A"},
    )
    assert first == second


def test_concatenation_ambiguity_cannot_collide_two_ids() -> None:
    """("a", "bc") and ("ab", "c") must not hash alike.

    The reason the material is JSON rather than an f-string join. A stance id
    and a reviewer id are both free-form strings, so a naive separator is a
    real collision surface rather than a theoretical one.
    """
    first = review_id(
        subject_table="stances",
        subject_id="a",
        subject_version=1,
        reviewer_id="bc",
        decision="approved",
        reason=None,
    )
    second = review_id(
        subject_table="stances",
        subject_id="ab",
        subject_version=1,
        reviewer_id="c",
        decision="approved",
        reason=None,
    )
    assert first != second


def test_every_id_carries_the_documented_prefix() -> None:
    row_id = review_id(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=1,
        reviewer_id=REVIEWER_A,
        decision="approved",
        reason=None,
    )
    assert row_id.startswith("pr:")
    assert len(row_id) == len("pr:") + 64


# --- the gate's version rule ----------------------------------------------


def test_an_approval_of_version_1_does_not_publish_version_2(
    panel: PanelReviewService, client: ZeroDBClient
) -> None:
    """The single mechanism behind "every edit bumps wording_version".

    Without this, a reviewer's approval of version 1 would carry forward onto
    every later rewrite — which is precisely the "unapproved wording was
    published" failure story 3.3 exists to prevent, arriving by the back door.
    """
    panel.record_decision(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=1,
        reviewer_id=REVIEWER_A,
        decision="approved",
    )
    assert ANCHOR_STANCE in {
        str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)
    }

    # Bump the row's version behind the service's back, as a rogue write or a
    # future ingest path could.
    stances: Repository[Stance] = Repository(client=client, model=Stance)
    stored = stances.get(ANCHOR_STANCE)
    assert stored is not None
    stances.upsert(stored.model_copy(update={"wording_version": 2}).row_data())

    assert ANCHOR_STANCE not in {
        str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)
    }
    assert panel.status_for(subject_table="stances", subject_id=ANCHOR_STANCE).decision is None


def test_a_decision_naming_the_wrong_version_is_refused(panel: PanelReviewService) -> None:
    with pytest.raises(VersionMismatchError):
        panel.record_decision(
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            subject_version=99,
            reviewer_id=REVIEWER_A,
            decision="approved",
        )


def test_a_decision_about_a_row_that_does_not_exist_is_refused(
    panel: PanelReviewService,
) -> None:
    with pytest.raises(UnknownSubjectError):
        panel.record_decision(
            subject_table="stances",
            subject_id="en-US:prescription-drug-pricing:v1:not-a-real-card",
            subject_version=1,
            reviewer_id=REVIEWER_A,
            decision="approved",
        )


def test_a_pending_proposal_does_not_unpublish_the_live_wording(
    panel: PanelReviewService,
) -> None:
    """`_NON_STATUS_DECISIONS`' purpose.

    An author proposing a better sentence must not take the current card off
    the menu while the proposal waits — that would make proposing an edit a
    way to censor live wording with no review at all.
    """
    panel.grandfather_existing_corpus()
    panel.propose(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        author_id=REVIEWER_A,
        label="Cap monthly out-of-pocket drug costs",
    )
    assert ANCHOR_STANCE in {
        str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)
    }
    assert (
        panel.status_for(subject_table="stances", subject_id=ANCHOR_STANCE).decision
        == "grandfathered"
    )


def test_the_newest_decision_about_one_version_wins(panel: PanelReviewService) -> None:
    """Newest-wins, so "B approves what A rejected" has a defined outcome.

    Asserted rather than left to ZeroDB's row order, which datamodel.md
    records as explicitly unspecified — a tie resolved by read order would
    make a row publishable on one request and not the next.
    """
    panel.record_decision(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=1,
        reviewer_id=REVIEWER_A,
        decision="rejected",
        reason="Synthetic rejection.",
    )
    panel.record_decision(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=1,
        reviewer_id=REVIEWER_B,
        decision="approved",
    )
    status = panel.status_for(subject_table="stances", subject_id=ANCHOR_STANCE)
    assert status.decision == "approved"
    assert status.reviewer_id == REVIEWER_B
    # And the rejection is still readable — nothing was overwritten.
    assert len(panel.history_for(subject_table="stances", subject_id=ANCHOR_STANCE)) == 2


def test_the_gate_withholds_everything_on_an_empty_ledger(panel: PanelReviewService) -> None:
    """Fails closed. The default must be "not published", not "published"."""
    assert panel.approved_stances_for_topic(ANCHOR_TOPIC) == []
    tree = panel.approved_topic_tree(locale=TAXONOMY_LOCALE, taxonomy_version=TAXONOMY_VERSION)
    assert tree.topics == []


# --- grandfathered is not approval ----------------------------------------


def test_grandfathered_is_publishable_but_never_panel_approved(
    panel: PanelReviewService,
) -> None:
    """The honesty invariant, from the status object's own two properties.

    These two booleans disagreeing is the entire grandfathering design: served
    AND not approved. If they ever agree for a backfilled row, the ledger has
    begun claiming a review that did not happen.
    """
    panel.grandfather_existing_corpus()
    status = panel.status_for(subject_table="stances", subject_id=ANCHOR_STANCE)
    assert status.decision == "grandfathered"
    assert status.is_publishable
    assert not status.is_panel_approved


def test_only_approved_and_grandfathered_wording_is_publishable() -> None:
    """Pinned as membership rather than set equality, so a new decision value
    added to `ReviewDecision` later is publishable only if someone writes it
    into this test as well — which is the moment to think about whether it
    should be.
    """
    assert "approved" in PUBLISHABLE_DECISIONS
    assert "grandfathered" in PUBLISHABLE_DECISIONS
    for never_published in ("rejected", "proposed", "withdrawn"):
        assert never_published not in PUBLISHABLE_DECISIONS
    assert len(PUBLISHABLE_DECISIONS) == 2


def test_a_rejected_row_is_not_publishable() -> None:
    status = ReviewStatus(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=1,
        decision="rejected",
        reviewer_id=REVIEWER_A,
        reason="Synthetic.",
    )
    assert not status.is_publishable
    assert not status.is_panel_approved


def test_an_approval_by_the_backfill_id_does_not_count_as_a_panel_approval() -> None:
    """Defence in depth on the honesty rule.

    `grandfather_existing_corpus` writes `grandfathered`, not `approved`, so
    this state should be unreachable. It is asserted anyway: if some later code
    path ever writes `approved` under the backfill's id — a migration, a
    convenience helper, a copy-paste — `is_panel_approved` must still refuse to
    call it a panel approval, because no panel member is named.
    """
    status = ReviewStatus(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=1,
        decision="approved",
        reviewer_id=BACKFILL_REVIEWER_ID,
    )
    assert status.is_publishable
    assert not status.is_panel_approved


def test_the_backfill_reviewer_id_does_not_read_as_a_person() -> None:
    """Anyone reading the ledger must tell a migration from a panel member."""
    assert BACKFILL_REVIEWER_ID == "informed:story-3.3-backfill"
    assert "3.3" in BACKFILL_REVIEWER_ID


def test_re_running_the_backfill_writes_no_second_decision(
    panel: PanelReviewService, client: ZeroDBClient
) -> None:
    first = panel.grandfather_existing_corpus()
    second = panel.grandfather_existing_corpus()
    assert first.row_count == len(STANCES) + 80
    assert second.row_count == 0
    assert len(second.already_marked) == first.row_count

    page = client.query_rows("panel_reviews", limit=5000)
    assert len(page.rows) == first.row_count


def test_the_backfill_does_not_downgrade_a_real_approval(panel: PanelReviewService) -> None:
    """Running the backfill after a panel pass must not un-approve anything."""
    panel.record_decision(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=1,
        reviewer_id=REVIEWER_A,
        decision="approved",
    )
    panel.grandfather_existing_corpus()
    status = panel.status_for(subject_table="stances", subject_id=ANCHOR_STANCE)
    assert status.decision == "approved"
    assert status.is_panel_approved


def test_the_queue_reports_the_whole_grandfathered_corpus(panel: PanelReviewService) -> None:
    panel.grandfather_existing_corpus()
    entries = panel.queue()
    assert len(entries) == len(STANCES) + 80
    assert all(entry.current_decision == "grandfathered" for entry in entries)


def test_a_real_approval_removes_one_row_from_the_queue(panel: PanelReviewService) -> None:
    panel.grandfather_existing_corpus()
    before = len(panel.queue())
    panel.record_decision(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=1,
        reviewer_id=REVIEWER_A,
        decision="approved",
    )
    after = {entry.subject_id for entry in panel.queue()}
    assert len(after) == before - 1
    assert ANCHOR_STANCE not in after


def test_the_queue_is_stably_ordered(panel: PanelReviewService) -> None:
    """ZeroDB's row order is unspecified; a reshuffling queue is unusable."""
    panel.grandfather_existing_corpus()
    first = [(entry.subject_table, entry.subject_id) for entry in panel.queue()]
    second = [(entry.subject_table, entry.subject_id) for entry in panel.queue()]
    assert first == second == sorted(first)


# --- proposing and approving an edit --------------------------------------


def test_approving_a_proposal_bumps_the_version_and_keeps_the_id(
    panel: PanelReviewService, client: ZeroDBClient
) -> None:
    panel.grandfather_existing_corpus()
    proposal = panel.propose(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        author_id=REVIEWER_A,
        label="Cap monthly out-of-pocket drug costs",
    )
    panel.decide(proposal_id=proposal.proposal_id, reviewer_id=REVIEWER_B, decision="approved")

    stances: Repository[Stance] = Repository(client=client, model=Stance)
    stored = stances.get(ANCHOR_STANCE)
    assert stored is not None
    assert stored.wording_version == 2
    assert stored.label == "Cap monthly out-of-pocket drug costs"
    # The description was not part of the proposal and must be untouched.
    declared = next(card for card in STANCES if card.row_id == ANCHOR_STANCE)
    assert stored.description == declared.description


def test_an_approved_edit_is_published_at_its_new_version(panel: PanelReviewService) -> None:
    """Step 3 of `decide`, which is easy to mistake for ceremony.

    Without carrying the approval onto the bumped version, an approved edit
    would bump the row past its own approval and immediately vanish from every
    gated read — the gate blocking the exact wording a reviewer just passed.
    """
    panel.grandfather_existing_corpus()
    proposal = panel.propose(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        author_id=REVIEWER_A,
        label="Cap monthly out-of-pocket drug costs",
    )
    panel.decide(proposal_id=proposal.proposal_id, reviewer_id=REVIEWER_B, decision="approved")
    served = {str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)}
    assert ANCHOR_STANCE in served
    status = panel.status_for(subject_table="stances", subject_id=ANCHOR_STANCE)
    assert status.subject_version == 2
    assert status.is_panel_approved


def test_rejecting_a_proposal_changes_neither_wording_nor_version(
    panel: PanelReviewService, client: ZeroDBClient
) -> None:
    panel.grandfather_existing_corpus()
    proposal = panel.propose(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        author_id=REVIEWER_A,
        label="Cap monthly out-of-pocket drug costs",
    )
    panel.decide(
        proposal_id=proposal.proposal_id,
        reviewer_id=REVIEWER_B,
        decision="rejected",
        reason="Loses the insulin example voters recognize.",
    )
    stances: Repository[Stance] = Repository(client=client, model=Stance)
    stored = stances.get(ANCHOR_STANCE)
    declared = next(card for card in STANCES if card.row_id == ANCHOR_STANCE)
    assert stored is not None
    assert stored.label == declared.label
    assert stored.wording_version == 1


def test_an_author_cannot_approve_their_own_proposal(panel: PanelReviewService) -> None:
    proposal = panel.propose(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        author_id=REVIEWER_A,
        label="Cap monthly out-of-pocket drug costs",
    )
    with pytest.raises(SelfReviewError):
        panel.decide(proposal_id=proposal.proposal_id, reviewer_id=REVIEWER_A, decision="approved")


def test_an_author_cannot_reject_their_own_proposal_either(panel: PanelReviewService) -> None:
    """The ban is on self-review, not on self-approval specifically.

    Letting an author "reject" their own proposal would be harmless in effect
    but would put their name in the ledger as the reviewer of wording nobody
    independent read — and `withdraw` is the honest verb for pulling one's own
    proposal, which is why it exists separately.
    """
    proposal = panel.propose(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        author_id=REVIEWER_A,
        label="Cap monthly out-of-pocket drug costs",
    )
    with pytest.raises(SelfReviewError):
        panel.decide(
            proposal_id=proposal.proposal_id,
            reviewer_id=REVIEWER_A,
            decision="rejected",
            reason="Synthetic.",
        )


def test_an_author_may_withdraw_their_own_proposal(panel: PanelReviewService) -> None:
    proposal = panel.propose(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        author_id=REVIEWER_A,
        label="Cap monthly out-of-pocket drug costs",
    )
    panel.withdraw(proposal_id=proposal.proposal_id, author_id=REVIEWER_A)
    assert proposal.proposal_id not in {
        pending.proposal_id for pending in panel.proposals_awaiting_review()
    }
    # And the withdrawal is in the record rather than deleted.
    decisions = [
        row.decision for row in panel.history_for(subject_table="stances", subject_id=ANCHOR_STANCE)
    ]
    assert "withdrawn" in decisions
    assert "proposed" in decisions


def test_someone_else_cannot_withdraw_a_proposal(panel: PanelReviewService) -> None:
    proposal = panel.propose(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        author_id=REVIEWER_A,
        label="Cap monthly out-of-pocket drug costs",
    )
    with pytest.raises(SelfReviewError):
        panel.withdraw(proposal_id=proposal.proposal_id, author_id=REVIEWER_B)


def test_deciding_on_a_proposal_that_does_not_exist_is_refused(
    panel: PanelReviewService,
) -> None:
    with pytest.raises(UnknownProposalError):
        panel.decide(proposal_id="pr:" + "0" * 64, reviewer_id=REVIEWER_B, decision="approved")


def test_a_decision_row_is_not_mistakable_for_a_proposal(panel: PanelReviewService) -> None:
    """`decide` must not accept a plain approval's id as a proposal id."""
    approval = panel.record_decision(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=1,
        reviewer_id=REVIEWER_A,
        decision="approved",
    )
    with pytest.raises(UnknownProposalError):
        panel.decide(proposal_id=str(approval.id), reviewer_id=REVIEWER_B, decision="approved")


def test_a_proposal_against_a_stale_version_is_refused_at_decision_time(
    panel: PanelReviewService, client: ZeroDBClient
) -> None:
    """Two reviewers approving two proposals of the same card, in sequence.

    The second proposal was written against wording the first one replaced, so
    approving it would apply a diff to a base that no longer exists — the
    classic lost-update. Refused with a conflict rather than silently applied.
    """
    panel.grandfather_existing_corpus()
    first = panel.propose(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        author_id=REVIEWER_A,
        label="Cap monthly out-of-pocket drug costs",
    )
    second = panel.propose(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        author_id=REVIEWER_A,
        label="Cap what patients pay each month",
    )
    panel.decide(proposal_id=first.proposal_id, reviewer_id=REVIEWER_B, decision="approved")
    with pytest.raises(VersionMismatchError):
        panel.decide(proposal_id=second.proposal_id, reviewer_id=REVIEWER_B, decision="approved")

    stances: Repository[Stance] = Repository(client=client, model=Stance)
    stored = stances.get(ANCHOR_STANCE)
    assert stored is not None
    assert stored.label == "Cap monthly out-of-pocket drug costs"
    assert stored.wording_version == 2


# --- proposals are validated before a reviewer's time is spent ------------


def test_a_proposal_that_changes_nothing_is_refused(panel: PanelReviewService) -> None:
    declared = next(card for card in STANCES if card.row_id == ANCHOR_STANCE)
    with pytest.raises(EmptyProposalError):
        panel.propose(
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            author_id=REVIEWER_A,
            label=declared.label,
        )


def test_an_empty_proposal_is_refused(panel: PanelReviewService) -> None:
    with pytest.raises(EmptyProposalError):
        panel.propose(subject_table="stances", subject_id=ANCHOR_STANCE, author_id=REVIEWER_A)


def test_a_blank_label_is_refused(panel: PanelReviewService) -> None:
    with pytest.raises(EmptyProposalError):
        panel.propose(
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            author_id=REVIEWER_A,
            label="   ",
        )


@pytest.mark.parametrize(
    "label",
    [
        "End the Democrats' price controls",
        "Stop the Republican giveaway to drug makers",
        "Reject this radical agenda",
    ],
)
def test_a_stance_label_naming_a_side_is_refused(panel: PanelReviewService, label: str) -> None:
    """Story 3.2's guard, reached through this story's proposal path.

    Both directions' badges, so the check cannot itself be partisan — the same
    reason `PARTISAN_IDENTITY_WORDS` lists both halves of every pair.
    """
    with pytest.raises(UnreviewableWordingError):
        panel.propose(
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            author_id=REVIEWER_A,
            label=label,
        )


@pytest.mark.parametrize(
    "name",
    ["Commonsense gun safety reform", "Gun rights protection", "Immigration reform"],
)
def test_a_topic_name_presupposing_a_stance_is_refused(
    panel: PanelReviewService, name: str
) -> None:
    """Story 3.1's guard, reached through this story's proposal path."""
    with pytest.raises(UnreviewableWordingError):
        panel.propose(
            subject_table="issue_topics",
            subject_id="en-US:firearms-policy:v1",
            author_id=REVIEWER_A,
            name=name,
        )


def test_a_neutral_topic_rename_is_accepted(panel: PanelReviewService) -> None:
    """The guards must not reject everything; a real rename has to get through."""
    proposal = panel.propose(
        subject_table="issue_topics",
        subject_id=ANCHOR_TOPIC,
        author_id=REVIEWER_A,
        name="Prescription drug prices",
    )
    assert proposal.name == "Prescription drug prices"
    assert proposal.previous_name == "Prescription drug pricing"


def test_a_stance_proposal_carrying_a_topic_name_is_refused(
    panel: PanelReviewService,
) -> None:
    with pytest.raises(UnreviewableWordingError):
        panel.propose(
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            author_id=REVIEWER_A,
            name="Not a stance field",
        )


def test_a_topic_proposal_carrying_stance_wording_is_refused(
    panel: PanelReviewService,
) -> None:
    with pytest.raises(UnreviewableWordingError):
        panel.propose(
            subject_table="issue_topics",
            subject_id=ANCHOR_TOPIC,
            author_id=REVIEWER_A,
            label="Not a topic field",
        )


# --- the stored row's own rules -------------------------------------------


def test_a_rejection_must_carry_a_reason() -> None:
    with pytest.raises(ValidationError):
        PanelReview(
            id="pr:" + "0" * 64,
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            subject_version=1,
            decision="rejected",
            reviewer_id=REVIEWER_A,
            decided_at="2026-10-16T12:00:00.000000Z",
        )


def test_a_whitespace_only_reason_does_not_satisfy_the_rejection_rule() -> None:
    with pytest.raises(ValidationError):
        PanelReview(
            id="pr:" + "0" * 64,
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            subject_version=1,
            decision="rejected",
            reviewer_id=REVIEWER_A,
            decided_at="2026-10-16T12:00:00.000000Z",
            reason="   ",
        )


def test_an_approval_needs_no_reason() -> None:
    row = PanelReview(
        id="pr:" + "0" * 64,
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=1,
        decision="approved",
        reviewer_id=REVIEWER_A,
        decided_at="2026-10-16T12:00:00.000000Z",
    )
    assert row.reason is None


def test_a_topic_decision_cannot_carry_stance_wording() -> None:
    with pytest.raises(ValidationError):
        PanelReview(
            id="pr:" + "0" * 64,
            subject_table="issue_topics",
            subject_id=ANCHOR_TOPIC,
            subject_version=1,
            decision="approved",
            reviewer_id=REVIEWER_A,
            decided_at="2026-10-16T12:00:00.000000Z",
            proposed_label="a stance field on a topic row",
        )


def test_a_stance_decision_cannot_carry_a_topic_name() -> None:
    with pytest.raises(ValidationError):
        PanelReview(
            id="pr:" + "0" * 64,
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            subject_version=1,
            decision="approved",
            reviewer_id=REVIEWER_A,
            decided_at="2026-10-16T12:00:00.000000Z",
            proposed_name="a topic field on a stance row",
        )


def test_an_unknown_subject_table_is_refused() -> None:
    """`ReviewSubjectTable` is a Literal for exactly this.

    A decision stored against table "stance" (singular) would be invisible to
    every gate — the row would serve unreviewed wording while the ledger
    showed it approved.
    """
    with pytest.raises(ValidationError):
        PanelReview(
            id="pr:" + "0" * 64,
            subject_table="stance",  # type: ignore[arg-type]
            subject_id=ANCHOR_STANCE,
            subject_version=1,
            decision="approved",
            reviewer_id=REVIEWER_A,
            decided_at="2026-10-16T12:00:00.000000Z",
        )


def test_an_unknown_decision_is_refused() -> None:
    with pytest.raises(ValidationError):
        PanelReview(
            id="pr:" + "0" * 64,
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            subject_version=1,
            decision="looks-fine-to-me",  # type: ignore[arg-type]
            reviewer_id=REVIEWER_A,
            decided_at="2026-10-16T12:00:00.000000Z",
        )


def test_version_zero_is_refused() -> None:
    with pytest.raises(ValidationError):
        PanelReview(
            id="pr:" + "0" * 64,
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            subject_version=0,
            decision="approved",
            reviewer_id=REVIEWER_A,
            decided_at="2026-10-16T12:00:00.000000Z",
        )


def test_an_anonymous_decision_is_refused() -> None:
    """A review nobody is named for cannot be a cross-partisan review."""
    with pytest.raises(ValidationError):
        PanelReview(
            id="pr:" + "0" * 64,
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            subject_version=1,
            decision="approved",
            reviewer_id="",
            decided_at="2026-10-16T12:00:00.000000Z",
        )


def test_the_ledger_row_forbids_unknown_fields() -> None:
    """`extra="forbid"`, for the reason `RowModel`'s docstring gives.

    A decision row carrying `reveiwer_id` would store happily and be invisible
    to every read — the gate would then see no decision and withhold wording a
    panel had actually approved.
    """
    with pytest.raises(ValidationError):
        PanelReview(
            id="pr:" + "0" * 64,
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            subject_version=1,
            decision="approved",
            reviewer_id=REVIEWER_A,
            decided_at="2026-10-16T12:00:00.000000Z",
            reveiwer_id="typo",  # type: ignore[call-arg]
        )


def test_the_ledger_table_is_declared_in_the_bootstrap_schema() -> None:
    """A table nothing creates is a table every live write 404s against."""
    assert "panel_reviews" in TABLE_SCHEMAS
    assert "panel_reviews" in INFRASTRUCTURE_TABLES
    assert set(TABLE_SCHEMAS["panel_reviews"]) == set(PanelReview.model_fields), (
        "the bootstrap schema and the Pydantic model disagree about the ledger's fields"
    )


# --- the gated topic tree -------------------------------------------------


def test_an_unreviewed_parent_takes_its_subtopics_with_it(
    panel: PanelReviewService, client: ZeroDBClient
) -> None:
    """A subtopic with no rendered parent has nowhere to appear in a picker."""
    panel.grandfather_existing_corpus()
    topics: Repository[IssueTopic] = Repository(client=client, model=IssueTopic)
    parent = topics.get("en-US:healthcare:v1")
    assert parent is not None
    # Bump the parent past its grandfathering so it fails the gate.
    topics.upsert(parent.model_copy(update={"taxonomy_version": 2}).row_data())

    tree = panel.approved_topic_tree(locale=TAXONOMY_LOCALE, taxonomy_version=TAXONOMY_VERSION)
    assert "en-US:healthcare:v1" not in {topic.id for topic in tree.topics}
    for topic in tree.topics:
        assert ANCHOR_TOPIC not in {sub.id for sub in topic.subtopics}


def test_the_gated_tree_is_alphabetical(panel: PanelReviewService) -> None:
    """Same ordering rule as `TopicService.tree`, and for the same reason."""
    panel.grandfather_existing_corpus()
    tree = panel.approved_topic_tree(locale=TAXONOMY_LOCALE, taxonomy_version=TAXONOMY_VERSION)
    names = [topic.name for topic in tree.topics]
    assert names == sorted(names)
    for topic in tree.topics:
        sub_names = [sub.name for sub in topic.subtopics]
        assert sub_names == sorted(sub_names)


def test_the_gated_tree_matches_the_ungated_one_when_everything_is_marked(
    panel: PanelReviewService, client: ZeroDBClient
) -> None:
    """The gate withholds unreviewed rows and nothing else.

    The failure this catches is an over-broad filter: a gate that dropped a
    whole locale, or every subtopic, would pass every "unreviewed wording is
    withheld" test while quietly emptying the product.
    """
    from app.topics.service import TopicService

    panel.grandfather_existing_corpus()
    gated = panel.approved_topic_tree(locale=TAXONOMY_LOCALE, taxonomy_version=TAXONOMY_VERSION)
    ungated = TopicService(client).tree(locale=TAXONOMY_LOCALE, taxonomy_version=TAXONOMY_VERSION)
    assert [topic.id for topic in gated.topics] == [topic.id for topic in ungated.topics]
    assert sum(len(topic.subtopics) for topic in gated.topics) == sum(
        len(topic.subtopics) for topic in ungated.topics
    )


def test_the_gated_stance_read_matches_the_ungated_one_when_everything_is_marked(
    panel: PanelReviewService, client: ZeroDBClient
) -> None:
    from app.stances.service import StanceService

    panel.grandfather_existing_corpus()
    gated = {str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)}
    ungated = {str(card.id) for card in StanceService(client).for_topic(ANCHOR_TOPIC)}
    assert gated == ungated
    assert gated == {card.row_id for card in STANCES if card.topic_id == ANCHOR_TOPIC}


def test_the_gated_stance_read_is_stably_ordered(panel: PanelReviewService) -> None:
    """Unlike `StanceService`, this path does not shuffle; see its docstring.

    Deliberate and asserted: a reviewer table that reshuffled between page
    loads would be unusable, and a voter-facing caller that moves onto this
    path is expected to shuffle what it gets back rather than rely on the
    service to do it.
    """
    panel.grandfather_existing_corpus()
    first = [str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)]
    second = [str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)]
    assert first == second


# --- history --------------------------------------------------------------


def test_the_history_of_an_unreviewed_row_is_empty(panel: PanelReviewService) -> None:
    assert panel.history_for(subject_table="stances", subject_id=ANCHOR_STANCE) == []


def test_the_history_is_newest_first_and_totally_ordered(panel: PanelReviewService) -> None:
    for decision, reason in (
        ("rejected", "First synthetic rejection."),
        ("approved", None),
        ("rejected", "Second synthetic rejection."),
    ):
        panel.record_decision(
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            subject_version=1,
            reviewer_id=REVIEWER_A,
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
        )
    history = panel.history_for(subject_table="stances", subject_id=ANCHOR_STANCE)
    assert len(history) == 3
    keys = [(row.decided_at, str(row.id)) for row in history]
    assert keys == sorted(keys, reverse=True)


def test_an_approved_edit_records_what_changed(panel: PanelReviewService) -> None:
    panel.grandfather_existing_corpus()
    declared = next(card for card in STANCES if card.row_id == ANCHOR_STANCE)
    proposal = panel.propose(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        author_id=REVIEWER_A,
        label="Cap monthly out-of-pocket drug costs",
    )
    panel.decide(proposal_id=proposal.proposal_id, reviewer_id=REVIEWER_B, decision="approved")
    approvals = [
        row
        for row in panel.history_for(subject_table="stances", subject_id=ANCHOR_STANCE)
        if row.decision == "approved"
    ]
    assert approvals
    assert approvals[0].previous_label == declared.label
    assert approvals[0].proposed_label == "Cap monthly out-of-pocket drug costs"


def test_one_rows_history_does_not_leak_into_anothers(panel: PanelReviewService) -> None:
    """A stance id extends its topic's id, so a prefix match would leak."""
    panel.record_decision(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        subject_version=1,
        reviewer_id=REVIEWER_A,
        decision="approved",
    )
    sibling = "en-US:prescription-drug-pricing:v1:let-the-market-set-prices"
    assert panel.history_for(subject_table="stances", subject_id=sibling) == []
    # And a topic's approval does not publish a card beneath it.
    panel.record_decision(
        subject_table="issue_topics",
        subject_id=ANCHOR_TOPIC,
        subject_version=1,
        reviewer_id=REVIEWER_A,
        decision="approved",
    )
    served = {str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)}
    assert served == {ANCHOR_STANCE}


def test_the_service_logs_no_wording_or_reviewer_reason(
    panel: PanelReviewService, caplog: pytest.LogCaptureFixture
) -> None:
    """Review decisions are public, but logs are not the publication channel.

    Reviewer ids and row ids are fine in logs — they are editorial metadata,
    not voter data (prd.md's privacy rule is about profiles). What must NOT
    reach a log line is a reason or a proposed sentence: an operator tailing
    the API should not be reading an unreviewed editorial draft, and a log
    aggregator is not where "versioned and public" is satisfied.
    """
    import logging

    with caplog.at_level(logging.INFO):
        panel.record_decision(
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            subject_version=1,
            reviewer_id=REVIEWER_A,
            decision="rejected",
            reason="A synthetic reason that must not be logged.",
        )
        proposal = panel.propose(
            subject_table="stances",
            subject_id=ANCHOR_STANCE,
            author_id=REVIEWER_A,
            label="A synthetic label that must not be logged",
        )
        panel.decide(proposal_id=proposal.proposal_id, reviewer_id=REVIEWER_B, decision="approved")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "must not be logged" not in logged, logged


def test_proposals_awaiting_review_excludes_decided_ones(panel: PanelReviewService) -> None:
    panel.grandfather_existing_corpus()
    proposal = panel.propose(
        subject_table="stances",
        subject_id=ANCHOR_STANCE,
        author_id=REVIEWER_A,
        label="Cap monthly out-of-pocket drug costs",
    )
    assert proposal.proposal_id in {item.proposal_id for item in panel.proposals_awaiting_review()}
    panel.decide(proposal_id=proposal.proposal_id, reviewer_id=REVIEWER_B, decision="approved")
    assert proposal.proposal_id not in {
        item.proposal_id for item in panel.proposals_awaiting_review()
    }


def test_the_subject_accessor_serves_unreviewed_wording(panel: PanelReviewService) -> None:
    """A reviewer must be able to read what they are judging.

    By definition that wording is not approved, so the gated reads cannot
    supply it — which is why `subject()` is public and ungated.
    """
    row = panel.subject(subject_table="stances", subject_id=ANCHOR_STANCE)
    assert isinstance(row, Stance)
    assert row.wording_version == WORDING_VERSION
    with pytest.raises(UnknownSubjectError):
        panel.subject(subject_table="stances", subject_id="en-US:x:v1:nope")
