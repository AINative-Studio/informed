"""Steps for story 14.6's shareable summary-card feature.

Every candidacy in this file is synthetic test data under an obviously-test
id scheme (``election:district:person``-style, ``person:zzz-...``), never a
real candidate. CLAUDE.md forbids real voter data in a fixture; this feature
is about candidate data, which is public, but the fixtures are kept synthetic
anyway so this file proves the endpoint's SHAPE rather than depending on
which real candidacies happen to be loaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import TABLE_SCHEMAS
from app.profiles import PROFILE_TABLES
from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.service import VoterProfileService
from app.repositories.base import Repository
from app.repositories.models import Candidacy, District, Election, Jurisdiction, Person
from app.summary_card.router import summary_card_service, zerodb_client_for_summary_card
from app.zerodb.client import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/summary_card.feature")

TEST_ROOT_KEY = bytes(range(32))

#: Tables this feature's fixtures need, beyond PROFILE_TABLES: the reference
#: chain a Candidacy references (datamodel.md rule 5), plus people.
_REFERENCE_TABLES = (
    Jurisdiction.table_name,
    Election.table_name,
    District.table_name,
    Person.table_name,
    Candidacy.table_name,
    "bodies",
)

CANDIDACY_ID = "election:district:person"
PERSON_ID = "person:zzz-summary-card-pat-rivera"
DISTRICT_ID = "ocd-division/country:us/state:zz/cd:9"


@dataclass
class SummaryCardContext:
    """State carried across one scenario's steps."""

    client: ZeroDBClient | None = None
    api: TestClient | None = None
    response: Any = None
    profile_user_id: str | None = None


@pytest.fixture
def card_ctx() -> SummaryCardContext:
    return SummaryCardContext()


def _fresh_client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    zerodb = ZeroDBClient(transport=transport, project_id="proj-test", sleep=lambda _: None)
    for table in (*PROFILE_TABLES, *_REFERENCE_TABLES):
        zerodb.create_table(table, TABLE_SCHEMAS[table])
    return zerodb


@given("a candidacy store seeded with real reference data", target_fixture="card_ctx")
def seed_store(card_ctx: SummaryCardContext) -> SummaryCardContext:
    card_ctx.client = _fresh_client()
    Repository(client=card_ctx.client, model=Jurisdiction).upsert(
        {"id": "us-federal", "name": "United States", "country_code": "US", "level": "federal",
         "adapter_name": "us-federal"}
    )
    card_ctx.client.upsert_row(
        "bodies",
        "us-house",
        {"id": "us-house", "jurisdiction_id": "us-federal", "name": "U.S. House",
         "chamber": "lower", "seats": 435},
    )
    Repository(client=card_ctx.client, model=Election).upsert(
        {"id": "election", "jurisdiction_id": "us-federal", "name": "Test General",
         "election_date": "2026-11-03", "kind": "general"}
    )
    Repository(client=card_ctx.client, model=District).upsert(
        {"id": "district", "body_id": "us-house", "name": "Test District",
         "state": "ZZ", "valid_from": "2025-01-03"}
    )
    return card_ctx


@given(
    parsers.parse(
        'a candidacy "{candidacy_id}" for "{name}", party "{party}", in a known district'
    )
)
def seed_candidacy(card_ctx: SummaryCardContext, candidacy_id: str, name: str, party: str) -> None:
    assert card_ctx.client is not None
    Repository(client=card_ctx.client, model=Person).upsert(
        {"id": PERSON_ID, "canonical_name": name}
    )
    Repository(client=card_ctx.client, model=Candidacy).upsert(
        {
            "id": candidacy_id,
            "election_id": "election",
            "district_id": "district",
            "person_id": PERSON_ID,
            "person_name": name,
            "party": party,
            "is_incumbent": False,
            "status": "filed",
        }
    )


@given("a voter profile exists with primary interests set")
def seed_voter_profile(card_ctx: SummaryCardContext) -> None:
    assert card_ctx.client is not None
    keyring = ProfileKeyring(client=card_ctx.client, root_key=ProfileRootKey(TEST_ROOT_KEY))
    service = VoterProfileService(client=card_ctx.client, keyring=keyring)
    card_ctx.profile_user_id = "synthetic-voter-14.6"
    service.save(
        card_ctx.profile_user_id,
        {
            "interests": [
                {
                    "topic_id": "test:summary-card:v1",
                    "stance_id": "test:summary-card:v1:test-stance",
                    "tier": "primary",
                    "intensity": 3,
                    "dealbreaker": False,
                }
            ]
        },
    )


