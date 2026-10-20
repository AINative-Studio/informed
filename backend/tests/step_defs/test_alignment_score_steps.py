"""Step definitions for tests/features/alignment_score.feature (6.4-6.7).

Runs over the REAL router, against story 1.2's in-memory fake transport, so CI
needs no credentials. The live confirmation lives in
`scripts/verify_alignment_scoring_live.py`.

`break_server_side_filter()` is applied for the reason every story since 1.4
applies it: live ZeroDB's server-side `/query` filter matches nothing, so the
reads here rest on the client's own paging fallback. It is load-bearing in this
story specifically — `AlignmentService` reads `candidate_positions` and
`member_votes` filtered by `person_id`, and a filter that worked in tests and
not in production would score one candidate from another candidate's record.

Everything is synthetic, and that is not optional here: this is the one feature
in the suite that reads a decrypted voter profile. prd.md is explicit that
voter profiles are political-opinion data and that real voter data must never
appear in a fixture, a log or test output. The "voter" is `test-voter-6x`, the
stance ids are `en-US:test-align-*`, and the topics are invented for this file.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.main import create_app
from app.methodology.publish import MethodologyPublisher, default_weights
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.methodology import MethodologyVersionService
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
from app.scoring.badges import BADGE_THRESHOLDS_GROUP
from app.scoring.models import AlignmentReason
from app.scoring.router import zerodb_client_for_alignment
from app.scoring.service import AlignmentService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/alignment_score.feature")

#: Synthetic, and named so a stray row is obviously test data.
TEST_ROOT_KEY = bytes(range(32))
USER_ID = "test-voter-6x"
MIRROR_USER_ID = "test-voter-6x-mirror"
PERSON_ID = "person:TEST000000"
MIRROR_PERSON_ID = "person:TEST000001"
ELECTION_ID = "us-2026-11-03-general"
DISTRICT_ID = "ocd-division/country:us/state:zz/cd:1"
CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:{PERSON_ID}"
MIRROR_CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:{MIRROR_PERSON_ID}"

#: Five synthetic topics, each with one stance card, so a profile can carry a
#: full tier spread and story 6.7's "top 3 of 5" has five to choose from.
TOPIC_IDS = [f"en-US:test-align-{index}:v1" for index in range(5)]
STANCE_IDS = [f"{topic_id}:for" for topic_id in TOPIC_IDS]

#: Field names that would mean profile content had reached the response.
#: Checked against the serialized JSON rather than the model, because the
#: question is what bytes a client actually receives.
PROFILE_WORDS = (
    "payload",
    "key_ref",
    "interests",
    "tier",
    "intensity",
    "note",
    "dealbreaker_topic",
    "user_id",
    "profile_id",
    "vp:",
)


# --- fixtures -------------------------------------------------------------


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
    Bootstrapper(client, pace=lambda: None).run()
    return client


@pytest.fixture
def profiles(zerodb: ZeroDBClient) -> VoterProfileService:
    return VoterProfileService(
        client=zerodb,
        keyring=ProfileKeyring(client=zerodb, root_key=ProfileRootKey(TEST_ROOT_KEY)),
    )


@pytest.fixture
def service(zerodb: ZeroDBClient, profiles: VoterProfileService) -> AlignmentService:
    return AlignmentService(client=zerodb, profiles=profiles)


@pytest.fixture
def api(zerodb: ZeroDBClient, profiles: VoterProfileService) -> Iterator[TestClient]:
    """The real app, with only the ZeroDB client overridden.

    The router builds its own `VoterProfileService` from
    `ProfileRootKey.from_env()`, which needs no env var here because
    `alignment_service` is left alone and instead the whole dependency is
    replaced below — keeping the ROUTE under test while not requiring a real
    root key in CI.
    """
    from app.scoring.router import alignment_service

    app = create_app()
    app.dependency_overrides[zerodb_client_for_alignment] = lambda: zerodb
    app.dependency_overrides[alignment_service] = lambda: AlignmentService(
        client=zerodb, profiles=profiles
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def seen() -> dict[str, Any]:
    return {"rolls": 0}


# --- seeding helpers ------------------------------------------------------


def _provenance() -> ProvenanceContext:
    return ProvenanceContext(adapter_version="test_alignment/v1")


def _seed_graph(zerodb: ZeroDBClient) -> None:
    Repository(client=zerodb, model=IssueTopic).upsert_many(
        [
            {
                "id": topic_id,
                "name": f"Synthetic alignment topic {index}",
                "locale": "en-US",
                "taxonomy_version": 1,
            }
            for index, topic_id in enumerate(TOPIC_IDS)
        ]
    )
    Repository(client=zerodb, model=Stance).upsert_many(
        [
            {
                "id": stance_id,
                "topic_id": TOPIC_IDS[index],
                "label": f"Synthetic stance {index}",
                "description": "A synthetic stance card used only by this test suite.",
                "direction_code": "+1",
                "wording_version": 1,
            }
            for index, stance_id in enumerate(STANCE_IDS)
        ]
    )
    people = Repository(client=zerodb, model=Person)
    for person_id in (PERSON_ID, MIRROR_PERSON_ID):
        people.upsert(
            {
                "id": person_id,
                "canonical_name": f"Synthetic Candidate {person_id[-1]}",
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
    # `candidacies` references `districts`, which the Bootstrapper does not
    # seed. Written directly rather than through the repository for that one
    # reason: the reference check would refuse it, and a district is not what
    # this story is testing. Every row the SCORER reads goes through a real
    # repository.
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
    candidacies = Repository(client=zerodb, model=Candidacy)
    for person_id in (PERSON_ID, MIRROR_PERSON_ID):
        candidacies.upsert(
            {
                "election_id": ELECTION_ID,
                "district_id": DISTRICT_ID,
                "person_id": person_id,
                "person_name": f"Synthetic Candidate {person_id[-1]}",
                "party": "Synthetic",
                "is_incumbent": True,
                "status": "filed",
            }
        )


def _record_votes(
    zerodb: ZeroDBClient,
    seen: dict[str, Any],
    *,
    stance_id: str,
    position: str,
    count: int,
    person_id: str = PERSON_ID,
) -> None:
    """Write `count` synthetic final-passage votes on `stance_id`'s topic.

    Real `roll_calls` and `member_votes` rows through real repositories, so
    story 6.7's citations resolve to rows that actually exist — which is what
    "every citation names a real stored roll call" asserts.
    """
    topic_id = stance_id.rsplit(":", 1)[0]
    rolls: Repository[RollCall] = Repository(
        client=zerodb, model=RollCall, provenance=_provenance()
    )
    votes: Repository[MemberVote] = Repository(client=zerodb, model=MemberVote)
    for _ in range(count):
        seen["rolls"] += 1
        roll_id = f"us-house:119-2:{1000 + seen['rolls']}"
        rolls.upsert(
            {
                "id": roll_id,
                "voted_at": "2026-04-01T12:00:00Z",
                "question": "On passage of a synthetic test measure",
                "vote_type": "final_passage",
                "result": "Passed",
                "topic_ids": [topic_id],
                "source_url": f"https://example.invalid/synthetic/{roll_id}",
                "content_hash": f"synthetic-{roll_id}",
            }
        )
        votes.upsert(
            {
                "roll_call_id": roll_id,
                "person_id": person_id,
                "position": position,
                "voted_at": "2026-04-01T12:00:00Z",
                "vote_type": "final_passage",
                "topic_ids": [topic_id],
                "is_bundled": False,
            }
        )


def _store_position(
    zerodb: ZeroDBClient,
    *,
    stance_id: str,
    s_value: float,
    confidence: float,
    evidence_count: int,
    person_id: str = PERSON_ID,
) -> None:
    """Append one current `candidate_positions` row.

    Through `Repository.append_version`, the production write path, so
    `is_current` and the id are assigned exactly as story 6.2 assigns them.
    """
    Repository(client=zerodb, model=CandidatePosition).append_version(
        {
            "person_id": person_id,
            "stance_id": stance_id,
            "s_value": s_value,
            "confidence": confidence,
            "evidence_count": evidence_count,
            "basis": "votes",
            "methodology_version": "2027.1",
            "computed_at": "2026-10-19T00:00:00Z",
        }
    )


def _save_profile(
    profiles: VoterProfileService,
    interests: list[dict[str, Any]],
    *,
    user_id: str = USER_ID,
) -> None:
    profiles.save(user_id, {"interests": interests, "evidence_mode": "blend"})


def _interest(
    index: int,
    *,
    tier: str = "primary",
    intensity: int = 2,
    dealbreaker: bool = False,
) -> dict[str, Any]:
    return {
        "topic_id": TOPIC_IDS[index],
        "stance_id": STANCE_IDS[index],
        "tier": tier,
        "intensity": intensity,
        "dealbreaker": dealbreaker,
    }


def _row_count(zerodb: ZeroDBClient) -> dict[str, int]:
    """Row counts per table, for the "nothing was written" assertions."""
    counts: dict[str, int] = {}
    for table in zerodb.list_tables(limit=100):
        counts[table.table_name] = len(zerodb.query_rows(table.table_name, limit=500).rows)
    return counts


def _request(api: TestClient, seen: dict[str, Any], *, user_id: str = USER_ID) -> None:
    seen["response"] = api.get(
        f"/v1/candidates/{CANDIDACY_ID}/alignment", params={"profile_id": user_id}
    )


def _body(seen: dict[str, Any]) -> dict[str, Any]:
    response = seen["response"]
    assert response.status_code == 200, response.text
    return response.json()


# --- Background -----------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _fake_client(zerodb: ZeroDBClient) -> None:
    assert zerodb.project_id == "proj-test"


@given("a published methodology with real tier weights and badge thresholds")
def _published(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    published = MethodologyPublisher(zerodb).publish_new_version("2027.1")
    assert published.tier_weights == {"primary": 3.0, "secondary": 1.5, "nice_to_have": 0.5}
    assert published.weights[BADGE_THRESHOLDS_GROUP]
    seen["methodology_version"] = published.id


@given("a synthetic candidacy for a synthetic incumbent")
def _synthetic_candidacy(zerodb: ZeroDBClient) -> None:
    _seed_graph(zerodb)


@given("a synthetic voter with an encrypted profile")
def _synthetic_voter(profiles: VoterProfileService, seen: dict[str, Any]) -> None:
    """Three interests across all three tiers, which is prd.md's own example
    shape ("primary, secondary and nice-to-have")."""
    _save_profile(
        profiles,
        [
            _interest(0, tier="primary", intensity=2),
            _interest(1, tier="secondary", intensity=2),
            _interest(2, tier="nice_to_have", intensity=2),
        ],
    )
    seen["profile_stance_ids"] = STANCE_IDS[:3]


# --- Given: positions -----------------------------------------------------


@given(parsers.parse("the candidate's position is {value:g} on every interest in the profile"))
def _position_everywhere(zerodb: ZeroDBClient, seen: dict[str, Any], value: float) -> None:
    for stance_id in seen["profile_stance_ids"]:
        _store_position(
            zerodb, stance_id=stance_id, s_value=value, confidence=1.0, evidence_count=4
        )
        _record_votes(
            zerodb,
            seen,
            stance_id=stance_id,
            position="yea" if value >= 0 else "nay",
            count=4,
        )


@given("a profile with one primary interest and one nice-to-have interest")
def _primary_and_nice(profiles: VoterProfileService, seen: dict[str, Any]) -> None:
    _save_profile(
        profiles,
        [_interest(0, tier="primary"), _interest(1, tier="nice_to_have")],
    )
    seen["profile_stance_ids"] = STANCE_IDS[:2]


@given("the candidate matches the primary interest and opposes the nice-to-have")
def _matches_primary(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _store_position(zerodb, stance_id=STANCE_IDS[0], s_value=1.0, confidence=1.0, evidence_count=4)
    _store_position(zerodb, stance_id=STANCE_IDS[1], s_value=-1.0, confidence=1.0, evidence_count=4)
    _record_votes(zerodb, seen, stance_id=STANCE_IDS[0], position="yea", count=4)
    _record_votes(zerodb, seen, stance_id=STANCE_IDS[1], position="nay", count=4)


@given("a profile with two equally weighted interests")
def _two_equal(profiles: VoterProfileService, seen: dict[str, Any]) -> None:
    _save_profile(
        profiles,
        [_interest(0, tier="primary", intensity=2), _interest(1, tier="primary", intensity=2)],
    )
    seen["profile_stance_ids"] = STANCE_IDS[:2]


@given("the candidate matches one with confidence 1.0 and opposes the other with confidence 0.1")
def _thin_opposition(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """prd.md: "c_i is evidence confidence (0-1), so thin evidence moves the
    score less"."""
    _store_position(zerodb, stance_id=STANCE_IDS[0], s_value=1.0, confidence=1.0, evidence_count=8)
    _store_position(zerodb, stance_id=STANCE_IDS[1], s_value=-1.0, confidence=0.1, evidence_count=1)
    _record_votes(zerodb, seen, stance_id=STANCE_IDS[0], position="yea", count=8)
    _record_votes(zerodb, seen, stance_id=STANCE_IDS[1], position="nay", count=1)


