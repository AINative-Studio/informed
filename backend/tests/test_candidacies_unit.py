"""Unit tests for the OpenFEC candidacy loader (story 2.6).

`tests/features/elections_and_candidacies.feature` covers the acceptance
criterion end to end. These tests cover what Gherkin reads badly: the pure
transform's edge cases, the OCD-ID and status mapping tables in isolation, the
models' own validation, and `fetch_candidates`' paging — the last driven
through a stub transport rather than the network, because no FEC API key
exists in this environment and CI must not depend on OpenFEC being reachable
(CLAUDE.md).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.ingest.candidacies import (
    INGESTED_OFFICES,
    CandidacyLoader,
    CandidacySourceError,
    candidacies_from_results,
    candidacy_status_for,
    district_name_for,
    fec_person_id,
    fetch_candidates,
    ocd_district_id,
)
from app.repositories import Repository, RowValidationError
from app.repositories.models import Candidacy, Election, Person
from app.seed_elections import UPCOMING_ELECTIONS, US_2026_GENERAL, seeded_row_ids
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

# --- the seeded election ---------------------------------------------------


def test_the_seeded_election_matches_datamodels_documented_id_and_a_real_date() -> None:
    """datamodel.md's worked example and the real 2026 general are one row.

    2026-11-03 is the Tuesday after the first Monday of November 2026, which
    is what 2 U.S.C. 7 fixes as the federal election day. Pinned as a test
    because a drifted date here would silently misdate every candidacy id.
    """
    assert US_2026_GENERAL.id == "us-2026-11-03-general"
    assert US_2026_GENERAL.election_date == "2026-11-03"
    assert US_2026_GENERAL.kind == "general"
    assert US_2026_GENERAL.jurisdiction_id == "us-federal"


def test_only_one_election_is_seeded() -> None:
    """A row added here is seeded permanently into every environment.

    Mirrors `test_seed_unit.py`'s pin on the three reference rows, for the
    same reason: adding a second election is a decision for the story that
    needs it, not a drive-by edit.
    """
    assert seeded_row_ids() == {("elections", "us-2026-11-03-general")}
    assert len(UPCOMING_ELECTIONS) == 1


# --- OCD-ID mapping --------------------------------------------------------


@pytest.mark.parametrize(
    ("office", "state", "district_number", "expected"),
    [
        # A numbered House seat: padding stripped, which is the whole point.
        ("H", "PA", 2, "ocd-division/country:us/state:pa/cd:2"),
        ("H", "PA", 17, "ocd-division/country:us/state:pa/cd:17"),
        # FEC's `00` for an at-large House seat -> statewide, never `/cd:0`.
        ("H", "DE", 0, "ocd-division/country:us/state:de"),
        ("H", "WY", 0, "ocd-division/country:us/state:wy"),
        # A Senate seat is statewide whatever the district field says.
        ("S", "DE", 0, "ocd-division/country:us/state:de"),
        ("S", "CA", None, "ocd-division/country:us/state:ca"),
        # State is lower-cased; the source sends it upper.
        ("H", "ca", 19, "ocd-division/country:us/state:ca/cd:19"),
    ],
)
def test_ocd_district_id_matches_story_2_3s_confirmed_scheme(
    office: str, state: str, district_number: Any, expected: str
) -> None:
    assert ocd_district_id(office=office, state=state, district_number=district_number) == expected


def test_ocd_district_id_accepts_the_padded_string_form_too() -> None:
    """`district_number` is an int live, but the padded string must not break it.

    The source publishes both `district: "02"` and `district_number: 2`. The
    loader reads the int, and this pins that a caller passing the string form
    still gets an unpadded id rather than `/cd:02`.
    """
    assert (
        ocd_district_id(office="H", state="PA", district_number="02")
        == "ocd-division/country:us/state:pa/cd:2"
    )


def test_ocd_district_id_refuses_a_filing_with_no_state() -> None:
    with pytest.raises(ValueError, match="no state"):
        ocd_district_id(office="H", state="", district_number=2)


def test_district_name_for_builds_only_from_fields_the_filing_carries() -> None:
    assert district_name_for(office="H", state="pa", district_number=2) == (
        "PA congressional district 2"
    )
    assert district_name_for(office="H", state="DE", district_number=0) == (
        "DE at-large congressional district"
    )
    assert district_name_for(office="S", state="de", district_number=0) == (
        "DE statewide (U.S. Senate)"
    )


# --- person id scheme ------------------------------------------------------


def test_fec_person_id_is_deterministic_and_prefixed() -> None:
    """The id-scheme decision, pinned.

    Deterministic (same input, same id) is what makes the weekly re-pull an
    idempotent upsert instead of minting a new person every run; the `person:`
    prefix is datamodel.md's, and `fec-` records that this person entered
    Informed as an FEC filer with no congressional service record.
    """
    assert fec_person_id("H6PA02205") == "person:fec-H6PA02205"
    assert fec_person_id("H6PA02205") == fec_person_id("H6PA02205")
    assert fec_person_id("S6DE00263") != fec_person_id("H6PA02205")


# --- status mapping --------------------------------------------------------


@pytest.mark.parametrize(
    ("fec_code", "expected"),
    [
        ("C", "filed"),
        ("F", "filed"),
        ("N", "statement"),
        ("P", "withdrawn"),
        # Case and whitespace are tolerated; the live feed is consistent but a
        # one-character field is not worth failing a national ingest over.
        ("c", "filed"),
        (" N ", "statement"),
    ],
)
def test_candidacy_status_maps_every_documented_fec_code(fec_code: str, expected: str) -> None:
    assert candidacy_status_for({"candidate_status": fec_code}) == expected


@pytest.mark.parametrize("missing", [None, "", "Z", 7])
def test_an_unknown_status_falls_back_to_the_weakest_claim(missing: Any) -> None:
    """Never drop the candidate, never over-claim their legal status.

    The filing exists (FEC published it), so dropping it would hide a real
    name from a voter; asserting `filed` would claim a statutory candidacy the
    source did not give. `statement` is the honest middle.
    """
    assert candidacy_status_for({"candidate_status": missing}) == "statement"


# --- the pure transform ----------------------------------------------------


def _record(**overrides: Any) -> dict[str, Any]:
    """One live-shaped record, overridable per test."""
    record: dict[str, Any] = {
        "candidate_id": "H6PA02205",
        "name": "ARRIAGA, JESSICA",
        "party": "REP",
        "party_full": "REPUBLICAN PARTY",
        "office": "H",
        "state": "PA",
        "district": "02",
        "district_number": 2,
        "incumbent_challenge": "C",
        "candidate_status": "N",
        "election_years": [2026],
    }
    record.update(overrides)
    return record


def _transform(*records: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Any]]:
    return candidacies_from_results(records, election=US_2026_GENERAL)


def test_the_transform_maps_every_field_from_the_live_shape() -> None:
    rows, skipped = _transform(_record())
    assert skipped == []
    (row,) = rows
    assert row["election_id"] == "us-2026-11-03-general"
    assert row["district_id"] == "ocd-division/country:us/state:pa/cd:2"
    assert row["person_name"] == "ARRIAGA, JESSICA"
    assert row["party"] == "REP"
    assert row["status"] == "statement"
    # person_id and is_incumbent need the stored people table; the loader
    # fills them. The transform must not guess them.
    assert "person_id" not in row
    assert "is_incumbent" not in row


def test_the_transform_keeps_the_party_code_not_the_full_name() -> None:
    """One codebase for every party (prd.md). The code is the stable value."""
    rows, _ = _transform(_record(party="LIB", party_full="LIBERTARIAN PARTY"))
    assert rows[0]["party"] == "LIB"


def test_a_filing_with_no_party_gets_a_null_party_rather_than_a_guess() -> None:
    rows, _ = _transform(_record(party=None))
    assert rows[0]["party"] is None


def test_a_filing_for_another_cycle_is_skipped() -> None:
    """A 2024-only filer must not appear on a 2026 ballot."""
    rows, skipped = _transform(_record(candidate_id="H4OLD00001", election_years=[2024]))
    assert rows == []
    assert [skip.candidate_id for skip in skipped] == ["H4OLD00001"]
    assert "2026" in skipped[0].reason


def test_a_filing_running_in_several_cycles_including_this_one_is_kept() -> None:
    """`election_years: [2024, 2026]` is the common case for an incumbent."""
    rows, skipped = _transform(_record(election_years=[2024, 2026]))
    assert skipped == []
    assert len(rows) == 1


def test_a_filing_with_no_election_years_is_skipped_rather_than_assumed() -> None:
    """Assuming would put a candidate with no evidence of running on a ballot."""
    rows, skipped = _transform(_record(election_years=None))
    assert rows == []
    assert len(skipped) == 1


@pytest.mark.parametrize("office", ["P", "", "X", None])
def test_only_house_and_senate_are_ingested(office: Any) -> None:
    """'P' especially: there is no district to place a presidential run in."""
    rows, skipped = _transform(_record(office=office))
    assert rows == []
    assert len(skipped) == 1
    assert "office" in skipped[0].reason


def test_house_and_senate_are_the_two_ingested_offices() -> None:
    assert INGESTED_OFFICES == ("H", "S")
    assert "P" not in INGESTED_OFFICES


def test_a_filing_with_no_candidate_id_is_skipped() -> None:
    """Without FEC's id there is no stable person id and no idempotency."""
    rows, skipped = _transform(_record(candidate_id=None))
    assert rows == []
    assert skipped[0].candidate_id is None
    assert "candidate_id" in skipped[0].reason


