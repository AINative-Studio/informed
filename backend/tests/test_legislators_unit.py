"""Unit tests for app.ingest.legislators's transform logic (story 2.2).

The Gherkin feature (tests/features/load_legislators.feature) proves the
story's acceptance criteria end to end through the repository layer; these
tests pin the transform's edge cases directly, which is cheaper than routing
every boundary condition through a full repository/ZeroDB round trip.
"""

from __future__ import annotations

import httpx
import pytest

from app.ingest import legislators as legislators_module
from app.ingest.legislators import (
    LegislatorSourceError,
    fetch_legislators_yaml,
    parse_legislators_yaml,
    transform_legislators,
)


def _record(**overrides: object) -> dict:
    base = {
        "id": {"bioguide": "X000001", "govtrack": 123456},
        "name": {"official_full": "Jane X. Doe"},
        "terms": [
            {
                "type": "rep",
                "start": "2023-01-03",
                "end": "2025-01-03",
                "state": "CA",
                "district": 19,
                "party": "Democrat",
            }
        ],
    }
    base.update(overrides)
    return base


# --- fetching ------------------------------------------------------------


def test_fetch_returns_body_text_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, timeout: float, follow_redirects: bool) -> httpx.Response:
        return httpx.Response(200, text="- id:\n    bioguide: X000001\n")

    monkeypatch.setattr(legislators_module.httpx, "get", fake_get)
    text = fetch_legislators_yaml(url="https://example.test/legislators-current.yaml")
    assert "bioguide: X000001" in text


def test_fetch_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, timeout: float, follow_redirects: bool) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    monkeypatch.setattr(legislators_module.httpx, "get", fake_get)
    with pytest.raises(LegislatorSourceError, match="404"):
        fetch_legislators_yaml(url="https://example.test/moved.yaml")


def test_fetch_wraps_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, timeout: float, follow_redirects: bool) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(legislators_module.httpx, "get", fake_get)
    with pytest.raises(LegislatorSourceError, match="could not reach"):
        fetch_legislators_yaml(url="https://example.test/unreachable.yaml")


# --- parsing ------------------------------------------------------------


def test_parse_legislators_yaml_rejects_non_list() -> None:
    try:
        parse_legislators_yaml("not_a_list: true\n")
    except LegislatorSourceError:
        pass
    else:
        raise AssertionError("expected LegislatorSourceError")


def test_parse_legislators_yaml_rejects_invalid_yaml() -> None:
    try:
        parse_legislators_yaml("key: [unterminated\n")
    except LegislatorSourceError:
        pass
    else:
        raise AssertionError("expected LegislatorSourceError")


def test_parse_legislators_yaml_accepts_a_list_of_records() -> None:
    records = parse_legislators_yaml("- id:\n    bioguide: X000001\n")
    assert records == [{"id": {"bioguide": "X000001"}}]


# --- id and name ---------------------------------------------------------


def test_bioguide_becomes_the_person_id() -> None:
    rows, skipped = transform_legislators([_record()])
    assert not skipped
    assert rows[0]["id"] == "person:X000001"


def test_missing_bioguide_is_skipped_not_raised() -> None:
    record = _record()
    del record["id"]["bioguide"]
    rows, skipped = transform_legislators([record])
    assert rows == []
    assert len(skipped) == 1
    assert skipped[0].bioguide is None
    assert "bioguide" in skipped[0].reason


def test_canonical_name_falls_back_to_first_middle_last() -> None:
    record = _record(name={"first": "Jane", "middle": "X.", "last": "Doe"})
    rows, skipped = transform_legislators([record])
    assert not skipped
    assert rows[0]["canonical_name"] == "Jane X. Doe"


def test_record_with_no_usable_name_is_skipped() -> None:
    record = _record(name={})
    rows, skipped = transform_legislators([record])
    assert rows == []
    assert len(skipped) == 1
    assert skipped[0].bioguide == "X000001"


# --- external_ids ----------------------------------------------------------


def test_external_ids_are_stringified() -> None:
    record = _record(id={"bioguide": "X000001", "govtrack": 123456, "icpsr": 99999})
    rows, _ = transform_legislators([record])
    assert rows[0]["external_ids"]["govtrack"] == "123456"
    assert rows[0]["external_ids"]["icpsr"] == "99999"


def test_fec_list_is_joined_not_dropped() -> None:
    record = _record(id={"bioguide": "X000001", "fec": ["H1CA01234", "S2CA05678"]})
    rows, _ = transform_legislators([record])
    assert rows[0]["external_ids"]["fec"] == "H1CA01234;S2CA05678"


def test_empty_fec_list_is_omitted() -> None:
    record = _record(id={"bioguide": "X000001", "fec": []})
    rows, _ = transform_legislators([record])
    assert "fec" not in rows[0]["external_ids"]


def test_photo_url_is_always_none() -> None:
    rows, _ = transform_legislators([_record()])
    assert rows[0]["photo_url"] is None


# --- terms[]: body_id and district_id --------------------------------------


