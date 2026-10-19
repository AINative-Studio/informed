"""Unit tests for app.ingest.senate_rollcalls's pure logic (story 8.1).

The Gherkin feature (tests/features/senate_rollcalls.feature) proves the
story's acceptance criteria end to end through the repository layer; these
tests pin the transform, classification and fetch edge cases directly --
the same split test_house_rollcalls_unit.py uses for story 5.1.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.ingest import senate_rollcalls as senate_rollcalls_module
from app.ingest.senate_rollcalls import (
    ADAPTER_VERSION,
    LoadReport,
    SenateRollCallSourceError,
    content_hash_for_xml,
    fetch_lis_to_bioguide_map,
    fetch_roll_call_xml,
    fetch_vote_numbers,
    roll_call_id,
    roll_call_item_url,
    transform_member_votes_for,
    transform_roll_call,
    transform_roll_calls,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "senate_rollcalls_sample.json"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _xml(key: str) -> str:
    return _fixture()[key]


def _bioguide_map() -> dict[str, str]:
    return _fixture()["lis_to_bioguide_sample"]


_MINIMAL_VOTE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?><roll_call_vote>
  <congress>{congress}</congress>
  <session>{session}</session>
  <vote_number>{vote_number}</vote_number>
  <vote_date>{vote_date}</vote_date>
  <question>{question}</question>
  <vote_result>{vote_result}</vote_result>
  <document>
    <document_type>{document_type}</document_type>
  </document>
  <amendment>
    <amendment_number>{amendment_number}</amendment_number>
  </amendment>
  <count>
    <yeas>{yeas}</yeas>
    <nays>{nays}</nays>
    <present>{present}</present>
    <absent>{absent}</absent>
  </count>
  <members>{members}</members>
</roll_call_vote>"""


def _minimal_xml(
    *,
    congress: int = 119,
    session: int = 2,
    vote_number: int = 254,
    vote_date: str = "September 30, 2026,  12:51 PM",
    question: str = "On Passage of the Bill",
    vote_result: str = "Bill Passed",
    document_type: str = "S.",
    amendment_number: str = "",
    yeas: int = 53,
    nays: int = 47,
    present: int = 0,
    absent: int = 0,
    members: str = "",
) -> str:
    return _MINIMAL_VOTE_TEMPLATE.format(
        congress=congress,
        session=session,
        vote_number=vote_number,
        vote_date=vote_date,
        question=question,
        vote_result=vote_result,
        document_type=document_type,
        amendment_number=amendment_number,
        yeas=yeas,
        nays=nays,
        present=present,
        absent=absent,
        members=members,
    )


# --- id, url and hashing --------------------------------------------------------


def test_roll_call_id_matches_datamodel_pattern() -> None:
    assert roll_call_id(congress=119, session=2, vote_number=254) == "us-senate:119-2:254"


def test_roll_call_item_url_matches_the_confirmed_live_pattern() -> None:
    url = roll_call_item_url(
        congress=119, session=2, vote_number=254, base_url="https://www.senate.gov/legislative/LIS"
    )
    assert url == (
        "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1192/vote_119_2_00254.xml"
    )


def test_roll_call_item_url_zero_pads_the_vote_number() -> None:
    url = roll_call_item_url(
        congress=118, session=1, vote_number=1, base_url="https://www.senate.gov/legislative/LIS"
    )
    assert url.endswith("vote_118_1_00001.xml")


def test_content_hash_for_xml_is_deterministic() -> None:
    xml = _minimal_xml()
    assert content_hash_for_xml(xml) == content_hash_for_xml(xml)


def test_content_hash_for_xml_changes_when_content_changes() -> None:
    a = _minimal_xml(vote_result="Bill Passed")
    b = _minimal_xml(vote_result="Bill Failed")
    assert content_hash_for_xml(a) != content_hash_for_xml(b)


