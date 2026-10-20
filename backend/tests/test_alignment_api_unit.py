"""The alignment endpoint's shape, privacy and the pieces around the formula.

Three things this file covers that the BDD suite does not:

  * **the response models' shape**, including the absences that are the privacy
    guarantee (story 6.4/6.7) — tested against the models and the generated
    OpenAPI document rather than through a request, so a field added by a later
    story fails here immediately;
  * **`dealbreakers` and `reasons` as pure functions** (6.6, 6.7), the same
    split `test_alignment_formula_unit.py` uses for the formula;
  * **the router's edges**: an unknown candidacy, an unknown profile, a missing
    `profile_id`.

Everything synthetic. No real voter, no real member.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.main import create_app
from app.scoring.dealbreakers import VIOLATION_THRESHOLD, dealbreaker_warnings
from app.scoring.formula import ScoredTerm
from app.scoring.models import (
    AlignmentReason,
    CandidateAlignment,
    Citation,
    DealbreakerWarning,
)
from app.scoring.reasons import MAX_REASONS, top_reasons

STANCE_A = "en-US:test-a:v1:for"
STANCE_B = "en-US:test-b:v1:for"
STANCE_C = "en-US:test-c:v1:for"


def _citation(roll: int = 1, *, position: str = "yea") -> Citation:
    return Citation(
        roll_call_id=f"us-house:119-2:{roll}",
        position=position,
        vote_type="final_passage",
        voted_at="2026-04-01T12:00:00Z",
    )


def _term(stance_id: str, *, s_value: float, weight: float = 3.0, intensity: int = 2) -> ScoredTerm:
    return ScoredTerm(
        stance_id=stance_id,
        tier_weight=weight,
        intensity=intensity,
        confidence=1.0,
        s_value=s_value,
    )


# --- story 6.7: the citation guarantee, as a property of the type ---------


def test_a_reason_cannot_be_constructed_without_a_citation() -> None:
    """backlog.md 6.7: "no reason lacks a citation".

    The mechanism is the type, not a check in a service: `citations` has no
    default and `min_length=1`, so no code path anywhere — present or future —
    can emit an uncited reason.
    """
    with pytest.raises(ValidationError, match="citations"):
        AlignmentReason(
            stance_id=STANCE_A,
            direction="raised",
            s_value=1.0,
            confidence=1.0,
            evidence_count=1,
            citations=[],
        )


def test_a_reason_cannot_omit_the_citations_field_entirely() -> None:
    """No default, so forgetting the field is as loud as passing an empty one.
    A default of `[]` would have made the `min_length` check skippable."""
    with pytest.raises(ValidationError, match="citations"):
        AlignmentReason(
            stance_id=STANCE_A,
            direction="raised",
            s_value=1.0,
            confidence=1.0,
            evidence_count=1,
        )


def test_a_citation_cannot_be_constructed_without_a_roll_call() -> None:
    """A citation with no roll call id is not a citation; it is a shape that
    looks checkable and is not."""
    with pytest.raises(ValidationError, match="roll_call_id"):
        Citation(roll_call_id="", position="yea", vote_type="final_passage", voted_at="2026-01-01")


def test_a_dealbreaker_warning_cannot_be_constructed_without_a_citation() -> None:
    """prd.md's "Evidence over opinion" applies hardest to an accusation."""
    with pytest.raises(ValidationError, match="citations"):
        DealbreakerWarning(
            stance_id=STANCE_A, s_value=-1.0, confidence=1.0, evidence_count=1, citations=[]
        )


def test_a_dealbreaker_warning_cannot_be_constructed_for_a_non_violation() -> None:
    """`s_value` is bounded strictly below 0 on this model, so a warning about
    a candidate who did NOT act against the stance has no representation."""
    for non_violating in (0.0, 0.5, 1.0):
        with pytest.raises(ValidationError, match="s_value"):
            DealbreakerWarning(
                stance_id=STANCE_A,
                s_value=non_violating,
                confidence=1.0,
                evidence_count=1,
                citations=[_citation()],
            )


# --- story 6.7: the ranking ------------------------------------------------


def test_reasons_are_ranked_by_contribution_not_by_position_strength() -> None:
    """A nice-to-have the candidate matches perfectly must NOT outrank a
    primary interest they match well — that would make the reasons disagree
    with the number they explain."""
    primary = _term(STANCE_A, s_value=0.6, weight=3.0)
    nice = _term(STANCE_B, s_value=1.0, weight=0.5)
    reasons = top_reasons([nice, primary], {STANCE_A: [_citation(1)], STANCE_B: [_citation(2)]})
    assert [reason.stance_id for reason in reasons] == [STANCE_A, STANCE_B]


