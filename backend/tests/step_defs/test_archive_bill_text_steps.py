"""Step definitions for tests/features/archive_bill_text.feature (story 5.4).

Runs entirely against story 1.2's in-memory fake transport and the fixture at
tests/fixtures/bill_text_sample.json -- never the live network, so CI needs no
CONGRESS_GOV_API_KEY, per CLAUDE.md.

The fixture holds REAL, live-captured Congress.gov responses: the
`/bill/119/hr/3424/text` and `/bill/119/hr/1/text` envelopes, plus the actual
bytes of two of H.R. 3424's published documents. That is why the "a version
with a null date" scenario exists at all -- the real H.R. 1 response's newest
version (the Enrolled Bill) carries `"date": null`, so "sort by date" is not a
total order on the live data and the loader has to say what it does about that.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.bill_text import (
    BillTextLoader,
    BillTextNotFoundError,
    FetchedDocument,
    TextVersion,
    select_text_version,
)
from app.repositories import Repository
from app.repositories.models import Bill
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/archive_bill_text.feature")

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SAMPLE = json.loads((FIXTURES / "bill_text_sample.json").read_text())

HR3424_TEXT_ENVELOPE = SAMPLE["text_versions_hr3424"]
HR1_TEXT_ENVELOPE = SAMPLE["text_versions_hr1"]
#: The real bytes of H.R. 3424's "Referred in Senate" XML -- the document the
#: default fixture's newest version points at.
HR3424_XML_URL = "https://www.congress.gov/119/bills/hr3424/BILLS-119hr3424rfs.xml"
HR3424_DOC = SAMPLE["documents"]["hr3424_rfs_xml"]
DOC_BYTES = HR3424_DOC["text"].encode("utf-8")
DOC_SHA256 = hashlib.sha256(DOC_BYTES).hexdigest()
CHANGED_BYTES = (HR3424_DOC["text"] + "\n<!-- amended -->\n").encode("utf-8")

BILL_ID = "us-house:119:hr-3424"
CAPTURED_AT = "2026-10-19T12:00:00+00:00"


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
def state() -> dict[str, Any]:
    """Everything one scenario builds up: the envelope to serve, the bytes to
    serve, and what the loader or the API answered.
    """
    return {
        "envelope": HR3424_TEXT_ENVELOPE,
        "doc_bytes": DOC_BYTES,
        "doc_content_type": "application/xml",
    }


def _loader(zerodb: ZeroDBClient, state: dict[str, Any]) -> BillTextLoader:
    def fetch_versions(**_kwargs: Any) -> dict[str, Any]:
        return state["envelope"]

    def fetch_document(url: str, **_kwargs: Any) -> FetchedDocument:
        state.setdefault("fetched_urls", []).append(url)
        return FetchedDocument(
            url=url,
            content=state["doc_bytes"],
            content_type=state["doc_content_type"],
        )

    return BillTextLoader(
        zerodb,
        pace=lambda: None,
        versions_fetcher=fetch_versions,
        document_fetcher=fetch_document,
        now=lambda: CAPTURED_AT,
    )


# --- background ---------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def _tables(zerodb: ZeroDBClient) -> None:
    Bootstrapper(zerodb).run()


@given(parsers.parse('the bill "{bill_id}" is already ingested'))
def _bill_ingested(zerodb: ZeroDBClient, bill_id: str) -> None:
    Repository(client=zerodb, model=Bill).upsert(
        {
            "id": bill_id,
            "title": "SPACE Act of 2025",
            "introduced_on": "2025-05-15",
            "source_url": "https://api.congress.gov/v3/bill/119/hr/3424?format=json",
            "content_hash": "seed",
        }
    )


# --- arranging the source -----------------------------------------------------


@given(parsers.parse('the bill text source lists four versions, newest dated "{date}"'))
def _four_versions(state: dict[str, Any], date: str) -> None:
    versions = state["envelope"]["textVersions"]
    assert len(versions) == 4, f"fixture changed: expected 4 versions, got {len(versions)}"
    assert str(versions[0]["date"]).startswith(date)


@given("the bill text source is the live H.R. 1 response, whose enrolled version has a null date")
def _hr1_envelope(state: dict[str, Any]) -> None:
    state["envelope"] = HR1_TEXT_ENVELOPE
    enrolled = [v for v in HR1_TEXT_ENVELOPE["textVersions"] if v["type"] == "Enrolled Bill"]
    assert enrolled and enrolled[0]["date"] is None, "fixture changed: H.R. 1 enrolled date is set"


@given(parsers.parse('the newest text version offers "{a}", "{b}" and "{c}"'))
def _three_formats(state: dict[str, Any], a: str, b: str, c: str) -> None:
    offered = {f["type"] for f in state["envelope"]["textVersions"][0]["formats"]}
    assert {a, b, c} <= offered, f"fixture changed: newest version offers {offered}"


@given(parsers.parse('the newest text version offers only "{fmt}"'))
def _one_format(state: dict[str, Any], fmt: str) -> None:
    envelope = json.loads(json.dumps(state["envelope"]))
    newest = envelope["textVersions"][0]
    newest["formats"] = [f for f in newest["formats"] if f["type"] == fmt]
    assert newest["formats"], f"fixture has no {fmt} format on the newest version"
    state["envelope"] = envelope
    state["doc_content_type"] = "application/pdf"


@given("the bill text source reports no text versions")
def _no_versions(state: dict[str, Any]) -> None:
    state["envelope"] = {"pagination": {"count": 0}, "textVersions": []}


@given("the newest text version offers no formats at all")
def _no_formats(state: dict[str, Any]) -> None:
    envelope = json.loads(json.dumps(state["envelope"]))
    for version in envelope["textVersions"]:
        version["formats"] = []
    state["envelope"] = envelope


@given(parsers.parse('the text of bill "{bill_id}" is already archived'))
def _already_archived(zerodb: ZeroDBClient, state: dict[str, Any], bill_id: str) -> None:
    report = _loader(zerodb, state).archive(bill_id=bill_id)
    assert report.archived, f"pre-archival failed: {report.reason}"
    state["first_report"] = report


@given("the bill text document has since changed")
def _doc_changed(state: dict[str, Any]) -> None:
    state["doc_bytes"] = CHANGED_BYTES


@given(parsers.parse('the bill "{bill_id}" names a text file with no stored bytes'))
def _dangling_file(
    zerodb: ZeroDBClient, fake: FakeZeroDBTransport, state: dict[str, Any], bill_id: str
) -> None:
    """The exact state story 9.1's archives were left in: a `files` metadata row
    with no object behind it, named by the entity row. See
    `ZeroDBClient.upload_file`'s docstring.
    """
    stored = zerodb.upload_file(
        file_key=f"bill-text/{bill_id}.xml",
        content=DOC_BYTES,
        file_name="dangling.xml",
        content_type="application/xml",
        file_metadata={"sha256": DOC_SHA256},
    )
    fake.file_content.pop(stored.file_id, None)
    bills: Repository[Bill] = Repository(client=zerodb, model=Bill)
    existing = bills.get(bill_id)
    assert existing is not None
    row = existing.row_data()
    row["id"] = bill_id
    row["text_file_id"] = stored.file_id
    bills.upsert(row)


# --- acting -------------------------------------------------------------------


@when(parsers.parse('I archive the text of bill "{bill_id}"'))
def _archive(zerodb: ZeroDBClient, state: dict[str, Any], bill_id: str) -> None:
    try:
        state["report"] = _loader(zerodb, state).archive(bill_id=bill_id)
    except BillTextNotFoundError as exc:
        state["error"] = exc


@when(parsers.parse('the UI requests the text of bill "{bill_id}"'))
def _ui_request(zerodb: ZeroDBClient, state: dict[str, Any], bill_id: str) -> None:
    from app.bills.router import router as bills_router
    from app.bills.router import zerodb_client_for_bills

    app = FastAPI()
    app.include_router(bills_router)
    app.dependency_overrides[zerodb_client_for_bills] = lambda: zerodb
    with TestClient(app) as client:
        state["response"] = client.get(f"/v1/bills/{bill_id}/text")


@when("I fetch the presigned url")
def _fetch_presigned(zerodb: ZeroDBClient, fake: FakeZeroDBTransport, state: dict[str, Any]) -> None:
    """The fake's presigned URL is not a real HTTP endpoint, so "fetching" it
    means resolving it the way the fake's own object store does: by file id.
    The live round-trip over a real signed URL is proven by
    `scripts/verify_bill_text_live.py`, not here -- CI makes no network calls.
    """
    body = state["response"].json()
    file_id = body["file_id"]
    assert file_id in body["url"], "the presigned url does not name the file it signs"
    state["fetched_bytes"] = fake.file_content[file_id]


# --- asserting: the stored file ----------------------------------------------


@then(parsers.parse('the bill "{bill_id}" has a text_file_id'))
def _has_text_file_id(zerodb: ZeroDBClient, bill_id: str) -> None:
    bill = Repository(client=zerodb, model=Bill).get(bill_id)
    assert bill is not None
    assert bill.text_file_id, "bills.text_file_id was not set"


@then("the stored file's bytes are byte-for-byte the fetched document")
def _bytes_match(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    report = state["report"]
    assert zerodb.download_file(file_id=report.text_file_id) == state["doc_bytes"]


@then("the stored file's metadata records the sha256 of those bytes")
def _metadata_sha(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    stored = zerodb.get_file(file_id=state["report"].text_file_id)
    assert stored.file_metadata["sha256"] == hashlib.sha256(state["doc_bytes"]).hexdigest()


@then("the stored file's metadata records the source url and captured_at")
def _metadata_provenance(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    stored = zerodb.get_file(file_id=state["report"].text_file_id)
    assert stored.file_metadata["source_url"] == state["report"].source_url
    assert stored.file_metadata["captured_at"] == CAPTURED_AT


@then("the report's sha256 is the sha256 of the fetched document")
def _report_sha(state: dict[str, Any]) -> None:
    assert state["report"].sha256 == hashlib.sha256(state["doc_bytes"]).hexdigest()


@then("the file metadata sha256 matches the report's sha256")
def _sha_agree(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    stored = zerodb.get_file(file_id=state["report"].text_file_id)
    assert stored.file_metadata["sha256"] == state["report"].sha256


@then(parsers.parse('the report names version type "{version_type}"'))
def _report_version_type(state: dict[str, Any], version_type: str) -> None:
    assert state["report"].version_type == version_type


@then(parsers.parse('the report names version date "{date}"'))
def _report_version_date(state: dict[str, Any], date: str) -> None:
    assert state["report"].version_date == date


@then(parsers.parse('the report names format "{fmt}"'))
def _report_format(state: dict[str, Any], fmt: str) -> None:
    assert state["report"].format_type == fmt


@then("the report's source url is the congress.gov document url")
def _report_source_url(state: dict[str, Any]) -> None:
    assert state["report"].source_url == HR3424_XML_URL


@then(parsers.parse('the stored file\'s content type is "{content_type}"'))
def _content_type(zerodb: ZeroDBClient, state: dict[str, Any], content_type: str) -> None:
    stored = zerodb.get_file(file_id=state["report"].text_file_id)
    assert stored.content_type == content_type


# --- asserting: graceful absence ---------------------------------------------


@then("no text was archived and the reason is recorded")
def _not_archived(state: dict[str, Any]) -> None:
    report = state["report"]
    assert report.archived is False
    assert report.text_file_id is None
    assert report.reason, "a skipped archival must say why"


@then(parsers.parse('the bill "{bill_id}" still has no text_file_id'))
def _still_no_file(zerodb: ZeroDBClient, bill_id: str) -> None:
    bill = Repository(client=zerodb, model=Bill).get(bill_id)
    assert bill is not None
    assert bill.text_file_id is None


@then("no error was raised")
def _no_error(state: dict[str, Any]) -> None:
    assert "error" not in state


@then("archiving fails because the bill is unknown")
def _unknown_bill(state: dict[str, Any]) -> None:
    assert isinstance(state.get("error"), BillTextNotFoundError)


# --- asserting: idempotency ---------------------------------------------------


@then("the report says no new upload was made")
def _no_new_upload(state: dict[str, Any]) -> None:
    assert state["report"].uploaded is False


@then("the report says a new upload was made")
def _new_upload(state: dict[str, Any]) -> None:
    assert state["report"].uploaded is True


@then(parsers.parse('the bill "{bill_id}" has {count:d} stored text file'))
@then(parsers.parse('the bill "{bill_id}" has {count:d} stored text files'))
def _file_count(fake: FakeZeroDBTransport, bill_id: str, count: int) -> None:
    keys = [
        envelope.get("file_key") or ""
        for envelope in fake.files.values()
        if str(envelope.get("file_key") or "").startswith("bill-text/")
    ]
    assert len(keys) == count, f"expected {count} bill-text files, found {keys}"


@then(parsers.parse('the bill "{bill_id}" points at the newest text file'))
def _points_at_newest(zerodb: ZeroDBClient, state: dict[str, Any], bill_id: str) -> None:
    bill = Repository(client=zerodb, model=Bill).get(bill_id)
    assert bill is not None
    assert bill.text_file_id == state["report"].text_file_id
    assert bill.text_file_id != state["first_report"].text_file_id


# --- asserting: the UI's presigned URL ---------------------------------------


@then(parsers.parse("the response is {code:d}"))
def _status(state: dict[str, Any], code: int) -> None:
    assert state["response"].status_code == code, state["response"].text


@then("the response carries a presigned url")
def _has_url(state: dict[str, Any]) -> None:
    assert state["response"].json()["url"].startswith("http")


@then("the response carries the sha256 of the archived bytes")
def _response_sha(state: dict[str, Any]) -> None:
    assert state["response"].json()["sha256"] == DOC_SHA256


@then("the response's url expires within an hour")
def _expires_soon(state: dict[str, Any]) -> None:
    expires_in = state["response"].json()["expires_in_seconds"]
    assert 0 < expires_in <= 3600


@then("the fetched bytes hash to the sha256 in the response")
def _round_trip(state: dict[str, Any]) -> None:
    actual = hashlib.sha256(state["fetched_bytes"]).hexdigest()
    assert actual == state["response"].json()["sha256"]


@then("the response carries the congress.gov source url")
def _response_source_url(state: dict[str, Any]) -> None:
    assert state["response"].json()["source_url"] == HR3424_XML_URL


@then("the response names the captured text version")
def _response_version(state: dict[str, Any]) -> None:
    body = state["response"].json()
    assert body["version_type"] == "Referred in Senate"
    assert body["format_type"] == "Formatted XML"


# --- asserting: provenance ----------------------------------------------------


def _provenance_rows(zerodb: ZeroDBClient) -> list[dict[str, Any]]:
    return [row.row_data for row in zerodb.query_rows("provenance_log", limit=200).rows]


@then("a provenance entry exists for the bill text file")
def _provenance_exists(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    matches = [
        row
        for row in _provenance_rows(zerodb)
        if row.get("entity_table") == "files" and row.get("entity_id") == state["report"].text_file_id
    ]
    assert matches, "no provenance_log row names the archived bill text file"
    state["provenance"] = matches[0]


@then("that provenance entry's source url is the congress.gov document url")
def _provenance_source(state: dict[str, Any]) -> None:
    assert state["provenance"]["source_url"] == HR3424_XML_URL


@then("that provenance entry's content hash is the archived sha256")
def _provenance_hash(zerodb: ZeroDBClient, state: dict[str, Any]) -> None:
    from app.repositories.provenance import provenance_id

    assert state["provenance"]["id"] == provenance_id(state["report"].sha256)


# --- a unit-level check the feature file cannot express cleanly ---------------


def test_select_text_version_prefers_the_newest_dated_version() -> None:
    """Direct cover for `select_text_version`'s ordering, including the live
    null-date case the H.R. 1 scenario exercises end to end.
    """
    chosen = select_text_version(HR1_TEXT_ENVELOPE)
    assert chosen is not None
    assert chosen.version_type == "Enrolled Bill"
    assert chosen.date is None

    chosen = select_text_version(HR3424_TEXT_ENVELOPE)
    assert chosen is not None
    assert chosen.version_type == "Referred in Senate"
    assert chosen.date == "2025-09-09T04:00:00Z"


def test_select_text_version_returns_none_when_there_are_no_versions() -> None:
    assert select_text_version({"textVersions": []}) is None
    assert select_text_version({}) is None


def test_a_text_version_with_no_usable_format_selects_nothing() -> None:
    assert TextVersion(version_type="Introduced in House", date=None, formats={}).best_format() is None