def test_content_hash_for_xml_is_a_sha256_hex_digest() -> None:
    digest = content_hash_for_xml(_minimal_xml())
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not valid hex


# --- transform: required fields, from real captured samples -------------------


def test_transform_builds_the_documented_id_from_real_sample() -> None:
    row = transform_roll_call(_xml("item_cloture_motion_to_proceed"), source_url="https://x")
    assert row["id"] == "us-senate:119-2:254"


def test_transform_reaches_the_118th_congress() -> None:
    row = transform_roll_call(_xml("item_final_passage_118"), source_url="https://x")
    assert row["id"] == "us-senate:118-1:77"


def test_transform_copies_voted_at_question_and_result_verbatim() -> None:
    row = transform_roll_call(_xml("item_final_passage_118"), source_url="https://x")
    assert row["voted_at"] == "March 29, 2023,  11:50 AM"
    assert row["question"] == "On Passage of the Bill"
    assert row["result"] == "Bill Passed"


def test_transform_uses_the_caller_supplied_source_url() -> None:
    row = transform_roll_call(
        _xml("item_cloture_motion_to_proceed"),
        source_url="https://www.senate.gov/legislative/LIS/roll_call_votes/vote1192/vote_119_2_00254.xml",
    )
    assert row["source_url"] == (
        "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1192/vote_119_2_00254.xml"
    )


def test_transform_computes_a_content_hash_of_the_raw_xml() -> None:
    xml = _xml("item_cloture_motion_to_proceed")
    row = transform_roll_call(xml, source_url="https://x")
    assert row["content_hash"] == content_hash_for_xml(xml)


def test_transform_leaves_bill_id_null_this_story() -> None:
    row = transform_roll_call(_xml("item_final_passage_118"), source_url="https://x")
    assert row["bill_id"] is None


def test_transform_leaves_topic_ids_empty_and_is_bundled_false_this_story() -> None:
    row = transform_roll_call(_xml("item_final_passage_118"), source_url="https://x")
    assert row["topic_ids"] == []
    assert row["is_bundled"] is False


# --- transform: skip paths ------------------------------------------------------


def test_transform_skips_unparseable_xml() -> None:
    rows, skipped = transform_roll_calls([("<not valid xml", "https://x")])
    assert rows == []
    assert len(skipped) == 1
    assert "could not parse" in skipped[0].reason


def test_transform_skips_a_record_missing_congress_session_or_vote_number() -> None:
    xml = _minimal_xml().replace("<congress>119</congress>", "<congress></congress>")
    rows, skipped = transform_roll_calls([(xml, "https://x")])
    assert rows == []
    assert "congress" in skipped[0].reason


def test_transform_skips_a_record_with_no_vote_date() -> None:
    xml = _minimal_xml(vote_date="").replace("<vote_date></vote_date>", "<vote_date></vote_date>")
    rows, skipped = transform_roll_calls([(xml, "https://x")])
    assert rows == []
    assert "vote_date" in skipped[0].reason


def test_transform_skips_a_record_with_no_vote_result() -> None:
    xml = _minimal_xml(vote_result="")
    rows, skipped = transform_roll_calls([(xml, "https://x")])
    assert rows == []
    assert "vote_result" in skipped[0].reason


def test_transform_many_collects_skips_without_failing_the_batch() -> None:
    good = (_xml("item_final_passage_118"), "https://x")
    bad = ("<not valid xml", "https://y")
    rows, skipped = transform_roll_calls([good, bad])
    assert len(rows) == 1
    assert len(skipped) == 1


# --- vote_type classification: this story's highest-value case -----------------


