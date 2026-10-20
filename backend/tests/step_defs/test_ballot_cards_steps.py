"""Steps for tests/features/ballot_cards.feature (story 7.1).

Runs the real FastAPI app against story 1.2's in-memory ZeroDB fake, in the
same shape `test_ballot_lookup_steps.py` uses for `/v1/ballot`: the path under
test is the production router, service, `CandidateCardReader` and repository,
and CI makes no network call and needs no credentials.

Fixture helpers (`_seed_reference_rows`, `_person`, `_candidacy`, `_position`)
mirror `test_candidate_cards_steps.py`'s own, because this feature builds on
exactly the same synthetic race story 7.2's tests already prove the read model
against — a second, drifted copy of that seeding would risk the two suites
quietly disagreeing about what a valid candidacy looks like.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.ballot.cards import CandidateCardBuilder
from app.ballot.cards_router import ballot_cards_service
from app.ballot.cards_service import BallotCardsService
from app.bootstrap import Bootstrapper
from app.main import create_app, zerodb_client
from app.repositories import Repository
from app.repositories.models import (
    Candidacy,
    CandidatePosition,
    District,
    Election,
    IssueTopic,
    Person,
    Stance,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/ballot_cards.feature")

PERSON_ID = "person:TEST7100000"
CHALLENGER_PERSON_ID = "person:TEST7100001"
OTHER_DISTRICT_PERSON_ID = "person:TEST7100002"
UNBUILT_PERSON_ID = "person:TEST7100003"

ELECTION_ID = "us-test71-11-03-general"
DISTRICT_ID = "ocd-division/country:us/state:zz/cd:test71"
OTHER_DISTRICT_ID = "ocd-division/country:us/state:zz/cd:test71-other"
DISTRICT_NAME = "Test Congressional District 71"

TOPIC_ID = "en-US:test71-topic:v1"
STANCE_ID = f"{TOPIC_ID}:for"

CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:{PERSON_ID}"


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
def builder(zerodb: ZeroDBClient) -> CandidateCardBuilder:
    return CandidateCardBuilder(zerodb, now=lambda: "2026-10-19T12:00:00Z")


@pytest.fixture
def service(zerodb: ZeroDBClient) -> BallotCardsService:
    return BallotCardsService(zerodb)


@pytest.fixture
def api(zerodb: ZeroDBClient, service: BallotCardsService) -> Any:
    app = create_app()
    app.dependency_overrides[zerodb_client] = lambda: zerodb
    app.dependency_overrides[ballot_cards_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seen() -> dict[str, Any]:
    return {}


# --- fixture helpers, mirroring test_candidate_cards_steps.py -------------


def _seed_reference_rows(zerodb: ZeroDBClient) -> None:
    Repository(client=zerodb, model=Election).upsert(
        {
            "id": ELECTION_ID,
            "jurisdiction_id": "us-federal",
            "name": "Test 71 general",
            "election_date": "2026-11-03",
            "kind": "general",
        }
    )
    Repository(client=zerodb, model=District).upsert_many(
        [
            {
                "id": DISTRICT_ID,
                "body_id": "us-house",
                "name": DISTRICT_NAME,
                "state": "ZZ",
                "valid_from": "2025-01-03",
            },
            {
                "id": OTHER_DISTRICT_ID,
                "body_id": "us-house",
                "name": "Test Congressional District 71-other",
                "state": "ZZ",
                "valid_from": "2025-01-03",
            },
        ]
    )
    Repository(client=zerodb, model=IssueTopic).upsert(
        {
            "id": TOPIC_ID,
            "name": "Synthetic story 7.1 topic",
            "locale": "en-US",
            "taxonomy_version": 1,
        }
    )
    Repository(client=zerodb, model=Stance).upsert(
        {
            "id": STANCE_ID,
            "topic_id": TOPIC_ID,
            "label": "Synthetic stance, for",
            "description": "A synthetic stance used only by story 7.1's tests.",
            "direction_code": "+2",
            "wording_version": 1,
        }
    )


def _person(zerodb: ZeroDBClient, person_id: str, *, name: str) -> None:
    Repository(client=zerodb, model=Person).upsert(
        {
            "id": person_id,
            "canonical_name": name,
            "photo_url": None,
            "terms": [
                {
                    "body_id": "us-house",
                    "district_id": DISTRICT_ID,
                    "party": "IND",
                    "start": "2025-01-03",
                }
            ],
        }
    )


def _candidacy(
    zerodb: ZeroDBClient,
    *,
    person_id: str,
    district_id: str = DISTRICT_ID,
    person_name: str,
    is_incumbent: bool = True,
) -> str:
    stored = Repository(client=zerodb, model=Candidacy).upsert(
        {
            "election_id": ELECTION_ID,
            "district_id": district_id,
            "person_id": person_id,
            "person_name": person_name,
            "party": "IND",
            "is_incumbent": is_incumbent,
            "status": "filed",
        }
    )
    return stored.id or ""


def _position(zerodb: ZeroDBClient, *, person_id: str, evidence_count: int) -> None:
    Repository(client=zerodb, model=CandidatePosition).append_version(
        {
            "person_id": person_id,
            "stance_id": STANCE_ID,
            "s_value": 0.5,
            "confidence": 0.8,
            "evidence_count": evidence_count,
            "basis": "votes",
            "methodology_version": "2026.1",
            "computed_at": "2026-10-19T11:00:00Z",
        }
    )


# --- Given ------------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _a_client(zerodb: ZeroDBClient) -> None:
    """The fixture does the work; this step names it in the feature file."""


@given("a candidacy for a synthetic incumbent")
def _a_candidacy(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _seed_reference_rows(zerodb)
    _person(zerodb, PERSON_ID, name="TEST71, Alpha")
    seen["candidacy_id"] = _candidacy(zerodb, person_id=PERSON_ID, person_name="TEST71, ALPHA")


@given(parsers.parse("a second candidacy in the same race, with a lexically earlier name"))
def _second_candidacy_earlier_name(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _person(zerodb, CHALLENGER_PERSON_ID, name="TEST71, Aaron")
    seen["second_candidacy_id"] = _candidacy(
        zerodb, person_id=CHALLENGER_PERSON_ID, person_name="TEST71, AARON", is_incumbent=False
    )


@given("a challenger candidacy with no current term in the same race")
def _challenger_no_term(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    Repository(client=zerodb, model=Person).upsert(
        {
            "id": CHALLENGER_PERSON_ID,
            "canonical_name": "TEST71, Challenger",
            "photo_url": None,
            "terms": [],
        }
    )
    seen["second_candidacy_id"] = _candidacy(
        zerodb,
        person_id=CHALLENGER_PERSON_ID,
        person_name="TEST71, CHALLENGER",
        is_incumbent=False,
    )


@given("every card has been rebuilt")
def _every_card_rebuilt(builder: CandidateCardBuilder, seen: dict[str, Any]) -> None:
    for key in ("candidacy_id", "second_candidacy_id", "other_district_candidacy_id"):
        candidacy_id = seen.get(key)
        if candidacy_id:
            builder.rebuild_candidacy(candidacy_id)


@given(parsers.parse("the incumbent has a current position on a stance backed by {count:d} votes"))
def _position_with_evidence(zerodb: ZeroDBClient, count: int) -> None:
    _position(zerodb, person_id=PERSON_ID, evidence_count=count)


@given("a candidacy in a different district, with its card rebuilt")
def _other_district_candidacy(
    zerodb: ZeroDBClient, builder: CandidateCardBuilder, seen: dict[str, Any]
) -> None:
    _person(zerodb, OTHER_DISTRICT_PERSON_ID, name="TEST71, Other District")
    seen["other_district_candidacy_id"] = _candidacy(
        zerodb,
        person_id=OTHER_DISTRICT_PERSON_ID,
        district_id=OTHER_DISTRICT_ID,
        person_name="TEST71, OTHER DISTRICT",
    )
    builder.rebuild_candidacy(seen["other_district_candidacy_id"])


@given("a candidacy exists with no card ever rebuilt for it")
def _unbuilt_candidacy(zerodb: ZeroDBClient, seen: dict[str, Any]) -> None:
    _person(zerodb, UNBUILT_PERSON_ID, name="TEST71, Unbuilt")
    seen["unbuilt_candidacy_id"] = _candidacy(
        zerodb, person_id=UNBUILT_PERSON_ID, person_name="TEST71, UNBUILT"
    )


# --- When --------------------------------------------------------------


@when("the ballot cards endpoint is asked for that race")
def _ask_for_race(api: TestClient, seen: dict[str, Any]) -> None:
    seen["response"] = api.get(
        "/v1/ballot/cards", params={"election_id": ELECTION_ID, "district_id": DISTRICT_ID}
    )


@when("the ballot cards endpoint is asked for a race with no candidacies")
def _ask_for_empty_race(api: TestClient, seen: dict[str, Any]) -> None:
    empty_district_id = "ocd-division/country:us/state:zz/cd:empty"
    seen["response"] = api.get(
        "/v1/ballot/cards",
        params={"election_id": ELECTION_ID, "district_id": empty_district_id},
    )


@when("the ballot cards endpoint is asked with no district_id")
def _ask_missing_param(api: TestClient, seen: dict[str, Any]) -> None:
    seen["response"] = api.get("/v1/ballot/cards", params={"election_id": ELECTION_ID})


@when("the API's OpenAPI schema is fetched")
def _fetch_openapi(api: TestClient, seen: dict[str, Any]) -> None:
    seen["response"] = api.get("/openapi.json")


# --- Then --------------------------------------------------------------


def _cards(seen: dict[str, Any]) -> list[dict[str, Any]]:
    response = seen["response"]
    assert response.status_code == 200, response.text
    return response.json()["cards"]


def _card_for(seen: dict[str, Any], candidacy_id: str) -> dict[str, Any]:
    matches = [card for card in _cards(seen) if card["candidacy_id"] == candidacy_id]
    assert len(matches) == 1, f"expected exactly 1 card for {candidacy_id}, got {matches}"
    return matches[0]


@then("the cards are returned in name order")
def _cards_in_name_order(seen: dict[str, Any]) -> None:
    names = [card["person_name"] for card in _cards(seen)]
    assert names == sorted(names), f"not in name order: {names}"


@then("nothing about the order implies a recommendation")
def _no_recommendation_in_order(seen: dict[str, Any]) -> None:
    body = str(seen["response"].json()).lower()
    for forbidden in ("recommend", "endorse", "vote for", "best match", "ranked"):
        assert forbidden not in body, f"{forbidden!r} appears in the payload"


@then("every card in the response carries an explicit is_incumbent flag")
def _every_card_has_incumbent_flag(seen: dict[str, Any]) -> None:
    cards = _cards(seen)
    assert cards, "no cards at all, so the flag proves nothing"
    for card in cards:
        assert isinstance(card["is_incumbent"], bool), f"not a bool: {card}"


@then("the incumbent candidacy is flagged true")
def _incumbent_flagged_true(seen: dict[str, Any]) -> None:
    card = _card_for(seen, CANDIDACY_ID)
    assert card["is_incumbent"] is True, card


@then("the challenger's card is flagged is_incumbent false")
def _challenger_flagged_false(seen: dict[str, Any]) -> None:
    challenger_id = f"{ELECTION_ID}:{DISTRICT_ID}:{CHALLENGER_PERSON_ID}"
    card = _card_for(seen, challenger_id)
    assert card["is_incumbent"] is False, card


@then(parsers.parse('the incumbent\'s card shows confidence badge "{badge}"'))
def _incumbent_badge(seen: dict[str, Any], badge: str) -> None:
    card = _card_for(seen, CANDIDACY_ID)
    assert card["confidence_badge"] == badge, card


@then("the response is 200 with an empty list of cards")
def _empty_cards(seen: dict[str, Any]) -> None:
    response = seen["response"]
    assert response.status_code == 200, response.text
    assert response.json()["cards"] == []


@then("that candidacy's card is absent from the response rather than a fabricated zero row")
def _unbuilt_card_absent(seen: dict[str, Any]) -> None:
    unbuilt_id = seen.get("unbuilt_candidacy_id")
    assert unbuilt_id, "the unbuilt candidacy id was not recorded by the Given step"
    ids = [card["candidacy_id"] for card in _cards(seen)]
    assert unbuilt_id not in ids, f"a card existed for an un-rebuilt candidacy: {ids}"


@then("the other district's candidate is not among the cards")
def _other_district_absent(seen: dict[str, Any]) -> None:
    ids = [card["person_id"] for card in _cards(seen)]
    assert OTHER_DISTRICT_PERSON_ID not in ids, f"other district's candidate leaked in: {ids}"


@then(parsers.parse("the response is {status:d}"))
def _status_code(seen: dict[str, Any], status: int) -> None:
    assert seen["response"].status_code == status, seen["response"].text


@then("no card in the response carries a personalized score or a profile reference")
def _no_personalized_score(seen: dict[str, Any]) -> None:
    body = seen["response"].json()
    forbidden_keys = {"score", "profile_id", "user_id", "alignment_score"}
    for card in body["cards"]:
        leaked = forbidden_keys & card.keys()
        assert not leaked, f"a personalized field leaked onto a card: {leaked}"


@then("the ballot cards endpoint documents that it never aggregates at request time")
def _documents_no_aggregation(seen: dict[str, Any]) -> None:
    schema = seen["response"].json()
    operation = schema["paths"]["/v1/ballot/cards"]["get"]
    text = f"{operation.get('summary', '')} {operation.get('description', '')}".lower()
    assert "never aggregates" in text, f"the no-aggregation guarantee is not documented: {text!r}"


@then("every field of the ballot cards response carries a description")
def _every_field_documented(seen: dict[str, Any]) -> None:
    schemas = seen["response"].json()["components"]["schemas"]
    undocumented: list[str] = []
    for name in ("BallotCardsResponse", "CandidateCardView"):
        assert name in schemas, f"{name} is missing from the OpenAPI schema"
        for field_name, field_schema in schemas[name].get("properties", {}).items():
            if not _described(field_schema):
                undocumented.append(f"{name}.{field_name}")
    assert not undocumented, f"undocumented fields: {undocumented}"


def _described(field_schema: dict[str, Any]) -> bool:
    if field_schema.get("description"):
        return True
    return any(branch.get("description") for branch in field_schema.get("anyOf", []))
