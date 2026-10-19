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
    `/v1/panel` review surface; story 12.2 adds the public `/v1/methodology`
    page and its changelog; story 15.1 adds the follow/unfollow endpoints;
    story 12.4 adds the public correction form and the reporter's status
    lookup; story 14.2 adds the published consent language and story 14.4 the
    one-action export and delete. Story 14.6 adds the shareable,
    candidate-data-only summary card; story 14.7 adds the privacy-safe
    analytics event recorder and its k-anonymous aggregate read.

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
        # Story 14.2: the GDPR special-category consent wording and its
        # version, published with the anonymous-mode alternative in the same
        # payload so a client cannot render one without the other. Needs no
        # identity — an anonymous voter reads it to decide whether to have an
        # account at all.
        "/v1/profiles/consent",
        # `GET` loads (4.4) and `DELETE` permanently erases + shreds the data
        # key (14.4). One path, two methods; the OpenAPI schema keys by path.
        "/v1/profiles/{user_id}",
        # Story 14.4: one-action export of everything stored for one voter,
        # decrypted for its owner, with the consent history travelling with it.
        "/v1/profiles/{user_id}/export",
        # Story 15.1: follow/unfollow an incumbent, read-modify-write through
        # the same encrypted VoterProfileService every other profile change
        # uses — see app.profiles.router's module docstring on this endpoint.
        "/v1/profiles/{user_id}/follow/{person_id}",
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
        # Story 12.2: the published methodology page and its changelog. The
        # only endpoints in this list that are PUBLIC — no credential of any
        # kind — which is required rather than an oversight: prd.md's trust
        # section calls for a methodology page, and one a journalist has to
        # log in to read is not published. See `app.methodology.router`'s
        # module docstring.
        "/v1/methodology",
        "/v1/methodology/versions",
        "/v1/methodology/{version}",
        # Story 12.4: the public correction form and the reporter's own status
        # lookup. `POST /v1/corrections` is the first uncredentialed WRITE in
        # this API — prd.md's "public correction form on every claim" cannot be
        # behind a login, since the people most likely to spot an error about a
        # candidate are the least likely to hold an account.
        #
        # Note what is NOT here: there is no `PATCH`/`PUT` on a correction, so
        # nothing in this list lets a caller change a report's status.
        # `CorrectionService.set_status` is a service method reached by
        # `scripts/triage_correction.py`, deliberately unmounted until an auth
        # story lands — see `app.corrections.service`'s module docstring, which
        # is explicit that an unauthenticated status write would let anyone
        # mark an inconvenient correction `rejected`.
        "/v1/corrections",
        "/v1/corrections/{tracking_token}",
        # Story 14.6: the shareable summary card. Takes no profile, user or
        # voter identity of any kind — see `app.summary_card.router`'s module
        # docstring — so its presence here is one more public, candidate-data-
        # only endpoint alongside /v1/methodology and /v1/corrections.
        "/v1/candidates/{candidacy_id}/summary-card",
        # Story 14.7: privacy-safe analytics. Recording takes no identity
        # parameter, and the aggregate read reveals a count only at or above
        # the k-anonymity threshold — see `app.analytics`'s module docstrings.
        "/v1/analytics/events",
        "/v1/analytics/aggregate",
        # Stories 6.4-6.7: prd.md's alignment endpoint — score, confidence
        # badge, dealbreaker warnings, top reasons. The opposite case from
        # 14.6's summary card directly above it: this endpoint REQUIRES a
        # voter identity (`profile_id`), because an alignment score is a
        # function of a voter's priorities and there is no meaningful
        # anonymous version of it.
        #
        # Its guarantee is therefore the other direction — personalized in,
        # never personal out: `app.scoring.models`' four response models
        # forbid extra fields and declare nothing that could carry an
        # interest's wording, tier, note or the encrypted payload. See
        # `app.scoring.router`'s module docstring for the contrast.
        #
        # Declared with the `{candidacy_id:path}` converter, because a real
        # candidacy id embeds an OCD-ID containing slashes — the OpenAPI
        # schema still renders it as `{candidacy_id}`.
        "/v1/candidates/{candidacy_id}/alignment",
    }

    # Story 12.4's deliberate omission, asserted rather than only described:
    # no mounted route can change a correction's status. The kind of gap a
    # later story closes on purpose, and must not close by accident.
    schema = client.get("/openapi.json").json()["paths"]
    assert set(schema["/v1/corrections"]) == {"post"}
    assert set(schema["/v1/corrections/{tracking_token}"]) == {"get"}

    # `/v1/profiles/methodology` (4.2) and `/v1/methodology` (12.2) read the
    # same ZeroDB table and are deliberately NOT merged. The first is the
    # profile editor's internal read — today's tier weights and nothing else,
    # so a component need not hard-code 3.0/1.5/0.5. The second is the
    # published document: any version including superseded ones, its
    # changelog_url, its canonical citation path, and an explicit account of
    # which documented weight groups are not in force yet. Collapsing them
    # would mean either putting history and changelog plumbing in the
    # onboarding path or dropping it from the public page.
    assert {"/v1/profiles/methodology", "/v1/methodology"} <= paths

    # The voter-facing taxonomy and stance reads are deliberately UNCHANGED by
    # story 3.3 and still serve everything stored; the gated equivalents are
    # the `/v1/panel/published/*` pair above. Asserted here rather than only
    # described, because "we did not switch the voter endpoints over" is the
    # kind of deliberate omission that a later story could undo by accident.
    assert "/v1/topics" in paths
    assert "/v1/topics/{topic_id}/stances" in paths

    # The remaining Core API endpoints are genuinely absent, not merely
    # untested. `/v1/corrections` left this list in story 12.4, and
    # `/v1/candidates/{id}/alignment` left it in stories 6.4-6.7 — it is
    # asserted present in the path set above instead.
    #
    # `/v1/candidates/{id}/promise-record` is the one still missing from
    # prd.md's Core API table alongside `/v1/agent/ask`. It is NOT checked by
    # a 404 here, deliberately: the alignment route is declared with the
    # `{candidacy_id:path}` converter (a real candidacy id embeds a
    # slash-bearing OCD-ID), and a greedy `:path` segment would swallow
    # `/v1/candidates/x/promise-record` into a candidacy id of
    # `x/promise-record` rather than 404ing. Asserting a 404 would therefore
    # have tested the converter, not the absence — and when the
    # promise-record route lands it will need its own `:path` declaration for
    # the same reason.
    for not_yet in ("/v1/agent/ask",):
        assert client.get(not_yet).status_code == 404
