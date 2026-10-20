"""Step definitions for tests/features/house_bills.feature (story 5.3).

Runs entirely against story 1.2's in-memory fake transport and the fixture at
tests/fixtures/house_bills_sample.json -- never the live network, so CI needs no
CONGRESS_GOV_API_KEY, per CLAUDE.md. The fixture holds REAL, live-captured
Congress.gov bill responses (H.R. 3424 and H.R. 1 of the 119th), which is why
the "a bill with no cosponsors" scenario exists at all: the real H.R. 1 detail
response omits the `cosponsors` block entirely rather than reporting zero.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.house_bills import (
    HouseBillLoader,
    transform_bill,
)
from app.ingest.house_rollcalls import HouseRollCallLoader, transform_roll_call
from app.repositories import Repository
from app.repositories.models import Bill, Person, RollCall
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/house_bills.feature")

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "house_bills_sample.json"
MEMBER_VOTES_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "house_member_votes_sample.json"
)


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )


@pytest.fixture
def loader(zerodb: ZeroDBClient) -> HouseBillLoader:
    return HouseBillLoader(zerodb, pace=lambda: None)


@pytest.fixture
def result() -> dict[str, Any]:
    return {}


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text())


def _sample(sample_key: str) -> dict[str, Any]:
    return copy.deepcopy(_fixture()[sample_key])


def _all_bioguides() -> set[str]:
    out: set[str] = set()
    for key, entry in _fixture().items():
        if key.startswith("_"):
            continue
        for sponsor in entry["detail"]["bill"].get("sponsors") or []:
            if sponsor.get("bioguideId"):
                out.add(str(sponsor["bioguideId"]))
        for cosponsor in entry["cosponsors"].get("cosponsors") or []:
            if cosponsor.get("bioguideId"):
                out.add(str(cosponsor["bioguideId"]))
    return out


# --- background --------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="bills_client")
def bills_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def reference_tables(zerodb: ZeroDBClient) -> None:
    Bootstrapper(zerodb, pace=lambda: None).run()


@given("every sponsor in the sample bill responses exists as a people row")
def sponsors_exist(zerodb: ZeroDBClient) -> None:
    """`bills.sponsors[].person_id` must resolve to a `people` row.

    Seeded here rather than written by this loader for the same reason the
    Senate story seeds its senators: the real `people` write is story 2.2's
    `legislators.py`, and a sponsorship ingest inventing a `people` row from a
    bill's own sponsor block would create a person record with no term history,
    which is worse than skipping the sponsorship.
    """
    people: Repository[Person] = Repository(client=zerodb, model=Person)
    for bioguide in sorted(_all_bioguides()):
        people.upsert({"id": f"person:{bioguide}", "canonical_name": f"Rep. {bioguide}"})


# --- ingesting ---------------------------------------------------------------


def _ingest(loader: HouseBillLoader, result: dict[str, Any], sample_key: str) -> None:
    sample = _sample(sample_key)
    row, skipped = transform_bill(
        detail=sample["detail"],
        cosponsors=sample["cosponsors"],
        subjects=sample["subjects"],
    )
    report = loader.load([row])
    result["report"] = report
    result["last_row"] = row
    result["sponsors_skipped"] = skipped


@when(parsers.parse('I ingest the sample bill "{sample_key}"'))
def ingest_bill(loader: HouseBillLoader, result: dict[str, Any], sample_key: str) -> None:
    _ingest(loader, result, sample_key)


@given(parsers.parse('I have ingested the sample bill "{sample_key}"'))
def given_ingested_bill(
    loader: HouseBillLoader, fake: FakeZeroDBTransport, result: dict[str, Any], sample_key: str
) -> None:
    _ingest(loader, result, sample_key)
    fake.calls.clear()


@when(
    parsers.parse(
        'I ingest the sample bill "{sample_key}" with bioguide "{bioguide}" missing from people'
    )
)
def ingest_bill_missing_person(
    zerodb: ZeroDBClient,
    loader: HouseBillLoader,
    result: dict[str, Any],
    sample_key: str,
    bioguide: str,
) -> None:
    zerodb.delete_row("people", f"person:{bioguide}")
    _ingest(loader, result, sample_key)


@when(parsers.parse('the source adds a cosponsor to bill "{bill_id}" and I ingest it again'))
def source_adds_cosponsor(
    loader: HouseBillLoader, zerodb: ZeroDBClient, result: dict[str, Any], bill_id: str
) -> None:
    """A real, recurring case: a bill gains cosponsors over its life, so the
    same bill is re-fetched and must upsert in place with a longer
    `sponsors[]` -- not duplicate, and not silently keep the stale list.
    """
    sample = _sample("bill_with_cosponsors")
    added = copy.deepcopy(sample["cosponsors"]["cosponsors"][0])
    added["bioguideId"] = "N000190"
    added["fullName"] = "Rep. Newcomer, Nora [D-XX-1]"
    added["sponsorshipDate"] = "2025-10-01"
    added["isOriginalCosponsor"] = False
    sample["cosponsors"]["cosponsors"].append(added)
    people: Repository[Person] = Repository(client=zerodb, model=Person)
    people.upsert({"id": "person:N000190", "canonical_name": "Rep. N000190"})
    row, skipped = transform_bill(
        detail=sample["detail"],
        cosponsors=sample["cosponsors"],
        subjects=sample["subjects"],
    )
    assert row["id"] == bill_id
    result["report"] = loader.load([row])
    result["sponsors_skipped"] = skipped


@when(
    parsers.parse(
        'I ingest the live-shaped House roll call "{rollcall_key}" linked to ingested bills'
    )
)
def ingest_rollcall_linked(zerodb: ZeroDBClient, result: dict[str, Any], rollcall_key: str) -> None:
    """Story 5.1 left `roll_calls.bill_id` null on purpose: it had no `bills`
    rows to point at, and pointing at an unverified id would have violated the
    reference check. With 5.3's `bills` rows in place, `bill_id_for` resolves
    the roll call's own `legislationType`/`legislationNumber` to a bill id and
    returns it ONLY if that bill is actually stored.
    """
    from app.ingest.house_bills import resolve_roll_call_bill_id

    envelope = copy.deepcopy(json.loads(MEMBER_VOTES_FIXTURE_PATH.read_text())[rollcall_key])
    row = transform_roll_call(envelope)
    row["bill_id"] = resolve_roll_call_bill_id(envelope, client=zerodb)
    rollcall_loader = HouseRollCallLoader(zerodb, pace=lambda: None)
    result["report"] = rollcall_loader.load([row])
    result["last_row"] = row


# --- assertions --------------------------------------------------------------


def _bill(zerodb: ZeroDBClient, bill_id: str) -> Bill:
    repo: Repository[Bill] = Repository(client=zerodb, model=Bill)
    stored = repo.get(bill_id)
    assert stored is not None, f"{bill_id} not stored"
    return stored


@then(parsers.parse('the bill "{bill_id}" is stored'))
def bill_stored(zerodb: ZeroDBClient, bill_id: str) -> None:
    _bill(zerodb, bill_id)


@then(parsers.parse('the bill "{bill_id}" has title "{title}"'))
def bill_title(zerodb: ZeroDBClient, bill_id: str, title: str) -> None:
    assert _bill(zerodb, bill_id).title == title


@then(parsers.parse('the bill "{bill_id}" has introduced_on "{introduced_on}"'))
def bill_introduced_on(zerodb: ZeroDBClient, bill_id: str, introduced_on: str) -> None:
    assert _bill(zerodb, bill_id).introduced_on == introduced_on


@then(parsers.parse('the bill "{bill_id}" has policy_area "{policy_area}"'))
def bill_policy_area(zerodb: ZeroDBClient, bill_id: str, policy_area: str) -> None:
    assert _bill(zerodb, bill_id).policy_area == policy_area


@then(parsers.parse('the bill "{bill_id}" has {count:d} sponsors'))
def bill_sponsor_count(zerodb: ZeroDBClient, bill_id: str, count: int) -> None:
    sponsors = _bill(zerodb, bill_id).sponsors
    assert len(sponsors) == count, (
        f"expected {count} sponsors, got {len(sponsors)}: "
        f"{[(s.person_id, s.role) for s in sponsors]}"
    )


@then(parsers.parse('the bill "{bill_id}" has a sponsor "{person_id}" with role "{role}"'))
def bill_has_sponsor_with_role(
    zerodb: ZeroDBClient, bill_id: str, person_id: str, role: str
) -> None:
    sponsors = _bill(zerodb, bill_id).sponsors
    matching = [s for s in sponsors if s.person_id == person_id]
    assert matching, f"{person_id} not among {[s.person_id for s in sponsors]}"
    assert matching[0].role == role


@then(parsers.parse('the bill "{bill_id}" has no sponsor "{person_id}"'))
def bill_has_no_sponsor(zerodb: ZeroDBClient, bill_id: str, person_id: str) -> None:
    sponsors = _bill(zerodb, bill_id).sponsors
    assert person_id not in [s.person_id for s in sponsors]


@then(parsers.parse('the sponsor "{person_id}" on bill "{bill_id}" has joined_on "{joined_on}"'))
def sponsor_joined_on(zerodb: ZeroDBClient, person_id: str, bill_id: str, joined_on: str) -> None:
    sponsors = [s for s in _bill(zerodb, bill_id).sponsors if s.person_id == person_id]
    assert sponsors, f"{person_id} not a sponsor of {bill_id}"
    assert sponsors[0].joined_on == joined_on


@then(parsers.parse('the bill "{bill_id}" has subject "{subject}"'))
def bill_has_subject(zerodb: ZeroDBClient, bill_id: str, subject: str) -> None:
    subjects = _bill(zerodb, bill_id).subjects
    assert subject in subjects, f"{subject!r} not among {subjects}"


@then(parsers.parse('the bill "{bill_id}" has no topic_ids'))
def bill_has_no_topic_ids(zerodb: ZeroDBClient, bill_id: str) -> None:
    assert _bill(zerodb, bill_id).topic_ids == []


@then(parsers.parse('{count:d} bill lists "{person_id}" as a sponsor'))
def bills_listing_sponsor(zerodb: ZeroDBClient, count: int, person_id: str) -> None:
    """The read story 6.3 will do: find every bill this person sponsored.

    Asserted here so 5.3's own shape is pinned against that future read, not so
    6.3's blending logic is built now.
    """
    repo: Repository[Bill] = Repository(client=zerodb, model=Bill)
    matching = [
        bill
        for bill in repo.query(limit=1000)
        if any(s.person_id == person_id and s.role == "sponsor" for s in bill.sponsors)
    ]
    assert len(matching) == count


@then(parsers.parse('{count:d} bill lists "{person_id}" as a cosponsor'))
def bills_listing_cosponsor(zerodb: ZeroDBClient, count: int, person_id: str) -> None:
    repo: Repository[Bill] = Repository(client=zerodb, model=Bill)
    matching = [
        bill
        for bill in repo.query(limit=1000)
        if any(
            s.person_id == person_id and s.role in ("cosponsor", "original_cosponsor")
            for s in bill.sponsors
        )
    ]
    assert len(matching) == count


@then(parsers.parse('exactly 1 row carries the bill id "{bill_id}"'))
def exactly_one_bill_row(zerodb: ZeroDBClient, bill_id: str) -> None:
    rows = zerodb.query_rows(Bill.table_name, limit=1000).rows
    matching = [row for row in rows if row.row_data.get("id") == bill_id]
    assert len(matching) == 1, f"expected 1 row for {bill_id}, found {len(matching)}"


# --- provenance ----------------------------------------------------------------


def _provenance_rows_for(fake: FakeZeroDBTransport, entity_id: str) -> list[dict[str, Any]]:
    """Mirrors test_house_rollcalls_steps.py's own helper, for the `bills` table."""
    stored = fake.rows.get("provenance_log", {})
    return [
        row["row_data"]
        for row in stored.values()
        if row["row_data"].get("entity_table") == "bills"
        and row["row_data"].get("entity_id") == entity_id
    ]


