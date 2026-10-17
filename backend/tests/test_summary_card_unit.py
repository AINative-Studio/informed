"""Unit tests for story 14.6's summary-card service and response model.

Complements `tests/step_defs/test_summary_card_steps.py`'s end-to-end HTTP
coverage with focused checks on `SummaryCardService` and
`CandidateSummaryCard` directly: the model can never carry a profile-shaped
field (structural, not just "the service doesn't fill it in"), and the
service's only lookup key is the candidacy id — nothing resembling a user or
profile id is ever an argument its public surface accepts.
"""

from __future__ import annotations

import inspect

import pytest

from app.bootstrap import TABLE_SCHEMAS
from app.repositories.base import Repository
from app.repositories.models import Candidacy, District, Election, Jurisdiction, Person
from app.summary_card.models import CandidateSummaryCard
from app.summary_card.service import CandidacyNotFoundError, SummaryCardService
from app.zerodb.client import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

PERSON_ID = "person:zzz-summary-card-unit"
#: Derived from Candidacy's natural key (election_id, district_id, person_id)
#: with the person_id prefix kept intact, exactly as datamodel.md documents
#: `{election_id}:{district_id}:{person_id}` — see `Candidacy`'s docstring.
CANDIDACY_ID = f"election:district:{PERSON_ID}"


@pytest.fixture
def client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    zerodb = ZeroDBClient(transport=transport, project_id="proj-test", sleep=lambda _: None)
    for table in (
        Jurisdiction.table_name,
        Election.table_name,
        District.table_name,
        Person.table_name,
        Candidacy.table_name,
        "bodies",
    ):
        zerodb.create_table(table, TABLE_SCHEMAS[table])
    Repository(client=zerodb, model=Jurisdiction).upsert(
        {
            "id": "us-federal",
            "name": "United States",
            "country_code": "US",
            "level": "federal",
            "adapter_name": "us-federal",
        }
    )
    zerodb.upsert_row(
        "bodies",
        "us-house",
        {
            "id": "us-house",
            "jurisdiction_id": "us-federal",
            "name": "U.S. House",
            "chamber": "lower",
            "seats": 435,
        },
    )
    Repository(client=zerodb, model=Election).upsert(
        {
            "id": "election",
            "jurisdiction_id": "us-federal",
            "name": "Test General",
            "election_date": "2026-11-03",
            "kind": "general",
        }
    )
    Repository(client=zerodb, model=District).upsert(
        {
            "id": "district",
            "body_id": "us-house",
            "name": "Test District 9",
            "state": "ZZ",
            "valid_from": "2025-01-03",
        }
    )
    Repository(client=zerodb, model=Person).upsert(
        {"id": PERSON_ID, "canonical_name": "Pat Rivera"}
    )
    Repository(client=zerodb, model=Candidacy).upsert(
        {
            "election_id": "election",
            "district_id": "district",
            "person_id": PERSON_ID,
            "person_name": "Pat Rivera",
            "party": "IND",
            "is_incumbent": False,
            "status": "filed",
        }
    )
    return zerodb


@pytest.fixture
def service(client: ZeroDBClient) -> SummaryCardService:
    return SummaryCardService(client=client)


def test_card_for_known_candidacy_carries_candidate_fields(service: SummaryCardService) -> None:
    card = service.card_for(CANDIDACY_ID)
    assert card.person_name == "Pat Rivera"
    assert card.party == "IND"
    assert card.district == "Test District 9"
    assert card.candidacy_id == CANDIDACY_ID


def test_not_yet_computed_fields_are_none_not_fabricated(service: SummaryCardService) -> None:
    card = service.card_for(CANDIDACY_ID)
    assert card.verdict_counts is None
    assert card.say_do_gap is None
    assert card.positions_summary is None


def test_unknown_candidacy_raises_a_named_error(service: SummaryCardService) -> None:
    with pytest.raises(CandidacyNotFoundError, match="does-not-exist"):
        service.card_for("does-not-exist")


def test_card_for_takes_only_a_candidacy_id(service: SummaryCardService) -> None:
    """The service's one lookup method takes exactly one argument: the
    candidacy id. No profile_id, user_id or voter_id parameter exists for a
    caller to (mis)use — checked against the live signature, not just by
    convention, so a later edit that adds one fails this test immediately.
    """
    signature = inspect.signature(SummaryCardService.card_for)
    params = [name for name in signature.parameters if name != "self"]
    assert params == ["candidacy_id"]


def test_the_response_model_structurally_forbids_profile_fields() -> None:
    """`CandidateSummaryCard` sets extra='forbid' and declares no field whose
    name resembles identity, so a caller cannot even construct one carrying a
    profile_id/user_id/interests field — this is checked at the model, not
    only at the one service method that builds it today.
    """
    field_names = set(CandidateSummaryCard.model_fields)
    forbidden_substrings = ("profile", "user_id", "voter", "interest")
    leaky = {
        name
        for name in field_names
        if any(substring in name.lower() for substring in forbidden_substrings)
    }
    assert not leaky

    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic ValidationError, extra="forbid"
        CandidateSummaryCard(
            candidacy_id="x",
            person_name="x",
            party=None,
            district="x",
            photo_url=None,
            verdict_counts=None,
            say_do_gap=None,
            positions_summary=None,
            updated_at="2026-01-01T00:00:00Z",
            profile_id="should-not-be-accepted",  # type: ignore[call-arg]
        )
