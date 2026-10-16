"""HTTP contract tests for the `/v1/panel` endpoints (story 3.3).

The feature file drives the happy paths through the API; these cover every
**status-code mapping**, which is the part of a router that is easy to get
wrong and invisible when it is. The distinctions matter to a client:

  * `404` — no such row or proposal: nothing to review, fix the id.
  * `409` — the row moved since the reviewer read it: re-read and re-decide.
    Deliberately NOT 422, because the request was correct when it was formed.
  * `403` — a reviewer tried to approve their own proposal: the request is
    perfectly well-formed and is refused because of *who is asking*, which is
    what 403 means. Saying so plainly matters, since this is the rule that
    makes the review cross-partisan rather than ceremonial.
  * `422` — the caller sent something unusable: a rejection with no reason, a
    proposal that changes nothing, wording that fails a neutrality guard, a
    missing `reviewer_id`.

Also pins the two things a reviewer screen reads and must not be allowed to
drift: that `is_panel_approved` is false for grandfathered wording even though
`is_published` is true, and that the queue reports the grandfathered corpus
separately from never-reviewed wording.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.panel_review.router import panel_review_service, zerodb_client_for_panel_review
from app.panel_review.service import PanelReviewService
from app.seed_stances import STANCES, StanceSeeder
from app.seed_taxonomy import TaxonomySeeder, taxonomy_rows
from app.stances.router import zerodb_client_for_stances
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


@pytest.fixture
def api(client: ZeroDBClient, panel: PanelReviewService) -> Iterator[TestClient]:
    """The app with every ZeroDB dependency pointed at this scenario's fake.

    The stance router's client is overridden as well as this story's, because
    one test here reads the **voter-facing** `GET /v1/topics/{id}/stances` to
    pin that story 3.3 left it serving everything. Each router declares its own
    `zerodb_client_for_*` (see `app.topics.router`'s docstring for why), so an
    override of one is not an override of the others — and without this second
    line that endpoint falls through to `from_env()` and fails on the missing
    credentials CI does not have.
    """
    app = create_app()
    app.dependency_overrides[zerodb_client_for_panel_review] = lambda: client
    app.dependency_overrides[panel_review_service] = lambda: panel
    app.dependency_overrides[zerodb_client_for_stances] = lambda: client
    with TestClient(app) as test_client:
        yield test_client


def _propose(api: TestClient, **overrides: object) -> dict:
    body = {
        "author_id": REVIEWER_A,
        "subject_table": "stances",
        "subject_id": ANCHOR_STANCE,
        "label": "Cap monthly out-of-pocket drug costs",
    } | overrides
    return api.post("/v1/panel/proposals", json=body)  # type: ignore[return-value]


# --- POST /v1/panel/reviews -----------------------------------------------


def test_recording_a_decision_returns_201_and_the_rows_state(api: TestClient) -> None:
    response = api.post(
        "/v1/panel/reviews",
        json={
            "reviewer_id": REVIEWER_A,
            "subject_table": "stances",
            "subject_id": ANCHOR_STANCE,
            "subject_version": 1,
            "decision": "approved",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["is_panel_approved"] is True
    assert body["is_published"] is True
    assert body["subject_version"] == 1
    assert body["label"]
    assert body["name"] is None


def test_a_decision_about_a_missing_row_is_404(api: TestClient) -> None:
    response = api.post(
        "/v1/panel/reviews",
        json={
            "reviewer_id": REVIEWER_A,
            "subject_table": "stances",
            "subject_id": "en-US:prescription-drug-pricing:v1:not-a-card",
            "subject_version": 1,
            "decision": "approved",
        },
    )
    assert response.status_code == 404, response.text


def test_a_decision_naming_the_wrong_version_is_409(api: TestClient) -> None:
    """A conflict with stored state, not a malformed request.

    The reviewer's body was correct when they loaded the wording; the wording
    moved underneath them. A client should re-read and re-decide, which is what
    409 tells it to do and 422 would not.
    """
    response = api.post(
        "/v1/panel/reviews",
        json={
            "reviewer_id": REVIEWER_A,
            "subject_table": "stances",
            "subject_id": ANCHOR_STANCE,
            "subject_version": 99,
            "decision": "approved",
        },
    )
    assert response.status_code == 409, response.text


def test_a_rejection_with_no_reason_is_422(api: TestClient) -> None:
    response = api.post(
        "/v1/panel/reviews",
        json={
            "reviewer_id": REVIEWER_A,
            "subject_table": "stances",
            "subject_id": ANCHOR_STANCE,
            "subject_version": 1,
            "decision": "rejected",
        },
    )
    assert response.status_code == 422, response.text


def test_a_decision_with_no_reviewer_is_422(api: TestClient) -> None:
    """An anonymous panel review is a contradiction, unlike an anonymous voter."""
    response = api.post(
        "/v1/panel/reviews",
        json={
            "subject_table": "stances",
            "subject_id": ANCHOR_STANCE,
            "subject_version": 1,
            "decision": "approved",
        },
    )
    assert response.status_code == 422, response.text


def test_a_blank_reviewer_id_is_422(api: TestClient) -> None:
    response = api.post(
        "/v1/panel/reviews",
        json={
            "reviewer_id": "",
            "subject_table": "stances",
            "subject_id": ANCHOR_STANCE,
            "subject_version": 1,
            "decision": "approved",
        },
    )
    assert response.status_code == 422, response.text


def test_the_api_cannot_be_used_to_grandfather_wording(api: TestClient) -> None:
    """`ApiDecision` is narrower than the stored vocabulary, on purpose.

    If a caller could POST `grandfathered`, anyone reaching the API could mark
    unreviewed wording publishable — which is precisely the gate this story
    installs. Same for `proposed`, which has its own endpoint.
    """
    for forbidden in ("grandfathered", "proposed", "withdrawn"):
        response = api.post(
            "/v1/panel/reviews",
            json={
                "reviewer_id": REVIEWER_A,
                "subject_table": "stances",
                "subject_id": ANCHOR_STANCE,
                "subject_version": 1,
                "decision": forbidden,
            },
        )
        assert response.status_code == 422, f"{forbidden} was accepted: {response.text}"


def test_an_unknown_subject_table_is_422(api: TestClient) -> None:
    response = api.post(
        "/v1/panel/reviews",
        json={
            "reviewer_id": REVIEWER_A,
            "subject_table": "stance",
            "subject_id": ANCHOR_STANCE,
            "subject_version": 1,
            "decision": "approved",
        },
    )
    assert response.status_code == 422, response.text


def test_an_unknown_field_in_the_body_is_422(api: TestClient) -> None:
    """`extra="forbid"` on the request model, so a typo is loud."""
    response = api.post(
        "/v1/panel/reviews",
        json={
            "reveiwer_id": REVIEWER_A,
            "reviewer_id": REVIEWER_A,
            "subject_table": "stances",
            "subject_id": ANCHOR_STANCE,
            "subject_version": 1,
            "decision": "approved",
        },
    )
    assert response.status_code == 422, response.text


# --- POST /v1/panel/proposals ---------------------------------------------


def test_proposing_returns_201_with_the_before_and_after(api: TestClient) -> None:
    response = _propose(api)
    assert response.status_code == 201, response.text
    body = response.json()
    declared = next(card for card in STANCES if card.row_id == ANCHOR_STANCE)
    assert body["label"] == "Cap monthly out-of-pocket drug costs"
    assert body["previous_label"] == declared.label
    assert body["decided"] is False
    assert body["author_id"] == REVIEWER_A
    assert body["proposal_id"].startswith("pr:")


def test_a_proposal_for_a_missing_row_is_404(api: TestClient) -> None:
    response = _propose(api, subject_id="en-US:prescription-drug-pricing:v1:not-a-card")
    assert response.status_code == 404, response.text


def test_a_proposal_that_changes_nothing_is_422(api: TestClient) -> None:
    declared = next(card for card in STANCES if card.row_id == ANCHOR_STANCE)
    response = _propose(api, label=declared.label)
    assert response.status_code == 422, response.text


def test_an_empty_proposal_is_422(api: TestClient) -> None:
    response = api.post(
        "/v1/panel/proposals",
        json={
            "author_id": REVIEWER_A,
            "subject_table": "stances",
            "subject_id": ANCHOR_STANCE,
        },
    )
    assert response.status_code == 422, response.text


def test_a_proposal_naming_a_political_side_is_422(api: TestClient) -> None:
    """Story 3.2's guard, reached over HTTP."""
    response = _propose(api, label="End the Democrats' price controls")
    assert response.status_code == 422, response.text
    assert "political side" in response.json()["detail"]


def test_a_topic_proposal_presupposing_a_stance_is_422(api: TestClient) -> None:
    """Story 3.1's guard, reached over HTTP."""
    response = api.post(
        "/v1/panel/proposals",
        json={
            "author_id": REVIEWER_A,
            "subject_table": "issue_topics",
            "subject_id": "en-US:firearms-policy:v1",
            "name": "Commonsense gun safety reform",
        },
    )
    assert response.status_code == 422, response.text
    assert "presupposes a stance" in response.json()["detail"]


def test_a_stance_proposal_carrying_a_topic_name_is_422(api: TestClient) -> None:
    response = _propose(api, label=None, name="Not a stance field")
    assert response.status_code == 422, response.text


def test_the_proposal_list_shows_it_then_stops_after_a_decision(api: TestClient) -> None:
    proposal_id = _propose(api).json()["proposal_id"]
    listed = api.get("/v1/panel/proposals")
    assert listed.status_code == 200, listed.text
    assert proposal_id in {item["proposal_id"] for item in listed.json()["proposals"]}

    decided = api.post(
        f"/v1/panel/proposals/{proposal_id}/decision",
        json={"reviewer_id": REVIEWER_B, "decision": "approved"},
    )
    assert decided.status_code == 201, decided.text
    after = api.get("/v1/panel/proposals").json()["proposals"]
    assert proposal_id not in {item["proposal_id"] for item in after}


# --- POST /v1/panel/proposals/{id}/decision -------------------------------


def test_approving_a_proposal_returns_the_bumped_version_and_new_wording(
    api: TestClient,
) -> None:
    proposal_id = _propose(api).json()["proposal_id"]
    response = api.post(
        f"/v1/panel/proposals/{proposal_id}/decision",
        json={"reviewer_id": REVIEWER_B, "decision": "approved"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["subject_version"] == 2
    assert body["label"] == "Cap monthly out-of-pocket drug costs"
    assert body["status"] == "approved"
    assert body["is_published"] is True
    assert body["is_panel_approved"] is True


def test_self_approval_over_http_is_403(api: TestClient) -> None:
    """Not 422: the request is well-formed and refused for who is asking."""
    proposal_id = _propose(api).json()["proposal_id"]
    response = api.post(
        f"/v1/panel/proposals/{proposal_id}/decision",
        json={"reviewer_id": REVIEWER_A, "decision": "approved"},
    )
    assert response.status_code == 403, response.text
    assert "cross-partisan" in response.json()["detail"]


def test_deciding_on_an_unknown_proposal_is_404(api: TestClient) -> None:
    response = api.post(
        f"/v1/panel/proposals/pr:{'0' * 64}/decision",
        json={"reviewer_id": REVIEWER_B, "decision": "approved"},
    )
    assert response.status_code == 404, response.text


def test_deciding_on_a_stale_proposal_is_409(api: TestClient) -> None:
    """Two proposals, the first approved: the second is now against old wording."""
    first = _propose(api).json()["proposal_id"]
    second = _propose(api, label="Cap what patients pay each month").json()["proposal_id"]
    assert (
        api.post(
            f"/v1/panel/proposals/{first}/decision",
            json={"reviewer_id": REVIEWER_B, "decision": "approved"},
        ).status_code
        == 201
    )
    response = api.post(
        f"/v1/panel/proposals/{second}/decision",
        json={"reviewer_id": REVIEWER_B, "decision": "approved"},
    )
    assert response.status_code == 409, response.text


def test_rejecting_a_proposal_with_no_reason_is_422(api: TestClient) -> None:
    proposal_id = _propose(api).json()["proposal_id"]
    response = api.post(
        f"/v1/panel/proposals/{proposal_id}/decision",
        json={"reviewer_id": REVIEWER_B, "decision": "rejected"},
    )
    assert response.status_code == 422, response.text


def test_rejecting_a_proposal_leaves_the_live_wording(api: TestClient) -> None:
    proposal_id = _propose(api).json()["proposal_id"]
    response = api.post(
        f"/v1/panel/proposals/{proposal_id}/decision",
        json={
            "reviewer_id": REVIEWER_B,
            "decision": "rejected",
            "reason": "Loses the insulin example voters recognize.",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    declared = next(card for card in STANCES if card.row_id == ANCHOR_STANCE)
    assert body["subject_version"] == 1
    assert body["label"] == declared.label
    assert body["status"] == "rejected"
    assert body["is_published"] is False


# --- GET /v1/panel/reviews/{table}/{id} -----------------------------------


def test_the_history_of_an_unreviewed_row_is_empty_not_404(api: TestClient) -> None:
    response = api.get(f"/v1/panel/reviews/stances/{ANCHOR_STANCE}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["history"] == []
    assert body["status"] is None
    assert body["is_panel_approved"] is False


def test_the_history_of_a_row_that_does_not_exist_is_404(api: TestClient) -> None:
    response = api.get("/v1/panel/reviews/stances/en-US:prescription-drug-pricing:v1:nope")
    assert response.status_code == 404, response.text


def test_the_history_reports_grandfathered_as_published_but_not_approved(
    api: TestClient, panel: PanelReviewService
) -> None:
    """The honesty invariant as a reviewer screen reads it over HTTP."""
    panel.grandfather_existing_corpus()
    response = api.get(f"/v1/panel/reviews/stances/{ANCHOR_STANCE}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "grandfathered"
    assert body["is_panel_approved"] is False
    assert len(body["history"]) == 1
    assert "NOT panel-approved" in body["history"][0]["reason"]


def test_an_unknown_subject_table_in_the_path_is_422(api: TestClient) -> None:
    response = api.get(f"/v1/panel/reviews/stance/{ANCHOR_STANCE}")
    assert response.status_code == 422, response.text


# --- GET /v1/panel/queue --------------------------------------------------


def test_the_queue_separates_grandfathered_from_never_reviewed(
    api: TestClient, panel: PanelReviewService
) -> None:
    panel.grandfather_existing_corpus()
    response = api.get("/v1/panel/queue")
    assert response.status_code == 200, response.text
    body = response.json()
    expected = len(STANCES) + len(taxonomy_rows())
    assert body["awaiting_review"] == expected
    assert body["grandfathered"] == expected
    assert body["never_reviewed"] == 0
    assert len(body["items"]) == expected
    assert all(item["wording"] for item in body["items"])


def test_an_empty_ledger_reports_everything_as_never_reviewed(api: TestClient) -> None:
    response = api.get("/v1/panel/queue")
    body = response.json()
    expected = len(STANCES) + len(taxonomy_rows())
    assert body["never_reviewed"] == expected
    assert body["grandfathered"] == 0


# --- GET /v1/panel/published/* --------------------------------------------


def test_the_gated_endpoints_serve_nothing_on_an_empty_ledger(api: TestClient) -> None:
    """Fails closed over HTTP, which is the acceptance criterion."""
    topics = api.get("/v1/panel/published/topics")
    assert topics.status_code == 200, topics.text
    assert topics.json()["topics"] == []

    stances = api.get(f"/v1/panel/published/topics/{ANCHOR_TOPIC}/stances")
    assert stances.status_code == 200, stances.text
    assert stances.json()["options"] == []


def test_the_gated_endpoints_serve_the_corpus_once_grandfathered(
    api: TestClient, panel: PanelReviewService
) -> None:
    panel.grandfather_existing_corpus()
    topics = api.get("/v1/panel/published/topics").json()
    assert len(topics["topics"]) == 20
    assert sum(len(topic["subtopics"]) for topic in topics["topics"]) == 60

    stances = api.get(f"/v1/panel/published/topics/{ANCHOR_TOPIC}/stances").json()
    assert stances["topic_id"] == ANCHOR_TOPIC
    served = {option["id"] for option in stances["options"]}
    assert served == {card.row_id for card in STANCES if card.topic_id == ANCHOR_TOPIC}


def test_the_gated_stance_response_marks_nothing_as_the_one_to_pick(
    api: TestClient, panel: PanelReviewService
) -> None:
    """Story 3.2's rule survives the gate: no card is flagged as a default."""
    panel.grandfather_existing_corpus()
    body = api.get(f"/v1/panel/published/topics/{ANCHOR_TOPIC}/stances").json()
    for option in body["options"]:
        assert set(option) == {"id", "label", "description"}


def test_the_voter_facing_endpoints_are_unchanged_by_this_story(api: TestClient) -> None:
    """The ungated reads still serve everything; see the router's docstring.

    Pinned so that switching them onto the gated path is a deliberate change
    with a failing test to update, rather than something that happens by
    accident in a later story.
    """
    stances = api.get(f"/v1/topics/{ANCHOR_TOPIC}/stances")
    assert stances.status_code == 200, stances.text
    assert len(stances.json()["options"]) == len(
        [card for card in STANCES if card.topic_id == ANCHOR_TOPIC]
    ), "the ungated voter-facing read should serve every stored card, reviewed or not"
