"""Step definitions for story 2.7's redistricting feature.

Every scenario runs against story 1.2's in-memory fake ZeroDB (CLAUDE.md: CI
never reaches a live service), and every district here is either a real OCD-ID
with synthetic dates or a deliberately impossible one (`state:zz`) so no row
can be mistaken for live reference data.

Why the two-version district is synthetic
=========================================

Informed has exactly one source of district boundaries, and it answers for one
vintage: the current one. There is no historical boundary dataset here, so
every real district has exactly one version today. Asserting the time-bounded
query against real data would therefore assert nothing — which is why the
multi-version scenarios build their rows by hand and say so.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.ballot.router import ballot_service
from app.ballot.service import BallotService
from app.districts.store import DistrictStore, OverlappingValidityError
from app.main import create_app, zerodb_client
from app.repositories import Repository
from app.repositories.models import District
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport
from tests.support.reference_data import REFERENCE_TABLES, seed_reference_data
from tests.support.resolvers import FixedDistrictResolver

scenarios("../features/redistricting.feature")

#: An address no voter owns. prd.md forbids real voter data in fixtures.
SYNTHETIC_ADDRESS = "1600 Pennsylvania Ave, Washington, DC"


@pytest.fixture
def zdb() -> ZeroDBClient:
    """A fake ZeroDB with the reference rows a `districts` write needs."""
    transport = FakeZeroDBTransport()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    for table in (*REFERENCE_TABLES, "districts", "people"):
        client.create_table(table, {"id": "string"})
    seed_reference_data(client)
    return client


@pytest.fixture
def districts(zdb: ZeroDBClient) -> Repository[District]:
    return Repository(client=zdb, model=District)


@pytest.fixture
def store(zdb: ZeroDBClient) -> DistrictStore:
    return DistrictStore(client=zdb)


@pytest.fixture
def found() -> dict[str, Any]:
    """What the last lookup returned, and what the last write raised."""
    return {}


def _state_of(district_id: str) -> str:
    return district_id.split("state:")[1].split("/")[0].upper()


def _version_row(
    district_id: str, *, valid_from: str, valid_to: str | None, name: str
) -> dict[str, Any]:
    return {
        "id": district_id,
        "body_id": "us-house",
        "name": name,
        "state": _state_of(district_id),
        "valid_from": valid_from,
        "valid_to": valid_to,
    }


# --- Given ---------------------------------------------------------------


@given(parsers.parse('a current district "{district_id}" valid from "{valid_from}"'))
def current_district(
    districts: Repository[District],
    found: dict[str, Any],
    district_id: str,
    valid_from: str,
) -> None:
    """One open-ended version, stored under the plain OCD-ID.

    This is exactly the shape story 2.3 writes and the shape all seven live
    rows are in: `valid_from` set, `valid_to` null.
    """
    found["district_id"] = district_id
    number = district_id.rsplit(":", 1)[-1]
    districts.upsert(
        _version_row(
            district_id,
            valid_from=valid_from,
            valid_to=None,
            name=f"Congressional District {number}",
        )
    )


@given(
    parsers.parse('a redistricted district "{district_id}" with these versions'),
    target_fixture="versions",
)
def redistricted_district(
    store: DistrictStore,
    found: dict[str, Any],
    district_id: str,
    datatable: list[list[str]],
) -> list[dict[str, str]]:
    """Several versions of one district, written through the store.

    Written through `DistrictStore.record_version` rather than the repository
    directly, so the ids and the overlap check are the production ones — a
    fixture that hand-rolled the id scheme could not notice it changing.
    """
    found["district_id"] = district_id
    header, *rows = datatable
    written = []
    for row in rows:
        values = dict(zip(header, row, strict=True))
        store.record_version(
            district_id,
            body_id="us-house",
            name=values["name"],
            state=_state_of(district_id),
            valid_from=values["valid_from"],
            valid_to=values["valid_to"] or None,
        )
        written.append(values)
    return written


@given(parsers.parse('the district "{district_id}" exists in ZeroDB'))
def district_exists(districts: Repository[District], district_id: str) -> None:
    number = district_id.rsplit(":", 1)[-1]
    districts.upsert(
        _version_row(
            district_id,
            valid_from="2025-01-03",
            valid_to=None,
            name=f"Congressional District {number}",
        )
    )


# --- When ----------------------------------------------------------------


@when(parsers.parse('the district "{district_id}" is looked up with no date'))
def lookup_without_date(store: DistrictStore, found: dict[str, Any], district_id: str) -> None:
    found["district"] = store.get(district_id)


@when(parsers.parse('the district "{district_id}" is looked up as of "{as_of}"'))
def lookup_as_of(store: DistrictStore, found: dict[str, Any], district_id: str, as_of: str) -> None:
    found["district"] = store.get(district_id, as_of=date.fromisoformat(as_of))


@when(parsers.parse('that district is redistricted on "{effective}" as "{name}"'))
def redistrict(store: DistrictStore, found: dict[str, Any], effective: str, name: str) -> None:
    found["redistricted"] = store.redistrict(
        found["district_id"],
        name=name,
        valid_from=effective,
    )


@when(
    parsers.parse(
        'a version of that district valid from "{valid_from}" to "{valid_to}" is written and fails'
    )
)
def write_overlapping_version(
    store: DistrictStore, found: dict[str, Any], valid_from: str, valid_to: str
) -> None:
    with pytest.raises(OverlappingValidityError) as raised:
        store.record_version(
            found["district_id"],
            body_id="us-house",
            name="Overlapping synthetic version",
            state=_state_of(found["district_id"]),
            valid_from=valid_from,
            valid_to=valid_to,
        )
    found["error"] = raised.value


@when(
    parsers.parse(
        'a district version valid from "{valid_from}" to "{valid_to}" is validated and fails'
    )
)
def validate_backwards_range(found: dict[str, Any], valid_from: str, valid_to: str) -> None:
    with pytest.raises(ValueError) as raised:
        District(
            id="ocd-division/country:us/state:zz/cd:9",
            body_id="us-house",
            name="Backwards synthetic version",
            state="ZZ",
            valid_from=valid_from,
            valid_to=valid_to,
        )
    found["error"] = raised.value


@when("the voter submits an address to the ballot endpoint")
def submit_address(zdb: ZeroDBClient, found: dict[str, Any]) -> None:
    app = create_app()
    service = BallotService(client=zdb, resolver=FixedDistrictResolver())
    app.dependency_overrides[zerodb_client] = lambda: zdb
    app.dependency_overrides[ballot_service] = lambda: service
    with TestClient(app) as client:
        found["response"] = client.get("/v1/ballot", params={"address": SYNTHETIC_ADDRESS})


# --- Then ----------------------------------------------------------------


@then(parsers.parse('the lookup returns the district valid from "{valid_from}"'))
def returned_valid_from(found: dict[str, Any], valid_from: str) -> None:
    district = found["district"]
    assert district is not None, "expected a district for that date, found none"
    assert district.valid_from == valid_from


@then("the returned district is open-ended")
def returned_open_ended(found: dict[str, Any]) -> None:
    assert found["district"].valid_to is None


@then("the lookup finds no district for that date")
def no_district_for_date(found: dict[str, Any]) -> None:
    assert found["district"] is None


@then(parsers.parse('the lookup returns the district named "{name}"'))
def returned_named(found: dict[str, Any], name: str) -> None:
    district = found["district"]
    assert district is not None, "expected a district for that date, found none"
    assert district.name == name


@then(parsers.parse('the district "{district_id}" looked up as of "{as_of}" is named "{name}"'))
def lookup_is_named(store: DistrictStore, district_id: str, as_of: str, name: str) -> None:
    district = store.get(district_id, as_of=date.fromisoformat(as_of))
    assert district is not None, f"expected a district valid on {as_of}, found none"
    assert district.name == name


@then(parsers.parse('the superseded version\'s valid_to is "{valid_to}"'))
def superseded_valid_to(found: dict[str, Any], valid_to: str) -> None:
    assert found["redistricted"].superseded.valid_to == valid_to


@then("the current version is still stored under the plain OCD-ID")
def current_under_plain_id(districts: Repository[District], found: dict[str, Any]) -> None:
    district_id = found["district_id"]
    stored = districts.get(district_id)
    assert stored is not None, "the current version must keep the plain OCD-ID"
    assert stored.valid_to is None


@then("the write is refused as an overlap")
def refused_as_overlap(found: dict[str, Any]) -> None:
    assert isinstance(found["error"], OverlappingValidityError)


@then("the validation names valid_to as before valid_from")
def names_backwards_range(found: dict[str, Any]) -> None:
    message = str(found["error"])
    assert "valid_to" in message
    assert "valid_from" in message


@then(parsers.parse('the ballot names the district "{district_id}"'))
def ballot_names_district(found: dict[str, Any], district_id: str) -> None:
    response = found["response"]
    assert response.status_code == 200, response.text
    assert response.json()["district"]["id"] == district_id
