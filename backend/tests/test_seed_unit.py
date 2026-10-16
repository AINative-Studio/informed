"""Unit tests for the fixed reference-data seed (story 2.1).

The Gherkin feature covers the acceptance criterion end to end through
`Bootstrapper.run()`. These tests cover what Gherkin reads badly: the seed
constants' fidelity to datamodel.md, the models' validation, and the pacing and
call-count properties of the `Seeder` itself.
"""

from __future__ import annotations

import pytest

from app.repositories import RowValidationError
from app.repositories.models import Body, Jurisdiction
from app.seed import (
    FEDERAL_BODIES,
    US_FEDERAL,
    Seeder,
    SeedReport,
    seeded_row_ids,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table("jurisdictions", {"id": "string"})
    client.create_table("bodies", {"id": "string"})
    return client


# --- the seed data itself --------------------------------------------------


def test_the_seed_is_exactly_the_three_rows_the_story_names() -> None:
    """Story 2.1's acceptance criterion names three ids and no more.

    Pinned as a set because a fourth row added here would be seeded into the
    live project permanently (these rows are deliberately never cleaned up),
    and that belongs in the story that needs it, not in this one.
    """
    assert seeded_row_ids() == {
        ("jurisdictions", "us-federal"),
        ("bodies", "us-house"),
        ("bodies", "us-senate"),
    }


def test_the_federal_jurisdiction_matches_datamodel_md() -> None:
    assert US_FEDERAL.id == "us-federal"
    assert US_FEDERAL.name == "United States"
    assert US_FEDERAL.country_code == "US"
    assert US_FEDERAL.level == "federal"
    assert US_FEDERAL.adapter_name == "us-federal"
    assert US_FEDERAL.active is True


def test_both_chambers_belong_to_the_federal_jurisdiction() -> None:
    assert [body.jurisdiction_id for body in FEDERAL_BODIES] == ["us-federal", "us-federal"]


def test_the_chambers_carry_their_real_seat_counts() -> None:
    """435 and 100 are constitutional/statutory facts, not placeholders.

    Worth asserting because they are the one piece of this seed a future
    reader might mistake for sample data.
    """
    seats = {body.id: body.seats for body in FEDERAL_BODIES}
    assert seats == {"us-house": 435, "us-senate": 100}


def test_the_house_is_the_lower_chamber_and_the_senate_the_upper() -> None:
    chambers = {body.id: body.chamber for body in FEDERAL_BODIES}
    assert chambers == {"us-house": "lower", "us-senate": "upper"}


# --- the models ------------------------------------------------------------


def test_a_jurisdiction_rejects_an_unknown_level() -> None:
    """`level` is a controlled vocabulary, like every other one in models.py.

    An out-of-vocabulary level would not error anywhere downstream — it would
    just make the jurisdiction invisible to a query filtering on the level it
    expected.
    """
    with pytest.raises(ValueError, match="level"):
        Jurisdiction(
            id="xx",
            name="Nowhere",
            country_code="XX",
            level="planetary",
            adapter_name="xx",
        )


def test_a_body_rejects_an_unknown_chamber() -> None:
    with pytest.raises(ValueError, match="chamber"):
        Body(
            id="xx-assembly",
            jurisdiction_id="xx",
            name="Assembly",
            chamber="middle",
            seats=1,
        )


def test_a_body_rejects_a_negative_seat_count() -> None:
    with pytest.raises(ValueError, match="seats"):
        Body(
            id="xx-assembly",
            jurisdiction_id="xx",
            name="Assembly",
            chamber="lower",
            seats=-1,
        )


def test_a_body_declares_its_jurisdiction_reference(client: ZeroDBClient) -> None:
    """datamodel.md rule 5: the repository checks the reference before writing."""
    assert Body.references == {"jurisdiction_id": "jurisdictions"}


def test_a_jurisdiction_or_body_needs_an_explicit_id(client: ZeroDBClient) -> None:
    """Neither has a composable natural key: the id IS the natural key.

    `us-federal` and `us-house` are not derivable from any field, so the
    repository must require the caller to supply them rather than minting
    something.
    """
    from app.repositories import Repository

    jurisdictions: Repository[Jurisdiction] = Repository(client=client, model=Jurisdiction)
    with pytest.raises(RowValidationError, match="needs an id"):
        jurisdictions.upsert(
            {
                "name": "United States",
                "country_code": "US",
                "level": "federal",
                "adapter_name": "us-federal",
            }
        )


def test_an_unknown_field_is_refused_rather_than_silently_stored() -> None:
    """`extra="forbid"`, as models.py's docstring explains for every row model."""
    with pytest.raises(ValueError, match="contry_code|extra"):
        Jurisdiction(id="xx", name="X", contry_code="XX", level="federal", adapter_name="x")


# --- the Seeder ------------------------------------------------------------


def test_seeding_writes_the_jurisdiction_then_both_bodies(client: ZeroDBClient) -> None:
    report = Seeder(client, pace=lambda: None).run()

    assert report.jurisdictions_written == ["us-federal"]
    assert report.bodies_written == ["us-house", "us-senate"]


def test_seeding_twice_stores_one_row_per_id(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """The acceptance criterion's idempotency, at the unit level.

    Checked against the fake's raw storage because `get_row` stops at the
    first match and so cannot see a duplicate.
    """
    seeder = Seeder(client, pace=lambda: None)
    seeder.run()
    seeder.run()

    for table, row_id in seeded_row_ids():
        stored = [
            row for row in fake.rows.get(table, {}).values() if row["row_data"]["id"] == row_id
        ]
        assert len(stored) == 1, f"{table}/{row_id} has {len(stored)} rows"


def test_seeding_paces_every_write(client: ZeroDBClient) -> None:
    """The live project throttles at 60 req/min (bootstrap's module docstring).

    The seed's writes land immediately after a cold bootstrap's ~32 calls, so
    they share the same pacer rather than being exempt from it.
    """
    paced: list[None] = []
    Seeder(client, pace=lambda: paced.append(None)).run()

    assert len(paced) >= 3, "each of the three seeded rows must be paced"


def test_a_seed_report_knows_how_many_rows_it_wrote() -> None:
    report = SeedReport(jurisdictions_written=["us-federal"], bodies_written=["us-house"])
    assert report.row_count == 2


def test_an_empty_seed_report_counts_nothing() -> None:
    assert SeedReport().row_count == 0