def test_a_filing_with_no_name_is_skipped() -> None:
    rows, skipped = _transform(_record(name=None))
    assert rows == []
    assert "name" in skipped[0].reason


def test_a_filing_with_no_state_is_skipped_not_crashed() -> None:
    """`ocd_district_id` raises ValueError; the batch must survive it."""
    rows, skipped = _transform(_record(state=None))
    assert rows == []
    assert len(skipped) == 1


def test_one_bad_record_does_not_block_the_others() -> None:
    """The reason every loader collects skips instead of raising."""
    rows, skipped = _transform(
        _record(candidate_id="H6PA02205"),
        _record(candidate_id=None),
        _record(candidate_id="H6PA03286", district_number=3),
    )
    assert len(rows) == 2
    assert len(skipped) == 1


def test_a_non_object_record_is_skipped() -> None:
    rows, skipped = candidacies_from_results(["not a record"], election=US_2026_GENERAL)  # type: ignore[list-item]
    assert rows == []
    assert len(skipped) == 1


def test_an_election_with_an_unparseable_date_is_refused_loudly() -> None:
    """Not a skip: every filing would be silently accepted from every cycle."""
    broken = Election(
        id="us-bad-general",
        jurisdiction_id="us-federal",
        name="Broken",
        election_date="not-a-date",
        kind="general",
    )
    with pytest.raises(ValueError, match="election_date"):
        candidacies_from_results([_record()], election=broken)