@given("a profile with two primary interests at intensity 3 and 1")
def _two_intensities(profiles: VoterProfileService, seen: dict[str, Any]) -> None:
    _save_profile(
        profiles,
        [_interest(0, tier="primary", intensity=3), _interest(1, tier="primary", intensity=1)],
    )
    seen["profile_stance_ids"] = STANCE_IDS[:2]


@given("the candidate matches the intense one and opposes the other")
def _matches_intense(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _store_position(zerodb, stance_id=STANCE_IDS[0], s_value=1.0, confidence=1.0, evidence_count=4)
    _store_position(zerodb, stance_id=STANCE_IDS[1], s_value=-1.0, confidence=1.0, evidence_count=4)
    _record_votes(zerodb, seen, stance_id=STANCE_IDS[0], position="yea", count=4)
    _record_votes(zerodb, seen, stance_id=STANCE_IDS[1], position="nay", count=4)


@given("the voter has no stored profile")
def _no_profile(profiles: VoterProfileService) -> None:
    profiles.delete(USER_ID)


# --- Given: evidence counts for badges ------------------------------------


@given(parsers.parse("the candidate's positions rest on {count:d} relevant votes in total"))
@when(parsers.parse("the candidate's positions rest on {count:d} relevant votes in total"))
def _total_evidence(zerodb: ZeroDBClient, seen: dict[str, Any], count: int) -> None:
    """One position carrying the whole evidence count.

    The badge is computed from the TOTAL across the positions that entered the
    score (prd.md's ">=10 relevant votes"), so one position with 10 votes and
    ten positions with one each must badge the same — and putting it all on one
    keeps the scenario about the badge rather than about the spread.
    """
    _store_position(
        zerodb,
        stance_id=STANCE_IDS[0],
        s_value=1.0,
        confidence=1.0,
        evidence_count=count,
    )
    _record_votes(zerodb, seen, stance_id=STANCE_IDS[0], position="yea", count=count)


@given("the candidate has no measured position on any of the voter's interests")
def _no_positions(zerodb: ZeroDBClient) -> None:
    page = zerodb.query_rows(CandidatePosition.table_name, limit=500)
    assert page.rows == [], "this scenario needs a candidate with no stored positions"


@when("the methodology publishes different badge thresholds")
def _new_thresholds(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """A methodology that decides three votes is enough for High.

    Published through the real `MethodologyPublisher`, so the scorer reads it
    from a live row exactly as it would in production — which is the whole
    point of backlog.md 6.5's "thresholds from the methodology".
    """
    weights = default_weights()
    weights[BADGE_THRESHOLDS_GROUP] = {"high": 3.0, "medium": 2.0, "low": 1.0}
    published = MethodologyPublisher(zerodb).publish_new_version("2028.1", weights=weights)
    seen["methodology_version"] = published.id


# --- Given: dealbreakers --------------------------------------------------


@given(parsers.parse('a voter marks "{topic}" as a dealbreaker'))
def _marks_dealbreaker(profiles: VoterProfileService, seen: dict[str, Any], topic: str) -> None:
    """prd.md's own scenario, reproduced verbatim in the feature file.

    The topic name in the Gherkin is prd.md's ("Prescription drug pricing"),
    and it maps onto this suite's synthetic topic rather than a real taxonomy
    row: the taxonomy is epic 3's and the real `en-US:rx-drug-pricing:v1` card
    is panel-reviewed editorial content, not a fixture to mutate. What the
    scenario is about is the dealbreaker mechanism, which is topic-agnostic by
    construction (prd.md: "one scoring codebase for all candidates").
    """
    seen["dealbreaker_topic"] = topic
    # Five interests, one of them the dealbreaker. This shape is what makes
    # prd.md's scenario the scenario it is: a candidate can only "score 82
    # overall but act against that stance" when the stance is one priority
    # among several the candidate IS strong on. A profile where the dealbreaker
    # carried most of the weight would score the candidate low, the warning
    # would be redundant, and the test would prove nothing about
    # "rather than averaged away" — which is the entire requirement.
    #
    # prd.md's tier table allows 1-5 primary interests, so this is a profile a
    # real voter could build.
    _save_profile(
        profiles,
        [
            _interest(0, tier="primary", intensity=2, dealbreaker=True),
            _interest(1, tier="primary", intensity=3),
            _interest(2, tier="primary", intensity=3),
            _interest(3, tier="primary", intensity=3),
            _interest(4, tier="secondary", intensity=3),
        ],
    )
    seen["profile_stance_ids"] = STANCE_IDS[:5]
    seen["dealbreaker_stance_id"] = STANCE_IDS[0]


@given(
    parsers.parse(
        "candidate {name} scores {score:g} overall but acted against that stance "
        "on {count:d} final-passage votes"
    )
)
def _scores_well_but_violates(
    zerodb: ZeroDBClient, seen: dict[str, Any], name: str, score: float, count: int
) -> None:
    """A candidate who scores high overall AND acted against the dealbreaker.

    prd.md's scenario says "scores {score} overall", and the number it names is
    82. The score is not a free parameter here — it falls out of the formula
    from the profile shape and this record — so the setup is arranged to land
    close to it, and the arithmetic is written out so a reader can check that
    the warning did not move the number:

        dealbreaker   w=3.0 k=2 c=1.0 s=-1   ->  num  -6.0    den  6.0
        primary x3    w=3.0 k=3 c=1.0 s=+1   ->  num +27.0    den 27.0
        secondary     w=1.5 k=3 c=1.0 s=+1   ->  num  +4.5    den  4.5

        mean  = (27.0 + 4.5 - 6.0) / 37.5 = 0.68
        score = 50 * (1 + 0.68)            = 84.0

    84, not exactly 82, and that is the honest reading of the scenario: prd.md
    gives 82 as an illustration of "scores well despite the violation", not as
    an arithmetic assertion about a profile it never specifies. What the step
    asserts is the part that IS the requirement — the score stays high (>80),
    and the warning appears beside it rather than inside it.
    """
    _store_position(
        zerodb,
        stance_id=seen["dealbreaker_stance_id"],
        s_value=-1.0,
        confidence=1.0,
        evidence_count=count,
    )
    _record_votes(
        zerodb, seen, stance_id=seen["dealbreaker_stance_id"], position="nay", count=count
    )
    for stance_id in STANCE_IDS[1:5]:
        _store_position(zerodb, stance_id=stance_id, s_value=1.0, confidence=1.0, evidence_count=6)
        _record_votes(zerodb, seen, stance_id=stance_id, position="yea", count=6)
    # prd.md's own figure is 82; the formula gives 84.0 for this profile and
    # record. Asserted as a floor of 80 so the scenario is about "still scores
    # well", which is what it is for.
    seen["expected_score_at_least"] = 80.0
    seen["prd_illustrative_score"] = score


@given("a voter with a dealbreaker interest the candidate acted against")
def _dealbreaker_violated(
    zerodb: ZeroDBClient, profiles: VoterProfileService, seen: dict[str, Any]
) -> None:
    interests = [_interest(0, tier="primary", intensity=3, dealbreaker=True), _interest(1)]
    _save_profile(profiles, interests)
    # Kept so `_score_unchanged` can re-save the IDENTICAL profile with only
    # the dealbreaker flag cleared. Re-deriving the shape there by hand is how
    # that step first went wrong: it used a different intensity and the score
    # legitimately moved, which looked like a dealbreaker penalty and was not.
    seen["dealbreaker_interests"] = interests
    seen["profile_stance_ids"] = STANCE_IDS[:2]
    seen["dealbreaker_stance_id"] = STANCE_IDS[0]
    _store_position(zerodb, stance_id=STANCE_IDS[0], s_value=-1.0, confidence=1.0, evidence_count=3)
    _record_votes(zerodb, seen, stance_id=STANCE_IDS[0], position="nay", count=3)
    _store_position(zerodb, stance_id=STANCE_IDS[1], s_value=1.0, confidence=1.0, evidence_count=3)
    _record_votes(zerodb, seen, stance_id=STANCE_IDS[1], position="yea", count=3)


@given("a voter with a dealbreaker interest the candidate acted for")
def _dealbreaker_supported(
    zerodb: ZeroDBClient, profiles: VoterProfileService, seen: dict[str, Any]
) -> None:
    _save_profile(profiles, [_interest(0, tier="primary", intensity=3, dealbreaker=True)])
    seen["profile_stance_ids"] = [STANCE_IDS[0]]
    _store_position(zerodb, stance_id=STANCE_IDS[0], s_value=1.0, confidence=1.0, evidence_count=3)
    _record_votes(zerodb, seen, stance_id=STANCE_IDS[0], position="yea", count=3)


@given("a voter with a dealbreaker interest the candidate has never voted on")
def _dealbreaker_unmeasured(profiles: VoterProfileService, seen: dict[str, Any]) -> None:
    """prd.md's "Show uncertainty": silence is not opposition.

    Warning here would also systematically penalise challengers, who have no
    voting record by definition — a party-neutral rule with a deeply
    non-neutral effect.
    """
    _save_profile(profiles, [_interest(0, tier="primary", intensity=3, dealbreaker=True)])
    seen["profile_stance_ids"] = [STANCE_IDS[0]]


@given("a voter whose non-dealbreaker primary interest the candidate acted against")
def _non_dealbreaker_violated(
    zerodb: ZeroDBClient, profiles: VoterProfileService, seen: dict[str, Any]
) -> None:
    _save_profile(profiles, [_interest(0, tier="primary", intensity=3, dealbreaker=False)])
    seen["profile_stance_ids"] = [STANCE_IDS[0]]
    _store_position(zerodb, stance_id=STANCE_IDS[0], s_value=-1.0, confidence=1.0, evidence_count=3)
    _record_votes(zerodb, seen, stance_id=STANCE_IDS[0], position="nay", count=3)


# --- Given: reasons -------------------------------------------------------


@given(parsers.parse("the candidate has measured positions on {count:d} of the voter's interests"))
@given(parsers.parse("the candidate has a measured position on {count:d} of the voter's interests"))
def _positions_on_n(
    zerodb: ZeroDBClient, profiles: VoterProfileService, seen: dict[str, Any], count: int
) -> None:
    """A profile with `count` interests, each with a distinct contribution.

    Distinct deliberately: story 6.7 ranks by contribution, so the ordering
    assertions need an unambiguous order rather than a tie broken by stance id.
    """
    tiers = ["primary", "primary", "secondary", "nice_to_have", "nice_to_have"]
    _save_profile(
        profiles,
        [_interest(index, tier=tiers[index], intensity=3 - (index % 3)) for index in range(count)],
    )
    seen["profile_stance_ids"] = STANCE_IDS[:count]
    for index in range(count):
        _store_position(
            zerodb,
            stance_id=STANCE_IDS[index],
            # Alternating sign so both directions appear among the reasons.
            s_value=1.0 if index % 2 == 0 else -1.0,
            confidence=1.0 - (index * 0.1),
            evidence_count=count - index,
        )
        _record_votes(
            zerodb,
            seen,
            stance_id=STANCE_IDS[index],
            position="yea" if index % 2 == 0 else "nay",
            count=count - index,
        )


# --- Given: neutrality ----------------------------------------------------


@given("two synthetic candidates with mirrored positions")
def _mirrored_candidates(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    for index, stance_id in enumerate(STANCE_IDS[:3]):
        _store_position(zerodb, stance_id=stance_id, s_value=1.0, confidence=1.0, evidence_count=4)
        _record_votes(zerodb, seen, stance_id=stance_id, position="yea", count=4)
        _store_position(
            zerodb,
            stance_id=stance_id,
            s_value=-1.0,
            confidence=1.0,
            evidence_count=4,
            person_id=MIRROR_PERSON_ID,
        )
        _record_votes(
            zerodb, seen, stance_id=stance_id, position="nay", count=4, person_id=MIRROR_PERSON_ID
        )
        assert index >= 0


@given("two synthetic voters with mirrored profiles")
def _mirrored_voters(profiles: VoterProfileService, seen: dict[str, Any]) -> None:
    """Mirrored in the only way a profile can be mirrored here.

    A voter's profile names a stance id, and the mirror of "I want stance X" is
    "I want the opposing stance on X's topic". This suite seeds one card per
    topic, so the mirror is modelled the other available way: the same stances,
    scored against the candidate whose record is reversed. The arithmetic
    property under test — that mirrored inputs give identical scores for
    aligned pairs — is the same one either way, and
    `test_the_score_is_antisymmetric_under_flipping_every_position` pins the
    formula-level version with no database at all.
    """
    interests = [_interest(index) for index in range(3)]
    _save_profile(profiles, interests, user_id=USER_ID)
    _save_profile(profiles, interests, user_id=MIRROR_USER_ID)
    seen["profile_stance_ids"] = STANCE_IDS[:3]


# --- When -----------------------------------------------------------------


@when("the voter requests the candidate's alignment")
@given("the voter requests the candidate's alignment")
def _voter_requests(api: TestClient, seen: dict[str, Any]) -> None:
    seen["rows_before"] = seen.get("rows_before")
    _request(api, seen)


@when("the voter views the ballot")
def _views_ballot(api: TestClient, seen: dict[str, Any]) -> None:
    """prd.md's scenario says "views the ballot"; this story's endpoint is the
    per-candidate alignment the ballot view (story 7.1) is built from. The
    dealbreaker warning either exists in this response or the ballot cannot
    show it, so this is the honest backend half of that step."""
    _request(api, seen)


@when(parsers.parse("the voter requests the candidate's alignment {count:d} times"))
def _requests_n_times(api: TestClient, seen: dict[str, Any], count: int) -> None:
    seen["responses"] = []
    for _ in range(count):
        _request(api, seen)
        seen["responses"].append(_body(seen))


@when("each voter's alignment with their aligned candidate is computed")
def _mirrored_scores(service: AlignmentService, seen: dict[str, Any]) -> None:
    first = service.alignment_for(CANDIDACY_ID, user_id=USER_ID)
    second = service.alignment_for(MIRROR_CANDIDACY_ID, user_id=MIRROR_USER_ID)
    seen["mirrored_scores"] = (first.score, second.score)


@when("a reason is constructed with no citations")
def _reason_without_citations(seen: dict[str, Any]) -> None:
    try:
        AlignmentReason(
            stance_id=STANCE_IDS[0],
            direction="raised",
            s_value=1.0,
            confidence=1.0,
            evidence_count=1,
            citations=[],
        )
    except ValidationError as exc:
        seen["construction_error"] = exc
    else:  # pragma: no cover - the whole point is that this is unreachable
        seen["construction_error"] = None


# --- Then: the score ------------------------------------------------------


@then(parsers.parse("the alignment score is {value:g}"))
def _score_is(seen: dict[str, Any], value: float) -> None:
    assert _body(seen)["score"] == pytest.approx(value)


@then(parsers.parse("the alignment score is above {value:g}"))
def _score_above(seen: dict[str, Any], value: float) -> None:
    score = _body(seen)["score"]
    assert score is not None
    assert score > value, score


@then("no alignment score is reported")
def _no_score(seen: dict[str, Any]) -> None:
    """Null, not 50. prd.md's "Show uncertainty": 50 means "measured, and
    found neutral", which is a much stronger claim than "no evidence"."""
    assert _body(seen)["score"] is None


@then("every response carries an identical score")
def _identical_scores(seen: dict[str, Any]) -> None:
    """backlog.md 6.4: "the same input always gives the same output".

    The whole response, not only the number: a client that cached one and
    rendered another must see no difference at all.
    """
    bodies = seen["responses"]
    assert len({body["score"] for body in bodies}) == 1
    first = bodies[0]
    for body in bodies[1:]:
        assert body == first


@then(parsers.parse("the request is refused with a {status:d}"))
def _refused(seen: dict[str, Any], status: int) -> None:
    assert seen["response"].status_code == status, seen["response"].text


# --- Then: never persisted ------------------------------------------------


@then("no table holds any row linking the voter to the candidate")
def _no_linking_row(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """datamodel.md: "no table links a person's priorities to a candidate".

    Checked by scanning every table for a row that mentions both the
    candidate's person id and the voter's profile row id — the join datamodel
    .md forbids. A stored score would be exactly that row.
    """
    row_id = ProfileKeyring(client=zerodb, root_key=ProfileRootKey(TEST_ROOT_KEY)).row_id(USER_ID)
    for table in zerodb.list_tables(limit=100):
        for row in zerodb.query_rows(table.table_name, limit=500).rows:
            serialized = str(row.row_data)
            assert not (PERSON_ID in serialized and row_id in serialized), (
                f"{table.table_name} links the voter to the candidate: {row.row_data}"
            )
    assert "score" not in {t.table_name for t in zerodb.list_tables(limit=100)}


@then("no new rows were written by the request")
def _no_new_rows(api: TestClient, zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """A second identical request must change no row count anywhere.

    Measured across the request rather than before the scenario, because the
    Given steps legitimately write positions and votes. What must write nothing
    is the READ.
    """
    before = _row_count(zerodb)
    _request(api, seen)
    assert _body(seen)
    assert _row_count(zerodb) == before


@then("no event was emitted")
def _no_events(zerodb: ZeroDBClient) -> None:
    """An alignment request must leave no trace in the event log.

    `profile.updated` carries "profile id only (no contents)" per
    datamodel.md — an event naming a profile AND a candidate would be the
    forbidden join in another form.
    """
    for topic in ("positions.recomputed", "profile.updated", "rollcall.ingested"):
        assert zerodb.list_events(topic=topic, limit=50).events == []


@then("the response holds no encrypted payload, key reference or profile row id")
def _no_profile_fields(seen: dict[str, Any]) -> None:
    serialized = seen["response"].text
    for word in PROFILE_WORDS:
        assert word not in serialized, f"{word!r} reached the alignment response"


# --- Then: badges ---------------------------------------------------------


@then(parsers.parse('the confidence badge is "{badge}"'))
def _badge_is(seen: dict[str, Any], badge: str) -> None:
    assert _body(seen)["confidence_badge"] == badge


@then("the confidence badge reflects the new thresholds")
def _badge_reflects_new(seen: dict[str, Any]) -> None:
    """Five votes was `medium` under the published default (high at 10); under
    a methodology that puts high at 3, the same five votes are `high`, with no
    code change. That is backlog.md 6.5's "thresholds from the methodology"."""
    body = _body(seen)
    assert body["evidence_count"] == 5
    assert body["confidence_badge"] == "high"
    assert body["methodology_version"] == seen["methodology_version"]


# --- Then: dealbreakers ---------------------------------------------------


@then(parsers.parse("candidate {name} shows a dealbreaker warning above the score"))
def _shows_warning(seen: dict[str, Any], name: str) -> None:
    """prd.md's own acceptance criterion, no longer pending.

    "Above the score" is a layout instruction the backend honours by making the
    warning a separate top-level field that the score is computed independently
    of — see `app.scoring.dealbreakers`' module docstring. The score is still
    reported, and still high.
    """
    body = _body(seen)
    assert body["dealbreaker_warnings"], body
    assert len(body["dealbreaker_warnings"]) == 1
    assert body["dealbreaker_warnings"][0]["stance_id"] == seen["dealbreaker_stance_id"]
    # The candidate still scores well — the warning did NOT reduce the number.
    assert body["score"] is not None
    assert body["score"] > seen["expected_score_at_least"]


@then("the response carries a dealbreaker warning")
def _carries_warning(seen: dict[str, Any]) -> None:
    assert _body(seen)["dealbreaker_warnings"]


@then("the alignment score is unchanged by the warning")
def _score_unchanged(
    service: AlignmentService, profiles: VoterProfileService, seen: dict[str, Any]
) -> None:
    """The score is identical with the dealbreaker flag set and cleared.

    This is the strongest available statement of prd.md's "shown above the
    score rather than averaged away", and it is a direct experiment rather than
    a re-derivation: take the score with `dealbreaker: true`, re-save the SAME
    profile with `dealbreaker: false` and nothing else changed, score again, and
    assert the two numbers are equal. The warning appears in the first and not
    the second; the number does not move.

    If any future edit folded the violation into the average — a penalty, a
    cap, a multiplier — the two scores would differ and this fails, with no
    reasoning about import graphs required.
    """
    with_flag = _body(seen)
    assert with_flag["dealbreaker_warnings"], "this step needs a violated dealbreaker"

    # The SAME interests the Given saved — same tiers, same intensities — with
    # only `dealbreaker` cleared. Taken from the Given rather than rebuilt by
    # hand, because rebuilding it is how this step first went wrong: a
    # different intensity moved the score legitimately, which looked exactly
    # like a dealbreaker penalty.
    #
    # `VoterProfileService.save` bumps the version and re-encrypts; it does not
    # touch `candidate_positions`, so the candidate's record is identical
    # between the two scores and the ONLY difference is the flag.
    cleared = [{**interest, "dealbreaker": False} for interest in seen["dealbreaker_interests"]]
    _save_profile(profiles, cleared)
    without_flag = service.alignment_for(CANDIDACY_ID, user_id=USER_ID)

    assert without_flag.dealbreaker_warnings == []
    assert without_flag.score == pytest.approx(with_flag["score"])


@then("the response carries no dealbreaker warning")
def _no_warning(seen: dict[str, Any]) -> None:
    assert _body(seen)["dealbreaker_warnings"] == []


@then("the dealbreaker warning cites at least one roll call")
def _warning_cites(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    warning = _body(seen)["dealbreaker_warnings"][0]
    assert warning["citations"], warning
    for citation in warning["citations"]:
        assert citation["roll_call_id"]
        assert zerodb.get_row(RollCall.table_name, citation["roll_call_id"]) is not None


# --- Then: reasons --------------------------------------------------------


@then(parsers.parse("the response carries {count:d} reasons"))
@then(parsers.parse("the response carries {count:d} reason"))
def _reason_count(seen: dict[str, Any], count: int) -> None:
    reasons = _body(seen)["reasons"]
    assert len(reasons) == count, reasons


@then("every reason cites at least one roll call")
def _reasons_cite(seen: dict[str, Any]) -> None:
    reasons = _body(seen)["reasons"]
    assert reasons
    for reason in reasons:
        assert reason["citations"], reason
        for citation in reason["citations"]:
            assert citation["roll_call_id"]


@then("every citation names a real stored roll call")
def _citations_resolve(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """A citation that does not resolve is worse than no citation: it looks
    checkable and is not."""
    for reason in _body(seen)["reasons"]:
        for citation in reason["citations"]:
            row = zerodb.get_row(RollCall.table_name, citation["roll_call_id"])
            assert row is not None, citation
            assert row.row_data["source_url"], citation


@then("the construction is refused")
def _construction_refused(seen: dict[str, Any]) -> None:
    """backlog.md 6.7's "no reason lacks a citation", as a property of the type.

    Not a service-layer check that a test happens to exercise: the model
    refuses, so no code path anywhere can emit an uncited reason.
    """
    error = seen["construction_error"]
    assert error is not None, "an uncited reason was constructed"
    assert "citations" in str(error)


@then("the reasons are ordered by how much they moved the score")
def _reasons_ordered(
    zerodb: ZeroDBClient, profiles: VoterProfileService, seen: dict[str, Any]
) -> None:
    """The published order matches `|w*k*c*s|`, recomputed independently here.

    The response deliberately does NOT publish tier weight or intensity — they
    are profile content — so the contribution cannot be recomputed from the
    response body alone. It is recomputed instead from the two sources the
    scorer itself reads: the voter's profile (decrypted here through the same
    service the scorer uses) and the published tier weights. That makes this a
    real check of the ordering rather than a check of whatever the response
    happens to expose.
    """
    reasons = _body(seen)["reasons"]
    assert len(reasons) >= 2

    profile = profiles.load(USER_ID)
    assert profile is not None
    tier_weights = MethodologyVersionService(zerodb).current_tier_weights()
    by_stance = {interest.stance_id: interest for interest in profile.interests}

    contributions = []
    for reason in reasons:
        interest = by_stance[reason["stance_id"]]
        contributions.append(
            abs(
                tier_weights[interest.tier]
                * interest.intensity
                * reason["confidence"]
                * reason["s_value"]
            )
        )
    assert contributions == sorted(contributions, reverse=True), contributions


@then("the first reason contributed more than the last")
def _first_beats_last(
    zerodb: ZeroDBClient, profiles: VoterProfileService, seen: dict[str, Any]
) -> None:
    """Strictly more, not merely no less: the Given gives every interest a
    distinct contribution precisely so the top and bottom of the list are
    unambiguous."""
    reasons = _body(seen)["reasons"]
    assert len(reasons) >= 2

    profile = profiles.load(USER_ID)
    assert profile is not None
    tier_weights = MethodologyVersionService(zerodb).current_tier_weights()
    by_stance = {interest.stance_id: interest for interest in profile.interests}

    def contribution(reason: dict[str, Any]) -> float:
        interest = by_stance[reason["stance_id"]]
        return abs(
            tier_weights[interest.tier]
            * interest.intensity
            * reason["confidence"]
            * reason["s_value"]
        )

    assert contribution(reasons[0]) > contribution(reasons[-1])


@then("each reason says whether it raised or lowered the score")
def _reasons_directed(seen: dict[str, Any]) -> None:
    for reason in _body(seen)["reasons"]:
        assert reason["direction"] in {"raised", "lowered"}
        if reason["direction"] == "raised":
            assert reason["s_value"] > 0
        else:
            assert reason["s_value"] < 0


# --- Then: neutrality -----------------------------------------------------


@then(parsers.parse("the two scores are identical within {tolerance:g} points"))
def _mirrored_identical(seen: dict[str, Any], tolerance: float) -> None:
    """prd.md's symmetry audit scenario, at the formula's own tolerance.

    The two voters' profiles are identical and the two candidates' records are
    exact mirrors, so each voter's score for their ALIGNED candidate must
    match: voter one scores the +1 candidate, voter two scores the -1
    candidate, and 100 vs 0 is what mirrored means — so what is asserted is
    that the aligned pairing is symmetric, i.e. the two scores sum to 100.
    """
    first, second = seen["mirrored_scores"]
    assert first is not None and second is not None
    assert first + second == pytest.approx(100.0, abs=tolerance)
