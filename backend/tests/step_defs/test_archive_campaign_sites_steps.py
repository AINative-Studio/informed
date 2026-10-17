"""Step definitions for tests/features/archive_campaign_sites.feature (9.1).

Runs entirely against story 1.2's in-memory fake transport and local fixtures
(tests/fixtures/campaign_site_sample*.html,
tests/fixtures/wayback_save_page_now_sample.json) -- never the live network in
CI, per CLAUDE.md. `fetch_campaign_page` and `submit_to_wayback` are the only
functions that reach the network; every scenario here calls the loader with a
fake `page_fetcher`/`wayback_client` injected, the same seam
`house_rollcalls.py` and `candidacies.py` use for their own network calls.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.campaign_sites import (
    CampaignSiteLoader,
    CaptureReport,
    FetchedPage,
    WaybackOutcome,
)
from app.repositories import Repository
from app.repositories.models import Candidacy, PromiseSource
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/archive_campaign_sites.feature")

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SAMPLE_HTML = (FIXTURES / "campaign_site_sample.html").read_text()
CHANGED_HTML = (FIXTURES / "campaign_site_sample_changed.html").read_text()
WAYBACK_FIXTURE = json.loads((FIXTURES / "wayback_save_page_now_sample.json").read_text())

CANDIDACY_ID = "us-2026-11-03-general:ocd-division/country:us/state:pa/cd:2:person:fec-H6PA02205"
SITE_URL = "https://arriaga-for-congress.example/"


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
def result() -> dict[str, Any]:
    return {}


def _loader(
    zerodb: ZeroDBClient,
    *,
    html: str = SAMPLE_HTML,
    wayback: WaybackOutcome | None = None,
) -> CampaignSiteLoader:
    def fetch(url: str) -> FetchedPage:
        return FetchedPage(url=url, html=html, content_type="text/html; charset=utf-8")

    def wayback_submit(url: str) -> WaybackOutcome:
        return wayback if wayback is not None else WaybackOutcome(ok=False, detail="not configured")

    return CampaignSiteLoader(
        zerodb,
        pace=lambda: None,
        page_fetcher=fetch,
        wayback_submitter=wayback_submit,
        now=lambda: "2026-10-16T22:00:00+00:00",
    )


# --- background --------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def _reference_tables(zerodb: ZeroDBClient) -> None:
    Bootstrapper(zerodb, pace=lambda: None).run()


@given(parsers.parse('a candidacy "{candidacy_id}" exists'))
def _candidacy_exists(zerodb: ZeroDBClient, candidacy_id: str) -> None:
    from app.repositories.models import District, Election, Person

    Repository(client=zerodb, model=Election).upsert(
        {
            "id": "us-2026-11-03-general",
            "jurisdiction_id": "us-federal",
            "name": "2026 federal general election",
            "election_date": "2026-11-03",
            "kind": "general",
        }
    )
    Repository(client=zerodb, model=District).upsert(
        {
            "id": "ocd-division/country:us/state:pa/cd:2",
            "body_id": "us-house",
            "name": "PA congressional district 2",
            "state": "PA",
            "valid_from": "2025-01-03",
        }
    )
    Repository(client=zerodb, model=Person).upsert(
        {
            "id": "person:fec-H6PA02205",
            "canonical_name": "ARRIAGA, JESSICA",
            "external_ids": {"fec": "H6PA02205"},
            "photo_url": None,
            "terms": [],
        }
    )
    Repository(client=zerodb, model=Candidacy).upsert(
        {
            "id": candidacy_id,
            "election_id": "us-2026-11-03-general",
            "district_id": "ocd-division/country:us/state:pa/cd:2",
            "person_id": "person:fec-H6PA02205",
            "person_name": "ARRIAGA, JESSICA",
            "party": "REP",
            "is_incumbent": False,
            "status": "filed",
        }
    )


@given("Internet Archive's Save Page Now API is unreachable")
def _spn_unreachable(result: dict[str, Any]) -> None:
    result["wayback"] = WaybackOutcome(ok=False, detail="Save Page Now request timed out")


@given("Internet Archive's Save Page Now API accepts the submission")
def _spn_accepts(result: dict[str, Any]) -> None:
    body = WAYBACK_FIXTURE["success_response"]["body"]
    snapshot_url = f"https://web.archive.org/web/{body['timestamp']}/{body['url']}"
    result["wayback"] = WaybackOutcome(ok=True, snapshot_url=snapshot_url)


# --- archiving -----------------------------------------------------------------


def _archive(
    zerodb: ZeroDBClient, result: dict[str, Any], *, html: str = SAMPLE_HTML
) -> CaptureReport:
    loader = _loader(zerodb, html=html, wayback=result.get("wayback"))
    report = loader.capture(candidacy_id=CANDIDACY_ID, url=SITE_URL)
    result["report"] = report
    result["html"] = html
    return report


@when("I archive the sample campaign site for that candidacy")
def archive_sample(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    _archive(zerodb, result)


@when("I archive the sample campaign site for that candidacy again")
def archive_sample_again(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    _archive(zerodb, result)


@given("I have already archived the sample campaign site for that candidacy once")
def archived_once(zerodb: ZeroDBClient, fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    _archive(zerodb, result)
    fake.calls.clear()


@when("I archive a changed version of the campaign site for that candidacy")
def archive_changed(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    _archive(zerodb, result, html=CHANGED_HTML)


@when("I try to archive a campaign site for that candidacy with no url")
def archive_no_url(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    loader = _loader(zerodb)
    try:
        loader.capture(candidacy_id=CANDIDACY_ID, url="")
    except ValueError as exc:
        result["error"] = exc
    else:
        result["error"] = None


# --- assertions ----------------------------------------------------------------


@then("an HTML file is stored with the campaign site's own sha256")
def html_file_stored(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    report: CaptureReport = result["report"]
    expected_sha256 = hashlib.sha256(result["html"].encode("utf-8")).hexdigest()
    assert report.html_sha256 == expected_sha256
    stored = fake.files.get(report.html_file_id)
    assert stored is not None, "no file stored for the HTML capture"
    assert stored["file_metadata"]["sha256"] == expected_sha256
    assert stored["content_type"].startswith("text/html")


@then("a PDF file is stored with its own sha256")
def pdf_file_stored(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    report: CaptureReport = result["report"]
    stored = fake.files.get(report.pdf_file_id)
    assert stored is not None, "no file stored for the PDF rendering"
    assert stored["content_type"] == "application/pdf"
    assert stored["file_metadata"]["sha256"] == report.pdf_sha256
    assert stored["file_metadata"]["sha256"] != report.html_sha256


@then("downloading the archived HTML returns the captured page's exact bytes")
def archived_html_downloads(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    report: CaptureReport = result["report"]
    downloaded = zerodb.download_file(file_id=report.html_file_id)
    assert downloaded == result["html"].encode("utf-8")


@then("the downloaded HTML hashes to the sha256 recorded on its file")
def archived_html_hash_matches(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    report: CaptureReport = result["report"]
    downloaded = zerodb.download_file(file_id=report.html_file_id)
    assert hashlib.sha256(downloaded).hexdigest() == report.html_sha256


@then("downloading the archived PDF returns a real PDF")
def archived_pdf_downloads(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    report: CaptureReport = result["report"]
    downloaded = zerodb.download_file(file_id=report.pdf_file_id)
    assert downloaded.startswith(b"%PDF-"), "the stored bytes are not a PDF"


@then("the downloaded PDF hashes to the sha256 recorded on its file")
def archived_pdf_hash_matches(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    report: CaptureReport = result["report"]
    downloaded = zerodb.download_file(file_id=report.pdf_file_id)
    assert hashlib.sha256(downloaded).hexdigest() == report.pdf_sha256


def _promise_source_rows(fake: FakeZeroDBTransport, candidacy_id: str) -> list[dict[str, Any]]:
    stored = fake.rows.get("promise_sources", {})
    return [
        row["row_data"]
        for row in stored.values()
        if row["row_data"].get("candidacy_id") == candidacy_id
    ]


@then(parsers.parse('a promise_sources row exists with id "src:{{html_sha256}}"'))
def promise_source_row_exists(result: dict[str, Any]) -> None:
    report: CaptureReport = result["report"]
    assert report.promise_source_id == f"src:{report.html_sha256}"


@then("that promise_sources row references the candidacy")
def promise_source_references_candidacy(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    repo: Repository[PromiseSource] = Repository(client=zerodb, model=PromiseSource)
    row = repo.get(result["report"].promise_source_id)
    assert row is not None
    assert row.candidacy_id == CANDIDACY_ID


@then(parsers.parse('that promise_sources row has source_type "{source_type}"'))
def promise_source_has_type(zerodb: ZeroDBClient, result: dict[str, Any], source_type: str) -> None:
    repo: Repository[PromiseSource] = Repository(client=zerodb, model=PromiseSource)
    row = repo.get(result["report"].promise_source_id)
    assert row is not None
    assert row.source_type == source_type


@then("that promise_sources row has the campaign site's own url")
def promise_source_has_url(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    repo: Repository[PromiseSource] = Repository(client=zerodb, model=PromiseSource)
    row = repo.get(result["report"].promise_source_id)
    assert row is not None
    assert row.url == SITE_URL


@then("that promise_sources row has a captured_at timestamp")
def promise_source_has_captured_at(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    repo: Repository[PromiseSource] = Repository(client=zerodb, model=PromiseSource)
    row = repo.get(result["report"].promise_source_id)
    assert row is not None
    assert row.captured_at


@then(parsers.parse("exactly {count:d} promise_sources row exists for that candidacy"))
@then(parsers.parse("exactly {count:d} promise_sources rows exist for that candidacy"))
def exact_promise_source_count(fake: FakeZeroDBTransport, count: int) -> None:
    rows = _promise_source_rows(fake, CANDIDACY_ID)
    assert len(rows) == count, f"found {len(rows)} promise_sources row(s), expected {count}"


@then(parsers.parse("{count:d} promise_sources row exist for that candidacy"))
@then(parsers.parse("{count:d} promise_sources rows exist for that candidacy"))
def n_promise_source_rows(fake: FakeZeroDBTransport, count: int) -> None:
    rows = _promise_source_rows(fake, CANDIDACY_ID)
    assert len(rows) == count, f"found {len(rows)} promise_sources row(s), expected {count}"


def _upload_calls(fake: FakeZeroDBTransport) -> list[Any]:
    """Calls to ZeroDB's binary-upload route — the one that stores bytes."""
    return [call for call in fake.calls if call.path.endswith("/files/upload")]


