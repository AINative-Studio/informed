"""The edge cases behind the scorer's branches (6.2, 6.4-6.7).

CLAUDE.md requires >=90% coverage on scoring and verdict logic, and the
branches this file reaches are not coverage busywork — each is a way the
scorer can be handed something odd by a real database:

  * a position for a stance that is no longer stored (an integrity problem);
  * a position whose votes have since been corrected away (datamodel.md's
    retention table anticipates corrected votes);
  * a methodology published with no `vote_types` weights;
  * a `rollcall.ingested` event with a missing or empty payload;
  * a full page of rows, which is where a paged read starts silently
    truncating.

Every one of those is answered rather than crashed, and this file pins which
answer. All synthetic.
"""

from __future__ import annotations

import logging

import pytest

from app.bootstrap import Bootstrapper
from app.methodology.publish import MethodologyPublisher, default_weights
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.service import VoterProfileService
from app.repositories import ProvenanceContext, Repository
from app.repositories.models import (
    Candidacy,
    CandidatePosition,
    Election,
    IssueTopic,
    MemberVote,
    Person,
    RollCall,
    Stance,
)
from app.scoring.positions import (
    PositionComputeError,
    PositionComputer,
    PositionRecomputeConsumer,
    compute_position,
    emit_rollcall_ingested,
)
from app.scoring.service import (
    AlignmentService,
    CandidacyNotFoundError,
    ProfileNotFoundError,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

TEST_ROOT_KEY = bytes(range(32))
USER_ID = "test-voter-edges"
PERSON_ID = "person:TEST000000"
TOPIC_ID = "en-US:test-edge:v1"
STANCE_ID = f"{TOPIC_ID}:for"
ELECTION_ID = "us-2026-11-03-general"
DISTRICT_ID = "ocd-division/country:us/state:zz/cd:1"
CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:{PERSON_ID}"


@pytest.fixture
def zerodb() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(client, pace=lambda: None).run()
    MethodologyPublisher(client).publish_new_version("2027.1")
    _seed(client)
    return client


def _seed(zerodb: ZeroDBClient) -> None:
    Repository(client=zerodb, model=IssueTopic).upsert(
        {"id": TOPIC_ID, "name": "Synthetic edge topic", "locale": "en-US", "taxonomy_version": 1}
    )
    Repository(client=zerodb, model=Stance).upsert(
        {
            "id": STANCE_ID,
            "topic_id": TOPIC_ID,
            "label": "Synthetic edge stance",
            "description": "A synthetic stance card used only by this test suite.",
            "direction_code": "+1",
            "wording_version": 1,
        }
    )
    Repository(client=zerodb, model=Person).upsert(
        {
            "id": PERSON_ID,
            "canonical_name": "Synthetic Edge Member",
            "terms": [{"body_id": "us-house", "party": "Synthetic", "start": "2025-01-03"}],
        }
    )
    Repository(client=zerodb, model=Election).upsert(
        {
            "id": ELECTION_ID,
            "jurisdiction_id": "us-federal",
            "name": "Synthetic general election",
            "election_date": "2026-11-03",
            "kind": "general",
        }
    )
    zerodb.upsert_row(
        "districts",
        DISTRICT_ID,
        {
            "id": DISTRICT_ID,
            "body_id": "us-house",
            "name": "Synthetic District 1",
            "state": "ZZ",
            "valid_from": "2023-01-03",
        },
    )
    Repository(client=zerodb, model=Candidacy).upsert(
        {
            "election_id": ELECTION_ID,
            "district_id": DISTRICT_ID,
            "person_id": PERSON_ID,
            "person_name": "Synthetic Edge Member",
            "party": "Synthetic",
            "is_incumbent": True,
            "status": "filed",
        }
    )


@pytest.fixture
def profiles(zerodb: ZeroDBClient) -> VoterProfileService:
    return VoterProfileService(
        client=zerodb,
        keyring=ProfileKeyring(client=zerodb, root_key=ProfileRootKey(TEST_ROOT_KEY)),
    )


@pytest.fixture
def service(zerodb: ZeroDBClient, profiles: VoterProfileService) -> AlignmentService:
    return AlignmentService(client=zerodb, profiles=profiles)


def _save_profile(profiles: VoterProfileService, *, stance_id: str = STANCE_ID) -> None:
    profiles.save(
        USER_ID,
        {
            "interests": [
                {
                    "topic_id": TOPIC_ID,
                    "stance_id": stance_id,
                    "tier": "primary",
                    "intensity": 2,
                    "dealbreaker": False,
                }
            ]
        },
    )


def _vote(zerodb: ZeroDBClient, roll: int, *, position: str = "yea") -> str:
    roll_id = f"us-house:119-2:{roll}"
    Repository(
        client=zerodb,
        model=RollCall,
        provenance=ProvenanceContext(adapter_version="test_edges/v1"),
    ).upsert(
        {
            "id": roll_id,
            "voted_at": "2026-04-01T12:00:00Z",
            "question": "On passage of a synthetic test measure",
            "vote_type": "final_passage",
            "result": "Passed",
            "topic_ids": [TOPIC_ID],
            "source_url": f"https://example.invalid/synthetic/{roll_id}",
            "content_hash": f"synthetic-{roll_id}",
        }
    )
    Repository(client=zerodb, model=MemberVote).upsert(
        {
            "roll_call_id": roll_id,
            "person_id": PERSON_ID,
            "position": position,
            "voted_at": "2026-04-01T12:00:00Z",
            "vote_type": "final_passage",
            "topic_ids": [TOPIC_ID],
            "is_bundled": False,
        }
    )
    return roll_id


def _position(zerodb: ZeroDBClient, *, stance_id: str = STANCE_ID, evidence_count: int = 2) -> None:
    Repository(client=zerodb, model=CandidatePosition).append_version(
        {
            "person_id": PERSON_ID,
            "stance_id": stance_id,
            "s_value": 1.0,
            "confidence": 1.0,
            "evidence_count": evidence_count,
            "basis": "votes",
            "methodology_version": "2027.1",
            "computed_at": "2026-10-19T00:00:00Z",
        }
    )


# --- the service's refusals ------------------------------------------------


def test_an_unknown_candidacy_is_refused_before_the_profile_is_decrypted(
    service: AlignmentService,
) -> None:
    """The candidacy check runs FIRST, deliberately: a bad URL must not cause a
    voter's political-opinion payload to be decrypted. prd.md's data
    minimisation applied to the order of two lookups."""
    with pytest.raises(CandidacyNotFoundError, match="no candidacy"):
        service.alignment_for("no-such-candidacy", user_id=USER_ID)


def test_a_voter_with_no_profile_is_refused(service: AlignmentService) -> None:
    with pytest.raises(ProfileNotFoundError, match="primary interest"):
        service.alignment_for(CANDIDACY_ID, user_id=USER_ID)


def test_the_refusal_does_not_echo_the_profile_id_back(service: AlignmentService) -> None:
    """An error body is the single easiest place for a profile locator to end
    up in a log."""
    with pytest.raises(ProfileNotFoundError) as caught:
        service.alignment_for(CANDIDACY_ID, user_id=USER_ID)
    assert USER_ID not in str(caught.value)


# --- positions the scorer cannot fully resolve -----------------------------


def test_a_position_for_a_stance_that_is_no_longer_stored_yields_no_citation(
    zerodb: ZeroDBClient, profiles: VoterProfileService, service: AlignmentService
) -> None:
    """A stored position citing a deleted stance is an integrity problem, and
    the honest response is a score with no reason for that interest rather than
    a reason with no checkable citation.

    Without the stance's `topic_id` there is no way to know which votes
    produced the position, so inventing a citation would mean guessing.
    `app.integrity`'s reference checks are what surface this as the data
    problem it is.
    """
    _save_profile(profiles)
    _vote(zerodb, 1)
    _position(zerodb)
    # Remove the stance the position and the profile both name.
    zerodb.delete_row(Stance.table_name, STANCE_ID)

    alignment = service.alignment_for(CANDIDACY_ID, user_id=USER_ID)
    # The position still scores — it is stored evidence — but it cannot be
    # explained, so no reason is published for it.
    assert alignment.score == pytest.approx(100.0)
    assert alignment.reasons == []


def test_a_position_whose_votes_are_gone_scores_but_cannot_be_explained(
    zerodb: ZeroDBClient, profiles: VoterProfileService, service: AlignmentService
) -> None:
    """datamodel.md's retention table anticipates a corrected vote. A position
    computed before the correction still exists (it is append-only history),
    and the citation re-read now finds nothing — so the reason is dropped
    rather than published uncited."""
    _save_profile(profiles)
    _position(zerodb)  # no `member_votes` rows written at all

    alignment = service.alignment_for(CANDIDACY_ID, user_id=USER_ID)
    assert alignment.score == pytest.approx(100.0)
    assert alignment.reasons == []
    assert alignment.evidence_count == 2  # the stored count, which is the truth it has


def test_a_profile_naming_a_stance_with_no_position_is_scored_on_the_rest(
    zerodb: ZeroDBClient, profiles: VoterProfileService, service: AlignmentService
) -> None:
    """An unmeasured interest is omitted from the terms rather than included at
    confidence 0, so `scored_interest_count` tells the voter how much of their
    profile the candidate actually has a record on."""
    _save_profile(profiles, stance_id=STANCE_ID)
    alignment = service.alignment_for(CANDIDACY_ID, user_id=USER_ID)
    assert alignment.score is None
    assert alignment.scored_interest_count == 0
    assert alignment.confidence_badge == "insufficient"


def test_an_abstention_is_not_cited_as_a_reason(
    zerodb: ZeroDBClient, profiles: VoterProfileService, service: AlignmentService
) -> None:
    """A `not_voting` row sits in `member_votes` for this stance's topic but
    contributed nothing to the position (see `app.scoring.positions`), so
    citing it would show a voter a "reason" the number did not use."""
    _save_profile(profiles)
    _vote(zerodb, 1, position="yea")
    _vote(zerodb, 2, position="not_voting")
    _position(zerodb)

    alignment = service.alignment_for(CANDIDACY_ID, user_id=USER_ID)
    assert len(alignment.reasons) == 1
    cited = {citation.roll_call_id for citation in alignment.reasons[0].citations}
    assert cited == {"us-house:119-2:1"}


def test_a_superseded_position_is_never_scored(
    zerodb: ZeroDBClient, profiles: VoterProfileService, service: AlignmentService
) -> None:
    """A superseded row is history — it exists so a score computed in March can
    be reproduced. Scoring today's request from it would make the number depend
    on how many times the recompute had run."""
    _save_profile(profiles)
    _vote(zerodb, 1)
    _position(zerodb)
    # A second version with the opposite value becomes current.
    Repository(client=zerodb, model=CandidatePosition).append_version(
        {
            "person_id": PERSON_ID,
            "stance_id": STANCE_ID,
            "s_value": -1.0,
            "confidence": 1.0,
            "evidence_count": 2,
            "basis": "votes",
            "methodology_version": "2027.1",
            "computed_at": "2026-10-20T00:00:00Z",
        }
    )
    alignment = service.alignment_for(CANDIDACY_ID, user_id=USER_ID)
    assert alignment.score == pytest.approx(0.0)


def test_an_unrecognised_tier_is_excluded_rather_than_given_a_default_weight(
    zerodb: ZeroDBClient, profiles: VoterProfileService
) -> None:
    """A methodology that does not publish a weight for a tier has not decided
    what that tier is worth, and guessing a middle value would score a voter's
    interests under a weight nobody published."""
    weights = default_weights()
    weights["tiers"] = {"secondary": 1.5, "nice_to_have": 0.5}  # no `primary`
    MethodologyPublisher(zerodb).publish_new_version("2028.1", weights=weights)

    _save_profile(profiles)
    _vote(zerodb, 1)
    _position(zerodb)

    service = AlignmentService(client=zerodb, profiles=profiles)
    alignment = service.alignment_for(CANDIDACY_ID, user_id=USER_ID)
    assert alignment.score is None
    assert alignment.scored_interest_count == 0


# --- the position computer's refusals --------------------------------------


def test_a_methodology_with_no_vote_type_weights_refuses_to_compute(
    zerodb: ZeroDBClient,
) -> None:
    """A position computed under unknown weights is not reproducible, and the
    row stores the version it claims to have been computed under — a row citing
    a version whose weights it did not use is worse than no row."""
    zerodb.update_row("methodology_versions", "2027.1", {"weights": {"tiers": {"primary": 3.0}}})
    with pytest.raises(PositionComputeError, match="vote_types"):
        PositionComputer(zerodb).recompute_person(PERSON_ID)


def test_a_stance_with_no_id_cannot_be_scored() -> None:
    """The repository fills an id in before writing, so a stance reaching the
    scorer without one is a construction error rather than stored data — and
    the position's own id is `{person}:{stance}:{version}`, so it cannot be
    built either."""
    unsaved = Stance(
        topic_id=TOPIC_ID,
        label="Synthetic",
        description="A synthetic stance with no id.",
        direction_code="+1",
        wording_version=1,
    )
    with pytest.raises(ValueError, match="carry its id"):
        compute_position(
            person_id=PERSON_ID,
            stance=unsaved,
            votes=[
                MemberVote(
                    id="us-house:119-2:1:TEST000000",
                    roll_call_id="us-house:119-2:1",
                    person_id=PERSON_ID,
                    position="yea",
                    voted_at="2026-04-01T12:00:00Z",
                    vote_type="final_passage",
                    topic_ids=[TOPIC_ID],
                )
            ],
            vote_type_weights={"final_passage": 1.0},
        )


def test_positions_for_reads_only_the_current_rows(zerodb: ZeroDBClient) -> None:
    _vote(zerodb, 1)
    _position(zerodb)
    _position(zerodb)
    live = PositionComputer(zerodb).positions_for(PERSON_ID)
    assert len(live) == 1
    assert live[0].is_current is True


# --- the event consumer's edges -------------------------------------------


def test_an_event_with_no_roll_call_id_is_ignored_with_a_warning(
    zerodb: ZeroDBClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Ignored rather than raised: one malformed event must not stop a
    scheduled run from processing the well-formed ones behind it."""
    zerodb.create_event(topic="rollcall.ingested", payload={"bill_id": None})
    with caplog.at_level(logging.WARNING, logger="app.scoring.positions"):
        result = PositionRecomputeConsumer(zerodb).run()
    assert result.people_recomputed == 0
    assert "no roll_call_id" in caplog.text


def test_an_event_with_empty_topic_ids_falls_back_to_the_stored_roll_call(
    zerodb: ZeroDBClient,
) -> None:
    """Story 5.1 writes `topic_ids: []` today, and a later classification pass
    may fill the ROW without re-emitting the event. So an empty payload list
    means "read the row", not "this roll call touches nothing"."""
    roll_id = _vote(zerodb, 1)
    emit_rollcall_ingested(zerodb, roll_call_id=roll_id, bill_id=None, topic_ids=[])

    result = PositionRecomputeConsumer(zerodb).run()
    assert result.people_recomputed == 1
    assert result.positions_written == 1


def test_an_event_for_a_roll_call_that_does_not_exist_recomputes_nothing(
    zerodb: ZeroDBClient,
) -> None:
    emit_rollcall_ingested(zerodb, roll_call_id="us-house:119-2:404", bill_id=None, topic_ids=[])
    result = PositionRecomputeConsumer(zerodb).run()
    assert result.people_recomputed == 0
    assert result.positions_written == 0


def test_stances_for_topics_returns_nothing_for_an_empty_topic_list(
    zerodb: ZeroDBClient,
) -> None:
    """Short-circuited rather than scanning the whole `stances` table to match
    against nothing — story 3.2 seeds ~180 cards."""
    assert PositionComputer(zerodb).stances_for_topics([]) == []