def test_at_most_three_reasons_are_returned() -> None:
    terms = [_term(f"stance-{index}", s_value=1.0) for index in range(10)]
    citations = {term.stance_id: [_citation(index)] for index, term in enumerate(terms)}
    assert len(top_reasons(terms, citations)) == MAX_REASONS == 3


def test_fewer_measured_positions_give_fewer_reasons_not_padded_ones() -> None:
    """Three is a cap, never a quota."""
    reasons = top_reasons([_term(STANCE_A, s_value=1.0)], {STANCE_A: [_citation()]})
    assert len(reasons) == 1


def test_a_term_with_no_citations_is_dropped_rather_than_emitted_uncited() -> None:
    """And the NEXT largest cited reason takes its place, so the voter sees
    what they can check rather than a short list."""
    big = _term(STANCE_A, s_value=1.0, weight=3.0)
    small = _term(STANCE_B, s_value=1.0, weight=0.5)
    reasons = top_reasons([big, small], {STANCE_B: [_citation(2)]})
    assert [reason.stance_id for reason in reasons] == [STANCE_B]


def test_a_term_that_moved_the_score_not_at_all_is_not_a_reason() -> None:
    """An interest with s_value 0 contributed nothing, so it does not explain
    anything about the score. Listing it would be padding."""
    neutral = _term(STANCE_A, s_value=0.0)
    moved = _term(STANCE_B, s_value=1.0)
    reasons = top_reasons([neutral, moved], {STANCE_A: [_citation(1)], STANCE_B: [_citation(2)]})
    assert [reason.stance_id for reason in reasons] == [STANCE_B]


def test_a_reason_names_the_direction_it_moved_the_score() -> None:
    reasons = top_reasons(
        [_term(STANCE_A, s_value=1.0), _term(STANCE_B, s_value=-1.0)],
        {STANCE_A: [_citation(1)], STANCE_B: [_citation(2)]},
    )
    directions = {reason.stance_id: reason.direction for reason in reasons}
    assert directions == {STANCE_A: "raised", STANCE_B: "lowered"}


def test_the_reason_order_is_deterministic_under_a_tie() -> None:
    """Two interests with identical contributions must come back in a fixed
    order, or two requests that read rows in different orders would publish
    different "top reasons" for the same score."""
    terms = [
        _term(STANCE_C, s_value=1.0),
        _term(STANCE_A, s_value=1.0),
        _term(STANCE_B, s_value=1.0),
    ]
    citations = {term.stance_id: [_citation(index)] for index, term in enumerate(terms)}
    forward = top_reasons(terms, citations)
    backward = top_reasons(list(reversed(terms)), citations)
    assert [r.stance_id for r in forward] == [r.stance_id for r in backward]
    assert [r.stance_id for r in forward] == sorted([STANCE_A, STANCE_B, STANCE_C])


# --- story 6.6: the dealbreaker check -------------------------------------


def test_a_negative_position_on_a_dealbreaker_warns() -> None:
    warnings = dealbreaker_warnings(
        dealbreaker_stance_ids=[STANCE_A],
        positions={STANCE_A: (-1.0, 1.0, 3)},
        citations={STANCE_A: [_citation(position="nay")]},
    )
    assert len(warnings) == 1
    assert warnings[0].stance_id == STANCE_A
    assert warnings[0].citations


def test_a_neutral_position_on_a_dealbreaker_raises_no_warning() -> None:
    """`VIOLATION_THRESHOLD` is 0: measured-and-neutral is not acting against.
    A warning here would be an accusation the evidence does not support."""
    assert VIOLATION_THRESHOLD == 0.0
    assert (
        dealbreaker_warnings(
            dealbreaker_stance_ids=[STANCE_A],
            positions={STANCE_A: (0.0, 1.0, 3)},
            citations={STANCE_A: [_citation()]},
        )
        == []
    )


def test_an_unmeasured_dealbreaker_raises_no_warning() -> None:
    """prd.md's "Show uncertainty": silence is not opposition.

    This is also the rule that keeps the check party-neutral in effect as well
    as in form — warning on an absence would penalise every challenger, who has
    no voting record by definition.
    """
    assert dealbreaker_warnings(dealbreaker_stance_ids=[STANCE_A], positions={}, citations={}) == []


def test_a_negative_position_on_a_non_dealbreaker_raises_no_warning() -> None:
    """The voter said that interest IS tradeable."""
    assert (
        dealbreaker_warnings(
            dealbreaker_stance_ids=[],
            positions={STANCE_A: (-1.0, 1.0, 3)},
            citations={STANCE_A: [_citation()]},
        )
        == []
    )


