"""Step definitions for story 2.3's address -> district feature.

Every scenario drives a **captured** Census Geocoder response rather than the
live service: CI must not depend on census.gov being reachable (CLAUDE.md), and
only a captured response can pin a shape the live service will not reproduce on
demand (seven matches across six districts, say). The captures are real — see
``tests/fixtures/census_geocoder_responses.json`` and its README note — so a
fixture-driven test is testing the real response shape, not an invented one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.ballot.service import BallotService
from app.districts.resolver import (
    AddressResolutionError,
    CensusAddressResolver,
    ResolvedAddress,
)
from app.main import create_app, zerodb_client
from app.repositories import Repository
from app.repositories.models import District
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport
from tests.support.reference_data import REFERENCE_TABLES, seed_reference_data

scenarios("../features/address_to_district.feature")

_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "census_geocoder_responses.json"
)
FIXTURES = json.loads(_FIXTURE_PATH.read_text())

#: The address every scenario submits. A real, public, non-residential
#: address (the Space Needle), never a person's home: prd.md forbids real voter
#: data in fixtures, logs or test output, and an address is voter data.
ADDRESS = "400 Broad St, Seattle, WA 98109"


@dataclass
class FakeCensusTransport:
    """Answers one canned Census response, recording what it was asked.

    Takes the place of ``httpx`` inside ``CensusAddressResolver`` so no test
    makes a network call. Recording the calls is what lets the "never called"
    and "address never logged" scenarios assert on absence rather than hope.
    """

    payload: Any = None
    status_code: int = 200
    raise_error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def get(self, url: str, *, params: dict[str, Any], timeout: float) -> httpx.Response:
        self.calls.append({"url": url, "params": dict(params)})
        if self.raise_error is not None:
            raise self.raise_error
        return httpx.Response(
            status_code=self.status_code,
            json=self.payload,
            request=httpx.Request("GET", url, params=params),
        )


@dataclass
class ResolveContext:
    transport: FakeCensusTransport | None = None
    resolved: ResolvedAddress | None = None
    error: Exception | None = None
    representative_district_id: str | None = None
    response: Any = None
    zerodb: ZeroDBClient | None = None


@pytest.fixture
def ctx() -> ResolveContext:
    return ResolveContext()


@pytest.fixture
def zdb() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    for table in (*REFERENCE_TABLES, "districts", "people"):
        client.create_table(table, {"id": "string"})
    # Story 2.3 declared `districts.body_id` a reference to `bodies`, so the
    # chambers must exist before the resolver's lazily-created district row can
    # be written — which is the whole point of the ballot-endpoint scenario.
    seed_reference_data(client)
    return client


def _resolver(ctx: ResolveContext) -> CensusAddressResolver:
    assert ctx.transport is not None
    return CensusAddressResolver(http_get=ctx.transport.get)


# --- Given ----------------------------------------------------------------


@given("the Census Geocoder matches an address in Washington's 7th district")
def census_matches_wa_07(ctx: ResolveContext) -> None:
    ctx.transport = FakeCensusTransport(payload=FIXTURES["seattle_wa_07"])


@given(parsers.parse('the Census Geocoder returns congressional district "{cd}" in "{state}"'))
def census_returns_cd(ctx: ResolveContext, cd: str, state: str) -> None:
    key = {
        ("WY", "00"): "cheyenne_wy_at_large",
        ("VT", "00"): "montpelier_vt_at_large",
        ("DC", "98"): "washington_dc_delegate",
        ("PR", "98"): "san_juan_pr_resident_commissioner",
    }[(state, cd)]
    ctx.transport = FakeCensusTransport(payload=FIXTURES[key])


@given("the Census Geocoder finds no match for the address")
def census_no_match(ctx: ResolveContext) -> None:
    ctx.transport = FakeCensusTransport(payload=FIXTURES["no_match"])


@given("the Census Geocoder returns matches in several different districts")
def census_ambiguous(ctx: ResolveContext) -> None:
    ctx.transport = FakeCensusTransport(payload=FIXTURES["ambiguous_springfield"])


@given("the Census Geocoder returns two matches in the same district")
def census_agreeing(ctx: ResolveContext) -> None:
    ctx.transport = FakeCensusTransport(payload=FIXTURES["agreeing_two_matches_ma_01"])


@given("the Census Geocoder cannot be reached")
def census_unreachable(ctx: ResolveContext) -> None:
    ctx.transport = FakeCensusTransport(
        raise_error=httpx.ConnectTimeout("connection timed out", request=None)
    )


@given(parsers.parse('the district "{district_id}" exists in ZeroDB'))
def district_exists(ctx: ResolveContext, zdb: ZeroDBClient, district_id: str) -> None:
    Repository(client=zdb, model=District).upsert(
        {
            "id": district_id,
            "body_id": "us-house",
            "name": "Congressional District 7",
            "state": "WA",
            "valid_from": "2025-01-03",
        }
    )
    ctx.zerodb = zdb


@given("a sitting representative whose term district id came from story 2.2")
def representative_from_2_2(ctx: ResolveContext) -> None:
    """The exact `terms[].district_id` story 2.2's loader builds for WA-07.

    Not a hand-written string: `_provisional_district_id` is imported and run,
    so this step fails if the two schemes ever drift apart — which is the whole
    point of the scenario.
    """
    from app.ingest.legislators import HOUSE_BODY_ID, _provisional_district_id

    ctx.representative_district_id = _provisional_district_id(
        {"state": "WA", "district": 7}, HOUSE_BODY_ID
    )


# --- When -----------------------------------------------------------------


@when("the voter's address is resolved")
def resolve_ok(ctx: ResolveContext) -> None:
    ctx.resolved = _resolver(ctx).resolve(ADDRESS)


@when("the voter's address is resolved and fails")
def resolve_fails(ctx: ResolveContext, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG), pytest.raises(AddressResolutionError) as excinfo:
        _resolver(ctx).resolve(ADDRESS)
    ctx.error = excinfo.value


@when("an empty address is resolved and fails")
def resolve_empty(ctx: ResolveContext) -> None:
    ctx.transport = FakeCensusTransport(payload=FIXTURES["seattle_wa_07"])
    with pytest.raises(AddressResolutionError) as excinfo:
        _resolver(ctx).resolve("   ")
    ctx.error = excinfo.value


@when("the voter submits that address to the ballot endpoint")
def submit_to_ballot(ctx: ResolveContext, zdb: ZeroDBClient) -> None:
    app = create_app()
    app.dependency_overrides[zerodb_client] = lambda: zdb
    service = BallotService(client=zdb, resolver=_resolver(ctx))
    with TestClient(app):
        ctx.response = service.ballot_for(ADDRESS)


# --- Then -----------------------------------------------------------------


@then(parsers.parse('the resolved district id is "{district_id}"'))
def district_id_is(ctx: ResolveContext, district_id: str) -> None:
    assert ctx.resolved is not None
    assert ctx.resolved.district_id == district_id


@then(parsers.parse('the resolved senate district id is "{district_id}"'))
def senate_district_id_is(ctx: ResolveContext, district_id: str) -> None:
    assert ctx.resolved is not None
    assert ctx.resolved.senate_district_id == district_id


@then(parsers.parse('the resolved state is "{state}"'))
def state_is(ctx: ResolveContext, state: str) -> None:
    assert ctx.resolved is not None
    assert ctx.resolved.state == state


@then(parsers.parse('the resolved district id does not contain "{fragment}"'))
def district_id_lacks(ctx: ResolveContext, fragment: str) -> None:
    assert ctx.resolved is not None
    assert fragment not in ctx.resolved.district_id


@then("the error tells the voter their address could not be found")
def error_not_found(ctx: ResolveContext) -> None:
    assert ctx.error is not None
    message = str(ctx.error).lower()
    assert "could not find" in message or "couldn't find" in message
    # Helpful, not generic: it must say what to do next.
    assert "street" in message or "check" in message or "zip" in message


@then("the error tells the voter an address is required")
def error_required(ctx: ResolveContext) -> None:
    assert ctx.error is not None
    assert "address is required" in str(ctx.error).lower()


@then("the error asks the voter for a more complete address")
def error_ambiguous(ctx: ResolveContext) -> None:
    assert ctx.error is not None
    message = str(ctx.error).lower()
    assert "more than one" in message or "several" in message or "complete" in message


@then("the error tells the voter the lookup is temporarily unavailable")
def error_unavailable(ctx: ResolveContext) -> None:
    assert ctx.error is not None
    assert "temporarily" in str(ctx.error).lower()


@then("the error message does not contain the address")
def error_omits_address(ctx: ResolveContext) -> None:
    assert ctx.error is not None
    assert_address_absent(str(ctx.error))


@then("the Census Geocoder was never called")
def geocoder_never_called(ctx: ResolveContext) -> None:
    assert ctx.transport is not None
    assert ctx.transport.calls == []


@then("no log line records the address")
def no_log_has_address(caplog: pytest.LogCaptureFixture) -> None:
    for record in caplog.records:
        assert_address_absent(record.getMessage())
        assert_address_absent(str(record.args))


@then("no row written to ZeroDB contains the address")
def no_row_has_address(zdb: ZeroDBClient) -> None:
    for table in ("districts", "people"):
        page = zdb.query_rows(table, limit=500)
        for row in page.rows:
            assert_address_absent(json.dumps(row.row_data))


@then("the response does not echo the address back")
def response_omits_address(ctx: ResolveContext) -> None:
    assert ctx.response is not None
    assert_address_absent(ctx.response.model_dump_json())


@then("nothing the resolver returns carries the address")
def resolution_omits_address(ctx: ResolveContext) -> None:
    """`ResolvedAddress` has no field the address could travel in.

    Asserted structurally, not just by value: a future field named
    `raw_address` would pass a string comparison on today's data and then leak
    the moment something serialises the result.
    """
    assert set(ResolvedAddress.__dataclass_fields__) == {
        "district_id",
        "senate_district_id",
        "state",
    }


@then("the resolved district id equals that representative's term district id")
def district_matches_representative(ctx: ResolveContext) -> None:
    assert ctx.resolved is not None
    assert ctx.representative_district_id is not None
    assert ctx.resolved.district_id == ctx.representative_district_id


def assert_address_absent(text: str) -> None:
    """The address must appear nowhere, in any casing or normalised form.

    Checks the distinctive parts separately rather than the whole string: the
    Census Geocoder answers with the address normalised and upper-cased
    ("400 BROAD ST, SEATTLE, WA, 98109"), so a check for the submitted spelling
    alone would miss a leak of the geocoder's echo of it.
    """
    haystack = text.lower()
    for fragment in ("400 broad", "broad st", "98109"):
        assert fragment not in haystack, f"{fragment!r} leaked into {text!r}"
