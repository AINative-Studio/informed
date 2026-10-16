"""Unit tests for the app factory and health endpoint (story 1.1)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app


def test_create_app_returns_a_fastapi_instance() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)
    assert app.title == "Informed API"


def test_health_payload_shape(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body == {"status": "ok", "service": "informed-api", "version": "0.1.0"}


def test_routes_are_only_the_ones_built_so_far(client: TestClient) -> None:
    """Story 1.5 mounted `/v1/ballot`; story 4.1 added the onboarding trio;
    story 3.5 added `/v1/topics/suggest`; stories 4.2/4.4 add the
    tier-weights read and the profile load endpoint; story 3.3 adds the
    `/v1/panel` review surface.

    Replaces story 1.1's "health is the only route" assertion, whose own
    docstring named 1.5 as the story that would change it, and that story's
    own replacement, which named 4.1 the same way. The check still earns its
    place: it fails the moment an endpoint is mounted without its own tests,
    which is how a half-built route reaches main unnoticed.
    """
    # Read from the OpenAPI schema rather than walking `app.routes`: an
    # included router is wrapped rather than flattened, so its paths are not on
    # `app.routes` at all, and the schema is the surface clients actually see.
    paths = {path for path in client.get("/openapi.json").json()["paths"] if path.startswith("/v1")}
    assert paths == {
        "/v1/ballot",
        "/v1/topics",
        "/v1/topics/suggest",
        "/v1/topics/{topic_id}/stances",
        "/v1/profiles",
        "/v1/profiles/methodology",
        "/v1/profiles/{user_id}",
        # Story 3.3: the advisory panel's review workflow. Reviewer-facing,
        # under one prefix so there is one thing to protect once an auth story
        # lands — see `app.panel_review.router`'s module docstring, which is
        # explicit that this prefix must not be routed from the public edge
        # before then.
        "/v1/panel/queue",
        "/v1/panel/proposals",
        "/v1/panel/proposals/{proposal_id}/decision",
        "/v1/panel/reviews",
        "/v1/panel/reviews/{subject_table}/{subject_id}",
        "/v1/panel/published/topics",
        "/v1/panel/published/topics/{topic_id}/stances",
    }

    # The voter-facing taxonomy and stance reads are deliberately UNCHANGED by
    # story 3.3 and still serve everything stored; the gated equivalents are
    # the `/v1/panel/published/*` pair above. Asserted here rather than only
    # described, because "we did not switch the voter endpoints over" is the
    # kind of deliberate omission that a later story could undo by accident.
    assert "/v1/topics" in paths
    assert "/v1/topics/{topic_id}/stances" in paths

    # The remaining Core API endpoints are genuinely absent, not merely untested.
    for not_yet in (
        "/v1/candidates/some-id/alignment",
        "/v1/agent/ask",
        "/v1/corrections",
    ):
        assert client.get(not_yet).status_code == 404