@then("no new file is uploaded by the second capture")
def no_new_file_uploaded(fake: FakeZeroDBTransport) -> None:
    uploads = _upload_calls(fake)
    assert len(uploads) == 0, f"expected no file-upload calls, saw {len(uploads)}"


@given("that capture's archived content has gone missing from storage")
def archive_content_missing(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    """Reproduce the live state of every pre-fix capture: rows, no bytes.

    The file metadata rows stay; only the stored objects go. That is exactly
    what story 9.1's archives looked like live — `promise_sources` and file
    rows intact, every download 404.
    """
    fake.file_content.clear()
    fake.calls.clear()


@then("the files are re-uploaded")
def files_were_reuploaded(fake: FakeZeroDBTransport) -> None:
    uploads = _upload_calls(fake)
    assert len(uploads) == 2, f"expected the HTML and PDF re-uploaded, saw {len(uploads)}"


# --- provenance ------------------------------------------------------------


def _provenance_rows_for(fake: FakeZeroDBTransport, entity_id: str) -> list[dict[str, Any]]:
    stored = fake.rows.get("provenance_log", {})
    return [
        row["row_data"]
        for row in stored.values()
        if row["row_data"].get("entity_table") == "promise_sources"
        and row["row_data"].get("entity_id") == entity_id
    ]


@then(parsers.parse('a provenance_log entry exists for promise_sources "src:{{html_sha256}}"'))
def provenance_entry_exists(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    report: CaptureReport = result["report"]
    rows = _provenance_rows_for(fake, report.promise_source_id)
    assert rows, f"no provenance_log entry for promise_sources {report.promise_source_id!r}"
    result["provenance_row"] = rows[0]


@then("that provenance_log entry has source_url equal to the campaign site's own url")
def provenance_entry_source_url(result: dict[str, Any]) -> None:
    assert result["provenance_row"]["source_url"] == SITE_URL


@then(parsers.parse('that provenance_log entry has adapter_version "{adapter_version}"'))
def provenance_entry_adapter_version(result: dict[str, Any], adapter_version: str) -> None:
    assert result["provenance_row"]["adapter_version"] == adapter_version


# --- Wayback submission (best-effort) ---------------------------------------


@then("the capture report notes the Wayback submission failed")
def wayback_failed(result: dict[str, Any]) -> None:
    report: CaptureReport = result["report"]
    assert report.wayback.ok is False
    assert report.wayback.snapshot_url is None


@then("the capture report carries the archive.org snapshot url")
def wayback_succeeded(result: dict[str, Any]) -> None:
    report: CaptureReport = result["report"]
    assert report.wayback.ok is True
    assert report.wayback.snapshot_url
    assert report.wayback.snapshot_url.startswith("https://web.archive.org/web/")


# --- refusal -----------------------------------------------------------------


@then("the archival is refused for having no url to capture")
def refused_no_url(result: dict[str, Any]) -> None:
    assert result["error"] is not None
    assert "url" in str(result["error"]).lower()
