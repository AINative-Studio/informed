"""Step definitions for tests/features/archive_debates_questionnaires.feature (9.2).

Runs entirely against story 1.2's in-memory fake transport and local fixtures
(tests/fixtures/questionnaire_sample*.html,
tests/fixtures/debate_transcript_sample.html) -- never the live network in CI,
per CLAUDE.md. `fetch_source_document` is the only function in
`app.ingest.debates_questionnaires` that reaches the network; every scenario
here injects a fake `document_fetcher`, the same seam story 9.1's
`CampaignSiteLoader` and every other ingest module in this package use.

The real live fetches -- real sitting members' real questionnaire answers and
real transcript text -- live in scripts/verify_debates_questionnaires_live.py,
not here. The fixtures use a fictional candidate deliberately: attributing
invented promise wording to a real member of Congress is the specific failure
this project's evidence rules exist to prevent.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.ingest.debates_questionnaires import (
    SOURCE_TYPE_DEBATE_TRANSCRIPT,
    SOURCE_TYPE_QUESTIONNAIRE,
    FetchedDocument,
    SourceArchiveReport,
    SourceDocumentError,
    SourceDocumentLoader,
)
from app.repositories import Repository
from app.repositories.models import Candidacy, PromiseSource
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/archive_debates_questionnaires.feature")

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
QUESTIONNAIRE_HTML = (FIXTURES / "questionnaire_sample.html").read_text()
QUESTIONNAIRE_CHANGED_HTML = (FIXTURES / "questionnaire_sample_changed.html").read_text()
TRANSCRIPT_HTML = (FIXTURES / "debate_transcript_sample.html").read_text()

CANDIDACY_ID = "us-2026-11-03-general:ocd-division/country:us/state:ga:person:bioguide-O000175"
QUESTIONNAIRE_URL = "https://voter-guide.example/candidate-detail?id=4242"
TRANSCRIPT_URL = "https://transcripts.example/crec/2026-09-30/PgH9999.htm"
CAMPAIGN_SITE_URL = "https://arriaga-for-congress.example/"


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
    documents: dict[str, str] | None = None,
    unfetchable: bool = False,
) -> SourceDocumentLoader:
    """A loader whose fetches come from the fixtures, never the network."""
    pages = (
        documents
        if documents is not None
        else {QUESTIONNAIRE_URL: QUESTIONNAIRE_HTML, TRANSCRIPT_URL: TRANSCRIPT_HTML}
    )

    def fetch(url: str) -> FetchedDocument:
        if unfetchable:
            raise SourceDocumentError(f"could not reach {url}: simulated network failure")
        if url not in pages:
            raise SourceDocumentError(f"could not reach {url}: not in this test's fixture set")
        return FetchedDocument(url=url, body=pages[url], content_type="text/html; charset=utf-8")

    return SourceDocumentLoader(
        zerodb,
        pace=lambda: None,
        document_fetcher=fetch,
        now=lambda: "2026-10-20T12:00:00+00:00",
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
            "id": "ocd-division/country:us/state:ga",
            "body_id": "us-senate",
            "name": "Georgia",
            "state": "GA",
            "valid_from": "2025-01-03",
        }
    )
    Repository(client=zerodb, model=Person).upsert(
        {
            "id": "person:bioguide-O000175",
            "canonical_name": "OSSOFF, JON",
            "external_ids": {"bioguide": "O000175"},
            "photo_url": None,
            "terms": [],
        }
    )
    Repository(client=zerodb, model=Candidacy).upsert(
        {
            "id": candidacy_id,
            "election_id": "us-2026-11-03-general",
            "district_id": "ocd-division/country:us/state:ga",
            "person_id": "person:bioguide-O000175",
            "person_name": "OSSOFF, JON",
            "party": "DEM",
            "is_incumbent": True,
            "status": "filed",
        }
    )


@given("a campaign website has already been archived for that candidacy")
def _campaign_site_archived(zerodb: ZeroDBClient) -> None:
    """A story-9.1-shaped row, so the "beyond websites" count means something.

    Written straight through the repository rather than by running
    `CampaignSiteLoader`: this story's criterion is about the source types
    present on a candidacy, and reproducing 9.1's whole capture path here
    would test 9.1 again rather than 9.2.
    """
    from app.ingest.campaign_sites import SOURCE_TYPE as CAMPAIGN_SITE_SOURCE_TYPE

    body = "<html><body><p>campaign site</p></body></html>"
    sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    stored = zerodb.upload_file(
        file_key=f"promise-sources/src:{sha256}.html",
        content=body.encode("utf-8"),
        file_name=f"src:{sha256}.html",
        content_type="text/html; charset=utf-8",
        file_metadata={"sha256": sha256},
    )
    Repository(client=zerodb, model=PromiseSource).upsert(
        {
            "id": f"src:{sha256}",
            "candidacy_id": CANDIDACY_ID,
            "source_type": CAMPAIGN_SITE_SOURCE_TYPE,
            "url": CAMPAIGN_SITE_URL,
            "archived_file_id": stored.file_id,
            "captured_at": "2026-10-16T22:00:00+00:00",
        }
    )


@given("the questionnaire url cannot be fetched")
def _questionnaire_unfetchable(result: dict[str, Any]) -> None:
    result["unfetchable"] = True


# --- archiving ---------------------------------------------------------------


def _archive(
    zerodb: ZeroDBClient,
    result: dict[str, Any],
    *,
    url: str,
    source_type: str,
    documents: dict[str, str] | None = None,
) -> SourceArchiveReport:
    loader = _loader(zerodb, documents=documents, unfetchable=result.get("unfetchable", False))
    report = loader.archive(candidacy_id=CANDIDACY_ID, url=url, source_type=source_type)
    result["report"] = report
    result[source_type] = report
    result["body"] = (documents or {}).get(url) or {
        QUESTIONNAIRE_URL: QUESTIONNAIRE_HTML,
        TRANSCRIPT_URL: TRANSCRIPT_HTML,
    }[url]
    result["url"] = url
    return report


@when("I archive the sample questionnaire for that candidacy")
@when("I archive the sample questionnaire for that candidacy again")
def archive_questionnaire(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    _archive(zerodb, result, url=QUESTIONNAIRE_URL, source_type=SOURCE_TYPE_QUESTIONNAIRE)


@given("I have already archived the sample questionnaire for that candidacy once")
def archived_questionnaire_once(
    zerodb: ZeroDBClient, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    _archive(zerodb, result, url=QUESTIONNAIRE_URL, source_type=SOURCE_TYPE_QUESTIONNAIRE)
    fake.calls.clear()


@when("I archive a changed version of the questionnaire for that candidacy")
def archive_questionnaire_changed(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    _archive(
        zerodb,
        result,
        url=QUESTIONNAIRE_URL,
        source_type=SOURCE_TYPE_QUESTIONNAIRE,
        documents={QUESTIONNAIRE_URL: QUESTIONNAIRE_CHANGED_HTML},
    )


@when("I archive the sample debate transcript for that candidacy")
def archive_transcript(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    _archive(zerodb, result, url=TRANSCRIPT_URL, source_type=SOURCE_TYPE_DEBATE_TRANSCRIPT)


# --- refusals ----------------------------------------------------------------


def _expect_refusal(call: Any, result: dict[str, Any]) -> None:
    try:
        call()
    except (ValueError, SourceDocumentError) as exc:
        result["error"] = exc
    else:
        result["error"] = None


@when("I try to archive a questionnaire for that candidacy with no url")
def archive_no_url(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    loader = _loader(zerodb)
    _expect_refusal(
        lambda: loader.archive(
            candidacy_id=CANDIDACY_ID, url="", source_type=SOURCE_TYPE_QUESTIONNAIRE
        ),
        result,
    )


@when(parsers.parse('I try to archive a source for that candidacy with source type "{bad_type}"'))
def archive_bad_source_type(zerodb: ZeroDBClient, result: dict[str, Any], bad_type: str) -> None:
    loader = _loader(zerodb)
    _expect_refusal(
        lambda: loader.archive(
            candidacy_id=CANDIDACY_ID, url=QUESTIONNAIRE_URL, source_type=bad_type
        ),
        result,
    )


@when("I try to archive a questionnaire whose page has no readable text")
def archive_textless(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    textless = "<html><head><style>p{color:red}</style></head><body></body></html>"
    loader = _loader(zerodb, documents={QUESTIONNAIRE_URL: textless})
    _expect_refusal(
        lambda: loader.archive(
            candidacy_id=CANDIDACY_ID,
            url=QUESTIONNAIRE_URL,
            source_type=SOURCE_TYPE_QUESTIONNAIRE,
        ),
        result,
    )


@when("I try to archive the sample questionnaire for that candidacy")
def try_archive_questionnaire(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    loader = _loader(zerodb, unfetchable=result.get("unfetchable", False))
    _expect_refusal(
        lambda: loader.archive(
            candidacy_id=CANDIDACY_ID,
            url=QUESTIONNAIRE_URL,
            source_type=SOURCE_TYPE_QUESTIONNAIRE,
        ),
        result,
    )


@when("I try to archive the sample questionnaire for a candidacy that does not exist")
def archive_unknown_candidacy(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    from app.repositories.errors import MissingReferenceError

    loader = _loader(zerodb)
    try:
        loader.archive(
            candidacy_id="us-2026-11-03-general:ocd-division/country:us/state:zz:person:nobody",
            url=QUESTIONNAIRE_URL,
            source_type=SOURCE_TYPE_QUESTIONNAIRE,
        )
    except MissingReferenceError as exc:
        result["error"] = exc
    else:
        result["error"] = None


# --- assertions: the archived document ---------------------------------------


@then("a document file is stored with the questionnaire's own sha256")
@then("a document file is stored with the transcript's own sha256")
def document_file_stored(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    report: SourceArchiveReport = result["report"]
    expected = hashlib.sha256(result["body"].encode("utf-8")).hexdigest()
    assert report.document_sha256 == expected
    stored = fake.files.get(report.document_file_id)
    assert stored is not None, "no file stored for the fetched document"
    assert stored["file_metadata"]["sha256"] == expected


@then("downloading the archived document returns the fetched bytes exactly")
def document_downloads(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    report: SourceArchiveReport = result["report"]
    downloaded = zerodb.download_file(file_id=report.document_file_id)
    assert downloaded == result["body"].encode("utf-8")


@then("the downloaded document hashes to the sha256 recorded on its file")
def document_hash_matches(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    report: SourceArchiveReport = result["report"]
    downloaded = zerodb.download_file(file_id=report.document_file_id)
    assert hashlib.sha256(downloaded).hexdigest() == report.document_sha256


@then("a text file is stored alongside the document")
def text_file_stored(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    report: SourceArchiveReport = result["report"]
    assert report.text_file_id, "no extracted-text file id on the report"
    assert report.text_file_id != report.document_file_id
    stored = fake.files.get(report.text_file_id)
    assert stored is not None, "no file stored for the extracted text"
    assert stored["content_type"].startswith("text/plain")
    assert stored["file_metadata"]["sha256"] == report.text_sha256


@then("the stored text contains the questionnaire's answer wording")
def text_contains_answer(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    report: SourceArchiveReport = result["report"]
    text = zerodb.download_file(file_id=report.text_file_id).decode("utf-8")
    assert "cap insulin co-pays at" in text
    assert "rural hospital grant program" in text


@then("the stored text carries no HTML markup")
def text_has_no_markup(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    report: SourceArchiveReport = result["report"]
    text = zerodb.download_file(file_id=report.text_file_id).decode("utf-8")
    assert "<p" not in text
    assert "<html" not in text
    assert "color: red" not in text, "a <style> block leaked into the extracted text"


# --- assertions: the promise_sources row -------------------------------------


def _promise_source_rows(fake: FakeZeroDBTransport, candidacy_id: str) -> list[dict[str, Any]]:
    """One candidacy's promise_sources rows, as the stored `row_data`.

    The Informed id is `row_data["id"]`; the fake's envelope `row_id` is
    ZeroDB's own internal uuid and is NOT what `provenance_log.entity_id`
    points at, so a step matching the two must read the former.
    """
    stored = fake.rows.get("promise_sources", {})
    return [
        row["row_data"]
        for row in stored.values()
        if row["row_data"].get("candidacy_id") == candidacy_id
    ]


@then(parsers.parse('a promise_sources row exists with id "src:{{document_sha256}}"'))
def promise_source_row_exists(result: dict[str, Any]) -> None:
    report: SourceArchiveReport = result["report"]
    assert report.promise_source_id == f"src:{report.document_sha256}"


@then(parsers.parse('a promise_sources row exists with source_type "{source_type}"'))
def promise_source_row_with_type(
    zerodb: ZeroDBClient, result: dict[str, Any], source_type: str
) -> None:
    repo: Repository[PromiseSource] = Repository(client=zerodb, model=PromiseSource)
    row = repo.get(result["report"].promise_source_id)
    assert row is not None, "the capture wrote no promise_sources row"
    assert row.source_type == source_type


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


@then("that promise_sources row has the questionnaire's own url")
@then("that promise_sources row has the transcript's own url")
def promise_source_has_url(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    repo: Repository[PromiseSource] = Repository(client=zerodb, model=PromiseSource)
    row = repo.get(result["report"].promise_source_id)
    assert row is not None
    assert row.url == result["url"]


@then("that promise_sources row has a captured_at timestamp")
def promise_source_has_captured_at(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    repo: Repository[PromiseSource] = Repository(client=zerodb, model=PromiseSource)
    row = repo.get(result["report"].promise_source_id)
    assert row is not None
    assert row.captured_at


@then(parsers.parse("exactly {count:d} promise_sources row exists for that candidacy"))
@then(parsers.parse("exactly {count:d} promise_sources rows exist for that candidacy"))
@then(parsers.parse("{count:d} promise_sources row exist for that candidacy"))
@then(parsers.parse("{count:d} promise_sources rows exist for that candidacy"))
def promise_source_count(fake: FakeZeroDBTransport, count: int) -> None:
    rows = _promise_source_rows(fake, CANDIDACY_ID)
    assert len(rows) == count, f"found {len(rows)} promise_sources row(s), expected {count}"


@then("no promise_sources row exists for that candidacy")
def no_promise_source_rows(fake: FakeZeroDBTransport) -> None:
    rows = _promise_source_rows(fake, CANDIDACY_ID)
    assert rows == [], f"a failed capture still wrote {len(rows)} promise_sources row(s)"


# --- assertions: the acceptance criterion as a count -------------------------


@then(
    parsers.parse(
        "the promise_sources rows for that candidacy carry at least {count:d} "
        'source types other than "{excluded}"'
    )
)
def non_website_source_type_count(fake: FakeZeroDBTransport, count: int, excluded: str) -> None:
    rows = _promise_source_rows(fake, CANDIDACY_ID)
    types = {row["source_type"] for row in rows} - {excluded}
    assert len(types) >= count, (
        f"story 9.2 needs at least {count} source types beyond {excluded!r}; found {sorted(types)}"
    )


@then("every one of those rows has a provenance_log entry")
def every_row_has_provenance(fake: FakeZeroDBTransport) -> None:
    """Each non-website source archived by THIS story is provenanced.

    The campaign-website row is skipped deliberately: this scenario's Given
    seeds that row straight through the repository (story 9.1's own loader
    writes its provenance), so asserting one here would test the fixture
    rather than this story's code.
    """
    provenance = fake.rows.get("provenance_log", {})
    provenanced = {
        row["row_data"].get("entity_id")
        for row in provenance.values()
        if row["row_data"].get("entity_table") == "promise_sources"
    }
    missing = [
        row["id"]
        for row in _promise_source_rows(fake, CANDIDACY_ID)
        if row.get("source_type") != "campaign_website" and row["id"] not in provenanced
    ]
    assert not missing, f"promise_sources rows with no provenance_log entry: {missing}"


# --- assertions: provenance --------------------------------------------------


def _provenance_rows_for(fake: FakeZeroDBTransport, entity_id: str) -> list[dict[str, Any]]:
    stored = fake.rows.get("provenance_log", {})
    return [
        row["row_data"]
        for row in stored.values()
        if row["row_data"].get("entity_table") == "promise_sources"
        and row["row_data"].get("entity_id") == entity_id
    ]


@then(parsers.parse('a provenance_log entry exists for promise_sources "src:{{document_sha256}}"'))
@then("a provenance_log entry exists for that transcript's promise_sources row")
def provenance_entry_exists(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    report: SourceArchiveReport = result["report"]
    rows = _provenance_rows_for(fake, report.promise_source_id)
    assert rows, f"no provenance_log entry for promise_sources {report.promise_source_id!r}"
    result["provenance_row"] = rows[0]


@then("that provenance_log entry has source_url equal to the questionnaire's own url")
@then("that provenance_log entry has source_url equal to the transcript's own url")
def provenance_entry_source_url(result: dict[str, Any]) -> None:
    assert result["provenance_row"]["source_url"] == result["url"]


@then(parsers.parse('that provenance_log entry has adapter_version "{adapter_version}"'))
def provenance_entry_adapter_version(result: dict[str, Any], adapter_version: str) -> None:
    assert result["provenance_row"]["adapter_version"] == adapter_version


@then("that provenance_log entry has the questionnaire's own content hash")
def provenance_entry_content_hash(fake: FakeZeroDBTransport, result: dict[str, Any]) -> None:
    """The hash is the provenance row's ID, per datamodel.md's `prov:{sha256}`.

    `write_provenance` takes `content_hash` and encodes it in the id rather
    than storing it as a field (datamodel.md's field list for
    `provenance_log` has no `content_hash` column), so that is what this
    asserts: the entry for this capture is the one keyed by the bytes this
    capture actually fetched.
    """
    from app.repositories.provenance import provenance_id

    report: SourceArchiveReport = result["report"]
    expected_id = provenance_id(report.document_sha256)
    rows = fake.rows.get("provenance_log", {})
    ids = {row["row_data"].get("id") for row in rows.values()}
    assert expected_id in ids, (
        f"no provenance_log row keyed by the captured document's hash "
        f"({expected_id}); found {sorted(i for i in ids if i)}"
    )


# --- assertions: readable back as extraction input ---------------------------


@then("the archived source's text can be read back from its promise_sources row")
def text_readable_from_row(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    """The path story 9.3's pipeline takes: row -> file id -> text.

    `promise_sources` has one `archived_file_id` field, so the row points at
    the document of record. The extracted text is found by the loader's own
    reader rather than by a second field datamodel.md does not define.
    """
    from app.ingest.debates_questionnaires import read_source_text

    repo: Repository[PromiseSource] = Repository(client=zerodb, model=PromiseSource)
    row = repo.get(result["report"].promise_source_id)
    assert row is not None
    result["read_back_text"] = read_source_text(zerodb, row)
    assert result["read_back_text"].strip()


@then("that text is the same text that was stored")
def read_back_text_matches(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    report: SourceArchiveReport = result["report"]
    stored = zerodb.download_file(file_id=report.text_file_id).decode("utf-8")
    assert result["read_back_text"] == stored


# --- assertions: idempotency and repair --------------------------------------


def _upload_calls(fake: FakeZeroDBTransport) -> list[Any]:
    """Calls to ZeroDB's binary-upload route — the one that stores bytes."""
    return [call for call in fake.calls if call.path.endswith("/files/upload")]


