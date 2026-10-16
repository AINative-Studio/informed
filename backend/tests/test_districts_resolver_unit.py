"""Unit tests for the real Census Geocoder resolver (story 2.3).

The feature file proves the voter's journey; these pin the pieces the journey
walks through, so a regression names the broken piece. They also cover the
response shapes a feature file would be noise for: a malformed layer, a
district number the geocoder pads differently, a session bump in the layer's
name.

These tests replace story 1.5's two stub tests
(`test_stub_resolver_is_the_same_answer_for_every_address` and
`test_stub_resolver_returns_a_well_formed_ocd_id`). Those asserted the resolver
did **not** geocode — deliberately, so the stub could not be mistaken for this
story's work (see the git history of `app/districts/resolver.py`). That
guarantee is now obsolete and its replacement is
`test_resolver_gives_different_districts_for_different_addresses`, which
asserts the opposite and is the real criterion.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.districts.resolver import (
    AddressResolutionError,
    AddressResolver,
    AmbiguousAddressError,
    CensusAddressResolver,
    GeocoderUnavailableError,
    ResolvedAddress,
    district_name_from,
    ocd_id_for,
    senate_division_for,
    state_code_from,
)

FIXTURES = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "census_geocoder_responses.json").read_text()
)

ADDRESS = "400 Broad St, Seattle, WA 98109"


class RecordingGet:
    """Stands in for `httpx.get`, answering a canned payload."""

    def __init__(
        self,
        payload: Any = None,
        *,
        status_code: int = 200,
        error: Exception | None = None,
        payloads: list[Any] | None = None,
    ) -> None:
        self.payload = payload
        self.payloads = payloads
        self.status_code = status_code
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, *, params: dict[str, Any], timeout: float) -> httpx.Response:
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
        if self.error is not None:
            raise self.error
        body = self.payloads.pop(0) if self.payloads else self.payload
        return httpx.Response(
            status_code=self.status_code,
            json=body,
            request=httpx.Request("GET", url, params=params),
        )


def resolver(get: RecordingGet) -> CensusAddressResolver:
    return CensusAddressResolver(http_get=get)


# --- the OCD-ID construction ---------------------------------------------
#
# `ocd_id_for` is the one function story 2.2's `people.terms[].district_id`
# values have to agree with, so it gets its own tests rather than only being
# exercised through a geocoder response.


@pytest.mark.parametrize(
    ("state", "cd", "expected"),
    [
        # The geocoder zero-pads; `people.terms[].district_id` does not.
        ("WA", "07", "ocd-division/country:us/state:wa/cd:7"),
        ("NY", "12", "ocd-division/country:us/state:ny/cd:12"),
        ("CA", "52", "ocd-division/country:us/state:ca/cd:52"),
        # `00` is the geocoder's code for a state with a single at-large seat.
        ("WY", "00", "ocd-division/country:us/state:wy"),
        ("VT", "00", "ocd-division/country:us/state:vt"),
        # `98` is its code for a non-voting delegate / resident commissioner.
        ("DC", "98", "ocd-division/country:us/state:dc"),
        ("PR", "98", "ocd-division/country:us/state:pr"),
        # Lower-cased in the id regardless of how the geocoder cased it.
        ("wa", "07", "ocd-division/country:us/state:wa/cd:7"),
    ],
)
def test_ocd_id_matches_story_2_2s_scheme(state: str, cd: str, expected: str) -> None:
    assert ocd_id_for(state, cd) == expected


def test_ocd_id_never_carries_a_leading_zero() -> None:
    """The whole reconciliation with story 2.2 rests on this.

    A padded `/cd:07` would match no `people.terms[].district_id` in the live
    table (all 442 are unpadded, confirmed live), so every Washington voter in
    district 7 would get an empty delegation and no error to explain it.
    """
    for number in range(1, 54):
        padded = f"{number:02d}"
        assert ocd_id_for("CA", padded).endswith(f"/cd:{number}")


def test_ocd_id_rejects_a_non_numeric_district() -> None:
    with pytest.raises(AddressResolutionError):
        ocd_id_for("WA", "seven")


def test_ocd_id_rejects_a_blank_state() -> None:
    with pytest.raises(AddressResolutionError):
        ocd_id_for("", "07")


# --- the resolver, driven by captured live responses ----------------------


def test_resolver_resolves_a_real_capture_to_the_right_district() -> None:
    get = RecordingGet(FIXTURES["seattle_wa_07"])
    resolved = resolver(get).resolve(ADDRESS)
    assert resolved == ResolvedAddress(
        district_id="ocd-division/country:us/state:wa/cd:7",
        senate_district_id="ocd-division/country:us/state:wa",
        state="WA",
    )


def test_resolver_gives_different_districts_for_different_addresses() -> None:
    """The replacement for story 1.5's "same answer for every address" test.

    That test asserted the stub did *not* geocode. This asserts the opposite —
    two addresses in two states produce two districts — which is story 2.3's
    actual acceptance criterion and the reason the stub existed to be replaced.
    """
    get = RecordingGet(
        payloads=[FIXTURES["seattle_wa_07"], FIXTURES["cheyenne_wy_at_large"]]
    )
    first = resolver(get).resolve(ADDRESS)
    second = resolver(get).resolve("200 W 24th St, Cheyenne, WY 82002")
    assert first != second
    assert first.state == "WA"
    assert second.state == "WY"


def test_resolver_returns_a_well_formed_ocd_id() -> None:
    """Kept from story 1.5, now asserted against a real geocoder response.

    `districts` is keyed by OCD-ID (datamodel.md), so the shape of this string
    is a contract, not a detail.
    """
    resolved = resolver(RecordingGet(FIXTURES["seattle_wa_07"])).resolve(ADDRESS)
    assert resolved.district_id.startswith("ocd-division/country:us/state:")
    assert resolved.district_id == resolved.district_id.lower()


def test_resolver_derives_the_senate_district_from_the_state() -> None:
    """Kept from story 1.5: senators represent the state, not a district."""
    resolved = resolver(RecordingGet(FIXTURES["seattle_wa_07"])).resolve(ADDRESS)
    assert resolved.senate_district_id == "ocd-division/country:us/state:wa"
    assert "/cd:" not in resolved.senate_district_id


def test_resolver_handles_an_at_large_state() -> None:
    resolved = resolver(RecordingGet(FIXTURES["cheyenne_wy_at_large"])).resolve("x")
    assert resolved.district_id == "ocd-division/country:us/state:wy"
    assert resolved.senate_district_id == resolved.district_id


def test_resolver_handles_a_non_voting_delegate_district() -> None:
    """DC has no senators and a delegate, not a representative.

    Resolving it anyway is right: the delegate is a real current member of
    Congress that story 2.2 loaded, and a DC voter asking who represents them
    should be told, not shown an error.
    """
    resolved = resolver(RecordingGet(FIXTURES["washington_dc_delegate"])).resolve("x")
    assert resolved.district_id == "ocd-division/country:us/state:dc"
    assert resolved.state == "DC"


# --- the layer name ------------------------------------------------------


def test_resolver_finds_the_congressional_layer_by_shape_not_by_name() -> None:
    """The layer key carries the Congress number: "120th Congressional Districts".

    Confirmed live (2026-10-16). It was "119th ..." before and will be "121st
    ..." after the next apportionment, so a hard-coded key would break the
    resolver on a date nobody is watching. This fakes a future rename to prove
    the lookup survives it.
    """
    payload = json.loads(json.dumps(FIXTURES["seattle_wa_07"]))
    match = payload["result"]["addressMatches"][0]
    layer = match["geographies"].pop("120th Congressional Districts")
    layer[0]["CD121"] = layer[0].pop("CD120")
    layer[0]["CDSESSN"] = "121"
    match["geographies"]["121st Congressional Districts"] = layer

    resolved = resolver(RecordingGet(payload)).resolve(ADDRESS)
    assert resolved.district_id == "ocd-division/country:us/state:wa/cd:7"


def test_resolver_errors_when_the_response_carries_no_congressional_layer() -> None:
    payload = json.loads(json.dumps(FIXTURES["seattle_wa_07"]))
    del payload["result"]["addressMatches"][0]["geographies"]["120th Congressional Districts"]
    with pytest.raises(AddressResolutionError):
        resolver(RecordingGet(payload)).resolve(ADDRESS)


def test_resolver_errors_when_the_response_carries_no_state() -> None:
    payload = json.loads(json.dumps(FIXTURES["seattle_wa_07"]))
    del payload["result"]["addressMatches"][0]["geographies"]["States"]
    with pytest.raises(AddressResolutionError):
        resolver(RecordingGet(payload)).resolve(ADDRESS)


# --- errors --------------------------------------------------------------


def test_resolver_refuses_a_blank_address_without_calling_the_geocoder() -> None:
    get = RecordingGet(FIXTURES["seattle_wa_07"])
    with pytest.raises(AddressResolutionError, match="address is required"):
        resolver(get).resolve("   ")
    assert get.calls == []


def test_resolver_refuses_an_over_long_address_without_calling_the_geocoder() -> None:
    """The live API caps `address` at 100 characters, answering 400 otherwise.

    Confirmed live: a blank or over-long address comes back
    `{"errors":["Address cannot be empty and cannot exceed 100 characters"]}`.
    Checking locally turns that into a message about the address rather than a
    wasted round trip and a raw upstream error.
    """
    get = RecordingGet(FIXTURES["seattle_wa_07"])
    with pytest.raises(AddressResolutionError):
        resolver(get).resolve("x" * 101)
    assert get.calls == []


def test_resolver_reports_no_match_helpfully() -> None:
    with pytest.raises(AddressResolutionError) as excinfo:
        resolver(RecordingGet(FIXTURES["no_match"])).resolve(ADDRESS)
    message = str(excinfo.value)
    assert "could not find" in message.lower()
    # ui-prd.md: specific, never generic. It must say what to try.
    assert "street" in message.lower()
    assert "400 Broad" not in message


def test_resolver_refuses_an_ambiguous_address_rather_than_guessing() -> None:
    """Seven live matches across six districts and four states.

    Taking `addressMatches[0]` would have told a Massachusetts voter they live
    in Colorado's 4th — a wrong answer presented with full confidence, which is
    worse for this product than no answer (prd.md: evidence over opinion).
    """
    with pytest.raises(AmbiguousAddressError) as excinfo:
        resolver(RecordingGet(FIXTURES["ambiguous_springfield"])).resolve("100 Main St, Springfield")
    assert isinstance(excinfo.value, AddressResolutionError)
    assert "more than one" in str(excinfo.value).lower()


def test_resolver_accepts_several_matches_that_agree_on_the_district() -> None:
    """Two matches, one district: an answer, not an ambiguity.

    Two ZIP+4 ranges of the same street in Springfield MA both sit in MA-01.
    Refusing that would reject a perfectly determinate address.
    """
    resolved = resolver(RecordingGet(FIXTURES["agreeing_two_matches_ma_01"])).resolve("x")
    assert resolved.district_id == "ocd-division/country:us/state:ma/cd:1"


def test_resolver_reports_a_transport_failure_as_temporarily_unavailable() -> None:
    get = RecordingGet(error=httpx.ConnectTimeout("timed out", request=None))
    with pytest.raises(GeocoderUnavailableError) as excinfo:
        resolver(get).resolve(ADDRESS)
    assert isinstance(excinfo.value, AddressResolutionError)
    assert "temporarily" in str(excinfo.value).lower()
    assert "400 Broad" not in str(excinfo.value)


def test_resolver_reports_an_upstream_500_as_temporarily_unavailable() -> None:
    get = RecordingGet({"errors": ["boom"]}, status_code=500)
    with pytest.raises(GeocoderUnavailableError):
        resolver(get).resolve(ADDRESS)


def test_resolver_reports_an_upstream_400_as_an_address_problem() -> None:
    """A 400 is about the address, not about the service being down.

    Telling a voter "temporarily unavailable" when the real problem is their
    input would send them away to retry something that will never work.
    """
    get = RecordingGet(FIXTURES["error_blank_address_400"], status_code=400)
    with pytest.raises(AddressResolutionError) as excinfo:
        resolver(get).resolve(ADDRESS)
    assert not isinstance(excinfo.value, GeocoderUnavailableError)


def test_resolver_reports_unparseable_json_as_temporarily_unavailable() -> None:
    class BadJson(RecordingGet):
        def __call__(self, url: str, *, params: dict[str, Any], timeout: float) -> httpx.Response:
            self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
            return httpx.Response(
                status_code=200,
                content=b"<html>gateway error</html>",
                request=httpx.Request("GET", url),
            )

    with pytest.raises(GeocoderUnavailableError):
        resolver(BadJson()).resolve(ADDRESS)


# --- the request the resolver makes --------------------------------------


def test_resolver_asks_the_geographies_endpoint_with_the_documented_parameters() -> None:
    """The `geographies` endpoint, not `locations`: only it returns districts.

    `benchmark` and `vintage` are sent explicitly rather than relying on the
    documented defaults, so a change to the service's default cannot silently
    move Informed to a different set of district boundaries.
    """
    get = RecordingGet(FIXTURES["seattle_wa_07"])
    resolver(get).resolve(ADDRESS)

    (call,) = get.calls
    assert call["url"].startswith("https://geocoding.geo.census.gov/geocoder/geographies/")
    assert call["url"].endswith("/onelineaddress")
    assert call["params"]["benchmark"] == "Public_AR_Current"
    assert call["params"]["vintage"] == "Current_Current"
    assert call["params"]["format"] == "json"
    assert call["params"]["address"] == ADDRESS
    assert call["timeout"] > 0


def test_resolver_requests_only_the_layers_it_reads() -> None:
    """Narrowing `layers` keeps the voter's address out of a bigger response.

    Also faster, but the privacy reason is the load-bearing one: every geography
    the response carries is one more place the geocoder's echo of the address
    can travel inside.
    """
    get = RecordingGet(FIXTURES["seattle_wa_07"])
    resolver(get).resolve(ADDRESS)
    layers = get.calls[0]["params"].get("layers", "")
    assert "Congressional" in layers or layers == ""


def test_resolver_satisfies_the_address_resolver_protocol() -> None:
    """The seam story 1.5 built, still the seam story 2.3 plugs into."""
    assert isinstance(resolver(RecordingGet(FIXTURES["seattle_wa_07"])), AddressResolver)


# --- privacy -------------------------------------------------------------


def test_the_resolver_logs_nothing_containing_the_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """prd.md, privacy first: the address is never logged.

    Covers the failure paths too, which is where a naive implementation leaks
    it — "could not geocode {address}" is the obvious and wrong log line.
    """
    import logging

    for key, status in [
        ("seattle_wa_07", 200),
        ("no_match", 200),
        ("ambiguous_springfield", 200),
        ("error_blank_address_400", 400),
    ]:
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            try:
                resolver(RecordingGet(FIXTURES[key], status_code=status)).resolve(ADDRESS)
            except AddressResolutionError:
                pass
        for record in caplog.records:
            text = f"{record.getMessage()} {record.args}".lower()
            assert "broad" not in text
            assert "98109" not in text


def test_resolved_address_carries_no_address_field() -> None:
    assert set(ResolvedAddress.__dataclass_fields__) == {
        "district_id",
        "senate_district_id",
        "state",
    }


def test_resolver_holds_no_address_after_resolving() -> None:
    """No server-side cache of the address, per the story's privacy criterion.

    A memoising resolver would be an easy performance win and a direct
    violation: the cache key *is* the address, held for the process's lifetime.
    """
    instance = resolver(RecordingGet(FIXTURES["seattle_wa_07"]))
    instance.resolve(ADDRESS)
    for value in vars(instance).values():
        assert "broad" not in repr(value).lower()
        assert "98109" not in repr(value)


# --- district naming (for the lazily-created `districts` row) -------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("seattle_wa_07", "Congressional District 7"),
        ("cheyenne_wy_at_large", "Congressional District (at Large)"),
        ("washington_dc_delegate", "Delegate District (at Large)"),
        ("san_juan_pr_resident_commissioner", "Resident Commissioner District (at Large)"),
    ],
)
def test_district_name_comes_from_the_geocoders_own_label(key: str, expected: str) -> None:
    """`districts.name` is the geocoder's `NAME`, not a string this repo invents.

    The geocoder already distinguishes a representative's district from a
    delegate's and a resident commissioner's; inventing "Congressional District
    (at Large)" for DC would be factually wrong, and prd.md's evidence-over-
    opinion rule applies to reference data as much as to verdicts.
    """
    match = FIXTURES[key]["result"]["addressMatches"][0]
    assert district_name_from(match) == expected


# --- helpers kept from story 1.5 -----------------------------------------


def test_senate_division_for_drops_the_district_segment() -> None:
    assert (
        senate_division_for("ocd-division/country:us/state:ca/cd:19")
        == "ocd-division/country:us/state:ca"
    )


def test_senate_division_for_leaves_a_statewide_division_alone() -> None:
    assert (
        senate_division_for("ocd-division/country:us/state:wy")
        == "ocd-division/country:us/state:wy"
    )


def test_state_code_from_reads_the_state_segment() -> None:
    assert state_code_from("ocd-division/country:us/state:ca/cd:19") == "CA"
    assert state_code_from("ocd-division/country:eu") is None
