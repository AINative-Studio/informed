"""Archive the exact bill text a vote was on (story 5.4).

What this is, and why it exists
================================

prd.md's non-negotiable: *"Evidence over opinion. Every claim links to a
primary source."* A roll-call citation that says "they voted yes on H.R. 3424"
is only checkable if a voter can open what H.R. 3424 actually said. Story 5.3
left `bills.text_file_id` null and named this story as the one that fills it
("It does not fetch a bill's full text"); this module is that fill.

Acceptance criteria (backlog.md 5.4, issue #31): *"Text is stored in Files
with its sha256; the UI opens it via a presigned URL."* Both halves live here:
`BillTextLoader.archive` is the storing half, and `app.bills.router`'s
`GET /v1/bills/{bill_id}/text` is the presigned-URL half, reading the
`text_file_id` this loader writes.

**This module ran live.** `scripts/verify_bill_text_live.py` exercises it
against the real api.congress.gov, the real www.congress.gov documents and the
real ZeroDB project.

The source: textVersions, verified live rather than assumed
=============================================================

    GET /v3/bill/{congress}/{billType}/{billNumber}/text

confirmed live 2026-10-19 against H.R. 3424 and H.R. 1 of the 119th Congress
(both envelopes captured verbatim in `tests/fixtures/bill_text_sample.json`).
It is a *listing* endpoint, not a text endpoint: it returns no bill text at
all, only the versions that exist and, per version, the URLs of that version's
published documents on www.congress.gov. So archiving bill text is two fetches
— this listing from the API (api_key required), then the document itself from
www.congress.gov (no api_key; it is a plain static document).

Three live findings shaped the design, each with a scenario of its own:

  * **`date` can be null, and `textVersions[]` is NOT sorted.** H.R. 1's
    envelope is the proof of both at once. Its first listed version — the
    Enrolled Bill — carries `"date": null`; its *last* listed version is the
    Public Law, dated `2025-07-05T03:59:59Z`, which is the latest date in the
    envelope. So neither "take `textVersions[0]`" nor "the list is already in
    order" nor a naive `max(..., key=lambda v: v["date"])` (which raises on the
    None) gets the right answer. See `select_text_version`.
  * **`formats[]` varies per version.** H.R. 3424's versions offer
    `Formatted Text` / `PDF` / `Formatted XML`; H.R. 1's enrolled version adds
    `United States Legislative Markup`. Format choice is therefore a
    preference list over whatever a version happens to publish
    (`FORMAT_PREFERENCE`), never a fixed key that could be absent.
  * **The documents are genuinely large.** H.R. 1's enrolled HTML is 1,022,332
    bytes and its PDF 890,258 bytes (measured live), against H.R. 3424's
    introduced XML at 4,233. A megabyte in one `files` object is ordinary
    here, so nothing in this path assumes a small body: the document is
    fetched with a generous timeout, held once in memory as bytes, hashed and
    uploaded, and never decoded to `str` or parsed.

Which version gets archived, and why that is the honest answer
=================================================================

`select_text_version` picks **the version with the latest date the source
publishes**, and places an undated version last.

Both halves of that rule are decided by the live H.R. 1 envelope, not by
guesswork, and the first draft of this module got it wrong in a way the real
data caught:

  * **Not `textVersions[0]`.** H.R. 1's first listed entry is the Enrolled
    Bill; its *last* listed entry is the Public Law dated `2025-07-05`, the
    latest date in the envelope. The list is not in date order, so position
    carries no information.
  * **Undated last, not first.** An earlier version of this module reasoned
    that Congress.gov publishes a null date on the enrolled text, that enrolled
    text postdates every draft, and therefore that null should sort first. That
    reasoning produced the wrong answer on the one bill it was built from: it
    archived H.R. 1's *Enrolled Bill* when a strictly better version — the
    **Public Law**, the text as enacted, carrying a real date — was sitting at
    the end of the same list. Sorting undated versions last gets the Public
    Law, which is the single most citable version of a bill that became law.
    The test asserting this reads the live envelope
    (`test_select_text_version_...`), so it is a fact about the real source
    rather than a rule about dates in general.

Within the same date, source order is preserved (Python's sort is stable), so
two versions published the same day resolve deterministically across runs.

**What this deliberately does NOT do: pick the version a specific roll call
voted on.** A roll call votes on a bill *as it stood that day*, which for a
bill with four text versions is a different document from the newest one. Doing
that correctly needs the roll call's own date compared against each version's
date — and the null dates above mean that comparison has a hole in exactly the
case that matters most. More importantly, `bills.text_file_id` is a field on
the BILL (datamodel.md's `bills` row), one file id for one bill; there is
nowhere in the current data model to hang a per-roll-call text version. So this
module archives the bill's newest text and `BillTextReport`/the API response
both state plainly which version that is (`version_type`, `version_date`), so a
voter reading a vote citation can see whether the text they are looking at is
the version that was voted on. Per-roll-call text selection is a real gap, named
in this story's PR for a later story, not papered over here.

Not every bill has text, and that is ordinary
================================================

A bill introduced hours ago has no published document yet; `textVersions` comes
back empty. `archive` returns `BillTextReport(archived=False, reason=...)` and
raises nothing, because "no text published yet" is the ordinary state of a new
bill, not a failure of this loader — the same posture `house_bills.py` takes
for H.R. 1's missing `cosponsors` block. Twenty of the 119th Congress's most
recently updated House bills were checked live and all twenty had text, so the
empty case is not *common*; it is still real (every bill passes through it), and
a loader that raised on it would fail its own nightly run the first time it met
a brand-new bill.

Idempotency: content-addressed, like every other archive here
================================================================

The file key is `bill-text/{bill_id}/{sha256}.{ext}`, so re-archiving an
unchanged document lands on the same key and this module uploads nothing
(`uploaded=False`). A changed document hashes differently, gets its own file,
and `bills.text_file_id` moves to point at it — the older file stays stored,
because a citation written against the previous text must keep resolving
(datamodel.md: files are kept indefinitely with full provenance).

The retrievability check mirrors `campaign_sites._archive_is_retrievable`
exactly, and for the same reason: a `text_file_id` is not proof that bytes
exist behind it — that is precisely the state story 9.1's archives were left in
before `ZeroDBClient.upload_file` was fixed. So the existing archive is
downloaded and re-hashed, and an unreadable or wrong-hashing one is repaired by
re-uploading rather than trusted.

Provenance: entity_table is "files", not "bills"
===================================================

The `provenance_log` entry this module writes names the FILE as its entity
(`entity_table="files"`, `entity_id=<file_id>`), with the www.congress.gov
document URL as `source_url` and the document's sha256 as `content_hash`.

That is deliberate, and different from `house_bills.py`, whose provenance entry
names the `bills` row with the API detail URL. The two are tracing different
things: the bill ROW came from the API's JSON and is traced to it; the archived
DOCUMENT came from a different URL on a different host and is the artifact a
citation actually opens. Pointing the file's provenance at the bill row's own
API URL would record that the text came from somewhere it did not. Like
`campaign_sites.py`, this means calling `write_provenance` directly rather than
through `ProvenanceContext` — the repository's automatic path traces the row it
writes, and the row written here (`bills`) is not the thing being traced.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.ingest.house_rollcalls import CONGRESS_GOV_BASE_URL, CONGRESS_GOV_USER_AGENT
from app.repositories import ProvenanceContext, Repository
from app.repositories.models import Bill
from app.repositories.provenance import utc_now_iso, write_provenance
from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBError

logger = logging.getLogger(__name__)

#: This module's provenance adapter version, distinct from
#: `house_bills.ADAPTER_VERSION`: a bill row and an archived bill document are
#: different sourced records fetched from different hosts.
ADAPTER_VERSION = "bill_text/v1"

#: Which published format to archive, most preferred first, matched against
#: whatever `formats[]` a version actually offers (all four of these strings
#: were observed live; see the module docstring).
#:
#: XML first because it is the structured, machine-readable form of the exact
#: enacted language — the best input for story 9.3's extraction agent and for
#: any later section-level citation — and it is also the smallest of the three
#: (4,233 bytes vs 2,583 HTML for H.R. 3424's introduced version, where the
#: HTML is the same text with less structure). `Formatted Text` (an HTML
#: document, despite the name) is the fallback a browser renders directly.
#: PDF last: it is what Congress.gov itself prints, so it is never wrong, but
#: it is the hardest of the three to extract from and the largest.
FORMAT_PREFERENCE: tuple[str, ...] = (
    "Formatted XML",
    "United States Legislative Markup",
    "Formatted Text",
    "PDF",
)

#: Content type and file extension per observed format type. A format this
#: table does not know is still archivable — `_file_extension`/`_content_type`
#: fall back to octet-stream and `.bin` rather than refusing — because a new
#: format appearing in the source should degrade the archive's labelling, not
#: break the archival.
_FORMAT_MEDIA: dict[str, tuple[str, str]] = {
    "Formatted XML": ("application/xml", "xml"),
    "United States Legislative Markup": ("application/xml", "xml"),
    "Formatted Text": ("text/html; charset=utf-8", "html"),
    "PDF": ("application/pdf", "pdf"),
}

#: Generous by the standards of this codebase's other fetches (30s), because
#: the real documents run to a megabyte or more — see the module docstring's
#: measured sizes.
DEFAULT_DOCUMENT_TIMEOUT_SECONDS = 120.0

#: Mirrors every other loader's pacing constant against the ~60 req/min ZeroDB
#: ceiling datamodel.md records.
DEFAULT_MIN_INTERVAL_SECONDS = 1.1


class BillTextSourceError(Exception):
    """The bill-text source did not answer as the fetch functions expect."""


class BillTextNotFoundError(Exception):
    """No `bills` row with that id is stored, so there is nothing to archive to.

    Distinct from "the bill has no published text" (an ordinary
    `BillTextReport(archived=False)`): this is the caller naming a bill that
    was never ingested, which would leave the archived file dangling with no
    row pointing at it.
    """


@dataclass(frozen=True)
class FetchedDocument:
    """One bill-text document as actually retrieved: raw bytes, never decoded.

    Held as `bytes` throughout. The documents are up to megabytes of XML,
    HTML or PDF; decoding a PDF to `str` is meaningless and decoding the others
    would only risk a round-trip that changes the bytes a sha256 was promised
    over.
    """

    url: str
    content: bytes
    content_type: str | None = None


@dataclass(frozen=True)
class TextVersion:
    """One entry of the source's `textVersions[]`, normalised.

    `formats` maps the source's format-type string to its document URL.
    """

    version_type: str
    date: str | None
    formats: dict[str, str]

    def best_format(self) -> tuple[str, str] | None:
        """`(format_type, url)` for the most preferred format on offer, or None.

        None means this version published no format this module can name — in
        practice an empty `formats[]`, which `archive` reports as a skip rather
        than an error.
        """
        for preferred in FORMAT_PREFERENCE:
            url = self.formats.get(preferred)
            if url:
                return preferred, url
        # A format outside FORMAT_PREFERENCE entirely: archive it anyway rather
        # than discard a real document because this module's preference list is
        # out of date. Deterministic order so two runs pick the same one.
        for format_type in sorted(self.formats):
            if self.formats[format_type]:
                return format_type, self.formats[format_type]
        return None


@dataclass(frozen=True)
class BillTextReport:
    """What one `BillTextLoader.archive` call actually did.

    `archived=False` with a `reason` is the graceful-absence outcome (no
    published text, or no usable format) — never an error. `uploaded=False`
    with `archived=True` is the idempotent re-run: the document was already
    stored, verified readable, and nothing new was written.
    """

    bill_id: str
    archived: bool
    uploaded: bool = False
    text_file_id: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    source_url: str | None = None
    version_type: str | None = None
    version_date: str | None = None
    format_type: str | None = None
    captured_at: str | None = None
    reason: str | None = None


def fetch_text_versions(
    *,
    congress: int,
    bill_type: str,
    bill_number: int | str,
    api_key: str,
    base_url: str = CONGRESS_GOV_BASE_URL,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """The bill's text-versions envelope (`{"textVersions": [...]}`).

    An empty `textVersions` list is NOT an error here — a bill whose text is
    not published yet is ordinary (module docstring).

    Raises:
        BillTextSourceError: the request failed or did not come back 200.
    """
    url = f"{base_url}/bill/{congress}/{bill_type.lower()}/{bill_number}/text"
    try:
        response = httpx.get(
            url,
            params={"api_key": api_key, "format": "json"},
            headers={"User-Agent": CONGRESS_GOV_USER_AGENT},
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise BillTextSourceError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise BillTextSourceError(f"GET {url} returned {response.status_code}, expected 200")
    return response.json()


def fetch_document(
    url: str, *, timeout_seconds: float = DEFAULT_DOCUMENT_TIMEOUT_SECONDS
) -> FetchedDocument:
    """GET one published bill-text document from www.congress.gov.

    A different host from `fetch_text_versions`' api.congress.gov and needing
    no api_key: these are plain static documents. Follows redirects, and keeps
    the body as bytes — see `FetchedDocument`.

    Raises:
        BillTextSourceError: the request failed, did not come back 200, or came
            back empty. An empty document would be archived as a citation
            pointing at nothing, which `ZeroDBClient.upload_file` refuses
            anyway; failing here says why.
    """
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": CONGRESS_GOV_USER_AGENT},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise BillTextSourceError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise BillTextSourceError(f"GET {url} returned {response.status_code}, expected 200")
    if not response.content:
        raise BillTextSourceError(f"GET {url} returned an empty document")
    return FetchedDocument(
        url=url,
        content=response.content,
        content_type=response.headers.get("content-type"),
    )


def select_text_version(envelope: dict[str, Any]) -> TextVersion | None:
    """The latest-dated text version the source lists, or None if it lists none.

    Ordering, and why: dated versions descend by date; an **undated version
    sorts last**; equal dates keep source order (the sort is stable). This
    deliberately ignores the source's own list order, which is not date-ordered
    — H.R. 1's envelope lists its latest version (the Public Law, 2025-07-05)
    LAST and an undated one first. See the module docstring's "Which version
    gets archived" for the live evidence behind each half of this rule.

    Dates are compared as the ISO-8601 strings the source publishes
    (`2025-09-09T04:00:00Z`), which sort lexicographically in the same order
    they sort chronologically — every live value observed carries the same
    fixed-width UTC shape, so no parsing is needed and none is done.

    Returns None for an empty or missing `textVersions`, which `archive`
    reports as a skip rather than an error.
    """
    raw = envelope.get("textVersions")
    if not isinstance(raw, list) or not raw:
        return None

    versions: list[TextVersion] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        formats: dict[str, str] = {}
        for fmt in entry.get("formats") or []:
            if isinstance(fmt, dict) and fmt.get("type") and fmt.get("url"):
                formats[str(fmt["type"])] = str(fmt["url"])
        date = entry.get("date")
        versions.append(
            TextVersion(
                version_type=str(entry.get("type") or ""),
                date=str(date) if date else None,
                formats=formats,
            )
        )
    if not versions:
        return None
    # `(1, date)` for a dated version, `(0, "")` for an undated one, so undated
    # sorts last under reverse=True. Python's `reverse=True` is stable and does
    # NOT reverse the order of equal-keyed elements, so two versions sharing a
    # date resolve to the one the source listed first — deterministic across
    # runs, which is what keeps a re-archive idempotent.
    versions.sort(key=lambda v: (1, v.date) if v.date else (0, ""), reverse=True)
    return versions[0]


def _content_type_for(format_type: str, fetched: FetchedDocument) -> str:
    """The content type to store, preferring this module's own mapping.

    The server's own `Content-Type` is used only for a format outside
    `_FORMAT_MEDIA`. www.congress.gov serves the XML documents as
    `application/xml` and the `.htm` ones as `text/html` with no charset
    (measured live), so the mapping is both correct and more complete than
    what comes back on the wire.
    """
    known = _FORMAT_MEDIA.get(format_type)
    if known:
        return known[0]
    return fetched.content_type or "application/octet-stream"


def _file_extension_for(format_type: str, url: str) -> str:
    known = _FORMAT_MEDIA.get(format_type)
    if known:
        return known[1]
    tail = url.rsplit("/", 1)[-1]
    if "." in tail:
        suffix = tail.rsplit(".", 1)[-1]
        if suffix.isalnum() and len(suffix) <= 5:
            return suffix.lower()
    return "bin"


def sha256_hex(data: bytes) -> str:
    """The sha256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


