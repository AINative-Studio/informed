"""Unit tests for app.ingest.house_rollcalls's pure logic (story 5.1).

The Gherkin feature (tests/features/house_rollcalls.feature) proves the
story's acceptance criteria end to end through the repository layer; these
tests pin the transform, hashing and fetch edge cases directly -- the same
split test_legislators_unit.py uses for story 2.2.
"""

from __future__ import annotations

import httpx
import pytest

from app.bootstrap import Bootstrapper
from app.ingest import house_rollcalls as house_rollcalls_module
from app.ingest.house_rollcalls import (
    HouseRollCallLoader,
    HouseRollCallSourceError,
    content_hash_for,
    fetch_roll_call_item,
    fetch_roll_call_numbers,
    roll_call_id,
    transform_roll_call,
    transform_roll_calls,
    write_provenance,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport


def _envelope(**vote_overrides: object) -> dict:
    vote = {
        "startDate": "2025-03-11T14:22:00-04:00",
        "updateDate": "2025-03-12T08:00:00-04:00",
        "identifier": 1192204520,
        "congress": 119,
        "sessionNumber": 2,
        "rollCallNumber": 452,
        "voteType": "Yea and Nay",
        "result": "Passed",
        "legislationType": "HR",
        "legislationNumber": "1234",
        "voteQuestion": "On Motion to Suspend the Rules and Pass",
        "votePartyTotal": {
            "item": [
                {
                    "party": {"type": "R"},
                    "yeaTotal": 10,
                    "nayTotal": 1,
                    "presentTotal": 0,
                    "notVotingTotal": 2,
                },
                {
                    "party": {"type": "D"},
                    "yeaTotal": 5,
                    "nayTotal": 2,
                    "presentTotal": 1,
                    "notVotingTotal": 0,
                },
            ]
        },
        "legislationUrl": "https://www.congress.gov/bill/119th-congress/house-bill/1234",
        "url": "https://api.congress.gov/v3/house-vote/119/2/452",
    }
    vote.update(vote_overrides)
    return {"houseRollCallVote": vote}


# --- id and hashing -----------------------------------------------------------


def test_roll_call_id_matches_datamodel_pattern() -> None:
    assert roll_call_id(congress=119, session=2, roll_call_number=452) == "us-house:119-2:452"


def test_content_hash_is_deterministic_regardless_of_key_order() -> None:
    a = {"x": 1, "y": 2}
    b = {"y": 2, "x": 1}
    assert content_hash_for(a) == content_hash_for(b)


def test_content_hash_changes_when_content_changes() -> None:
    a = {"result": "Passed"}
    b = {"result": "Failed"}
    assert content_hash_for(a) != content_hash_for(b)


def test_content_hash_is_a_sha256_hex_digest() -> None:
    digest = content_hash_for({"a": 1})
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not valid hex


# --- transform: required fields ----------------------------------------------


def test_transform_builds_the_documented_id() -> None:
    row = transform_roll_call(_envelope())
    assert row["id"] == "us-house:119-2:452"


def test_transform_copies_voted_at_question_and_result() -> None:
    row = transform_roll_call(_envelope())
    assert row["voted_at"] == "2025-03-11T14:22:00-04:00"
    assert row["question"] == "On Motion to Suspend the Rules and Pass"
    assert row["result"] == "Passed"


def test_transform_uses_the_item_url_as_source_url_not_legislation_url() -> None:
    row = transform_roll_call(_envelope())
    assert row["source_url"] == "https://api.congress.gov/v3/house-vote/119/2/452"
    assert row["source_url"] != row.get("legislation_url")


def test_transform_computes_a_content_hash_of_the_whole_envelope() -> None:
    envelope = _envelope()
    row = transform_roll_call(envelope)
    assert row["content_hash"] == content_hash_for(envelope)


def test_transform_leaves_bill_id_null_this_story() -> None:
    row = transform_roll_call(_envelope())
    assert row["bill_id"] is None


def test_transform_leaves_topic_ids_empty_and_is_bundled_false_this_story() -> None:
    row = transform_roll_call(_envelope())
    assert row["topic_ids"] == []
    assert row["is_bundled"] is False


# --- transform: skip paths ----------------------------------------------------


def test_transform_skips_an_envelope_with_no_vote_object() -> None:
    rows, skipped = transform_roll_calls([{"not_a_vote": True}])
    assert rows == []
    assert len(skipped) == 1
    assert "houseRollCallVote" in skipped[0].reason


def test_transform_skips_a_record_missing_congress_session_or_roll() -> None:
    rows, skipped = transform_roll_calls([_envelope(congress=None)])
    assert rows == []
    assert skipped[0].roll_call_number == 452


def test_transform_skips_a_record_with_no_start_date() -> None:
    rows, skipped = transform_roll_calls([_envelope(startDate=None)])
    assert rows == []
    assert "startDate" in skipped[0].reason


def test_transform_skips_a_record_with_no_result() -> None:
    rows, skipped = transform_roll_calls([_envelope(result=None)])
    assert rows == []
    assert "result" in skipped[0].reason


def test_transform_skips_a_record_with_no_url() -> None:
    rows, skipped = transform_roll_calls([_envelope(url=None)])
    assert rows == []
    assert "url" in skipped[0].reason


def test_transform_many_collects_skips_without_failing_the_batch() -> None:
    good = _envelope()
    bad = {"not_a_vote": True}
    rows, skipped = transform_roll_calls([good, bad])
    assert len(rows) == 1
    assert len(skipped) == 1


# --- vote_type classification --------------------------------------------------


@pytest.mark.parametrize(
    "overrides,expected",
    [
        (
            {
                "amendmentType": "HAMDT",
                "amendmentNumber": "6",
                "voteQuestion": "On Agreeing to the Amendment",
            },
            "amendment",
        ),
        ({"voteQuestion": "On Motion to Suspend the Rules and Pass"}, "final_passage"),
        ({"voteQuestion": "On Motion to Suspend the Rules and Agree, as Amended"}, "final_passage"),
        ({"voteQuestion": "On Agreeing to the Resolution"}, "final_passage"),
        ({"voteQuestion": "On Motion to Recommit"}, "motion_to_recommit"),
        ({"voteQuestion": "On Ordering the Previous Question"}, "procedural"),
        ({"voteQuestion": "On Motion to Reconsider"}, "procedural"),
        ({"voteQuestion": "On Motion to Table"}, "procedural"),
        ({"voteQuestion": "Something nobody has seen before"}, "other"),
        ({"voteQuestion": None}, "other"),
    ],
)
def test_vote_type_classification(overrides: dict, expected: str) -> None:
    row = transform_roll_call(_envelope(**overrides))
    assert row["vote_type"] == expected


def test_amendment_classification_wins_even_with_a_passage_like_question() -> None:
    """An amendment vote's question text can itself say "agreeing to the
    amendment", which the final_passage phrase list does not match, but this
    pins that even an edge-case question does not override the amendment
    signal -- amendmentType/amendmentNumber always wins (see the module
    docstring's priority order).
    """
    row = transform_roll_call(
        _envelope(
            amendmentType="HAMDT",
            amendmentNumber="6",
            voteQuestion="On Agreeing to the Amendment, as further amended",
        )
    )
    assert row["vote_type"] == "amendment"


def test_no_house_vote_is_ever_classified_cloture() -> None:
    """Documents the deliberate gap: the House has no cloture motion, so this
    loader must never produce "cloture" from House data -- see the module
    docstring's "Why nothing maps to cloture" note.
    """
    for question in (
        "On Motion to Suspend the Rules and Pass",
        "On Agreeing to the Amendment",
        "On Motion to Recommit",
        "On Ordering the Previous Question",
        "anything else",
    ):
        row = transform_roll_call(
            _envelope(voteQuestion=question, amendmentType=None, amendmentNumber=None)
        )
        assert row["vote_type"] != "cloture"


# --- amendment_ref -------------------------------------------------------------


def test_amendment_ref_is_type_and_number_when_present() -> None:
    row = transform_roll_call(_envelope(amendmentType="HAMDT", amendmentNumber="6"))
    assert row["amendment_ref"] == "HAMDT 6"


def test_amendment_ref_is_none_when_absent() -> None:
    row = transform_roll_call(_envelope(amendmentType=None, amendmentNumber=None))
    assert row["amendment_ref"] is None


def test_amendment_ref_is_none_when_only_type_present_without_number() -> None:
    row = transform_roll_call(_envelope(amendmentType="HAMDT", amendmentNumber=None))
    assert row["amendment_ref"] is None


# --- tally ----------------------------------------------------------------------


def test_tally_sums_across_every_party() -> None:
    row = transform_roll_call(_envelope())
    tally = row["tally"]
    assert (tally.yea, tally.nay, tally.present, tally.not_voting) == (15, 3, 1, 2)


def test_tally_is_all_zero_when_vote_party_total_is_absent() -> None:
    row = transform_roll_call(_envelope(votePartyTotal=None))
    tally = row["tally"]
    assert (tally.yea, tally.nay, tally.present, tally.not_voting) == (0, 0, 0, 0)


def test_tally_is_all_zero_when_item_list_is_empty() -> None:
    row = transform_roll_call(_envelope(votePartyTotal={"item": []}))
    tally = row["tally"]
    assert (tally.yea, tally.nay, tally.present, tally.not_voting) == (0, 0, 0, 0)


def test_tally_treats_a_non_numeric_total_as_zero_rather_than_crashing() -> None:
    row = transform_roll_call(
        _envelope(votePartyTotal={"item": [{"yeaTotal": "not-a-number", "nayTotal": 3}]})
    )
    tally = row["tally"]
    assert tally.yea == 0
    assert tally.nay == 3


def test_tally_skips_a_non_dict_entry_in_the_item_list() -> None:
    """A malformed `votePartyTotal.item[]` entry that is not even an object
    (a bare string, say) is skipped rather than crashing the whole record --
    the remaining, well-formed entries still contribute.
    """
    row = transform_roll_call(
        _envelope(
            votePartyTotal={
                "item": [
                    "not-a-dict",
                    {"yeaTotal": 7, "nayTotal": 0, "presentTotal": 0, "notVotingTotal": 0},
                ]
            }
        )
    )
    tally = row["tally"]
    assert tally.yea == 7


# --- fetching (mocked httpx) ---------------------------------------------------


def test_fetch_roll_call_numbers_returns_every_roll_call_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, *, params: dict, timeout: float) -> httpx.Response:
        assert "house-vote/118/2" in url
        assert params["api_key"] == "test-key"
        body = {"houseRollCallVotes": [{"rollCallNumber": 397}, {"rollCallNumber": 398}]}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(house_rollcalls_module.httpx, "get", fake_get)
    numbers = fetch_roll_call_numbers(congress=118, session=2, api_key="test-key")
    assert numbers == [397, 398]


