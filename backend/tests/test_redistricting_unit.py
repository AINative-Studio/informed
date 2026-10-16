"""Unit tests for story 2.7's time-bounded district lookup.

The feature file covers the behaviour a reader cares about ("a lookup with an
election date returns the district valid on that date"). These cover the edges
that behaviour rests on and that a scenario would only obscure: how a version's
id is built, how `valid_to` is interpreted on each boundary, what an unparseable
date does, and what happens when a redistricting is recorded twice.

Every district here is synthetic. `state:zz` is not a USPS code, so no row
written by these tests could ever be mistaken for the live reference data story
2.3 created.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.districts.store import (
    DEFAULT_VALID_FROM,
    DistrictStore,
    OverlappingValidityError,
    contains,
    version_id_for,
)
from app.repositories import Repository
from app.repositories.models import District
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport
from tests.support.reference_data import REFERENCE_TABLES, seed_reference_data

DISTRICT_ID = "ocd-division/country:us/state:zz/cd:1"
AT_LARGE_ID = "ocd-division/country:us/state:zz"


@pytest.fixture
def transport() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(transport: FakeZeroDBTransport) -> ZeroDBClient:
    zdb = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    for table in (*REFERENCE_TABLES, "districts"):
        zdb.create_table(table, {"id": "string"})
    seed_reference_data(zdb)
    return zdb


@pytest.fixture
def store(client: ZeroDBClient) -> DistrictStore:
    return DistrictStore(client=client)


@pytest.fixture
def districts(client: ZeroDBClient) -> Repository[District]:
    return Repository(client=client, model=District)


def write_current(store: DistrictStore, district_id: str, *, valid_from: str, name: str) -> None:
    store.record_version(
        district_id,
        body_id="us-house",
        name=name,
        state="ZZ",
        valid_from=valid_from,
        valid_to=None,
    )


# --- version ids ---------------------------------------------------------


def test_the_open_ended_version_keeps_the_plain_ocd_id() -> None:
    """The current version IS the OCD-ID row every other reader already uses."""
    assert version_id_for(DISTRICT_ID, valid_to=None) == DISTRICT_ID


def test_a_closed_version_is_suffixed_with_the_day_it_ended() -> None:
    assert version_id_for(DISTRICT_ID, valid_to="2022-12-31") == f"{DISTRICT_ID}@2022-12-31"


def test_a_version_id_is_deterministic() -> None:
    """Re-ingesting one superseded version must not mint a second row."""
    first = version_id_for(DISTRICT_ID, valid_to="2022-12-31")
    assert first == version_id_for(DISTRICT_ID, valid_to="2022-12-31")


def test_an_at_large_division_gets_a_version_id_too() -> None:
    """A state-only division has no `/cd:` segment and must still version."""
    assert version_id_for(AT_LARGE_ID, valid_to="2022-12-31") == f"{AT_LARGE_ID}@2022-12-31"


# --- range containment ---------------------------------------------------


@pytest.mark.parametrize(
    ("valid_from", "valid_to", "as_of", "expected"),
    [
        # Both bounds inclusive: `valid_from` is the first day the map applies
        # and `valid_to` the last, so excluding either leaves a one-day hole in
        # which no district is valid at all.
        ("2023-01-03", None, "2023-01-03", True),
        ("2023-01-03", None, "2023-01-02", False),
        ("2023-01-03", None, "2099-01-01", True),
        ("2013-01-03", "2022-12-31", "2022-12-31", True),
        ("2013-01-03", "2022-12-31", "2023-01-01", False),
        ("2013-01-03", "2022-12-31", "2013-01-03", True),
        ("2013-01-03", "2022-12-31", "2013-01-02", False),
        ("2013-01-03", "2022-12-31", "2016-11-08", True),
    ],
)
def test_contains_treats_both_bounds_as_inclusive(
    valid_from: str, valid_to: str | None, as_of: str, expected: bool
) -> None:
    district = District(
        id=DISTRICT_ID,
        body_id="us-house",
        name="Synthetic",
        state="ZZ",
        valid_from=valid_from,
        valid_to=valid_to,
    )
    assert contains(district, date.fromisoformat(as_of)) is expected


def test_an_unparseable_valid_from_excludes_the_version() -> None:
    """A date Informed cannot read must not be stretched into "always valid".

    The opposite choice — treating an unreadable bound as open — would make one
    bad row answer for every date and silently shadow the good rows for the
    same district. A version Informed cannot place in time is a version it
    cannot vouch for.
    """
    district = District(
        id=DISTRICT_ID,
        body_id="us-house",
        name="Synthetic",
        state="ZZ",
        valid_from="not-a-date",
    )
    assert contains(district, date(2026, 11, 3)) is False


# --- the lookup ----------------------------------------------------------


def test_get_with_no_date_returns_the_current_version(store: DistrictStore) -> None:
    write_current(store, DISTRICT_ID, valid_from="2025-01-03", name="Current")
    found = store.get(DISTRICT_ID)
    assert found is not None
    assert found.name == "Current"


def test_get_with_no_date_does_not_read_the_whole_table(
    store: DistrictStore, transport: FakeZeroDBTransport
) -> None:
    """The dateless lookup must stay the single `get_row` story 2.4 budgeted for.

    Story 2.4's p95 is 800 ms with the geocode taking ~500 of it, and the
    `districts` read it measured was already two round trips. A time-bounded
    lookup that scanned versions on *every* call — including the overwhelmingly
    common "who represents me today" — would spend that budget to answer a
    question nobody asked.
    """
    write_current(store, DISTRICT_ID, valid_from="2025-01-03", name="Current")
    before = len(transport.calls)
    store.get(DISTRICT_ID)
    assert len(transport.calls) - before == 1


def test_get_as_of_today_returns_the_current_version(store: DistrictStore) -> None:
    write_current(store, DISTRICT_ID, valid_from="2025-01-03", name="Current")
    found = store.get(DISTRICT_ID, as_of=date(2026, 11, 3))
    assert found is not None
    assert found.name == "Current"


def test_get_as_of_a_date_before_the_district_existed_is_none(store: DistrictStore) -> None:
    write_current(store, DISTRICT_ID, valid_from="2025-01-03", name="Current")
    assert store.get(DISTRICT_ID, as_of=date(2024, 1, 1)) is None


def test_get_as_of_picks_the_version_whose_range_contains_the_date(
    store: DistrictStore,
) -> None:
    store.record_version(
        DISTRICT_ID,
        body_id="us-house",
        name="Old map",
        state="ZZ",
        valid_from="2013-01-03",
        valid_to="2022-12-31",
    )
    write_current(store, DISTRICT_ID, valid_from="2023-01-03", name="New map")

    old = store.get(DISTRICT_ID, as_of=date(2016, 11, 8))
    new = store.get(DISTRICT_ID, as_of=date(2026, 11, 3))
    assert old is not None and old.name == "Old map"
    assert new is not None and new.name == "New map"


def test_get_as_of_ignores_other_districts_versions(store: DistrictStore) -> None:
    """Two districts' versions must not be confused for one another's.

    The scan matches on the OCD-ID prefix, and `.../cd:1` is a prefix of
    `.../cd:19` as a plain string — so a naive `startswith` would hand district
    1's voters district 19's map. This is the test that pins the separator.
    """
    store.record_version(
        DISTRICT_ID,
        body_id="us-house",
        name="District 1 old",
        state="ZZ",
        valid_from="2013-01-03",
        valid_to="2022-12-31",
    )
    neighbour = "ocd-division/country:us/state:zz/cd:19"
    store.record_version(
        neighbour,
        body_id="us-house",
        name="District 19 old",
        state="ZZ",
        valid_from="2013-01-03",
        valid_to="2022-12-31",
    )

    found = store.get(DISTRICT_ID, as_of=date(2016, 11, 8))
    assert found is not None
    assert found.name == "District 1 old"


def test_get_as_of_for_an_unknown_district_is_none(store: DistrictStore) -> None:
    assert store.get("ocd-division/country:us/state:zz/cd:404", as_of=date(2026, 1, 1)) is None


def test_versions_lists_every_version_oldest_first(store: DistrictStore) -> None:
    store.record_version(
        DISTRICT_ID,
        body_id="us-house",
        name="Old map",
        state="ZZ",
        valid_from="2013-01-03",
        valid_to="2022-12-31",
    )
    write_current(store, DISTRICT_ID, valid_from="2023-01-03", name="New map")
    assert [v.name for v in store.versions(DISTRICT_ID)] == ["Old map", "New map"]


# --- recording a redistricting -------------------------------------------


def test_redistrict_closes_the_old_version_the_day_before_the_new_one(
    store: DistrictStore,
) -> None:
    write_current(store, DISTRICT_ID, valid_from="2023-01-03", name="Old map")
    result = store.redistrict(DISTRICT_ID, name="New map", valid_from="2027-01-03")

    assert result.superseded.valid_to == "2027-01-02"
    assert result.current.valid_from == "2027-01-03"
    assert result.current.valid_to is None


def test_redistrict_leaves_no_gap_and_no_overlap(store: DistrictStore) -> None:
    write_current(store, DISTRICT_ID, valid_from="2023-01-03", name="Old map")
    store.redistrict(DISTRICT_ID, name="New map", valid_from="2027-01-03")

    # Every day across the boundary resolves to exactly one version.
    assert store.get(DISTRICT_ID, as_of=date(2027, 1, 2)).name == "Old map"
    assert store.get(DISTRICT_ID, as_of=date(2027, 1, 3)).name == "New map"


def test_redistrict_keeps_the_current_version_under_the_plain_ocd_id(
    store: DistrictStore, districts: Repository[District]
) -> None:
    """The ballot endpoint reads the plain OCD-ID, so the new map must own it."""
    write_current(store, DISTRICT_ID, valid_from="2023-01-03", name="Old map")
    store.redistrict(DISTRICT_ID, name="New map", valid_from="2027-01-03")

    plain = districts.get(DISTRICT_ID)
    assert plain is not None
    assert plain.name == "New map"
    assert plain.valid_to is None


def test_redistrict_is_idempotent(store: DistrictStore) -> None:
    """Running the same redistricting twice leaves two versions, not three."""
    write_current(store, DISTRICT_ID, valid_from="2023-01-03", name="Old map")
    store.redistrict(DISTRICT_ID, name="New map", valid_from="2027-01-03")
    store.redistrict(DISTRICT_ID, name="New map", valid_from="2027-01-03")

    assert len(store.versions(DISTRICT_ID)) == 2


def test_redistrict_refuses_a_date_at_or_before_the_current_version_start(
    store: DistrictStore,
) -> None:
    """A new map cannot begin before the map it replaces did."""
    write_current(store, DISTRICT_ID, valid_from="2023-01-03", name="Old map")
    with pytest.raises(OverlappingValidityError):
        store.redistrict(DISTRICT_ID, name="New map", valid_from="2023-01-03")


def test_redistrict_on_an_unknown_district_writes_the_first_version(
    store: DistrictStore,
) -> None:
    """Nothing to supersede is not an error: it is the first version."""
    result = store.redistrict(DISTRICT_ID, name="First map", valid_from="2027-01-03")
    assert result.superseded is None
    assert result.current.valid_from == "2027-01-03"


# --- integrity -----------------------------------------------------------


def test_record_version_refuses_an_overlapping_range(store: DistrictStore) -> None:
    write_current(store, DISTRICT_ID, valid_from="2023-01-03", name="Current")
    with pytest.raises(OverlappingValidityError) as raised:
        store.record_version(
            DISTRICT_ID,
            body_id="us-house",
            name="Overlapping",
            state="ZZ",
            valid_from="2024-01-01",
            valid_to="2025-01-01",
        )
    assert DISTRICT_ID in str(raised.value)


def test_record_version_allows_an_adjacent_range(store: DistrictStore) -> None:
    """Touching but not overlapping: the day after one ends the next begins."""
    store.record_version(
        DISTRICT_ID,
        body_id="us-house",
        name="Old map",
        state="ZZ",
        valid_from="2013-01-03",
        valid_to="2022-12-31",
    )
    write_current(store, DISTRICT_ID, valid_from="2023-01-01", name="New map")
    assert len(store.versions(DISTRICT_ID)) == 2


def test_record_version_restating_one_version_is_an_upsert_not_an_overlap(
    store: DistrictStore,
) -> None:
    """Re-ingesting the identical version must stay idempotent (rule 1)."""
    write_current(store, DISTRICT_ID, valid_from="2023-01-03", name="Current")
    write_current(store, DISTRICT_ID, valid_from="2023-01-03", name="Current, restated")

    versions = store.versions(DISTRICT_ID)
    assert len(versions) == 1
    assert versions[0].name == "Current, restated"


def test_the_model_refuses_a_valid_to_before_its_valid_from() -> None:
    with pytest.raises(ValueError, match="valid_to"):
        District(
            id=DISTRICT_ID,
            body_id="us-house",
            name="Backwards",
            state="ZZ",
            valid_from="2025-01-03",
            valid_to="2024-01-01",
        )


def test_the_model_refuses_an_unparseable_valid_from() -> None:
    with pytest.raises(ValueError, match="valid_from"):
        District(
            id=DISTRICT_ID,
            body_id="us-house",
            name="Unparseable",
            state="ZZ",
            valid_from="January 2025",
        )


def test_the_model_still_accepts_the_shape_already_live(
    districts: Repository[District],
) -> None:
    """All seven live rows are `valid_from` set, `valid_to` null. Keep reading them."""
    stored = districts.upsert(
        {
            "id": "ocd-division/country:us/state:wa/cd:7",
            "body_id": "us-house",
            "name": "Congressional District 7",
            "state": "WA",
            "valid_from": DEFAULT_VALID_FROM,
            "valid_to": None,
        }
    )
    assert stored.valid_from == DEFAULT_VALID_FROM
    assert stored.valid_to is None
