"""Unit tests for `app.ingest.lis_crosswalk` (story 8.2).

The feature file drives the whole write-read-alert loop through the in-memory
fake. These are the narrower cases: the roster parse, the senator filter, the
alert bookkeeping and the metrics rendering, exercised directly with no
ZeroDB involved at all.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import httpx
import pytest

from app.bootstrap import Bootstrapper
from app.ingest.lis_crosswalk import (
    LEGISLATORS_YAML_URL,
    LIS_EXTERNAL_ID_KEY,
    CoverageReport,
    LisCrosswalk,
    LisCrosswalkSourceError,
    LisCrosswalkWriter,
    SittingSenator,
    UnmappedLisMemberIdError,
    check_coverage,
    fetch_roster,
    load_crosswalk,
    render_coverage_prometheus_text,
    sitting_senators_from_roster,
)
from app.repositories import Repository
from app.repositories.models import Person
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport


def _record(
    *,
    bioguide: str,
    lis: str | None,
    term_type: str = "sen",
    name: str = "A Senator",
) -> dict[str, Any]:
    ids: dict[str, Any] = {"bioguide": bioguide}
    if lis is not None:
        ids["lis"] = lis
    return {
        "id": ids,
        "name": {"official_full": name},
        "terms": [{"type": term_type, "start": "2025-01-03", "end": "2031-01-03", "state": "ZZ"}],
    }


# --- sitting_senators_from_roster -------------------------------------------


def test_only_records_whose_latest_term_is_senate_are_sitting_senators() -> None:
    roster = [
        _record(bioguide="S000001", lis="S001"),
        _record(bioguide="H000001", lis=None, term_type="rep"),
    ]
    senators = sitting_senators_from_roster(roster)
    assert [s.bioguide_id for s in senators] == ["S000001"]


def test_a_former_representative_now_in_the_senate_is_a_sitting_senator() -> None:
    """The last term's type is what decides, not any term's -- a senator with
    a House term in their history (Blackburn, Scott in the live file) must
    still be counted.
    """
    roster = [
        {
            "id": {"bioguide": "B001243", "lis": "S396"},
            "name": {"official_full": "Marsha Blackburn"},
            "terms": [
                {"type": "rep", "start": "2017-01-03", "end": "2019-01-03", "state": "TN"},
                {"type": "sen", "start": "2019-01-03", "end": "2025-01-03", "state": "TN"},
            ],
        }
    ]
    assert [s.bioguide_id for s in sitting_senators_from_roster(roster)] == ["B001243"]


def test_a_senator_who_has_since_moved_to_the_house_is_not_a_sitting_senator() -> None:
    """The mirror of the case above: a `sen` term followed by a `rep` term is
    a representative today, and must not be counted toward the Senate's 100.
    """
    roster = [
        {
            "id": {"bioguide": "X000001", "lis": "S001"},
            "name": {"official_full": "Someone"},
            "terms": [
                {"type": "sen", "start": "2017-01-03", "end": "2023-01-03", "state": "ZZ"},
                {"type": "rep", "start": "2025-01-03", "end": "2027-01-03", "state": "ZZ"},
            ],
        }
    ]
    assert sitting_senators_from_roster(roster) == []


def test_a_sitting_senator_with_no_lis_id_is_still_a_sitting_senator() -> None:
    """Critical for the acceptance criterion: a senator the SOURCE has no LIS
    id for must be counted in the denominator, not filtered out of it --
    filtering by "has a lis id" would make "100% of sitting senators map"
    trivially and dishonestly true.
    """
    senators = sitting_senators_from_roster([_record(bioguide="S000002", lis=None)])
    assert len(senators) == 1
    assert senators[0].lis_member_id is None


def test_a_record_with_no_bioguide_id_is_skipped() -> None:
    """`people` is keyed `person:{bioguide}`; a record with no bioguide has no
    row to write the mapping onto.
    """
    assert sitting_senators_from_roster([{"id": {"lis": "S001"}, "terms": [{"type": "sen"}]}]) == []


def test_a_record_with_no_terms_is_skipped() -> None:
    assert sitting_senators_from_roster([{"id": {"bioguide": "S000003"}, "terms": []}]) == []


def test_a_non_dict_record_is_skipped_rather_than_crashing_the_whole_roster() -> None:
    roster: list[Any] = ["not a record", _record(bioguide="S000004", lis="S004")]
    assert [s.bioguide_id for s in sitting_senators_from_roster(roster)] == ["S000004"]


def test_a_record_whose_id_block_is_not_a_mapping_is_skipped() -> None:
    roster = [{"id": "A000001", "terms": [{"type": "sen", "start": "2025-01-03"}]}]
    assert sitting_senators_from_roster(roster) == []


def test_a_record_whose_terms_block_is_not_a_list_is_skipped() -> None:
    assert sitting_senators_from_roster([{"id": {"bioguide": "S000009"}, "terms": "sen"}]) == []


def test_a_record_whose_latest_term_is_not_a_mapping_is_skipped() -> None:
    assert sitting_senators_from_roster([{"id": {"bioguide": "S000010"}, "terms": ["sen"]}]) == []


def test_a_record_with_a_blank_bioguide_is_skipped() -> None:
    roster = [{"id": {"bioguide": "  ", "lis": "S011"}, "terms": [{"type": "sen"}]}]
    assert sitting_senators_from_roster(roster) == []


def test_canonical_name_is_empty_when_the_name_block_is_not_a_mapping() -> None:
    """Defensive, not expected live: a malformed name must cost the senator
    their display name, never their mapping -- the mapping is the point.
    """
    roster = [
        {
            "id": {"bioguide": "S000012", "lis": "S012"},
            "name": "Jo Smith",
            "terms": [{"type": "sen"}],
        }
    ]
    senators = sitting_senators_from_roster(roster)
    assert [s.bioguide_id for s in senators] == ["S000012"]
    assert senators[0].canonical_name == ""


def test_canonical_name_falls_back_to_first_and_last_when_official_full_is_absent() -> None:
    roster = [
        {
            "id": {"bioguide": "S000005", "lis": "S005"},
            "name": {"first": "Jo", "last": "Smith"},
            "terms": [{"type": "sen", "start": "2025-01-03"}],
        }
    ]
    assert sitting_senators_from_roster(roster)[0].canonical_name == "Jo Smith"


def test_person_id_is_the_documented_people_id_pattern() -> None:
    senator = sitting_senators_from_roster([_record(bioguide="S000006", lis="S006")])[0]
    assert senator.person_id == "person:S000006"


def test_a_blank_lis_id_is_treated_as_absent_not_as_a_mapping_to_the_empty_string() -> None:
    roster = [
        {
            "id": {"bioguide": "S000007", "lis": "   "},
            "name": {"official_full": "A Senator"},
            "terms": [{"type": "sen", "start": "2025-01-03"}],
        }
    ]
    assert sitting_senators_from_roster(roster)[0].lis_member_id is None


def test_a_non_string_lis_id_is_stringified_rather_than_dropped() -> None:
    """Defensive: every `id.lis` in the live file is a string (`S317`), but
    the file's other id fields are ints, so a future schema change that made
    `lis` numeric must not silently drop every mapping.
    """
    roster = [
        {
            "id": {"bioguide": "S000008", "lis": 313},
            "name": {"official_full": "A Senator"},
            "terms": [{"type": "sen", "start": "2025-01-03"}],
        }
    ]
    assert sitting_senators_from_roster(roster)[0].lis_member_id == "313"


# --- LisCrosswalk resolution -------------------------------------------------


def test_resolve_bioguide_id_returns_the_mapped_bioguide() -> None:
    crosswalk = LisCrosswalk({"S317": "B001261"})
    assert crosswalk.resolve_bioguide_id("S317") == "B001261"


def test_resolve_person_id_returns_the_people_id_member_votes_uses() -> None:
    crosswalk = LisCrosswalk({"S317": "B001261"})
    assert crosswalk.resolve_person_id("S317") == "person:B001261"


def test_resolve_person_id_returns_none_for_an_unmapped_id() -> None:
    assert LisCrosswalk({}).resolve_person_id("S999") is None


def test_resolve_bioguide_id_returns_none_for_an_unmapped_id_by_default() -> None:
    assert LisCrosswalk({}).resolve_bioguide_id("S999") is None


def test_resolve_bioguide_id_does_not_alert_unless_asked() -> None:
    crosswalk = LisCrosswalk({})
    crosswalk.resolve_bioguide_id("S999")
    assert crosswalk.unmapped_alert_count == 0


def test_require_bioguide_id_raises_naming_the_id() -> None:
    with pytest.raises(UnmappedLisMemberIdError, match="S999"):
        LisCrosswalk({}).require_bioguide_id("S999")


def test_require_bioguide_id_returns_the_bioguide_when_mapped() -> None:
    assert LisCrosswalk({"S317": "B001261"}).require_bioguide_id("S317") == "B001261"


def test_require_bioguide_id_also_counts_the_alert() -> None:
    """A raise is itself an alert -- the counter must see it too, so a caller
    that chooses the strict path still shows up on the metrics scrape.
    """
    crosswalk = LisCrosswalk({})
    with pytest.raises(UnmappedLisMemberIdError):
        crosswalk.require_bioguide_id("S999")
    assert crosswalk.unmapped_alert_count == 1


def test_len_is_the_number_of_mappings() -> None:
    assert len(LisCrosswalk({"S1": "A", "S2": "B"})) == 2


def test_contains_tests_for_a_lis_id() -> None:
    crosswalk = LisCrosswalk({"S317": "B001261"})
    assert "S317" in crosswalk
    assert "S999" not in crosswalk


def test_bioguide_ids_are_exposed_for_a_coverage_check() -> None:
    assert LisCrosswalk({"S1": "A", "S2": "B"}).bioguide_ids == {"A", "B"}


# --- alerting ----------------------------------------------------------------


def test_an_unmapped_id_with_alerting_logs_at_error_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    crosswalk = LisCrosswalk({})
    with caplog.at_level(logging.ERROR, logger="app.ingest.lis_crosswalk"):
        crosswalk.resolve_bioguide_id("S999", alert=True)
    assert caplog.records
    assert caplog.records[0].levelno == logging.ERROR


def test_an_unmapped_id_alert_carries_the_story_and_operation_tags(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`app.observability.logging_config`'s documented convention: a line that
    is worth tagging says `story` and `operation` the same way everywhere.
    """
    crosswalk = LisCrosswalk({})
    with caplog.at_level(logging.ERROR, logger="app.ingest.lis_crosswalk"):
        crosswalk.resolve_bioguide_id("S999", alert=True)
    record = caplog.records[0]
    assert record.story == "8.2"
    assert record.operation == "resolve_lis_member_id"
    assert record.lis_member_id == "S999"