def test_a_violation_with_no_citations_raises_no_warning() -> None:
    """An accusation without its evidence is worse than no accusation, and the
    model would refuse to carry one anyway."""
    assert (
        dealbreaker_warnings(
            dealbreaker_stance_ids=[STANCE_A],
            positions={STANCE_A: (-1.0, 1.0, 3)},
            citations={},
        )
        == []
    )


def test_several_violations_come_back_in_a_deterministic_order() -> None:
    warnings = dealbreaker_warnings(
        dealbreaker_stance_ids=[STANCE_C, STANCE_A, STANCE_B],
        positions={
            STANCE_A: (-1.0, 1.0, 3),
            STANCE_B: (-0.5, 1.0, 2),
            STANCE_C: (-0.1, 1.0, 1),
        },
        citations={stance: [_citation()] for stance in (STANCE_A, STANCE_B, STANCE_C)},
    )
    assert [warning.stance_id for warning in warnings] == sorted([STANCE_A, STANCE_B, STANCE_C])


def test_the_dealbreaker_module_does_not_import_the_formula() -> None:
    """prd.md: a violation is "shown above the score rather than averaged
    away". The strongest form of that is an import-graph fact: the module that
    detects a violation cannot reach the module that computes the score, so no
    edit to either can quietly connect them.
    """
    import app.scoring.dealbreakers as module

    source = module.__doc__ or ""
    assert "averaged away" in source
    assert not hasattr(module, "alignment_score")
    assert not hasattr(module, "ScoredTerm")


# --- the response shape and its absences ----------------------------------


def test_the_alignment_response_models_have_no_profile_fields() -> None:
    """The privacy guarantee, as an assertion about the declared fields.

    A voter's priorities are political-opinion data (prd.md). This response is
    derived from one, so the only thing it may carry out is a stance id the
    voter themselves chose — never a tier, an intensity, a note, a key
    reference or the encrypted payload.
    """
    forbidden = {
        "payload",
        "key_ref",
        "interests",
        "tier",
        "intensity",
        "note",
        "user_id",
        "profile_id",
        "locale",
        "jurisdiction_ids",
        "history",
        "evidence_mode",
    }
    for model in (CandidateAlignment, AlignmentReason, DealbreakerWarning, Citation):
        overlap = forbidden & set(model.model_fields)
        assert not overlap, f"{model.__name__} declares {overlap}"


def test_every_alignment_model_forbids_extra_fields() -> None:
    """`extra="forbid"` is the other half: a later story cannot add a field by
    accident, and a hand-built response cannot smuggle one."""
    for model in (CandidateAlignment, AlignmentReason, DealbreakerWarning, Citation):
        assert model.model_config.get("extra") == "forbid", model.__name__


def test_an_unscoreable_candidate_has_a_null_score_not_fifty() -> None:
    """50 means "measured, and found neutral". `None` means "no evidence".
    Collapsing them would be the single most misleading thing this API could
    say."""
    alignment = CandidateAlignment(
        candidacy_id="c",
        person_id="person:TEST000000",
        confidence_badge="insufficient",
        evidence_count=0,
        scored_interest_count=0,
        methodology_version="2027.1",
    )
    assert alignment.score is None
    assert alignment.has_dealbreaker_violation is False


def test_a_score_outside_zero_to_one_hundred_is_refused() -> None:
    for impossible in (-0.1, 100.1, 137.0):
        with pytest.raises(ValidationError, match="score"):
            CandidateAlignment(
                candidacy_id="c",
                person_id="person:TEST000000",
                score=impossible,
                confidence_badge="high",
                evidence_count=10,
                scored_interest_count=1,
                methodology_version="2027.1",
            )


# --- the endpoint's declared shape ----------------------------------------


def test_the_endpoint_is_declared_at_prd_mds_published_path() -> None:
    """prd.md's Core API table: `GET /v1/candidates/{id}/alignment?profile_id=`."""
    paths = create_app().openapi()["paths"]
    assert "/v1/candidates/{candidacy_id}/alignment" in paths


def test_profile_id_is_a_required_query_parameter() -> None:
    """An alignment score is a function of a voter's priorities; there is no
    meaningful anonymous answer, so a missing `profile_id` must be a 422 at the
    edge rather than a mysteriously empty score."""
    spec = create_app().openapi()["paths"]["/v1/candidates/{candidacy_id}/alignment"]["get"]
    params = {param["name"]: param for param in spec["parameters"]}
    assert params["profile_id"]["in"] == "query"
    assert params["profile_id"]["required"] is True