def test_rep_term_maps_to_us_house_with_cd_district_id() -> None:
    rows, _ = transform_legislators([_record()])
    term = rows[0]["terms"][0]
    assert term["body_id"] == "us-house"
    assert term["district_id"] == "ocd-division/country:us/state:ca/cd:19"


def test_sen_term_maps_to_us_senate_with_statewide_district_id() -> None:
    record = _record(
        terms=[
            {
                "type": "sen",
                "start": "2023-01-03",
                "end": "2029-01-03",
                "state": "WA",
                "class": 1,
                "party": "Democrat",
            }
        ]
    )
    rows, _ = transform_legislators([record])
    term = rows[0]["terms"][0]
    assert term["body_id"] == "us-senate"
    assert term["district_id"] == "ocd-division/country:us/state:wa"


def test_at_large_district_zero_gets_statewide_id_with_no_cd_segment() -> None:
    record = _record(
        terms=[
            {
                "type": "rep",
                "start": "2023-01-03",
                "end": "2025-01-03",
                "state": "DC",
                "district": 0,
                "party": "Democrat",
            }
        ]
    )
    rows, _ = transform_legislators([record])
    term = rows[0]["terms"][0]
    assert term["district_id"] == "ocd-division/country:us/state:dc"


def test_term_with_unrecognised_type_is_skipped_but_person_keeps_other_terms() -> None:
    record = _record(
        terms=[
            {"type": "weird", "start": "2023-01-03", "end": "2025-01-03", "state": "CA"},
            {
                "type": "rep",
                "start": "2023-01-03",
                "end": "2025-01-03",
                "state": "CA",
                "district": 19,
                "party": "Democrat",
            },
        ]
    )
    rows, skipped = transform_legislators([record])
    assert not skipped
    assert len(rows[0]["terms"]) == 1
    assert rows[0]["terms"][0]["body_id"] == "us-house"


def test_term_missing_state_gets_no_district_id() -> None:
    record = _record(
        terms=[{"type": "rep", "start": "2023-01-03", "end": "2025-01-03", "district": 19}]
    )
    rows, _ = transform_legislators([record])
    assert rows[0]["terms"][0]["district_id"] is None


def test_independent_party_is_kept_verbatim() -> None:
    record = _record(
        terms=[
            {
                "type": "sen",
                "start": "2023-01-03",
                "end": "2029-01-03",
                "state": "VT",
                "party": "Independent",
                "caucus": "Democrat",
            }
        ]
    )
    rows, _ = transform_legislators([record])
    assert rows[0]["terms"][0]["party"] == "Independent"


# --- terms[]: 118th/119th coverage filter -----------------------------------


def test_a_term_entirely_before_the_118th_is_dropped() -> None:
    record = _record(
        terms=[
            {
                "type": "rep",
                "start": "2019-01-03",
                "end": "2021-01-03",
                "state": "CA",
                "district": 19,
                "party": "Democrat",
            },
            {
                "type": "rep",
                "start": "2023-01-03",
                "end": "2025-01-03",
                "state": "CA",
                "district": 19,
                "party": "Democrat",
            },
        ]
    )
    rows, _ = transform_legislators([record])
    assert len(rows[0]["terms"]) == 1
    assert rows[0]["terms"][0]["start"] == "2023-01-03"


def test_a_term_entirely_after_the_119th_is_dropped() -> None:
    record = _record(
        terms=[
            {
                "type": "sen",
                "start": "2023-01-03",
                "end": "2025-01-03",
                "state": "WA",
                "party": "Democrat",
            },
            {
                "type": "sen",
                "start": "2031-01-03",
                "end": "2037-01-03",
                "state": "WA",
                "party": "Democrat",
            },
        ]
    )
    rows, _ = transform_legislators([record])
    assert len(rows[0]["terms"]) == 1
    assert rows[0]["terms"][0]["start"] == "2023-01-03"


def test_a_term_straddling_the_118th_119th_boundary_is_kept_once() -> None:
    record = _record(
        terms=[
            {
                "type": "sen",
                "start": "2019-01-03",
                "end": "2025-01-03",
                "state": "WA",
                "party": "Democrat",
            }
        ]
    )
    rows, _ = transform_legislators([record])
    assert len(rows[0]["terms"]) == 1


def test_a_term_spanning_both_congresses_is_kept() -> None:
    record = _record(
        terms=[
            {
                "type": "sen",
                "start": "2019-01-03",
                "end": "2031-01-03",
                "state": "WA",
                "party": "Democrat",
            }
        ]
    )
    rows, _ = transform_legislators([record])
    assert len(rows[0]["terms"]) == 1


def test_missing_bounds_do_not_disqualify_a_term() -> None:
    """Defensive only: the real source always supplies both bounds."""
    record = _record(terms=[{"type": "rep", "state": "CA", "district": 19, "party": "Democrat"}])
    rows, _ = transform_legislators([record])
    assert len(rows[0]["terms"]) == 1