@pytest.mark.parametrize(
    "question,amendment_number,expected",
    [
        ("On Cloture on the Motion to Proceed", "", "cloture"),
        ("On the Cloture Motion", "", "cloture"),
        ("On Cloture", "", "cloture"),
        ("On the Amendment", "S.Amdt. 5235", "amendment"),
        ("On Passage of the Bill", "", "final_passage"),
        ("On Passage of the Joint Resolution", "", "final_passage"),
        ("On the Joint Resolution", "", "final_passage"),
        ("On the Resolution", "", "final_passage"),
        ("On the Concurrent Resolution", "", "final_passage"),
        ("On Agreeing to the Conference Report", "", "final_passage"),
        ("On the Motion to Recommit", "", "motion_to_recommit"),
        ("On the Motion to Table", "", "procedural"),
        ("On the Motion to Reconsider", "", "procedural"),
        ("On Ordering the Previous Question", "", "procedural"),
        ("On the Nomination", "", "other"),
        ("Something nobody has seen before", "", "other"),
    ],
)
def test_vote_type_classification(question: str, amendment_number: str, expected: str) -> None:
    xml = _minimal_xml(question=question, amendment_number=amendment_number)
    row = transform_roll_call(xml, source_url="https://x")
    assert row["vote_type"] == expected


def test_cloture_wins_over_final_passage_even_when_the_bill_is_named() -> None:
    """The case the module docstring calls out explicitly: a cloture vote on
    proceeding to a specific bill must classify as cloture, not final_passage,
    even though live Senate `question` text for this case often also reads
    like it is "about" the bill (confirmed from the real
    item_cloture_motion_to_proceed sample)."""
    row = transform_roll_call(_xml("item_cloture_motion_to_proceed"), source_url="https://x")
    assert row["vote_type"] == "cloture"


def test_cloture_wins_over_amendment_when_both_signals_are_present() -> None:
    xml = _minimal_xml(question="On the Cloture Motion", amendment_number="S.Amdt. 5235")
    row = transform_roll_call(xml, source_url="https://x")
    assert row["vote_type"] == "cloture"


def test_cloture_on_a_nomination_classifies_cloture_not_other() -> None:
    row = transform_roll_call(_xml("item_cloture_nomination"), source_url="https://x")
    assert row["vote_type"] == "cloture"


def test_bare_nomination_classifies_other_not_final_passage() -> None:
    """A Senate confirmation vote is not a vote on legislation --
    datamodel.md's vote_type vocabulary has no dedicated value for it, so it
    falls to `other` rather than being force-fit into `final_passage`."""
    row = transform_roll_call(_xml("item_nomination"), source_url="https://x")
    assert row["vote_type"] == "other"


# --- amendment_ref ---------------------------------------------------------------


def test_amendment_ref_is_copied_verbatim_when_present() -> None:
    row = transform_roll_call(_xml("item_amendment"), source_url="https://x")
    assert row["amendment_ref"] == "S.Amdt. 5235"


def test_amendment_ref_is_none_when_absent() -> None:
    row = transform_roll_call(_xml("item_final_passage_118"), source_url="https://x")
    assert row["amendment_ref"] is None


# --- tally, including the absent -> not_voting mapping --------------------------


def test_tally_maps_absent_to_not_voting() -> None:
    row = transform_roll_call(_xml("item_final_passage_118"), source_url="https://x")
    tally = row["tally"]
    assert (tally.yea, tally.nay, tally.present, tally.not_voting) == (66, 30, 0, 4)


def test_tally_is_zero_when_a_count_subfield_is_a_self_closing_empty_element() -> None:
    """Confirmed live: `<present/>` (self-closing, no text) occurs on real
    Senate votes where nobody voted present -- this must contribute 0, not
    crash on an int() of an empty string."""
    row = transform_roll_call(_xml("item_cloture_motion_to_proceed"), source_url="https://x")
    tally = row["tally"]
    assert tally.present == 0


def test_tally_treats_a_non_numeric_count_as_zero_rather_than_crashing() -> None:
    xml = _minimal_xml(yeas=53).replace("<nays>47</nays>", "<nays>not-a-number</nays>")
    row = transform_roll_call(xml, source_url="https://x")
    assert row["tally"].yea == 53
    assert row["tally"].nay == 0


