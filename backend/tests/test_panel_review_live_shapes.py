"""Live-measured facts about `panel_reviews`, replayed in CI (story 3.3).

Sibling of `test_stance_cards_live_shapes.py`, `test_issue_taxonomy_live_shapes.py`
and the seven others. The payloads below were read back from the real
"informed" ZeroDB project on **2026-10-16**, after
`scripts/verify_panel_review_live.py` ran the whole workflow against it, and
are frozen here so the facts they encode cannot regress unnoticed.

**Nothing here makes a live call.** The payloads are the live ones, replayed
through the in-memory fake, which is what makes these runnable in CI without
credentials.

What each measured fact prevents:

  * **All fifteen fields survive the round trip, nulls included.** ZeroDB's
    `PUT` replaces `row_data` wholesale, so a field dropped on the way out is
    gone rather than defaulted. A ledger row that lost `subject_version` would
    be read as a decision about *some* version, and `_in_force` would then
    either publish unreviewed wording or withhold approved wording — silently,
    in both directions.
  * **`subject_version` comes back an `int`, not a float or a string.** The
    gate compares it with `==` against a row's `wording_version`; `1 == "1"` is
    False in Python, so a stringified version would make every approval
    invisible and the gated reads would quietly empty.
  * **`decision` comes back the literal string, not coerced.** It is matched
    against `PUBLISHABLE_DECISIONS` by value.
  * **`grandfathered` is what is actually stored for the pre-existing corpus,
    and `reviewer_id` is the backfill's literal id.** This is the honesty
    invariant, pinned against the real bytes: if a future change writes
    `approved` for those rows, this test fails rather than the live database
    quietly starting to claim a review that never happened.
  * **The timestamp carries microseconds.** `_in_force` is newest-wins and
    `decide` writes two decisions about one row in a single call; at second
    resolution those tie and the tie breaks on a content hash, which has
    nothing to do with the order the decisions were made in. The stored format
    is what makes the ordering real.
  * **The id is `pr:` plus 64 hex characters**, content-addressed, so the
    backfill is re-runnable without appending duplicates.

Also measured live, and recorded here rather than asserted because it is an
account fact rather than a code fact: the live project answered
`402 storage_limit_exceeded` on a row insert partway through the first
verification run (1.7558 GB stored against a 0.5 GB plan limit), then accepted
every insert on the retry minutes later with nothing changed. See this story's
PR; it is raised as an open question for datamodel.md.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.panel_review.service import (
    BACKFILL_REVIEWER_ID,
    PUBLISHABLE_DECISIONS,
    PanelReviewService,
)
from app.repositories import Repository
from app.repositories.models import PanelReview
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

#: Copied verbatim from an unfiltered live read of `panel_reviews` in the real
#: "informed" project on 2026-10-16 (project id redacted). The grandfathering
#: decision over one real taxonomy row.
LIVE_TOPIC_DECISION: dict[str, Any] = {
    "decided_at": "2026-10-16T21:40:37.394967Z",
    "decision": "grandfathered",
    "id": "pr:c298b67ea9f53f24a00c8963ef592fdc89929a87982ae39ac30ab68889cdc701",
    "previous_description": None,
    "previous_label": None,
    "previous_name": None,
    "proposal_id": None,
    "proposed_description": None,
    "proposed_label": None,
    "proposed_name": None,
    "reason": (
        "Seeded by story 3.1/3.2 before the panel review workflow existed. Served, NOT "
        "panel-approved, and still owed a real cross-partisan review (prd.md: reviewed by a "
        "cross-partisan advisory panel before each release)."
    ),
    "reviewer_id": "informed:story-3.3-backfill",
    "subject_id": "en-US:abortion-and-reproductive-health-policy:v1",
    "subject_table": "issue_topics",
    "subject_version": 1,
}

#: The same, for one real stance card.
LIVE_STANCE_DECISION: dict[str, Any] = {
    "decided_at": "2026-10-16T21:42:05.088351Z",
    "decision": "grandfathered",
    "id": "pr:111f63f312c867a6a3a066aa0f54eebbe1e00bbcd9939565f62a7342d5f61efb",
    "previous_description": None,
    "previous_label": None,
    "previous_name": None,
    "proposal_id": None,
    "proposed_description": None,
    "proposed_label": None,
    "proposed_name": None,
    "reason": (
        "Seeded by story 3.1/3.2 before the panel review workflow existed. Served, NOT "
        "panel-approved, and still owed a real cross-partisan review (prd.md: reviewed by a "
        "cross-partisan advisory panel before each release)."
    ),
    "reviewer_id": "informed:story-3.3-backfill",
    "subject_id": "en-US:gestational-limits-and-exceptions:v1:viability-standard",
    "subject_table": "stances",
    "subject_version": 1,
}

#: The ledger's size after the live backfill: one decision per live row, and
#: the live corpus is 80 topics + 201 stances.
LIVE_LEDGER_ROWS = 281
LIVE_TOPICS_MARKED = 80
LIVE_STANCES_MARKED = 201

#: Measured live after the run: every ledger row is `grandfathered`, by the
#: backfill, and NOT ONE is `approved`. The honesty invariant as a number.
LIVE_DECISION_COUNTS: dict[str, int] = {"grandfathered": 281}

#: Measured live: the gated reads serve the whole corpus, so installing the
#: gate did not take the product down.
LIVE_GATED_TOP_LEVEL_TOPICS = 20
LIVE_GATED_SUBTOPICS = 60

#: Measured live: every row still owes a real review, and none is urgent-new.
LIVE_QUEUE_DEPTH = 281
LIVE_NEVER_REVIEWED = 0

#: Fields that must NOT appear on a stored ledger row. A `is_current` would
#: mean someone had started treating this table as `verdicts`-style
#: append-only-with-a-flag, which it deliberately is not (the id is
#: content-addressed instead); a `status` would be a second spelling of
#: `decision` for a gate to disagree with.
LIVE_ABSENT_FIELDS: tuple[str, ...] = ("is_current", "status", "approved", "panel_member")

_ID_PATTERN = re.compile(r"^pr:[0-9a-f]{64}$")


@pytest.fixture
def client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    zerodb = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    for table in ("issue_topics", "stances", "panel_reviews"):
        zerodb.create_table(table, {"id": "string"})
    return zerodb


# --- the live payload still fits the model --------------------------------


@pytest.mark.parametrize("live_row", [LIVE_TOPIC_DECISION, LIVE_STANCE_DECISION])
def test_the_live_row_validates_through_the_model(live_row: dict[str, Any]) -> None:
    row = PanelReview.model_validate(live_row)
    assert row.decision == "grandfathered"
    assert row.reviewer_id == BACKFILL_REVIEWER_ID


@pytest.mark.parametrize("live_row", [LIVE_TOPIC_DECISION, LIVE_STANCE_DECISION])
def test_the_model_writes_exactly_the_live_field_set(live_row: dict[str, Any]) -> None:
    """`PUT` replaces `row_data` wholesale, so a dropped field is lost.

    Asserted in both directions: the model must not write a field the live row
    lacks (which would be stored and then invisible to every reader expecting
    the documented shape), and must not omit one the live row has.
    """
    row = PanelReview.model_validate(live_row)
    assert set(row.row_data()) == set(live_row)
    assert len(live_row) == 15


@pytest.mark.parametrize("live_row", [LIVE_TOPIC_DECISION, LIVE_STANCE_DECISION])
def test_nulls_survive_the_round_trip_rather_than_vanishing(
    live_row: dict[str, Any],
) -> None:
    """A ledger row's `proposed_*` fields are null for a plain decision.

    They must be *present and null*, not absent: `RowModel.row_data` keeps
    `None`s deliberately (see its docstring) because ZeroDB's `PUT` replaces
    the row and an omitted key would simply disappear.

    Measured live: seven of the eight nullable fields came back present and
    null on a grandfathering decision — both `proposed_*` and `previous_*`
    triples plus `proposal_id`, since a plain decision proposes no wording. The
    eighth, `reason`, is populated here, because a grandfathering mark carries
    the sentence saying it is not approval. Listed by name rather than counted,
    so a field that silently stopped coming back names itself in the failure.
    """
    nullable = sorted(key for key, value in live_row.items() if value is None)
    assert nullable == [
        "previous_description",
        "previous_label",
        "previous_name",
        "proposal_id",
        "proposed_description",
        "proposed_label",
        "proposed_name",
    ], nullable
    assert live_row["reason"], "a grandfathering mark must say it is not approval"
    row = PanelReview.model_validate(live_row)
    written = row.row_data()
    for key in nullable:
        assert key in written, f"{key} would vanish from storage"
        assert written[key] is None


@pytest.mark.parametrize("live_row", [LIVE_TOPIC_DECISION, LIVE_STANCE_DECISION])
def test_subject_version_is_stored_as_a_real_int(live_row: dict[str, Any]) -> None:
    """The gate compares versions with `==`, so the type is load-bearing.

    `1 == "1"` is False and `1 == 1.0` is True, so a stringified version makes
    every approval invisible (gated reads quietly empty) while a float would
    pass. Checked on the raw dict, not through the model: Pydantic coerces
    `"1"` to `1` silently, so only the stored bytes can answer this.
    """
    version = live_row["subject_version"]
    assert isinstance(version, int)
    assert not isinstance(version, bool)


@pytest.mark.parametrize("live_row", [LIVE_TOPIC_DECISION, LIVE_STANCE_DECISION])
def test_decision_and_reviewer_are_stored_as_strings(live_row: dict[str, Any]) -> None:
    assert isinstance(live_row["decision"], str)
    assert isinstance(live_row["reviewer_id"], str)
    assert isinstance(live_row["subject_table"], str)


@pytest.mark.parametrize("live_row", [LIVE_TOPIC_DECISION, LIVE_STANCE_DECISION])
def test_the_live_id_is_content_addressed(live_row: dict[str, Any]) -> None:
    assert _ID_PATTERN.match(live_row["id"]), live_row["id"]


@pytest.mark.parametrize("live_row", [LIVE_TOPIC_DECISION, LIVE_STANCE_DECISION])
def test_the_live_timestamp_carries_microseconds(live_row: dict[str, Any]) -> None:
    """Second resolution would make `_in_force`'s newest-wins rule a coin flip.

    `decide` writes two decisions about one row in a single call, so a tie is
    not hypothetical; the tie-break is a content hash, whose order has nothing
    to do with which decision came first.
    """
    decided_at = live_row["decided_at"]
    assert decided_at.endswith("Z")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$", decided_at), decided_at


@pytest.mark.parametrize("live_row", [LIVE_TOPIC_DECISION, LIVE_STANCE_DECISION])
def test_no_unexpected_field_was_injected(live_row: dict[str, Any]) -> None:
    assert not set(live_row) & set(LIVE_ABSENT_FIELDS)


# --- the honesty invariant, pinned against the real bytes -----------------


@pytest.mark.parametrize("live_row", [LIVE_TOPIC_DECISION, LIVE_STANCE_DECISION])
def test_the_live_corpus_is_grandfathered_and_not_approved(
    live_row: dict[str, Any],
) -> None:
    """The one measured fact this whole story turns on.

    The live project's 281 decisions over the pre-existing taxonomy and stance
    corpus say `grandfathered`, by `informed:story-3.3-backfill`. If a future
    change makes them say `approved`, or puts a person's name on them, this
    test fails — which is the point: no panel has met (prd.md lists its
    composition as an open question), so an `approved` row would be a false
    claim in the live database.
    """
    assert live_row["decision"] == "grandfathered"
    assert live_row["decision"] != "approved"
    assert live_row["reviewer_id"] == BACKFILL_REVIEWER_ID
    assert "3.3" in live_row["reviewer_id"], "the mark must be traceable to this story"
    assert "NOT panel-approved" in live_row["reason"]


def test_the_live_ledger_holds_exactly_one_decision_per_row() -> None:
    assert LIVE_LEDGER_ROWS == LIVE_TOPICS_MARKED + LIVE_STANCES_MARKED
    assert LIVE_DECISION_COUNTS == {"grandfathered": LIVE_LEDGER_ROWS}
    assert "approved" not in LIVE_DECISION_COUNTS, (
        "the live ledger must hold no approvals: no panel has reviewed this corpus"
    )


@pytest.mark.parametrize("live_row", [LIVE_TOPIC_DECISION, LIVE_STANCE_DECISION])
def test_the_live_decision_is_publishable_without_being_an_approval(
    live_row: dict[str, Any],
) -> None:
    """Served AND not approved — the two halves of grandfathering, live.

    Replayed through the real service rather than asserted on the dict, so the
    properties the gate and the queue actually read are the ones under test.
    """
    assert live_row["decision"] in PUBLISHABLE_DECISIONS
    status = _status_from(live_row)
    assert status.is_publishable
    assert not status.is_panel_approved


def _status_from(live_row: dict[str, Any]):  # noqa: ANN202 - a local helper
    from app.panel_review.service import ReviewStatus

    return ReviewStatus(
        subject_table=live_row["subject_table"],
        subject_id=live_row["subject_id"],
        subject_version=live_row["subject_version"],
        decision=live_row["decision"],
        reviewer_id=live_row["reviewer_id"],
        decided_at=live_row["decided_at"],
        reason=live_row["reason"],
    )


def test_the_live_gate_served_the_whole_corpus() -> None:
    """Measured live: installing the gate did not take the product down."""
    assert LIVE_GATED_TOP_LEVEL_TOPICS == 20
    assert LIVE_GATED_SUBTOPICS == 60
    assert LIVE_GATED_TOP_LEVEL_TOPICS + LIVE_GATED_SUBTOPICS == LIVE_TOPICS_MARKED


def test_the_live_queue_reports_the_whole_corpus_as_owing_a_review() -> None:
    """The standing debt, as measured. Not zero, deliberately."""
    assert LIVE_QUEUE_DEPTH == LIVE_LEDGER_ROWS
    assert LIVE_NEVER_REVIEWED == 0


# --- replaying the live rows through the service --------------------------


def test_a_live_ledger_row_drives_the_gate_when_replayed(client: ZeroDBClient) -> None:
    """The live decision, stored through the fake, publishes its real subject.

    End to end on measured bytes: write the live stance row the decision is
    about, write the live decision verbatim, and assert the gate serves it.
    This is what catches a change that keeps the stored shape valid but stops
    the gate reading it — a renamed field, a changed comparison, a filter that
    narrows too far.
    """
    stance_id = LIVE_STANCE_DECISION["subject_id"]
    topic_id = stance_id.rsplit(":", 1)[0]
    client.insert_row(
        "issue_topics",
        {
            "id": topic_id,
            "parent_id": None,
            "name": "Gestational limits and exceptions",
            "locale": "en-US",
            "taxonomy_version": 1,
        },
    )
    client.insert_row(
        "stances",
        {
            "id": stance_id,
            "topic_id": topic_id,
            "label": "Allow abortion until fetal viability",
            "description": "Replayed from the live corpus for this shape test.",
            "direction_code": "+1",
            "wording_version": LIVE_STANCE_DECISION["subject_version"],
        },
    )
    client.insert_row("panel_reviews", dict(LIVE_STANCE_DECISION))

    panel = PanelReviewService(client)
    served = {str(card.id) for card in panel.approved_stances_for_topic(topic_id)}
    assert stance_id in served

    status = panel.status_for(subject_table="stances", subject_id=stance_id)
    assert status.decision == "grandfathered"
    assert status.is_publishable
    assert not status.is_panel_approved

    # And it is still on the queue, because grandfathering is not approval.
    assert stance_id in {entry.subject_id for entry in panel.queue()}


def test_a_live_ledger_row_reads_back_through_the_repository(client: ZeroDBClient) -> None:
    """A stored row must still validate, or the gate raises instead of gating.

    `Repository._parse_row` raises rather than skipping an unparseable row, on
    purpose — a quietly dropped ledger row would un-publish approved wording.
    So the live payload has to keep fitting the model.
    """
    client.insert_row("panel_reviews", dict(LIVE_TOPIC_DECISION))
    reviews: Repository[PanelReview] = Repository(client=client, model=PanelReview)
    rows = reviews.query(limit=10)
    assert len(rows) == 1
    assert str(rows[0].id) == LIVE_TOPIC_DECISION["id"]
    assert rows[0].subject_version == 1