# --- the models ------------------------------------------------------------


def test_candidacy_id_is_derived_with_the_person_prefix_intact() -> None:
    """datamodel.md documents `{person_id}`, prefix kept — unlike member_votes."""
    assert Candidacy.build_id(
        {
            "election_id": "us-2026-11-03-general",
            "district_id": "ocd-division/country:us/state:pa/cd:2",
            "person_id": "person:fec-H6PA02205",
        }
    ) == ("us-2026-11-03-general:ocd-division/country:us/state:pa/cd:2:person:fec-H6PA02205")


def test_candidacy_declares_all_three_references() -> None:
    """What makes datamodel.md's integrity rule real rather than aspirational."""
    assert Candidacy.references == {
        "election_id": "elections",
        "district_id": "districts",
        "person_id": "people",
    }


def test_candidacy_is_not_append_only() -> None:
    """datamodel.md rule 6 names verdicts, positions and provenance — not this.

    Which is what lets a weekly re-pull update a withdrawn candidate in place
    instead of appending a second row.
    """
    assert Candidacy.version_key is None


def test_an_out_of_vocabulary_candidacy_status_is_rejected() -> None:
    with pytest.raises(RowValidationError):
        _repository_for(Candidacy).upsert(
            {
                "election_id": "us-2026-11-03-general",
                "district_id": "ocd-division/country:us/state:pa/cd:2",
                "person_id": "person:fec-H6PA02205",
                "person_name": "ARRIAGA, JESSICA",
                "status": "running",
            },
            check_references=False,
        )