class BillTextLoader:
    """Fetches, hashes, stores and records one bill's text document.

    `versions_fetcher` and `document_fetcher` are injected (defaulting to the
    real network functions) so every test here runs against a fixture with no
    network at all, the same seam every other ingest module in this package
    uses.
    """

    def __init__(
        self,
        client: ZeroDBClient,
        *,
        pace: Callable[[], None] | None = None,
        api_key: str | None = None,
        versions_fetcher: Callable[..., dict[str, Any]] | None = None,
        document_fetcher: Callable[..., FetchedDocument] | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        self._client = client
        self._pace = pace if pace is not None else (lambda: None)
        self._api_key = api_key
        self._fetch_versions = (
            versions_fetcher if versions_fetcher is not None else fetch_text_versions
        )
        self._fetch_document = document_fetcher if document_fetcher is not None else fetch_document
        self._now = now if now is not None else utc_now_iso
        # A ProvenanceContext is required, not optional: `bills` declares
        # `source_url`, so `Repository._refuse_if_untraceable` refuses any
        # write to it from a repository that has none (story 16.3's structural
        # rule). It also writes nothing new in practice — this loader only
        # sets `text_file_id`, leaving the row's `content_hash` alone, and the
        # provenance id is content-addressed, so `write_provenance_for_row`
        # finds story 5.3's existing entry and returns None. That is the right
        # outcome: the bill ROW has not been re-sourced, and the row's trace
        # must keep naming the API URL it genuinely came from.
        #
        # The archived DOCUMENT gets its own, separate entry, written
        # explicitly in `archive` with `entity_table="files"` — see the module
        # docstring's "Provenance" section for why the two are different
        # records.
        self._bills: Repository[Bill] = Repository(
            client=client,
            model=Bill,
            provenance=ProvenanceContext(adapter_version=ADAPTER_VERSION),
        )

    def archive(self, *, bill_id: str) -> BillTextReport:
        """Archive one bill's newest published text, idempotently.

        Raises:
            BillTextNotFoundError: no `bills` row with `bill_id` is stored.
            BillTextSourceError: the source could not be reached, or the
                document fetch failed.
        """
        bill = self._bills.get(bill_id)
        if bill is None:
            raise BillTextNotFoundError(
                f"no bills row {bill_id!r} is stored; ingest the bill "
                f"(app.ingest.house_bills) before archiving its text"
            )

        congress, bill_type, bill_number = parse_bill_id(bill_id)

        self._pace()
        envelope = self._fetch_versions(
            congress=congress,
            bill_type=bill_type,
            bill_number=bill_number,
            api_key=self._api_key or "",
        )
        version = select_text_version(envelope)
        if version is None:
            logger.info("bill_text: %s has no published text versions yet", bill_id)
            return BillTextReport(
                bill_id=bill_id,
                archived=False,
                reason="the source lists no text versions for this bill yet",
            )

        chosen = version.best_format()
        if chosen is None:
            logger.info(
                "bill_text: %s's newest version (%s) publishes no usable format",
                bill_id,
                version.version_type,
            )
            return BillTextReport(
                bill_id=bill_id,
                archived=False,
                version_type=version.version_type,
                version_date=version.date,
                reason=(
                    f"the newest text version ({version.version_type!r}) "
                    f"publishes no document format"
                ),
            )
        format_type, document_url = chosen

        self._pace()
        fetched = self._fetch_document(document_url)
        digest = sha256_hex(fetched.content)
        captured_at = self._now()
        extension = _file_extension_for(format_type, document_url)
        file_key = f"bill-text/{bill_id}/{digest}.{extension}"

        existing_id = self._retrievable_archive_id(bill, digest)
        if existing_id is not None:
            logger.info(
                "bill_text: %s already archived as %s (%s); no new upload",
                bill_id,
                existing_id,
                digest,
            )
            return BillTextReport(
                bill_id=bill_id,
                archived=True,
                uploaded=False,
                text_file_id=existing_id,
                sha256=digest,
                size_bytes=len(fetched.content),
                source_url=document_url,
                version_type=version.version_type,
                version_date=version.date,
                format_type=format_type,
                captured_at=captured_at,
                reason="the same document was already archived and is readable",
            )

        stored = self._client.upload_file(
            file_key=file_key,
            content=fetched.content,
            file_name=document_url.rsplit("/", 1)[-1] or f"{digest}.{extension}",
            content_type=_content_type_for(format_type, fetched),
            file_metadata={
                "sha256": digest,
                "source_url": document_url,
                "captured_at": captured_at,
                "bill_id": bill_id,
                "version_type": version.version_type,
                "version_date": version.date,
                "format_type": format_type,
            },
        )

        row = bill.row_data()
        row["id"] = bill_id
        row["text_file_id"] = stored.file_id
        self._bills.upsert(row)

        # The FILE is the entity traced here, not the bills row — see the
        # module docstring's "Provenance" section.
        write_provenance(
            self._client,
            entity_table="files",
            entity_id=stored.file_id,
            source_url=document_url,
            fetched_at=captured_at,
            content_hash=digest,
            adapter_version=ADAPTER_VERSION,
        )

        logger.info(
            "bill_text: archived %s for %s (%s, %s, %d bytes)",
            stored.file_id,
            bill_id,
            version.version_type,
            format_type,
            len(fetched.content),
        )
        return BillTextReport(
            bill_id=bill_id,
            archived=True,
            uploaded=True,
            text_file_id=stored.file_id,
            sha256=digest,
            size_bytes=len(fetched.content),
            source_url=document_url,
            version_type=version.version_type,
            version_date=version.date,
            format_type=format_type,
            captured_at=captured_at,
        )

    def _retrievable_archive_id(self, bill: Bill, digest: str) -> str | None:
        """The already-stored file id for this exact document, or None.

        Mirrors `campaign_sites.CampaignSiteLoader._archive_is_retrievable`,
        and for the same reason: a `text_file_id` is not proof that bytes exist
        behind it. The stored object is downloaded and re-hashed; a missing,
        unreadable or wrong-hashing archive returns None so `archive`
        re-uploads (a repair, not a second capture — the key is the same
        content-addressed key).
        """
        if not bill.text_file_id:
            return None
        try:
            stored = self._client.download_file(file_id=bill.text_file_id)
        except ZeroDBError as exc:
            logger.info(
                "bill_text: archive %s is not readable (%s); re-uploading",
                bill.text_file_id,
                exc,
            )
            return None
        actual = sha256_hex(stored)
        if actual != digest:
            # Not a corruption alarm: the ordinary case is that the bill's text
            # has genuinely changed since the last archive (a new version
            # published), which SHOULD produce a second file rather than
            # overwrite the one older citations point at.
            logger.info(
                "bill_text: archive %s hashes to %s, not the fetched %s; "
                "archiving the new document alongside it",
                bill.text_file_id,
                actual,
                digest,
            )
            return None
        return bill.text_file_id


def parse_bill_id(bill_id: str) -> tuple[int, str, str]:
    """`us-house:119:hr-3424` -> `(119, "hr", "3424")`.

    The inverse of `app.ingest.house_bills.bill_id`, needed because this module
    is handed a stored bill's id and has to fetch from the source that id was
    built from. Kept here rather than in `house_bills` because this is the only
    caller that needs to go backwards.

    Raises:
        BillTextNotFoundError: the id is not shaped like a bills id, so no
            source fetch can be built from it.
    """
    parts = bill_id.split(":")
    if len(parts) != 3 or "-" not in parts[2]:
        raise BillTextNotFoundError(
            f"{bill_id!r} is not a bills id of the form 'us-house:119:hr-3424'"
        )
    try:
        congress = int(parts[1])
    except ValueError as exc:
        raise BillTextNotFoundError(f"{bill_id!r} has a non-numeric congress") from exc
    bill_type, _, number = parts[2].partition("-")
    if not bill_type or not number:
        raise BillTextNotFoundError(f"{bill_id!r} has no bill type and number")
    return congress, bill_type, number


__all__ = [
    "ADAPTER_VERSION",
    "DEFAULT_DOCUMENT_TIMEOUT_SECONDS",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "FORMAT_PREFERENCE",
    "BillTextLoader",
    "BillTextNotFoundError",
    "BillTextReport",
    "BillTextSourceError",
    "FetchedDocument",
    "TextVersion",
    "fetch_document",
    "fetch_text_versions",
    "parse_bill_id",
    "select_text_version",
    "sha256_hex",
]
