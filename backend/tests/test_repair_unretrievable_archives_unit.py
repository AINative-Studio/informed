"""Unit tests for the unretrievable-archive repair script.

The script classifies each `promise_sources` row by a fact it can observe —
does the archive read back, and can the captured bytes still be obtained — and
only the "yes to both" case is automated. These tests pin that classification,
because getting it wrong in either direction is harmful: a false "mended"
leaves a citation pointing at nothing while reporting success, and a false
"unmendable" invites deleting a row whose archive was recoverable.

Everything runs against story 1.2's in-memory fake. No live call, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap import Bootstrapper
from app.ingest.campaign_sites import (
    CampaignSiteError,
    CampaignSiteLoader,
    FetchedPage,
    WaybackOutcome,
    sha256_hex,
)
from app.repositories import Repository
from app.repositories.models import Candidacy, District, Election, Person, PromiseSource
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport
from scripts.repair_unretrievable_archives import (
    OK_OUTCOMES,
    archive_reads_back,
    check_and_repair,
)

CANDIDACY_ID = "us-2026-11-03-general:ocd-division/country:us/state:pa/cd:2:person:fec-H6PA02205"
SITE_URL = "https://arriaga-for-congress.example/"
CAPTURED_HTML = "<html><body><h1>Platform</h1><p>I will cap insulin at $35.</p></body></html>"
CHANGED_HTML = "<html><body><h1>Platform</h1><p>A different promise entirely.</p></body></html>"


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    zerodb = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(zerodb, pace=lambda: None).run()
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
            "id": CANDIDACY_ID,
            "election_id": "us-2026-11-03-general",
            "district_id": "ocd-division/country:us/state:pa/cd:2",
            "person_id": "person:fec-H6PA02205",
            "person_name": "ARRIAGA, JESSICA",
            "party": "REP",
            "is_incumbent": False,
            "status": "filed",
        }
    )
    return zerodb


def _loader(client: ZeroDBClient, html: str) -> CampaignSiteLoader:
    return CampaignSiteLoader(
        client,
        pace=lambda: None,
        page_fetcher=lambda url: FetchedPage(
            url=url, html=html, content_type="text/html; charset=utf-8"
        ),
        wayback_submitter=lambda _url: WaybackOutcome(ok=False, detail="not configured"),
        now=lambda: "2026-10-16T22:00:00+00:00",
    )


def _archive(client: ZeroDBClient, html: str = CAPTURED_HTML) -> PromiseSource:
    _loader(client, html).capture(candidacy_id=CANDIDACY_ID, url=SITE_URL)
    stored = Repository(client=client, model=PromiseSource).get(f"src:{sha256_hex(html)}")
    assert stored is not None
    return stored


def _break_the_archive(fake: FakeZeroDBTransport) -> None:
    """Reproduce the live pre-fix state: metadata rows intact, no bytes."""
    fake.file_content.clear()


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, html: str | None) -> None:
    """Point the script's own `fetch_campaign_page` at a fixture, or make it fail."""

    def fetch(url: str, **_kwargs: Any) -> FetchedPage:
        if html is None:
            raise CampaignSiteError(f"refused: {url}")
        return FetchedPage(url=url, html=html, content_type="text/html; charset=utf-8")

    monkeypatch.setattr("scripts.repair_unretrievable_archives.fetch_campaign_page", fetch)


# --- archive_reads_back ------------------------------------------------------


def test_a_healthy_archive_reads_back(client: ZeroDBClient) -> None:
    assert archive_reads_back(client, _archive(client)) is True