def test_the_same_unmapped_id_logs_once_but_counts_every_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The counter is the metric; the log line is for a human. One senator's
    unmapped id across 500 member_votes rows must not write 500 identical log
    lines, but the count must still be 500 so the scrape is honest.
    """
    crosswalk = LisCrosswalk({})
    with caplog.at_level(logging.ERROR, logger="app.ingest.lis_crosswalk"):
        for _ in range(3):
            crosswalk.resolve_bioguide_id("S999", alert=True)
    assert crosswalk.unmapped_alert_count == 3
    assert len(crosswalk.unmapped_lis_ids) == 1
    assert len(caplog.records) == 1


def test_distinct_unmapped_ids_each_log(caplog: pytest.LogCaptureFixture) -> None:
    crosswalk = LisCrosswalk({})
    with caplog.at_level(logging.ERROR, logger="app.ingest.lis_crosswalk"):
        crosswalk.resolve_bioguide_id("S999", alert=True)
        crosswalk.resolve_bioguide_id("S998", alert=True)
    assert len(caplog.records) == 2
    assert crosswalk.unmapped_lis_ids == {"S998", "S999"}


def test_a_mapped_id_never_alerts(caplog: pytest.LogCaptureFixture) -> None:
    crosswalk = LisCrosswalk({"S317": "B001261"})
    with caplog.at_level(logging.ERROR, logger="app.ingest.lis_crosswalk"):
        crosswalk.resolve_bioguide_id("S317", alert=True)
    assert not caplog.records
    assert crosswalk.unmapped_alert_count == 0


# --- check_coverage ----------------------------------------------------------


def _senator(bioguide: str, lis: str | None) -> SittingSenator:
    return SittingSenator(
        bioguide_id=bioguide, lis_member_id=lis, canonical_name=f"Senator {bioguide}"
    )


def test_full_coverage_is_complete() -> None:
    crosswalk = LisCrosswalk({"S1": "A000001", "S2": "A000002"})
    coverage = check_coverage(crosswalk, [_senator("A000001", "S1"), _senator("A000002", "S2")])
    assert coverage.is_complete
    assert coverage.mapped_count == 2
    assert coverage.sitting_count == 2
    assert coverage.unmapped_person_ids == []


def test_a_senator_absent_from_the_crosswalk_is_reported_unmapped() -> None:
    coverage = check_coverage(
        LisCrosswalk({"S1": "A000001"}), [_senator("A000001", "S1"), _senator("A000002", "S2")]
    )
    assert not coverage.is_complete
    assert coverage.unmapped_person_ids == ["person:A000002"]
    assert coverage.mapped_count == 1


def test_coverage_is_measured_by_bioguide_not_by_lis_id() -> None:
    """A crosswalk row whose LIS id has changed (the Senate reissuing one) but
    whose bioguide still appears is still coverage of that senator -- the
    acceptance criterion is about senators mapping, and the bioguide is the
    senator.
    """
    coverage = check_coverage(LisCrosswalk({"S-new": "A000001"}), [_senator("A000001", "S-old")])
    assert coverage.is_complete


def test_coverage_percentage_is_reported() -> None:
    coverage = check_coverage(
        LisCrosswalk({"S1": "A000001"}), [_senator("A000001", "S1"), _senator("A000002", "S2")]
    )
    assert coverage.percentage == pytest.approx(50.0)


def test_coverage_percentage_of_an_empty_sitting_senate_is_zero_not_a_division_error() -> None:
    assert check_coverage(LisCrosswalk({}), []).percentage == 0.0


def test_an_empty_sitting_senate_is_not_reported_as_complete_coverage() -> None:
    """100% of zero is not the acceptance criterion being met -- it is the
    roster failing to load, and reporting it green would hide exactly the
    failure this check exists to catch.
    """
    assert not check_coverage(LisCrosswalk({}), []).is_complete


def test_unmapped_person_ids_are_sorted_for_a_stable_report() -> None:
    coverage = check_coverage(
        LisCrosswalk({}),
        [_senator("B000002", "S2"), _senator("A000001", "S1")],
    )
    assert coverage.unmapped_person_ids == ["person:A000001", "person:B000002"]


# --- metrics rendering -------------------------------------------------------


def test_complete_coverage_renders_zero_unmapped_and_complete_one() -> None:
    text = render_coverage_prometheus_text(
        check_coverage(LisCrosswalk({"S1": "A000001"}), [_senator("A000001", "S1")])
    )
    assert "lis_crosswalk_unmapped_senators 0" in text
    assert "lis_crosswalk_coverage_complete 1" in text
    assert "lis_crosswalk_sitting_senators 1" in text


def test_incomplete_coverage_renders_the_gauge_an_alert_rule_fires_on() -> None:
    text = render_coverage_prometheus_text(
        check_coverage(LisCrosswalk({}), [_senator("A000001", "S1")])
    )
    assert "lis_crosswalk_unmapped_senators 1" in text
    assert "lis_crosswalk_coverage_complete 0" in text


def test_rendered_metrics_declare_help_and_type_for_every_gauge() -> None:
    text = render_coverage_prometheus_text(
        check_coverage(LisCrosswalk({"S1": "A000001"}), [_senator("A000001", "S1")])
    )
    for name in (
        "lis_crosswalk_sitting_senators",
        "lis_crosswalk_mapped_senators",
        "lis_crosswalk_unmapped_senators",
        "lis_crosswalk_coverage_complete",
    ):
        assert f"# HELP {name} " in text, name
        assert f"# TYPE {name} gauge" in text, name


def test_rendered_metrics_end_with_a_newline_like_the_other_renderers() -> None:
    text = render_coverage_prometheus_text(check_coverage(LisCrosswalk({}), []))
    assert text.endswith("\n")


# --- fetch_roster ------------------------------------------------------------


def test_fetch_roster_returns_the_parsed_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, timeout: float) -> httpx.Response:
        assert url == LEGISLATORS_YAML_URL
        return httpx.Response(200, text="- id:\n    bioguide: A000001\n")

    monkeypatch.setattr(httpx, "get", fake_get)
    assert fetch_roster() == [{"id": {"bioguide": "A000001"}}]


def test_fetch_roster_raises_on_a_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda url, timeout: httpx.Response(404, text=""))
    with pytest.raises(LisCrosswalkSourceError, match="404"):
        fetch_roster()


def test_fetch_roster_raises_on_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(LisCrosswalkSourceError, match="could not reach"):
        fetch_roster()


def test_fetch_roster_raises_when_the_body_is_not_a_yaml_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "get", lambda url, timeout: httpx.Response(200, text="not: a list"))
    with pytest.raises(LisCrosswalkSourceError, match="YAML list"):
        fetch_roster()


def test_fetch_roster_raises_on_unparseable_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda url, timeout: httpx.Response(200, text="a: [1,\n"))
    with pytest.raises(LisCrosswalkSourceError, match="YAML"):
        fetch_roster()


# --- constants ---------------------------------------------------------------


def test_the_external_id_key_matches_the_one_legislators_already_writes() -> None:
    assert LIS_EXTERNAL_ID_KEY == "lis"


# --- load_crosswalk and the writer, against the in-memory fake ---------------


def _client() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(client, pace=lambda: None).run()
    return client


def test_load_crosswalk_pages_past_the_first_page_of_people() -> None:
    """The live `people` table is 542 rows against a 500-row page, and the
    client's filtered read is documented as unreliable past the first page,
    so a crosswalk that read one page would silently miss senators. 501 rows
    with the mapping on the LAST one is what pins the paging.
    """
    client = _client()
    people: Repository[Person] = Repository(client=client, model=Person)
    for index in range(500):
        people.upsert({"id": f"person:FILLER{index:04d}", "canonical_name": "Filler"})
    people.upsert(
        {
            "id": "person:B001261",
            "canonical_name": "John Barrasso",
            "external_ids": {"lis": "S317"},
        }
    )

    crosswalk = load_crosswalk(client)
    assert crosswalk.resolve_bioguide_id("S317") == "B001261"


def test_load_crosswalk_ignores_a_people_row_with_no_lis_id() -> None:
    client = _client()
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert({"id": "person:P000197", "canonical_name": "A Representative"})
    assert len(load_crosswalk(client)) == 0


def test_refresh_reports_a_senator_with_no_people_row_rather_than_inventing_one() -> None:
    """Story 2.2's roster load is what creates `people` rows. Conjuring a thin
    one here would hide an out-of-date roster behind a crosswalk that looks
    complete.
    """
    client = _client()
    report = LisCrosswalkWriter(client, pace=lambda: None).refresh([_senator("Z000099", "S999")])
    assert report.missing_people_rows == ["person:Z000099"]
    assert report.mapped == []
    assert len(load_crosswalk(client)) == 0


def test_refresh_does_not_rewrite_a_row_whose_mapping_is_already_correct() -> None:
    """The common path on every re-run. Re-PUTting an identical row is a
    wasted request against the documented ~60 req/min ceiling, so the row's
    `updated_at` must not move -- while the senator still counts as mapped.
    """
    client = _client()
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert(
        {
            "id": "person:B001261",
            "canonical_name": "John Barrasso",
            "external_ids": {"lis": "S317"},
        }
    )
    before = client.get_row("people", "person:B001261")
    assert before is not None

    report = LisCrosswalkWriter(client, pace=lambda: None).refresh([_senator("B001261", "S317")])
    after = client.get_row("people", "person:B001261")
    assert after is not None
    assert report.mapped == ["person:B001261"]
    assert after.updated_at == before.updated_at


def test_refresh_corrects_a_row_whose_mapping_is_stale() -> None:
    """The Senate reissuing an id, or a bad earlier write: the roster is the
    source of truth, so the row is corrected rather than left alone.
    """
    client = _client()
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert(
        {
            "id": "person:B001261",
            "canonical_name": "John Barrasso",
            "external_ids": {"lis": "S-stale"},
        }
    )
    LisCrosswalkWriter(client, pace=lambda: None).refresh([_senator("B001261", "S317")])
    assert load_crosswalk(client).resolve_bioguide_id("S317") == "B001261"


def test_refresh_preserves_the_terms_already_on_the_row() -> None:
    """`Repository.upsert` replaces a row wholesale (ZeroDB's PUT), so the
    merge has to carry `terms[]` across too -- not just `external_ids`. A
    crosswalk refresh that wiped story 2.2's terms would break
    `BallotService`'s delegation lookup.
    """
    client = _client()
    people: Repository[Person] = Repository(client=client, model=Person)
    people.upsert(
        {
            "id": "person:B001261",
            "canonical_name": "John Barrasso",
            "external_ids": {"govtrack": "412251"},
            "terms": [{"body_id": "us-senate", "party": "Republican", "start": "2021-01-03"}],
        }
    )
    LisCrosswalkWriter(client, pace=lambda: None).refresh([_senator("B001261", "S317")])

    person = people.get("person:B001261")
    assert person is not None
    assert person.external_ids == {"govtrack": "412251", "lis": "S317"}
    assert [term.body_id for term in person.terms] == ["us-senate"]
    assert person.canonical_name == "John Barrasso"


def test_the_roster_url_is_the_one_legislators_already_fetches() -> None:
    from app.ingest.legislators import LEGISLATORS_CURRENT_URL

    assert LEGISLATORS_YAML_URL == LEGISLATORS_CURRENT_URL


def test_coverage_report_is_frozen() -> None:
    """A measured acceptance criterion must not be mutable after the fact."""
    coverage = CoverageReport(
        sitting_count=1, mapped_count=1, unmapped_person_ids=[], unmapped_lis_ids=[]
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        coverage.sitting_count = 2  # type: ignore[misc]


def test_a_stale_mapping_for_a_departed_senator_is_reported_but_is_not_a_failure() -> None:
    """A retired senator's LIS id still resolving is how a 118th-Congress
    roll call keeps joining to a person -- reported, never a coverage
    failure. See `CoverageReport.unmapped_lis_ids`.
    """
    coverage = check_coverage(
        LisCrosswalk({"S1": "A000001", "S-old": "Z000099"}), [_senator("A000001", "S1")]
    )
    assert coverage.is_complete
    assert coverage.unmapped_lis_ids == ["S-old"]