def test_fetch_roll_call_numbers_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, params: dict, timeout: float) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "API_KEY_INVALID"}})

    monkeypatch.setattr(house_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(HouseRollCallSourceError, match="403"):
        fetch_roll_call_numbers(congress=118, session=2, api_key="bad-key")


def test_fetch_roll_call_numbers_raises_when_body_shape_is_unexpected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, *, params: dict, timeout: float) -> httpx.Response:
        return httpx.Response(200, json={"somethingElse": []})

    monkeypatch.setattr(house_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(HouseRollCallSourceError, match="houseRollCallVotes"):
        fetch_roll_call_numbers(congress=118, session=2, api_key="test-key")


def test_fetch_roll_call_numbers_wraps_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, params: dict, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(house_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(HouseRollCallSourceError, match="could not reach"):
        fetch_roll_call_numbers(congress=118, session=2, api_key="test-key")


def test_fetch_roll_call_numbers_skips_an_entry_with_a_non_int_roll_call_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One malformed list entry (a string or missing rollCallNumber) must not
    poison the whole list -- mirrors `transform_roll_calls`' "a gap is
    skipped, not fatal" posture for the list endpoint itself.
    """

    def fake_get(url: str, *, params: dict, timeout: float) -> httpx.Response:
        body = {
            "houseRollCallVotes": [
                {"rollCallNumber": 397},
                {"rollCallNumber": "not-an-int"},
                {"no_roll_call_number_at_all": True},
            ]
        }
        return httpx.Response(200, json=body)

    monkeypatch.setattr(house_rollcalls_module.httpx, "get", fake_get)
    numbers = fetch_roll_call_numbers(congress=118, session=2, api_key="test-key")
    assert numbers == [397]


def test_fetch_roll_call_item_returns_the_full_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _envelope()

    def fake_get(url: str, *, params: dict, timeout: float) -> httpx.Response:
        assert "house-vote/119/2/452" in url
        return httpx.Response(200, json=expected)

    monkeypatch.setattr(house_rollcalls_module.httpx, "get", fake_get)
    envelope = fetch_roll_call_item(
        congress=119, session=2, roll_call_number=452, api_key="test-key"
    )
    assert envelope == expected


def test_fetch_roll_call_item_raises_when_vote_object_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, *, params: dict, timeout: float) -> httpx.Response:
        return httpx.Response(200, json={"nope": True})

    monkeypatch.setattr(house_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(HouseRollCallSourceError, match="houseRollCallVote"):
        fetch_roll_call_item(congress=119, session=2, roll_call_number=452, api_key="test-key")


def test_fetch_roll_call_item_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, params: dict, timeout: float) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})

    monkeypatch.setattr(house_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(HouseRollCallSourceError, match="404"):
        fetch_roll_call_item(congress=119, session=2, roll_call_number=999999, api_key="test-key")


def test_fetch_roll_call_item_wraps_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, params: dict, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(house_rollcalls_module.httpx, "get", fake_get)
    with pytest.raises(HouseRollCallSourceError, match="could not reach"):
        fetch_roll_call_item(congress=119, session=2, roll_call_number=452, api_key="test-key")


# --- LoadReport ----------------------------------------------------------------


def test_load_report_row_count_reflects_roll_calls_written() -> None:
    from app.ingest.house_rollcalls import LoadReport

    report = LoadReport(roll_calls_written=["us-house:119-2:452", "us-house:119-2:453"])
    assert report.row_count == 2


# --- provenance write path -----------------------------------------------------


def _client() -> ZeroDBClient:
    fake = FakeZeroDBTransport()
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table(
        "provenance_log",
        {
            "id": "string",
            "entity_table": "string",
            "entity_id": "string",
            "source_url": "string",
            "fetched_at": "string",
            "adapter_version": "string",
        },
    )
    return client


def test_write_provenance_inserts_a_row_with_the_content_addressed_id() -> None:
    client = _client()
    written = write_provenance(
        client,
        entity_table="roll_calls",
        entity_id="us-house:119-2:452",
        source_url="https://api.congress.gov/v3/house-vote/119/2/452",
        fetched_at="2026-10-16T00:00:00+00:00",
        content_hash="abc123",
    )
    assert written is not None
    assert written.id == "prov:abc123"


def test_write_provenance_is_a_noop_when_the_same_content_hash_already_exists() -> None:
    client = _client()
    kwargs = dict(
        entity_table="roll_calls",
        entity_id="us-house:119-2:452",
        source_url="https://api.congress.gov/v3/house-vote/119/2/452",
        fetched_at="2026-10-16T00:00:00+00:00",
        content_hash="abc123",
    )
    first = write_provenance(client, **kwargs)
    second = write_provenance(client, **{**kwargs, "fetched_at": "2026-10-17T00:00:00+00:00"})
    assert first is not None
    assert second is None  # already present under prov:abc123 -- nothing written


def test_write_provenance_writes_a_second_row_for_a_different_content_hash() -> None:
    client = _client()
    base = dict(
        entity_table="roll_calls",
        entity_id="us-house:119-2:452",
        source_url="https://api.congress.gov/v3/house-vote/119/2/452",
        fetched_at="2026-10-16T00:00:00+00:00",
    )
    first = write_provenance(client, **base, content_hash="abc123")
    second = write_provenance(client, **base, content_hash="def456")
    assert first is not None
    assert second is not None
    assert first.id != second.id


# --- HouseRollCallLoader --------------------------------------------------


def _bootstrapped_client() -> ZeroDBClient:
    fake = FakeZeroDBTransport()
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(client, pace=lambda: None).run()
    return client


def test_loader_writes_no_provenance_entry_when_a_row_has_no_content_hash_or_source_url() -> None:
    """A row missing `content_hash`/`source_url` entirely (should not happen
    given `transform_roll_call` always sets both, but defended against
    anyway) is still upserted into `roll_calls`; only the provenance write is
    skipped for that one row.
    """
    client = _bootstrapped_client()
    row = transform_roll_call(_envelope())
    row["content_hash"] = None
    row["source_url"] = None
    loader = HouseRollCallLoader(client, pace=lambda: None)
    report = loader.load([row])
    assert report.roll_calls_written == ["us-house:119-2:452"]
    assert report.provenance_written == []
