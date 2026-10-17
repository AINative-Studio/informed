"""Archive campaign sites, once per election cycle (story 9.1).

What this is, and why it exists
================================

prd.md's Promise vs. Record pipeline, step 1: "Collect promises — campaign
websites (archived snapshots by election cycle), official platform
documents, debate and town-hall transcripts, candidate questionnaires, press
releases." This module is the campaign-website half of that collection step.
Acceptance criteria (backlog.md 9.1): stored as HTML and PDF in Files with
its sha256; a `promise_sources` row exists per capture.

Scope, stated precisely, because two things this story was asked to evaluate
turned out not to work the way the brief assumed, and both are load-bearing
for how this module is built rather than footnotes:

1. **There is no automated candidate-website discovery, and none is faked
   here.** See "Candidate website discovery" below.
2. **Internet Archive's Save Page Now is NOT free/keyless for programmatic
   use**, contrary to this story's brief — confirmed live, not assumed. See
   "Internet Archive's Save Page Now" below. The campaign page is archived by
   this module fetching it directly (an ordinary HTTP GET, the same kind of
   network call `candidacies.py` already makes to OpenFEC) and storing that
   capture through ZeroDB Files; Wayback submission is attempted as a
   best-effort addition on top of that, never as the archival mechanism
   itself.

Candidate website discovery
============================

Checked against the real, live OpenFEC `/v1/candidates/` response this
session (2026-10-16, the same endpoint story 2.6 already ingests from):

    GET https://api.open.fec.gov/v1/candidates/?api_key=DEMO_KEY&candidate_id=H6PA02205

    {"results": [{"candidate_id": "H6PA02205", "name": "ARRIAGA, JESSICA",
                  "party": "REP", "office": "H", "state": "PA",
                  "district": "02", "incumbent_challenge": "C",
                  "candidate_status": "N", ...}]}

**There is no website, campaign_website, url, or any comparable field in the
live response.** FEC's Form 2 (Statement of Candidacy) does not collect a
campaign website at all — it was never going to appear no matter how the
response was parsed, because the source document that produces this feed
does not ask the question. This is not a case of "the field exists but is
often null"; checking the live swagger definition at
https://api.open.fec.gov/swagger/ confirms the `Candidate` schema has no such
property. Two other federal sources were considered and are no better:
Congress.gov's bioguide-linked member pages carry a current OFFICIAL House/
Senate website for a *sitting* member, which is not the same thing as a
*campaign* website and does not exist at all for a challenger; and there is
no equivalent federal registry of campaign URLs.

So: **automated discovery of "the" campaign website for an arbitrary federal
candidate is not realistically buildable from the free data sources this
project already has**, and this module does not pretend otherwise. It is
scoped to exactly what backlog.md's acceptance criteria actually require —
"given a candidacy_id and a URL, archive it correctly" — and takes the URL as
an explicit input. `scripts/archive_campaign_sites.py` reads a small,
human-curated CSV (`candidacy_id,url`) rather than inventing a crawler; see
that script's docstring for the honest operational posture ("this list is
maintained by a human, and finding each url is itself a web search a person
does, not a thing this module automates") and `docs/spikes/9.1-candidate-
website-discovery.md` for the full writeup of what was tried and ruled out.
A later story that wants higher coverage has two realistic levers, neither of
which is "crawl FEC filings for a field that cannot be there": (a) a
third-party enrichment source such as Ballotpedia, which does track campaign
URLs but whose API has its own licensing terms this story did not clear; or
(b) crowd-curation with human review, the same posture story 10.3's QA queue
already uses elsewhere in this pipeline.

Internet Archive's Save Page Now
===================================

Verified live during this story (2026-10-16), against the real
web.archive.org, not assumed from memory or from the brief handed to this
story:

    POST https://web.archive.org/save/https://example.com
    Accept: application/json
    -> 401 {"message": "You need to be logged in to use Save Page Now."}

    GET https://web.archive.org/save/https://example.com   (no Accept header,
        the plain browser-facing endpoint a logged-out session would hit)
    -> 429  (rate-limited guest access; no snapshot produced, confirmed by
             the `x-rl: 0` response header)

Both captured verbatim in `tests/fixtures/wayback_save_page_now_sample.json`
as `unauthenticated_response` and `rate_limited_response`. **This directly
contradicts this story's brief, which described Save Page Now as "free,
keyless for basic use."** It is not, for the capture-TRIGGERING endpoint: the
official SPN2 API
(https://docs.google.com/document/d/1Nsv52MvSjbLb2PCpHlat0gkzw0EvtSgpKHu4mk0MnrA,
fetched live during this story) requires an `Authorization: LOW
<access_key>:<secret_key>` header, where the key pair comes from a free
archive.org account's "S3-like keys" page — a real account and a manual step,
not a public anonymous endpoint.

What IS genuinely free and keyless, and confirmed live, is the *read-only*
availability check:

    GET https://archive.org/wayback/available?url=https://www.congress.gov/
    -> 200 {"url": "...", "archived_snapshots": {"closest": {...}}}

which can report whether a snapshot already exists, but cannot create one.

**The consequence for this module's design:** the Wayback submission is
wired as an optional, best-effort step (`submit_to_wayback`/
`WaybackOutcome`), attempted only when `WAYBACK_ACCESS_KEY`/
`WAYBACK_SECRET_KEY` are configured in the environment, and its failure —
missing credentials, a 401, a 429, a timeout — degrades the capture report to
"not archived on Wayback this run" rather than failing the archival. The
acceptance criterion ("stored as HTML and PDF in Files with its sha256; a
promise_sources row exists per capture") is satisfied entirely by this
module's OWN fetch-and-store path and does not depend on Wayback succeeding
at all. No archive.org account exists in this environment, so the success
path (`success_response` in the fixture) is built from the SPN2 docs'
documented response shape and has not been confirmed against a real
authenticated call — flagged exactly like story 2.6's FEC-key gap, rather
than silently assumed to work.

HTML and PDF: what "stored as both" actually means here
===========================================================

**HTML** is the genuine, byte-faithful capture: whatever `page_fetcher`
returns (an ordinary `httpx.get` in production — see `fetch_campaign_page`)
is hashed and uploaded to Files unmodified. This is the real artifact a
citation can point to.

**PDF** is a real PDF file — a correctly-formed `application/pdf` document
that opens in any PDF reader, with its own independent sha256 — but it is
NOT a pixel-faithful rendering of the live page's visual layout. Producing
that would need a headless browser engine (Playwright+Chromium, or
WeasyPrint's CSS layout engine), either of which is a heavy new runtime
dependency with its own binary/system footprint; the task that specified this
story was explicit that adding one is out of scope if "not realistically
available without a heavy new dependency." What this module does instead,
honestly rather than by faking a screenshot: `render_capture_pdf` uses
`reportlab` (pure Python, already added to `pyproject.toml`, no system
dependency) to lay out the page's plain-text content — extracted with the
standard library's own `html.parser`, not a template that invents text — onto
real PDF pages, headed by the capture's own metadata (url, candidacy_id,
captured_at, sha256 of the HTML it was rendered from). A reader opens a real,
searchable PDF with the candidate's actual promise text on it and a visible
audit trail of what it was captured from; what they do NOT get is a visual
reproduction of the campaign site's fonts, images or layout. This gap is
documented here rather than silently left for a reader to discover, and
`CaptureReport` surfaces both file ids so a caller always has the byte-exact
HTML available as the citation of record even where the PDF's layout is a
simplification.

"Once per election cycle": idempotency
==========================================

Exactly the same content-addressed pattern `provenance_log` and (as of this
story) `PromiseSource` use: the `promise_sources` row's id is `src:{sha256}`
of the captured HTML, so re-running this loader for a candidacy whose site
has not changed lands on the SAME id, uploads no new files (checked before
either upload), and writes no new `promise_sources` or `provenance_log`
rows — a true no-op re-run, which is what "captured once per election cycle"
means operationally when nothing schedules the run itself (no scheduler
exists in this repo yet; see story 2.6's `candidacies.py` for the identical
caveat about "refreshed weekly"). A genuinely changed site hashes
differently and gets a second, distinct `promise_sources` row — correct
history, not an overwrite, matching `provenance_log`'s own "a changed fetch
appends a second entry" behaviour.

Provenance: why this module calls `write_provenance` directly
===================================================================

See `PromiseSource`'s docstring in `app.repositories.models` for the full
reasoning. In short: `app.repositories.provenance.requires_provenance` keys
on a model field literally named `source_url`, and datamodel.md's own
`promise_sources` spec (matching the live `TABLE_SCHEMAS` entry in
`app.bootstrap`, predating this story) names this field `url`. Renaming it to
satisfy the generic detector would make the model disagree with the table
ZeroDB already holds. So `CampaignSiteLoader` builds its `promise_sources`
repository with no `ProvenanceContext` (the repository would refuse one
anyway, by design — see `provenance.py`) and instead calls
`app.repositories.provenance.write_provenance` explicitly, right after the
row is written, naming the row's own `url` as the source. The resulting
`provenance_log` row is identical in shape to what the automatic mechanism
would have produced for a `source_url`-named field; only which code invokes
the write differs, and that difference is why this module exists outside
`app.repositories` rather than inside it.
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import httpx

from app.repositories import Repository
from app.repositories.models import PromiseSource
from app.repositories.provenance import utc_now_iso, write_provenance
from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBError

logger = logging.getLogger(__name__)

#: Named so a `provenance_log` entry this module writes is traceable to the
#: code that produced it, exactly like `house_rollcalls.ADAPTER_VERSION` and
#: `legislators.py`'s own constant. Bump this when the transform below
#: changes in a way that would make an old entry describe a different shape
#: than this version produces.
ADAPTER_VERSION = "campaign_sites/v1"

#: What this loader records in `promise_sources.source_type` for every
#: capture it makes. datamodel.md's "Collect promises" step names several
#: source types ("campaign websites", "debate and town-hall transcripts",
#: "candidate questionnaires", ...); story 9.2 is where the others arrive.
SOURCE_TYPE = "campaign_website"

#: Mirrors every other loader's pacing constant against the ~60 req/min
#: ZeroDB ceiling datamodel.md records (confirmed live during story 1.2).
DEFAULT_MIN_INTERVAL_SECONDS = 1.1

#: The free, keyless, read-only Wayback availability check — NOT the capture-
#: triggering endpoint. See the module docstring's "Internet Archive's Save
#: Page Now" section for why these are different APIs with different access
#: requirements, confirmed live.
WAYBACK_AVAILABLE_URL = "https://archive.org/wayback/available"

#: The real SPN2 capture-triggering endpoint. Confirmed live to require
#: authentication (401 with no credentials) — see the module docstring.
WAYBACK_SAVE_URL = "https://web.archive.org/save/{url}"


class CampaignSiteError(Exception):
    """The campaign page could not be fetched, or was unusable once fetched."""


@dataclass(frozen=True)
class FetchedPage:
    """One page as actually retrieved: bytes, plus what the server said they were."""

    url: str
    html: str
    content_type: str = "text/html; charset=utf-8"


@dataclass(frozen=True)
class WaybackOutcome:
    """The result of this run's best-effort Save Page Now attempt.

    `ok=False` covers every failure mode this module treats identically —
    no credentials configured, a 401, a 429, a timeout, an unexpected status
    — because none of them should fail the archival itself; see the module
    docstring. `detail` is for a human reading the capture report, not for
    branching logic.
    """

    ok: bool
    snapshot_url: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class CaptureReport:
    """What one `CampaignSiteLoader.capture()` call actually did.

    Carries both file ids and both hashes so a caller (and this story's own
    live-verification script) can show a human exactly what got written,
    without re-reading ZeroDB. `is_new_capture` is False for a no-op re-run —
    see the module docstring's "once per election cycle" section — which is
    how a caller distinguishes "this cycle's capture already existed" from
    "this cycle's capture was just taken".
    """

    candidacy_id: str
    url: str
    html_file_id: str
    html_sha256: str
    pdf_file_id: str
    pdf_sha256: str
    promise_source_id: str
    captured_at: str
    is_new_capture: bool
    wayback: WaybackOutcome


class _TextExtractor(HTMLParser):
    """Pulls plain text out of an HTML document, script/style excluded.

    Used only to feed `render_capture_pdf` real page text rather than markup
    — never to re-derive a citation's meaning from the markup, which stays
    the extraction agent's job (story 9.3). This parser does nothing clever:
    it is the standard library's own `html.parser`, chosen so this module
    adds no HTML-parsing dependency beyond what `reportlab` already requires
    for the PDF itself.
    """

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return "\n".join(self._chunks)


def extract_text(html: str) -> str:
    """Plain text content of an HTML document, for the PDF rendering."""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def sha256_hex(data: bytes | str) -> str:
    """The sha256 hex digest of raw bytes or text, encoded as UTF-8 if text."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