@then(parsers.parse('a provenance_log entry exists for bills "{entity_id}"'))
def provenance_entry_exists(
    fake: FakeZeroDBTransport, entity_id: str, result: dict[str, Any]
) -> None:
    rows = _provenance_rows_for(fake, entity_id)
    assert rows, f"no provenance_log entry for bills {entity_id!r}"
    result["provenance_row"] = rows[0]


@then(parsers.parse('that provenance_log entry has adapter_version "{adapter_version}"'))
def provenance_entry_adapter_version(result: dict[str, Any], adapter_version: str) -> None:
    assert result["provenance_row"]["adapter_version"] == adapter_version


@then(parsers.parse('exactly {count:d} provenance_log row exists for bills "{entity_id}"'))
@then(parsers.parse('exactly {count:d} provenance_log rows exist for bills "{entity_id}"'))
def exact_provenance_row_count(fake: FakeZeroDBTransport, count: int, entity_id: str) -> None:
    rows = _provenance_rows_for(fake, entity_id)
    assert len(rows) == count, f"found {len(rows)} provenance_log row(s), expected {count}"


@then(parsers.parse('the roll call "{roll_call_id}" has bill_id "{bill_id}"'))
def roll_call_bill_id(zerodb: ZeroDBClient, roll_call_id: str, bill_id: str) -> None:
    repo: Repository[RollCall] = Repository(client=zerodb, model=RollCall)
    stored = repo.get(roll_call_id)
    assert stored is not None
    assert stored.bill_id == bill_id


@then(parsers.parse('the roll call "{roll_call_id}" has no bill_id'))
def roll_call_no_bill_id(zerodb: ZeroDBClient, roll_call_id: str) -> None:
    repo: Repository[RollCall] = Repository(client=zerodb, model=RollCall)
    stored = repo.get(roll_call_id)
    assert stored is not None
    assert stored.bill_id is None