@then("no new file is uploaded by the second capture")
def no_new_file_uploaded(fake: FakeZeroDBTransport) -> None:
    uploads = _upload_calls(fake)
    assert len(uploads) == 0, f"expected no file-upload calls, saw {len(uploads)}"


@given("that capture's archived document has gone missing from storage")
def archive_content_missing(fake: FakeZeroDBTransport) -> None:
    """Reproduce the live state of every pre-fix capture: rows, no bytes."""
    fake.file_content.clear()
    fake.calls.clear()


@then("the files are re-uploaded")
def files_were_reuploaded(fake: FakeZeroDBTransport) -> None:
    uploads = _upload_calls(fake)
    assert len(uploads) == 2, f"expected the document and its text re-uploaded, saw {len(uploads)}"


# --- assertions: refusals ----------------------------------------------------


@then("the archival is refused for having no url to capture")
def refused_no_url(result: dict[str, Any]) -> None:
    assert result["error"] is not None, "a capture with no url was not refused"
    assert "url" in str(result["error"]).lower()


@then("the archival is refused for an unrecognised source type")
def refused_bad_source_type(result: dict[str, Any]) -> None:
    assert result["error"] is not None, "an unknown source type was not refused"
    assert "source_type" in str(result["error"]) or "source type" in str(result["error"])


@then("the archival is refused for having no extractable text")
def refused_no_text(result: dict[str, Any]) -> None:
    assert result["error"] is not None, "a text-free document was not refused"
    assert "text" in str(result["error"]).lower()


@then("the archival is refused for being unfetchable")
def refused_unfetchable(result: dict[str, Any]) -> None:
    assert result["error"] is not None, "an unfetchable url was not refused"
    assert isinstance(result["error"], SourceDocumentError)


@then("the archival is refused for an unresolvable candidacy")
def refused_unknown_candidacy(result: dict[str, Any]) -> None:
    from app.repositories.errors import MissingReferenceError

    assert result["error"] is not None, "a capture for a missing candidacy was not refused"
    assert isinstance(result["error"], MissingReferenceError)