def test_an_out_of_vocabulary_election_kind_is_rejected() -> None:
    with pytest.raises(RowValidationError):
        _repository_for(Election).upsert(
            {
                "id": "us-2026-11-03-genral",
                "jurisdiction_id": "us-federal",
                "name": "Typo",
                "election_date": "2026-11-03",
                "kind": "genral",
            },
            check_references=False,
        )


def _repository_for(model: Any) -> Repository[Any]:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table(model.table_name, {"id": "string"})
    return Repository(client=client, model=model)


# --- incumbency, in isolation ---------------------------------------------


def test_a_sitting_member_running_for_a_different_seat_is_not_its_incumbent() -> None:
    """A representative running for Senate is a challenger for that seat.

    The clearest reason `is_incumbent` compares the SEAT and not just the
    person: showing them as the Senate seat's incumbent would tell a voter
    somebody holds an office they do not hold.
    """
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table("people", {"id": "string"})
    Repository(client=client, model=Person).upsert(
        {
            "id": "person:R000999",
            "canonical_name": "Rep Running For Senate",
            "external_ids": {"fec": "H6PA02205;S6PA00111"},
            "photo_url": None,
            "terms": [
                {
                    "body_id": "us-house",
                    "district_id": "ocd-division/country:us/state:pa/cd:2",
                    "party": "Democrat",
                    "start": "2025-01-03",
                    "end": "2027-01-03",
                }
            ],
        }
    )
    loader = CandidacyLoader(client, pace=lambda: None)

    # Incumbent of the House seat they actually hold...
    assert (
        loader._is_incumbent_for(
            person_id="person:R000999",
            district_id="ocd-division/country:us/state:pa/cd:2",
        )
        is True
    )
    # ...but not of the statewide Senate seat they are running for.
    assert (
        loader._is_incumbent_for(
            person_id="person:R000999", district_id="ocd-division/country:us/state:pa"
        )
        is False
    )


def test_a_person_with_no_row_is_never_incumbent() -> None:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table("people", {"id": "string"})
    loader = CandidacyLoader(client, pace=lambda: None)
    assert (
        loader._is_incumbent_for(
            person_id="person:fec-H6PA02205",
            district_id="ocd-division/country:us/state:pa/cd:2",
        )
        is False
    )


# --- fetching --------------------------------------------------------------


class _StubFECTransport(httpx.BaseTransport):
    """Answers `/candidates/` from canned pages, recording what was asked.

    A transport stub rather than a mocked `httpx.get`, so the real query
    string, paging loop and error handling are all exercised — and no network
    call is made, which is what lets this run in CI with no FEC key.
    """

    def __init__(self, pages: list[dict[str, Any]], *, status_code: int = 200) -> None:
        self.pages = pages
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"error": "nope"}, request=request)
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=self.pages[page - 1], request=request)


def _page(results: list[dict[str, Any]], *, page: int, pages: int) -> dict[str, Any]:
    return {
        "api_version": "1.0",
        "pagination": {"page": page, "pages": pages, "per_page": 100, "count": 150},
        "results": results,
    }