def _api_for(card_ctx: SummaryCardContext) -> TestClient:
    if card_ctx.api is None:
        from app.main import create_app

        app = create_app(rate_limit=False)
        assert card_ctx.client is not None
        app.dependency_overrides[zerodb_client_for_summary_card] = lambda: card_ctx.client
        card_ctx.api = TestClient(app)
    return card_ctx.api


@when(parsers.parse('a voter requests the summary card for that candidacy'))
def request_card(card_ctx: SummaryCardContext) -> None:
    api = _api_for(card_ctx)
    card_ctx.response = api.get(f"/v1/candidates/{CANDIDACY_ID}/summary-card")


@when("a voter requests the summary card for that candidacy, however they try to pass their profile")
def request_card_trying_to_leak_profile(card_ctx: SummaryCardContext) -> None:
    api = _api_for(card_ctx)
    # Every plausible way a caller might try to smuggle identity through:
    # query params, a body, and headers. The endpoint accepts GET with no
    # body parameters that name a profile, so all three land on the same
    # response as the plain request — which is exactly the point.
    card_ctx.response = api.get(
        f"/v1/candidates/{CANDIDACY_ID}/summary-card",
        params={
            "profile_id": card_ctx.profile_user_id,
            "user_id": card_ctx.profile_user_id,
            "voter_id": card_ctx.profile_user_id,
        },
        headers={"x-user-id": card_ctx.profile_user_id or ""},
    )


@when("a voter requests the summary card for a candidacy that does not exist")
def request_unknown_card(card_ctx: SummaryCardContext) -> None:
    if card_ctx.client is None:
        card_ctx.client = _fresh_client()
    api = _api_for(card_ctx)
    card_ctx.response = api.get("/v1/candidates/does-not-exist/summary-card")


@then("the card shows the candidate's name, party and district")
def check_candidate_fields(card_ctx: SummaryCardContext) -> None:
    assert card_ctx.response.status_code == 200
    body = card_ctx.response.json()
    assert body["person_name"] == "Pat Rivera"
    assert body["party"] == "IND"
    assert body["district"]


@then("the card's not-yet-computed fields are honestly absent, not fabricated")
def check_not_yet_computed_fields_absent(card_ctx: SummaryCardContext) -> None:
    body = card_ctx.response.json()
    assert body["verdict_counts"] is None
    assert body["say_do_gap"] is None
    assert body["positions_summary"] is None


@then("the card contains no field named profile_id, user_id or interests")
def check_no_profile_fields(card_ctx: SummaryCardContext) -> None:
    body = card_ctx.response.json()
    forbidden = {"profile_id", "user_id", "interests", "voter_id"}
    assert forbidden.isdisjoint(body.keys())


@then("the summary card response contains zero trace of the voter's interests")
def check_no_leaked_interests(card_ctx: SummaryCardContext) -> None:
    assert card_ctx.response.status_code == 200
    body = card_ctx.response.json()
    serialized = str(body)
    assert "test:summary-card" not in serialized
    assert "primary" not in serialized
    forbidden = {"profile_id", "user_id", "interests", "voter_id"}
    assert forbidden.isdisjoint(body.keys())


@then("the summary-card endpoint has no request parameter that accepts a profile or user id")
def check_endpoint_schema_has_no_identity_param(card_ctx: SummaryCardContext) -> None:
    api = _api_for(card_ctx)
    openapi = api.get("/openapi.json").json()
    path_item = openapi["paths"]["/v1/candidates/{candidacy_id}/summary-card"]["get"]
    param_names = {param["name"] for param in path_item.get("parameters", [])}
    # Only the path parameter itself (candidacy_id) is declared; nothing
    # named like an identity ever reaches this endpoint's signature.
    identity_like = {
        name
        for name in param_names
        if name != "candidacy_id"
        and ("user" in name or "profile" in name or "voter" in name)
    }
    assert not identity_like


@then(parsers.parse("the response is a {status:d} naming the candidacy, not a 500"))
def check_404(card_ctx: SummaryCardContext, status: int) -> None:
    assert card_ctx.response.status_code == status
    assert "does-not-exist" in card_ctx.response.json()["detail"]


@then("the card contains no alignment score and no recommendation")
def check_no_score_no_recommendation(card_ctx: SummaryCardContext) -> None:
    body = card_ctx.response.json()
    forbidden = {"score", "alignment", "alignment_score", "recommended", "recommendation"}
    assert forbidden.isdisjoint(body.keys())


__all__: list[str] = []