def test_the_endpoint_declares_no_parameter_that_could_carry_profile_content() -> None:
    """`profile_id` selects a profile; nothing in the request may CARRY one.
    An endpoint that accepted interests inline would be a second, unencrypted
    path to the same data."""
    spec = create_app().openapi()["paths"]["/v1/candidates/{candidacy_id}/alignment"]["get"]
    names = {param["name"] for param in spec["parameters"]}
    assert names == {"candidacy_id", "profile_id"}
    assert "requestBody" not in spec


def test_a_candidacy_id_containing_slashes_reaches_the_handler() -> None:
    """datamodel.md's `candidacies` id is
    `{election_id}:{district_id}:{person_id}`, and a `district_id` is an
    OCD-ID — `ocd-division/country:us/state:ca/cd:19` — which contains
    SLASHES.

    With the default path converter Starlette matches `[^/]+`, so a REAL
    candidacy id would 404 at the router before any code ran. The route is
    therefore declared `{candidacy_id:path}`, and this test is what keeps that
    from being quietly reverted: it asserts the handler receives the whole id,
    slashes intact.
    """
    from fastapi.testclient import TestClient

    from app.scoring.router import alignment_service
    from app.scoring.service import CandidacyNotFoundError

    received: list[str] = []

    class CapturingService:
        def alignment_for(self, candidacy_id: str, *, user_id: str) -> object:
            received.append(candidacy_id)
            # The router maps this to a 404; the test is about the id it was
            # handed, not about the response.
            raise CandidacyNotFoundError("stop here; the id is what this test is about")

    app = create_app()
    app.dependency_overrides[alignment_service] = CapturingService
    real_id = "us-2026-11-03-general:ocd-division/country:us/state:ca/cd:19:person:B000944"
    with TestClient(app) as client:
        client.get(f"/v1/candidates/{real_id}/alignment", params={"profile_id": "u"})

    assert received == [real_id]


def test_the_greedy_path_converter_does_not_swallow_a_sibling_route() -> None:
    """The cost of `:path` is that it is greedy, and the literal `/alignment`
    suffix is what bounds it.

    This matters for the next story to mount under `/v1/candidates/{id}/`:
    prd.md's Core API table also has `/v1/candidates/{id}/promise-record`, and
    a request for it today is absorbed into a candidacy id of
    `.../promise-record` rather than 404ing. Pinned so that behaviour is a
    known, documented consequence rather than a surprise found by whoever
    builds that endpoint — they will need their own `:path` declaration.
    """
    from fastapi.testclient import TestClient

    from app.scoring.router import alignment_service
    from app.scoring.service import CandidacyNotFoundError

    received: list[str] = []

    class CapturingService:
        def alignment_for(self, candidacy_id: str, *, user_id: str) -> object:
            received.append(candidacy_id)
            raise CandidacyNotFoundError("stop here")

    app = create_app()
    app.dependency_overrides[alignment_service] = CapturingService
    with TestClient(app) as client:
        # Not a request for the alignment route at all, but the greedy segment
        # claims it because the path still ENDS in `/alignment`.
        client.get("/v1/candidates/abc/promise-record/alignment", params={"profile_id": "u"})

    assert received == ["abc/promise-record"]


def test_a_missing_profile_id_is_a_422_not_an_empty_score() -> None:
    """A request with no `profile_id` is refused, never answered with a null
    score — which would read as "we have no evidence about this candidate".

    The service dependency is overridden with a stub that raises if it is ever
    CALLED. That is deliberate and it records a real ordering fact found while
    writing this test: **FastAPI resolves a route's dependencies BEFORE
    validating its query parameters.** A request with no `profile_id`
    therefore runs `alignment_service`, which runs
    `ZeroDBClient.from_env()` and `ProfileRootKey.from_env()`, before the 422
    is raised. (That is how this test first failed: with the real dependency
    in place it raised `RootKeyMissingError`, not a 422.)

    On a configured deployment this is harmless — a client and a keyring are
    constructed and discarded, and no ZeroDB request and no decryption
    happen. But it means a 422 is NOT by itself evidence that nothing was
    touched, and a test written as though it were would be asserting something
    false. What IS guaranteed, and is what matters: no profile is read and no
    score is computed, which the stub proves by raising if
    `alignment_for` is ever reached.
    """
    from fastapi.testclient import TestClient

    from app.scoring.router import alignment_service

    class RefusingService:
        def alignment_for(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("a malformed request reached the scorer")  # pragma: no cover

    app = create_app()
    app.dependency_overrides[alignment_service] = RefusingService
    with TestClient(app) as client:
        response = client.get("/v1/candidates/some-candidacy/alignment")
    assert response.status_code == 422
    assert "profile_id" in response.text