def test_fetch_candidates_walks_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """PA alone had 107 House filings live; one page would drop candidates."""
    stub = _StubFECTransport(
        [
            _page([_record(candidate_id=f"H6PA{n:05d}") for n in range(100)], page=1, pages=2),
            _page([_record(candidate_id=f"H6PA{n:05d}") for n in range(100, 107)], page=2, pages=2),
        ]
    )
    _patch_httpx(monkeypatch, stub)

    records = fetch_candidates(election_year=2026, office="H", state="PA", api_key="k")

    assert len(records) == 107
    assert len(stub.requests) == 2
    assert stub.requests[0].url.params["election_year"] == "2026"
    assert stub.requests[0].url.params["office"] == "H"
    assert stub.requests[0].url.params["state"] == "PA"
    assert stub.requests[1].url.params["page"] == "2"


def test_fetch_candidates_stops_after_a_single_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DE case: one page, `pages: 1`, and no second request is issued.

    Worth pinning separately from the multi-page test: an off-by-one in the
    loop's exit condition would re-fetch page 1 forever against a real API,
    which is exactly how a rate limit gets burned.
    """
    senate_filing = _record(candidate_id="S6DE00263", office="S")
    stub = _StubFECTransport([_page([senate_filing], page=1, pages=1)])
    _patch_httpx(monkeypatch, stub)

    records = fetch_candidates(election_year=2026, office="S", state="DE", api_key="k")

    assert len(records) == 1
    assert len(stub.requests) == 1


def test_fetch_candidates_omits_the_state_filter_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A national pull must not send `state=None` as a literal filter."""
    stub = _StubFECTransport([_page([_record()], page=1, pages=1)])
    _patch_httpx(monkeypatch, stub)

    fetch_candidates(election_year=2026, office="H", api_key="k")

    assert "state" not in stub.requests[0].url.params


def test_a_page_with_no_pagination_block_ends_the_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing `pages` must terminate, not loop.

    The live feed always sends it; a schema surprise that dropped it should
    yield the one page fetched rather than spin.
    """
    stub = _StubFECTransport([{"api_version": "1.0", "results": [_record()]}])
    _patch_httpx(monkeypatch, stub)

    records = fetch_candidates(election_year=2026, office="H", api_key="k")

    assert len(records) == 1
    assert len(stub.requests) == 1


def test_a_rate_limited_response_says_so_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 is by far most likely DEMO_KEY's 10/hour, not a transient blip.

    The error names it, because an operator seeing a bare 429 would retry
    rather than go and register a key.
    """
    stub = _StubFECTransport([], status_code=429)
    _patch_httpx(monkeypatch, stub)
    with pytest.raises(CandidacySourceError, match="DEMO_KEY"):
        fetch_candidates(election_year=2026, office="H", api_key="DEMO_KEY")


def test_a_403_is_reported_with_its_status(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubFECTransport([], status_code=403)
    _patch_httpx(monkeypatch, stub)
    with pytest.raises(CandidacySourceError, match="403"):
        fetch_candidates(election_year=2026, office="H", api_key="bad")


def test_a_body_with_no_results_array_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubFECTransport([{"api_version": "1.0", "pagination": {}}])
    _patch_httpx(monkeypatch, stub)
    with pytest.raises(CandidacySourceError, match="results"):
        fetch_candidates(election_year=2026, office="H", api_key="k")


def test_an_unreachable_host_is_reported_as_a_source_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("app.ingest.candidacies.httpx.get", boom)
    with pytest.raises(CandidacySourceError, match="could not reach"):
        fetch_candidates(election_year=2026, office="H", api_key="k")


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, stub: _StubFECTransport) -> None:
    """Route `httpx.get` through the stub transport, keeping real URL building."""

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=stub) as client:
            return client.get(url, params=kwargs.get("params"))

    monkeypatch.setattr("app.ingest.candidacies.httpx.get", fake_get)