# --- transform_member_votes_for: this story's wider scope than 5.1's ----------


def test_member_votes_are_denormalized_from_the_roll_call_row() -> None:
    xml = _xml("item_cloture_motion_to_proceed")
    row = transform_roll_call(xml, source_url="https://x")
    rows, skipped = transform_member_votes_for(row, xml, bioguide_by_lis_id=_bioguide_map())
    assert skipped == []
    assert len(rows) == 6
    for member_row in rows:
        assert member_row["roll_call_id"] == row["id"]
        assert member_row["voted_at"] == row["voted_at"]
        assert member_row["vote_type"] == row["vote_type"] == "cloture"
        assert member_row["bill_id"] == row["bill_id"] is None
        assert member_row["is_bundled"] == row["is_bundled"] is False


def test_member_votes_person_id_is_the_prefixed_people_id() -> None:
    """person_id must be `person:{bioguide}` (the people table's own id
    shape), not the bare bioguide -- MemberVote.references checks this field
    verbatim against the people table."""
    xml = _xml("item_cloture_motion_to_proceed")
    row = transform_roll_call(xml, source_url="https://x")
    rows, _ = transform_member_votes_for(row, xml, bioguide_by_lis_id=_bioguide_map())
    for member_row in rows:
        assert member_row["person_id"].startswith("person:")


def test_member_votes_cast_maps_to_the_documented_vote_position_vocabulary() -> None:
    xml = _xml("item_cloture_motion_to_proceed")
    row = transform_roll_call(xml, source_url="https://x")
    rows, _ = transform_member_votes_for(row, xml, bioguide_by_lis_id=_bioguide_map())
    positions = {r["position"] for r in rows}
    assert positions <= {"yea", "nay", "present", "not_voting"}
    assert positions  # at least one real position present


def test_an_unmapped_lis_member_id_is_skipped_not_guessed() -> None:
    xml = _xml("item_amendment")
    row = transform_roll_call(xml, source_url="https://x")
    partial_map = {k: v for k, v in _bioguide_map().items() if k != "S428"}
    rows, skipped = transform_member_votes_for(row, xml, bioguide_by_lis_id=partial_map)
    assert all(r["person_id"] != "person:A000382" for r in rows)  # S428 -> A000382
    assert len(skipped) == 1
    assert skipped[0].lis_member_id == "S428"
    assert "no bioguide mapping" in skipped[0].reason


def test_an_unrecognized_vote_cast_is_skipped_not_guessed() -> None:
    xml = _minimal_xml(
        members="""
        <member>
          <last_name>Test</last_name>
          <vote_cast>Excused</vote_cast>
          <lis_member_id>S999</lis_member_id>
        </member>
        """
    )
    row = transform_roll_call(xml, source_url="https://x")
    rows, skipped = transform_member_votes_for(row, xml, bioguide_by_lis_id={"S999": "Z000001"})
    assert rows == []
    assert len(skipped) == 1
    assert "unrecognized vote_cast" in skipped[0].reason


def test_member_votes_unparseable_xml_returns_a_single_skip_not_a_crash() -> None:
    row = transform_roll_call(_xml("item_final_passage_118"), source_url="https://x")
    rows, skipped = transform_member_votes_for(
        row, "<not valid xml", bioguide_by_lis_id=_bioguide_map()
    )
    assert rows == []
    assert len(skipped) == 1
    assert "could not parse" in skipped[0].reason


# --- fetch_lis_to_bioguide_map (mocked httpx) -----------------------------------