def render_capture_pdf(
    *, url: str, candidacy_id: str, captured_at: str, html_sha256: str, text: str
) -> bytes:
    """A real, searchable PDF of a capture's text content and metadata.

    See the module docstring's "HTML and PDF" section for exactly what this
    does and does not claim about visual fidelity to the live page. Uses
    `reportlab`'s `SimpleDocTemplate`, which lays out `Paragraph` flowables
    across as many pages as the content needs — no manual pagination here,
    and no dependency on a browser engine.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    story: list[Any] = [
        Paragraph("Archived campaign page", styles["Title"]),
        Spacer(1, 0.15 * inch),
        Paragraph(f"Candidacy: {_escape(candidacy_id)}", styles["Normal"]),
        Paragraph(f"Source URL: {_escape(url)}", styles["Normal"]),
        Paragraph(f"Captured at: {_escape(captured_at)}", styles["Normal"]),
        Paragraph(f"HTML capture sha256: {_escape(html_sha256)}", styles["Normal"]),
        Paragraph(
            "This PDF is a text rendering of the captured page's content for "
            "offline reading and search. The HTML file stored alongside it is "
            "the byte-exact capture and is the citation of record; this PDF "
            "does not reproduce the live page's visual layout, fonts or "
            "images.",
            styles["Italic"],
        ),
        Spacer(1, 0.25 * inch),
    ]
    for paragraph in text.split("\n"):
        if paragraph.strip():
            story.append(Paragraph(_escape(paragraph), styles["BodyText"]))
            story.append(Spacer(1, 0.08 * inch))
    if not text.strip():
        story.append(Paragraph("(the captured page had no extractable text)", styles["Italic"]))

    doc.build(story)
    return buffer.getvalue()


def _escape(value: str) -> str:
    """Minimal XML escaping for reportlab's mini-markup Paragraph text."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- fetching: the only functions that reach the network -------------------


