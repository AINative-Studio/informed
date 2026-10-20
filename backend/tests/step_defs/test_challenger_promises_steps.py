"""Step definitions for tests/features/challenger_promises.feature (9.6).

Runs over the REAL router, against story 1.2's in-memory fake transport --
same posture as `test_alignment_score_steps.py`, whose fixtures this file
mirrors deliberately so the two read paths are exercised the same way.

Everything is synthetic, for the same reason that file states: this is
another feature that reads a decrypted voter profile, and prd.md forbids
real voter data in any fixture, log or test output. The "voter" is
`test-voter-9x`, the stance ids are `en-US:test-challenger-*`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.main import create_app
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.service import VoterProfileService
from app.promises.router import challenger_promises_service, zerodb_client_for_promises
from app.promises.service import ChallengerPromisesService
from app.repositories import Repository
from app.repositories.models import Candidacy, Election, Person, Promise, PromiseSource
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/challenger_promises.feature")

TEST_ROOT_KEY = bytes(range(32))
USER_ID = "test-voter-9x"
PERSON_ID = "person:TEST9X0001"
ELECTION_ID = "us-2026-11-03-general"
DISTRICT_ID = "ocd-division/country:us/state:zz/cd:9"
CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:{PERSON_ID}"
UNKNOWN_CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:person:does-not-exist"

#: Two synthetic interests, so "grouped by MY interests, plural" has two to group.
TOPIC_IDS = [f"en-US:test-challenger-{index}:v1" for index in range(2)]
STANCE_IDS = [f"{topic_id}:for" for topic_id in TOPIC_IDS]
#: A stance the voter did NOT choose, for the "not one of my interests" case.
OTHER_STANCE_ID = "en-US:test-challenger-other:v1:for"

SOURCE_ID = "src:9f00000000000000000000000000000000000000000000000000000000009x"
SOURCE_URL = "https://challenger.example.invalid/promises"
CAPTURED_AT = "2026-09-01T00:00:00Z"

#: A body distinct from this candidacy's own district -- a genuinely prior office.
PRIOR_BODY_ID = "us-state-house-zz"
PRIOR_DISTRICT_ID = "ocd-division/country:us/state:zz/sldl:4"


# --- fixtures ---------------------------------------------------------------


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
def service(zerodb: ZeroDBClient, profiles: VoterProfileService) -> ChallengerPromisesService:
    return ChallengerPromisesService(client=zerodb, profiles=profiles)


@pytest.fixture
def api(zerodb: ZeroDBClient, profiles: VoterProfileService) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[zerodb_client_for_promises] = lambda: zerodb
    app.dependency_overrides[challenger_promises_service] = lambda: ChallengerPromisesService(
        client=zerodb, profiles=profiles
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def seen() -> dict[str, Any]:
    return {"promise_count": 0, "profile_saved": True}


# --- background ---------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("a synthetic voter with an encrypted profile naming two interests")
def _profile(zerodb: ZeroDBClient, profiles: VoterProfileService) -> None:
    profiles.save(
        USER_ID,
        {
            "interests": [
                {
                    "topic_id": TOPIC_IDS[0],
                    "stance_id": STANCE_IDS[0],
                    "tier": "primary",
                    "intensity": 2,
                    "dealbreaker": False,
                },
                {
                    "topic_id": TOPIC_IDS[1],
                    "stance_id": STANCE_IDS[1],
                    "tier": "secondary",
                    "intensity": 2,
                    "dealbreaker": False,
                },
            ],
            "evidence_mode": "blend",
        },
    )


@given("the voter has no stored profile")
def _no_profile(seen: dict[str, Any]) -> None:
    seen["profile_saved"] = False


# --- candidacy / person seeding ------------------------------------------------


def _seed_person_and_candidacy(zerodb: ZeroDBClient, *, terms: list[dict[str, Any]]) -> None:
    Repository(client=zerodb, model=Person).upsert(
        {
            "id": PERSON_ID,
            "canonical_name": "Synthetic Challenger",
            "terms": terms,
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
            "name": "Synthetic District 9",
            "state": "ZZ",
            "valid_from": "2023-01-03",
        },
    )
    Repository(client=zerodb, model=Candidacy).upsert(
        {
            "election_id": ELECTION_ID,
            "district_id": DISTRICT_ID,
            "person_id": PERSON_ID,
            "person_name": "Synthetic Challenger",
            "party": "Synthetic",
            "is_incumbent": False,
            "status": "filed",
        }
    )


@given("a synthetic challenger with no voting record")
def _challenger_no_record(zerodb: ZeroDBClient) -> None:
    _seed_person_and_candidacy(zerodb, terms=[])


@given("a synthetic challenger who previously served in a different body")
def _challenger_with_prior_office(zerodb: ZeroDBClient) -> None:
    _seed_person_and_candidacy(
        zerodb,
        terms=[
            {
                "body_id": PRIOR_BODY_ID,
                "district_id": PRIOR_DISTRICT_ID,
                "party": "Synthetic",
                "start": "2021-01-01",
                "end": "2025-01-01",
            }
        ],
    )


# --- promise seeding ------------------------------------------------------------


def _seed_source(zerodb: ZeroDBClient) -> None:
    existing = Repository(client=zerodb, model=PromiseSource).get(SOURCE_ID)
    if existing is not None:
        return
    Repository(client=zerodb, model=PromiseSource).upsert(
        {
            "id": SOURCE_ID,
            "candidacy_id": CANDIDACY_ID,
            "source_type": "campaign_website",
            "url": SOURCE_URL,
            "archived_file_id": "file-synthetic-9x",
            "captured_at": CAPTURED_AT,
        }
    )


def _seed_promise(
    zerodb: ZeroDBClient,
    seen: dict[str, Any],
    *,
    stance_id: str | None,
    testable: bool,
) -> None:
    _seed_source(zerodb)
    seen["promise_count"] += 1
    index = seen["promise_count"]
    promise_id = f"prm:9x{index:062d}"
    Repository(client=zerodb, model=Promise).upsert(
        {
            "id": promise_id,
            "candidacy_id": CANDIDACY_ID,
            "person_id": PERSON_ID,
            "source_id": SOURCE_ID,
            "quote_span": f"I will vote to fix synthetic issue {index}",
            "actor": "the candidate",
            "action": "vote to fix" if testable else None,
            "object": f"synthetic issue {index}" if testable else None,
            "direction": "support" if testable else "unclear",
            "topic_id": stance_id.rsplit(":", 1)[0] if stance_id else None,
            "stance_id": stance_id,
            "testable": testable,
            "extractor_version": "test_challenger_promises/v1",
            "confidence": 0.9,
        }
    )
    seen["last_promise_id"] = promise_id


@given("the challenger has a testable promise about the voter's first interest")
def _promise_first_interest(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _seed_promise(zerodb, seen, stance_id=STANCE_IDS[0], testable=True)


@given("the challenger has a testable promise about the voter's second interest")
def _promise_second_interest(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _seed_promise(zerodb, seen, stance_id=STANCE_IDS[1], testable=True)


@given("the challenger has a testable promise about an interest the voter did not choose")
def _promise_other_interest(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _seed_promise(zerodb, seen, stance_id=OTHER_STANCE_ID, testable=True)


@given("the challenger has a non-testable promise about the voter's first interest")
def _non_testable_promise(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _seed_promise(zerodb, seen, stance_id=STANCE_IDS[0], testable=False)


@given("the challenger has made no promises")
def _no_promises() -> None:
    pass


@given("the challenger has a promise whose archived source row no longer exists")
def _promise_with_missing_source(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    """A `promises` row naming a `source_id` that is not (or no longer) stored.

    Written directly rather than through `_seed_promise`: that helper
    guarantees the source exists first (`Promise.references` would refuse an
    `upsert` otherwise), where this scenario needs the row to have survived a
    source's removal -- the same "written when valid, orphaned later" shape
    `ChallengerPromisesService._render`'s docstring is guarding against.
    Bypassing the repository's reference check is the only way to construct
    that state in a test.
    """
    missing_source_id = "src:does-not-exist-0000000000000000000000000000000000000000000000"
    seen["promise_count"] += 1
    promise_id = f"prm:9xmissing{seen['promise_count']:055d}"
    zerodb.upsert_row(
        Promise.table_name,
        promise_id,
        {
            "id": promise_id,
            "candidacy_id": CANDIDACY_ID,
            "person_id": PERSON_ID,
            "source_id": missing_source_id,
            "quote_span": "I will vote to fix the orphaned-source case",
            "actor": "the candidate",
            "action": "vote to fix",
            "object": "the orphaned-source case",
            "direction": "support",
            "topic_id": TOPIC_IDS[0].rsplit(":", 1)[0],
            "stance_id": STANCE_IDS[0],
            "testable": True,
            "extractor_version": "test_challenger_promises/v1",
            "confidence": 0.9,
            "vector_id": None,
            "mappings": [],
            "qa_pending": False,
        },
    )


# --- requests -------------------------------------------------------------------


def _request(api: TestClient, seen: dict[str, Any], *, candidacy_id: str = CANDIDACY_ID) -> None:
    user_id = USER_ID if seen.get("profile_saved", True) else "a-voter-with-no-profile"
    seen["response"] = api.get(
        f"/v1/candidates/{candidacy_id}/promises", params={"profile_id": user_id}
    )


@when("the voter requests the challenger's promises")
def _request_promises(api: TestClient, seen: dict[str, Any]) -> None:
    _request(api, seen)


@when("the voter requests promises for an unknown candidacy")
def _request_unknown_candidacy(api: TestClient, seen: dict[str, Any]) -> None:
    _request(api, seen, candidacy_id=UNKNOWN_CANDIDACY_ID)


# --- assertions -----------------------------------------------------------------


def _body(seen: dict[str, Any]) -> dict[str, Any]:
    return seen["response"].json()


@then("the response groups the promises under each matching interest")
def _groups_both_interests(seen: dict[str, Any]) -> None:
    body = _body(seen)
    stance_ids = {group["stance_id"] for group in body["groups"]}
    assert stance_ids == {STANCE_IDS[0], STANCE_IDS[1]}, body


@then("each group names the real stance the voter chose")
def _groups_name_real_stance(seen: dict[str, Any]) -> None:
    body = _body(seen)
    for group in body["groups"]:
        assert group["stance_id"] in STANCE_IDS


@then("the response reports it as not matching one of the voter's interests")
def _unmatched_count_positive(seen: dict[str, Any]) -> None:
    body = _body(seen)
    assert body["unmatched_count"] >= 1, body


@then("no interest group names a promise the voter never asked about")
def _no_group_for_other_stance(seen: dict[str, Any]) -> None:
    body = _body(seen)
    stance_ids = {group["stance_id"] for group in body["groups"]}
    assert OTHER_STANCE_ID not in stance_ids, body


@then("the promise appears in its group")
def _promise_in_group(seen: dict[str, Any]) -> None:
    body = _body(seen)
    assert len(body["groups"]) == 1
    assert len(body["groups"][0]["promises"]) == 1


@then("it is marked not testable")
def _marked_not_testable(seen: dict[str, Any]) -> None:
    body = _body(seen)
    promise = body["groups"][0]["promises"][0]
    assert promise["testable"] is False, promise


@then("the promise shows its verbatim quote")
def _shows_quote(seen: dict[str, Any]) -> None:
    body = _body(seen)
    promise = body["groups"][0]["promises"][0]
    assert promise["quote_span"].startswith("I will vote to fix")


@then("the promise links the archived campaign source and its capture date")
def _links_source(seen: dict[str, Any]) -> None:
    body = _body(seen)
    promise = body["groups"][0]["promises"][0]
    assert promise["source_url"] == SOURCE_URL
    assert promise["captured_at"] == CAPTURED_AT


@then("the response names the challenger's prior office")
def _names_prior_office(seen: dict[str, Any]) -> None:
    body = _body(seen)
    assert len(body["prior_offices"]) == 1, body


@then("the prior office names the body they served in")
def _prior_office_body(seen: dict[str, Any]) -> None:
    body = _body(seen)
    assert body["prior_offices"][0]["body_id"] == PRIOR_BODY_ID


@then("the response names no prior office")
def _no_prior_office(seen: dict[str, Any]) -> None:
    body = _body(seen)
    assert body["prior_offices"] == [], body


@then("that promise is omitted rather than shown without a citation")
def _promise_omitted_without_citation(seen: dict[str, Any]) -> None:
    body = _body(seen)
    assert body["groups"] == [], body
    assert body["unmatched_count"] == 0, body


@then("the response carries no promise groups")
def _no_groups(seen: dict[str, Any]) -> None:
    body = _body(seen)
    assert body["groups"] == [], body


@then("the response does not fail")
def _response_ok(seen: dict[str, Any]) -> None:
    assert seen["response"].status_code == 200


@then("the response requires no voting record for the challenger")
def _no_voting_record_required(seen: dict[str, Any]) -> None:
    # The request already succeeded with a 200 and real groups, against a
    # candidacy whose person has zero `member_votes`/`candidate_positions`
    # rows -- the assertion IS that this did not 404 or come back empty for
    # that reason. See the "groups both interests" assertion above for the
    # positive half.
    assert seen["response"].status_code == 200
    body = _body(seen)
    assert len(body["groups"]) >= 1, body


@then(parsers.parse("the request is refused with a {status:d}"))
def _refused(seen: dict[str, Any], status: int) -> None:
    assert seen["response"].status_code == status


@then("the response carries no recommendation, endorsement or ranking")
def _no_recommendation(seen: dict[str, Any]) -> None:
    text = seen["response"].text.lower()
    for forbidden in ("vote for", "recommend", "endorse", "best match", "ranked"):
        assert forbidden not in text, f"{forbidden!r} found in response"