def test_an_archive_with_no_stored_bytes_does_not_read_back(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """The exact live state story 9.1 left behind."""
    source = _archive(client)
    _break_the_archive(fake)

    assert archive_reads_back(client, source) is False


def test_an_archive_whose_bytes_do_not_match_its_hash_does_not_read_back(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """Readable is not enough — the wrong bytes make the citation wrong."""
    source = _archive(client)
    fake.file_content[source.archived_file_id] = b"<html>someone else's page</html>"

    assert archive_reads_back(client, source) is False


# --- classification ---------------------------------------------------------


def test_a_healthy_row_is_reported_retrievable_and_left_alone(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    source = _archive(client)
    fake.calls.clear()

    outcome = check_and_repair(client, source, apply=True, loader=_loader(client, CAPTURED_HTML))

    assert outcome.outcome == "retrievable"
    assert not [c for c in fake.calls if c.path.endswith("/files/upload")]


def test_an_unchanged_page_is_mendable_and_a_report_only_run_changes_nothing(
    client: ZeroDBClient, fake: FakeZeroDBTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _archive(client)
    _break_the_archive(fake)
    _patch_fetch(monkeypatch, CAPTURED_HTML)
    fake.calls.clear()

    outcome = check_and_repair(client, source, apply=False)

    assert outcome.outcome == "mendable"
    assert not [c for c in fake.calls if c.path.endswith("/files/upload")]
    assert archive_reads_back(client, source) is False, "a report-only run must not repair"


def test_applying_the_repair_to_an_unchanged_page_makes_the_archive_readable(
    client: ZeroDBClient, fake: FakeZeroDBTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repair keeps the row's id: same bytes, same sha256, same capture."""
    source = _archive(client)
    _break_the_archive(fake)
    _patch_fetch(monkeypatch, CAPTURED_HTML)

    outcome = check_and_repair(client, source, apply=True, loader=_loader(client, CAPTURED_HTML))

    assert outcome.outcome == "mended"
    repo: Repository[PromiseSource] = Repository(client=client, model=PromiseSource)
    reread = repo.get(source.id)
    assert reread is not None
    assert archive_reads_back(client, reread) is True
    assert client.download_file(file_id=reread.archived_file_id) == CAPTURED_HTML.encode("utf-8")
    # One capture, not two: a repair must not fork the evidence graph.
    assert len([r for r in repo.query(limit=100) if r.url == SITE_URL]) == 1


def test_a_changed_page_is_unmendable_and_is_never_silently_replaced(
    client: ZeroDBClient, fake: FakeZeroDBTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Today's bytes are a different capture; passing them off as this one would lie."""
    source = _archive(client)
    _break_the_archive(fake)
    _patch_fetch(monkeypatch, CHANGED_HTML)
    fake.calls.clear()

    outcome = check_and_repair(client, source, apply=True, loader=_loader(client, CHANGED_HTML))

    assert outcome.outcome == "unmendable"
    assert "cannot be reconstructed" in outcome.detail
    assert not [c for c in fake.calls if c.path.endswith("/files/upload")]
    assert outcome.outcome not in OK_OUTCOMES


# --- CampaignSiteLoader._archive_is_retrievable ------------------------------
#
# The same question asked from inside the loader, where it decides whether a
# re-run is a no-op or a repair. Covered here rather than in
# test_campaign_sites_unit.py because the fixtures a real capture needs (a
# bootstrapped client and a candidacy to cite) already exist in this file.


def test_the_loader_treats_a_row_naming_no_file_as_not_retrievable(
    client: ZeroDBClient,
) -> None:
    orphan = PromiseSource(
        id=f"src:{sha256_hex(CAPTURED_HTML)}",
        candidacy_id=CANDIDACY_ID,
        source_type="campaign_website",
        url=SITE_URL,
        archived_file_id="",
        captured_at="2026-10-16T22:00:00+00:00",
    )

    loader = _loader(client, CAPTURED_HTML)
    assert loader._archive_is_retrievable(orphan, sha256_hex(CAPTURED_HTML)) is False  # noqa: SLF001


def test_the_loader_treats_readable_but_wrong_bytes_as_not_retrievable(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """Readable is not enough: the wrong bytes make the citation wrong, not missing.

    Without the hash half of the check, a re-run would bless any readable
    object as this capture — so a mixed-up `file_key` would pass silently.
    """
    source = _archive(client)
    loader = _loader(client, CAPTURED_HTML)
    captured_sha256 = sha256_hex(CAPTURED_HTML)
    assert loader._archive_is_retrievable(source, captured_sha256) is True  # noqa: SLF001

    fake.file_content[source.archived_file_id] = b"<html>a different page entirely</html>"

    assert loader._archive_is_retrievable(source, captured_sha256) is False  # noqa: SLF001


def test_a_page_that_cannot_be_refetched_is_reported_unknown(
    client: ZeroDBClient, fake: FakeZeroDBTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A site refusing us is not evidence the archive is unrecoverable."""
    source = _archive(client)
    _break_the_archive(fake)
    _patch_fetch(monkeypatch, None)

    outcome = check_and_repair(client, source, apply=True)

    assert outcome.outcome == "unknown"
    assert outcome.outcome not in OK_OUTCOMES