def test_fetch_lis_to_bioguide_map_builds_the_crosswalk(monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_text = """
- id:
    bioguide: B001230
    lis: S354
  name:
    official_full: Tammy Baldwin
- id:
    bioguide: B001261
    lis: S317
  name:
    official_full: John Barrasso
- id:
    bioguide: NOLIS001
  name:
    official_full: No LIS id on this one
"""

    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        assert "legislators-current.yaml" in url
        return httpx.Response(200, text=yaml_text)

    monkeypatch.setattr(senate_rollcalls_module.httpx, "get", fake_get)
    mapping = fetch_lis_to_bioguide_map()
    assert mapping == {"S354": "B001230", "S317": "B001261"}


def test_fetch_lis_to_bioguide_map_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        return httpx.Response(404)

    monkeypatch.setattr(senate_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(SenateRollCallSourceError, match="404"):
        fetch_lis_to_bioguide_map()


def test_fetch_lis_to_bioguide_map_wraps_a_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(senate_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(SenateRollCallSourceError, match="could not reach"):
        fetch_lis_to_bioguide_map()


# --- fetch_vote_numbers / fetch_roll_call_xml (mocked httpx) --------------------


def test_fetch_vote_numbers_returns_every_vote_number(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = """<?xml version="1.0"?><vote_summary>
      <votes>
        <vote><vote_number>00001</vote_number></vote>
        <vote><vote_number>00002</vote_number></vote>
      </votes>
    </vote_summary>"""

    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        assert "vote_menu_119_2.xml" in url
        return httpx.Response(200, text=xml)

    monkeypatch.setattr(senate_rollcalls_module.httpx, "get", fake_get)
    numbers = fetch_vote_numbers(congress=119, session=2)
    assert numbers == [1, 2]


def test_fetch_vote_numbers_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        return httpx.Response(404)

    monkeypatch.setattr(senate_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(SenateRollCallSourceError, match="404"):
        fetch_vote_numbers(congress=119, session=2)


def test_fetch_vote_numbers_raises_on_unparseable_xml(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        return httpx.Response(200, text="<not valid xml")

    monkeypatch.setattr(senate_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(SenateRollCallSourceError, match="did not parse"):
        fetch_vote_numbers(congress=119, session=2)


def test_fetch_vote_numbers_raises_on_wrong_root_element(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        return httpx.Response(200, text="<?xml version='1.0'?><something_else/>")

    monkeypatch.setattr(senate_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(SenateRollCallSourceError, match="vote_summary"):
        fetch_vote_numbers(congress=119, session=2)


def test_fetch_vote_numbers_wraps_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(senate_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(SenateRollCallSourceError, match="could not reach"):
        fetch_vote_numbers(congress=119, session=2)


def test_fetch_roll_call_xml_returns_the_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = _minimal_xml()

    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        assert "vote_119_2_00254.xml" in url
        return httpx.Response(200, text=xml)

    monkeypatch.setattr(senate_rollcalls_module.httpx, "get", fake_get)
    text = fetch_roll_call_xml(congress=119, session=2, vote_number=254)
    assert text == xml


def test_fetch_roll_call_xml_raises_on_wrong_root_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        return httpx.Response(200, text="<?xml version='1.0'?><something_else/>")

    monkeypatch.setattr(senate_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(SenateRollCallSourceError, match="roll_call_vote"):
        fetch_roll_call_xml(congress=119, session=2, vote_number=254)


def test_fetch_roll_call_xml_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        return httpx.Response(404)

    monkeypatch.setattr(senate_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(SenateRollCallSourceError, match="404"):
        fetch_roll_call_xml(congress=119, session=2, vote_number=999999)


def test_fetch_roll_call_xml_wraps_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(senate_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(SenateRollCallSourceError, match="could not reach"):
        fetch_roll_call_xml(congress=119, session=2, vote_number=254)


# --- LoadReport ----------------------------------------------------------------


def test_load_report_row_count_reflects_roll_calls_written() -> None:
    report = LoadReport(roll_calls_written=["us-senate:119-2:254", "us-senate:119-2:255"])
    assert report.row_count == 2


def test_adapter_version_is_recorded() -> None:
    assert ADAPTER_VERSION == "senate_rollcalls/v1"
