"""Unit tests for story 4.2's tier-weight reader (`app.profiles.methodology`).

prd.md's Feature 1 tier table gives the three weights (primary 3.0, secondary
1.5, nice_to_have 0.5) as the CURRENT methodology's numbers, and datamodel.md
is explicit that "the live values come from `methodology_versions` and are
never hard-coded" into a profile or its code path. Story 6.1 ("Methodology
versions", iteration 4) owns publishing a new methodology, its changelog and
re-scoring on a bump; this story only needs ONE current row to exist and be
read live rather than hard-coded in `app.profiles` — see that module's
docstring for the full scope line.

Every row here is the real production methodology id (`2026.1`), not a
synthetic marker: unlike a voter profile, a methodology version is public
reference data (prd.md: "weights {tiers, evidence_blend, vote_types}" is
published alongside a `changelog_url`), so there is nothing here CLAUDE.md's
"never put real voter data in a fixture" rule applies to.
"""

from __future__ import annotations

import pytest

from app.bootstrap import TABLE_SCHEMAS
from app.profiles.methodology import (
    DEFAULT_METHODOLOGY_VERSION,
    DEFAULT_TIER_WEIGHTS,
    MethodologyVersionService,
)
from app.zerodb.client import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport


@pytest.fixture
def client() -> ZeroDBClient:
    zdb = ZeroDBClient(
        transport=FakeZeroDBTransport(), project_id="proj-test", sleep=lambda _: None
    )
    zdb.create_table("methodology_versions", TABLE_SCHEMAS["methodology_versions"])
    return zdb


@pytest.fixture
def service(client: ZeroDBClient) -> MethodologyVersionService:
    return MethodologyVersionService(client)


def test_default_weights_match_prd_tier_table() -> None:
    """prd.md's Feature 1 table: primary 3.0, secondary 1.5, nice_to_have 0.5."""
    assert DEFAULT_TIER_WEIGHTS == {"primary": 3.0, "secondary": 1.5, "nice_to_have": 0.5}


def test_reading_with_no_current_row_seeds_and_returns_the_default(
    service: MethodologyVersionService, client: ZeroDBClient
) -> None:
    """datamodel.md: weights "come from `methodology_versions`". With nothing
    stored yet, the reader seeds the one current row rather than falling back
    to an in-process constant silently — a live project with none would
    otherwise disagree with every other environment about where its own
    weights came from.
    """
    weights = service.current_tier_weights()
    assert weights == DEFAULT_TIER_WEIGHTS

    stored = client.get_row("methodology_versions", DEFAULT_METHODOLOGY_VERSION)
    assert stored is not None
    assert stored.row_data["weights"]["tiers"] == DEFAULT_TIER_WEIGHTS
    assert stored.row_data["is_current"] is True


def test_reading_twice_does_not_duplicate_the_seeded_row(
    service: MethodologyVersionService, client: ZeroDBClient
) -> None:
    service.current_tier_weights()
    service.current_tier_weights()
    rows = client.query_rows("methodology_versions", limit=100)
    assert len(rows.rows) == 1


def test_an_existing_current_row_is_read_live_rather_than_reseeded(
    service: MethodologyVersionService, client: ZeroDBClient
) -> None:
    """A later methodology (story 6.1's job to publish) must be read as-is,
    never silently replaced by this story's default.
    """
    client.upsert_row(
        "methodology_versions",
        "2027.1",
        {
            "id": "2027.1",
            "published_at": "2027-01-01T00:00:00Z",
            "weights": {"tiers": {"primary": 4.0, "secondary": 2.0, "nice_to_have": 1.0}},
            "changelog_url": "https://informed.example/methodology/2027.1",
            "is_current": True,
        },
    )
    weights = service.current_tier_weights()
    assert weights == {"primary": 4.0, "secondary": 2.0, "nice_to_have": 1.0}


def test_current_version_id_is_exposed_alongside_the_weights(
    service: MethodologyVersionService,
) -> None:
    version = service.current_version()
    assert version.id == DEFAULT_METHODOLOGY_VERSION
    assert version.weights["tiers"] == DEFAULT_TIER_WEIGHTS