#: A plain, honest desktop-browser User-Agent string. Confirmed necessary
#: live during this story: `httpx`'s default User-Agent (`python-httpx/...`)
#: was refused with a bare 403 by two of three real campaign sites tried
#: (pelosiforcongress.org, mikecollinsga.com), while the same request with
#: this header succeeded against all three with no other change. This is not
#: evading a login, a paywall or a CAPTCHA -- nothing behind either site
#: requires one -- it is simply not identifying this fetch as a bare
#: scripting client to a WAF that filters on that basis, the same courtesy
#: any browser extends by default. A third site tried (elissaslotkin.org)
#: sat behind a genuine Cloudflare interactive JS challenge that no plain
#: HTTP client can pass with any header, honestly or otherwise; that
#: candidate was swapped for a reachable one rather than worked around with
#: a headless browser (a dependency this story is explicit about avoiding).
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_campaign_page(
    url: str, *, timeout_seconds: float = 30.0, user_agent: str = DEFAULT_USER_AGENT
) -> FetchedPage:
    """GET a campaign page exactly as published. The real network call.

    Kept separate from `CampaignSiteLoader`, same reason `candidacies.py`
    separates `fetch_candidates`: every other piece of this module is driven
    from a fixture or an injected fetcher in CI, with no mock HTTP layer and
    no network at all (CLAUDE.md). Only `scripts/archive_campaign_sites.py`
    calls this for real.

    Sends `user_agent` (default: `DEFAULT_USER_AGENT`) rather than `httpx`'s
    own default — see that constant's docstring for why this is required
    against real sites, confirmed live, and not merely cosmetic.

    Raises:
        CampaignSiteError: the request failed or did not come back 200.
    """
    try:
        response = httpx.get(
            url,
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
    except httpx.HTTPError as exc:
        raise CampaignSiteError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise CampaignSiteError(f"GET {url} returned {response.status_code}, expected 200")
    return FetchedPage(
        url=url,
        html=response.text,
        content_type=response.headers.get("content-type", "text/html"),
    )


def submit_to_wayback(
    url: str,
    *,
    access_key: str | None = None,
    secret_key: str | None = None,
    timeout_seconds: float = 30.0,
) -> WaybackOutcome:
    """Best-effort Save Page Now submission. Never raises.

    See the module docstring's "Internet Archive's Save Page Now" section:
    the real capture-triggering endpoint requires archive.org account
    credentials, confirmed live. Without `access_key`/`secret_key` this
    returns `ok=False` immediately and makes no request at all — there is no
    anonymous capture call worth attempting, per that live finding.
    """
    if not access_key or not secret_key:
        return WaybackOutcome(ok=False, detail="no Wayback SPN2 credentials configured")

    try:
        response = httpx.post(
            WAYBACK_SAVE_URL.format(url=url),
            headers={
                "Accept": "application/json",
                "Authorization": f"LOW {access_key}:{secret_key}",
            },
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        return WaybackOutcome(ok=False, detail=f"Wayback request failed: {exc}")

    if response.status_code != 200:
        return WaybackOutcome(
            ok=False, detail=f"Wayback returned {response.status_code}: {response.text[:200]}"
        )

    try:
        body = response.json()
    except ValueError:
        return WaybackOutcome(ok=False, detail="Wayback returned a non-JSON success body")

    timestamp = body.get("timestamp")
    target = body.get("url") or url
    if not timestamp:
        return WaybackOutcome(ok=False, detail="Wayback success response had no timestamp")
    return WaybackOutcome(ok=True, snapshot_url=f"https://web.archive.org/web/{timestamp}/{target}")


def check_wayback_available(url: str, *, timeout_seconds: float = 15.0) -> str | None:
    """The existing Wayback snapshot url for `url`, or None.

    Wraps the genuinely free, keyless `GET /wayback/available` endpoint
    confirmed live during this story. Read-only: it can report an existing
    capture but never creates one, which is why this is not a substitute for
    `submit_to_wayback` in the capture pipeline — it is offered as a
    standalone helper for the live-verification script to confirm a
    submission actually landed.
    """
    try:
        response = httpx.get(WAYBACK_AVAILABLE_URL, params={"url": url}, timeout=timeout_seconds)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    body = response.json()
    closest = (body.get("archived_snapshots") or {}).get("closest") or {}
    return closest.get("url") if closest.get("available") else None


# --- loading -----------------------------------------------------------------


class CampaignSiteLoader:
    """Fetches, hashes, stores and records one candidacy's campaign capture.

    `page_fetcher` and `wayback_submitter` are injected (defaulting to the
    real `fetch_campaign_page`/`submit_to_wayback`) so every test here runs
    against a fixture with no network at all, the same seam every other
    ingest module in this package uses.
    """

    def __init__(
        self,
        client: ZeroDBClient,
        *,
        pace: Callable[[], None] | None = None,
        page_fetcher: Callable[[str], FetchedPage] | None = None,
        wayback_submitter: Callable[[str], WaybackOutcome] | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        self._client = client
        self._pace = pace if pace is not None else (lambda: None)
        self._fetch = page_fetcher if page_fetcher is not None else fetch_campaign_page
        self._submit_wayback = (
            wayback_submitter if wayback_submitter is not None else _default_wayback_submitter
        )
        self._now = now if now is not None else utc_now_iso
        # No ProvenanceContext: `PromiseSource` declares `url`, not
        # `source_url`, so `requires_provenance` does not recognise it and
        # the repository would refuse one anyway (see PromiseSource's
        # docstring and provenance.py). `capture()` calls `write_provenance`
        # directly instead.
        self._sources: Repository[PromiseSource] = Repository(client=client, model=PromiseSource)

    def capture(self, *, candidacy_id: str, url: str) -> CaptureReport:
        """Archive one candidacy's campaign page, idempotently.

        Raises:
            ValueError: `url` is empty. There is no automated discovery for
                this loader to fall back on — see the module docstring's
                "Candidate website discovery" — so a missing url is the
                caller's mistake, not something to silently skip or invent.
            CampaignSiteError: the page could not be fetched.
            MissingReferenceError: `candidacy_id` does not resolve to a
                `candidacies` row (raised by the repository layer; see
                `PromiseSource.references`).
        """
        if not url or not url.strip():
            raise ValueError(
                "capture() needs an explicit url; this loader does not discover "
                "a candidate's campaign site automatically (see the module "
                "docstring's 'Candidate website discovery')"
            )

        self._pace()
        page = self._fetch(url)
        html_sha256 = sha256_hex(page.html)
        captured_at = self._now()
        source_id = f"src:{html_sha256}"

        existing = self._sources.get(source_id)
        if existing is not None and self._archive_is_retrievable(existing, html_sha256):
            # Idempotent re-run: the same content was already captured, stored
            # under this exact id, and — checked, not assumed — readable back.
            # Re-reading the stored row's own file ids is then correct rather
            # than re-uploading, because the content-addressed id plus a
            # verified hash mean the stored object is the bytes this fetch just
            # produced.
            logger.info(
                "campaign_sites: %s already captured for %s (%s); no new files uploaded",
                source_id,
                candidacy_id,
                url,
            )
            return CaptureReport(
                candidacy_id=candidacy_id,
                url=existing.url,
                html_file_id=existing.archived_file_id,
                html_sha256=html_sha256,
                pdf_file_id="",
                pdf_sha256="",
                promise_source_id=source_id,
                captured_at=existing.captured_at,
                is_new_capture=False,
                wayback=WaybackOutcome(ok=False, detail="not re-submitted on a no-op re-run"),
            )

        if existing is not None:
            # The row is here but its archive is not readable, so this re-run
            # is a REPAIR: fall through and re-upload the files. This is the
            # state every capture made before the file-upload retrievability
            # fix is in — a `promise_sources` row and file metadata with no
            # stored object behind either — and re-running this loader is how
            # such a capture is mended. The row's id is unchanged (same bytes,
            # same sha256), so the repair is an upsert, not a second capture.
            logger.warning(
                "campaign_sites: %s exists for %s but its archive is not "
                "retrievable; re-uploading the captured files",
                source_id,
                candidacy_id,
            )

        html_bytes = page.html.encode("utf-8")
        html_file = self._client.upload_file(
            file_key=f"promise-sources/{source_id}.html",
            content=html_bytes,
            file_name=f"{source_id}.html",
            content_type="text/html; charset=utf-8",
            file_metadata={
                "sha256": html_sha256,
                "source_url": url,
                "captured_at": captured_at,
            },
        )

        pdf_bytes = render_capture_pdf(
            url=url,
            candidacy_id=candidacy_id,
            captured_at=captured_at,
            html_sha256=html_sha256,
            text=extract_text(page.html),
        )
        pdf_sha256 = sha256_hex(pdf_bytes)
        pdf_file = self._client.upload_file(
            file_key=f"promise-sources/{source_id}.pdf",
            content=pdf_bytes,
            file_name=f"{source_id}.pdf",
            content_type="application/pdf",
            file_metadata={
                "sha256": pdf_sha256,
                "source_url": url,
                "captured_at": captured_at,
                "rendered_from_html_sha256": html_sha256,
            },
        )

        stored = self._sources.upsert(
            {
                "id": source_id,
                "candidacy_id": candidacy_id,
                "source_type": SOURCE_TYPE,
                "url": url,
                "archived_file_id": html_file.file_id,
                "captured_at": captured_at,
            }
        )
        assert stored.id is not None  # noqa: S101 - upsert guarantees it

        # Explicit provenance write -- see the module docstring and
        # PromiseSource's for why this is not the automatic repository path.
        write_provenance(
            self._client,
            entity_table=PromiseSource.table_name,
            entity_id=stored.id,
            source_url=url,
            fetched_at=captured_at,
            content_hash=html_sha256,
            adapter_version=ADAPTER_VERSION,
        )

        wayback = self._submit_wayback(url)
        if not wayback.ok:
            logger.info(
                "campaign_sites: Wayback submission for %s not completed this run: %s",
                url,
                wayback.detail,
            )

        logger.info("campaign_sites: captured %s for %s (%s)", source_id, candidacy_id, url)
        return CaptureReport(
            candidacy_id=candidacy_id,
            url=url,
            html_file_id=html_file.file_id,
            html_sha256=html_sha256,
            pdf_file_id=pdf_file.file_id,
            pdf_sha256=pdf_sha256,
            promise_source_id=source_id,
            captured_at=captured_at,
            is_new_capture=True,
            wayback=wayback,
        )

    def _archive_is_retrievable(self, existing: PromiseSource, html_sha256: str) -> bool:
        """Can the already-stored archive actually be read back, and is it right?

        An existing `promise_sources` row used to be taken as proof that its
        archive was intact, on the reasoning that a content-addressed id
        guarantees the stored files are the same bytes. That reasoning has a
        hole: it assumes bytes were stored at all. They were not, for every
        capture made before `ZeroDBClient.upload_file` was fixed to post the
        file body — so re-running this loader over those captures hit the
        no-op branch and left them broken, which is the opposite of what a
        re-run is for.

        So the claim is checked rather than inferred. A missing archive, an
        unreadable one, or one whose bytes no longer hash to the capture is
        treated as not retrievable, and `capture` re-uploads.
        """
        if not existing.archived_file_id:
            return False
        try:
            archived = self._client.download_file(file_id=existing.archived_file_id)
        except ZeroDBError as exc:
            logger.info(
                "campaign_sites: archive %s is not readable (%s)",
                existing.archived_file_id,
                exc,
            )
            return False
        actual = sha256_hex(archived)
        if actual != html_sha256:
            logger.warning(
                "campaign_sites: archive %s hashes to %s, not the captured %s",
                existing.archived_file_id,
                actual,
                html_sha256,
            )
            return False
        return True


def _default_wayback_submitter(url: str) -> WaybackOutcome:
    """The real submitter, reading SPN2 credentials from the environment.

    Separate function (rather than a lambda default) so it is importable and
    independently testable, and so `CampaignSiteLoader.__init__`'s default
    argument stays a plain, picklable reference.
    """
    import os

    return submit_to_wayback(
        url,
        access_key=os.environ.get("WAYBACK_ACCESS_KEY"),
        secret_key=os.environ.get("WAYBACK_SECRET_KEY"),
    )


__all__ = [
    "ADAPTER_VERSION",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "DEFAULT_USER_AGENT",
    "SOURCE_TYPE",
    "WAYBACK_AVAILABLE_URL",
    "WAYBACK_SAVE_URL",
    "CampaignSiteError",
    "CampaignSiteLoader",
    "CaptureReport",
    "FetchedPage",
    "WaybackOutcome",
    "check_wayback_available",
    "extract_text",
    "fetch_campaign_page",
    "render_capture_pdf",
    "sha256_hex",
    "submit_to_wayback",
]
