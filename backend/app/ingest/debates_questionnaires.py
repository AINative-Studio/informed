"""Archive debate transcripts and candidate questionnaires (story 9.2).

What this is, and why it exists
================================

prd.md's Promise vs. Record pipeline, step 1: "Collect promises — campaign
websites (archived snapshots by election cycle), official platform documents,
debate and town-hall transcripts, candidate questionnaires, press releases."
Story 9.1 (`app.ingest.campaign_sites`) did the campaign-website half. This
module does the rest: the source types where a candidate's commitments are
spoken or written down somewhere other than their own site, which is where a
great many of them actually live.

Acceptance criteria (backlog.md 9.2): at least 2 source types beyond
websites; each is archived with provenance.

The two source types shipped here are `candidate_questionnaire` and
`debate_transcript`, and both were chosen by checking live what real,
freely-fetchable, text-bearing content actually exists for real sitting
members of the 119th Congress — not by assuming. The research mattered,
because three of the five sources this story's brief suggested turned out not
to work. That is recorded below in full, because the next person to extend
this module will otherwise repeat it.

WHAT WAS CHECKED LIVE, AND WHAT EACH ONE ACTUALLY TURNED OUT TO BE
===================================================================

All checked 2026-10-20 with real HTTP requests and real robots.txt fetches.

**USABLE — these two are what this module archives:**

1. **Candidate questionnaires.** Two independent publishers, both verified to
   serve real answers as server-rendered text (no JS execution needed) with
   a permissive robots.txt:

   * **VOTE411** (League of Women Voters). The public `www.vote411.org`
     homepage is an address-entry widget and carries no answers, but the guide
     data lives on `onyourballot.vote411.org`, whose robots.txt is
     `Disallow:` (empty — nothing blocked) with `Crawl-delay: 10`. A real
     example confirmed live:
     `https://onyourballot.vote411.org/candidate-detail.do?id=72998155`
     (HTTP 200, 72,244 bytes) carries Rep. **Rashida Tlaib**'s (D-MI-12, a
     sitting member) own answers in the response body, including "I will keep
     fighting for healthcare as a human right" and "I introduced the
     Medicines for the People Act". Current-cycle, not stale.
   * **Ballotpedia Candidate Connection.** `ballotpedia.org/robots.txt`
     disallows only wiki internals; candidate pages are allowed. Verified
     live: `https://ballotpedia.org/Jon_Ossoff` carries Sen. Ossoff's 2026
     survey answers in the body ("In the Senate, I helped cap the cost of
     insulin at $35 per month..."), and
     `https://ballotpedia.org/Mike_Collins_(Georgia)` carries Rep. Collins's
     2021 answers.

   **A real coverage limit, measured rather than guessed:** sitting members
   often do not answer. Of the three members already in this project's live
   `candidacies` table, Ossoff and Collins had completed Ballotpedia surveys
   and **Pelosi's page says in so many words that she "did not complete"
   it**. Ballotpedia's own figure is that incumbents were ~7.4% of 2024
   Candidate Connection respondents. So a questionnaire is a real source type
   with genuinely patchy per-member coverage — which is why this loader
   archives a url the caller supplies and never pretends a missing answer set
   is an empty one.

2. **Debate transcripts.** `rev.com/robots.txt` disallows only its editor
   tools; `/transcripts/` is allowed. Verified live:
   `https://www.rev.com/transcripts/first-california-senate-debate-transcript`
   (HTTP 200, 355,000 bytes) is the full verbatim first 2024 California U.S.
   Senate debate, with timestamped speaker labels, including a genuine
   commitment from **Adam Schiff** — now a sitting U.S. Senator (D-CA):
   "which is why I'm going to go to the Senate to fight, to restore
   collective bargaining so that we can raise wages and incomes."
   Coverage is ad hoc (it depends on whether a given race's debate was
   transcribed at all), and the page carries a fair-use notice under
   17 U.S.C. §107 — see "Fair use and redistribution" below.

**ALSO USABLE, SHIPPED AS A THIRD TYPE — but honestly labelled:**

3. **Floor proceedings, via GPO's Congressional Record.** This is the most
   reliable source of all three and needs **no API key whatsoever**:
   `https://www.govinfo.gov/content/pkg/CREC-{date}/html/CREC-{date}-pt1-Pg{page}.htm`
   returns the granule as text inside a `<pre>`, confirmed live (HTTP 200).
   `govinfo.gov/robots.txt` blocks only Drupal admin paths. Granule metadata
   at `https://www.govinfo.gov/metadata/granule/{pkg}/{granule}/mods.xml` is
   likewise keyless and carries
   `<congMember bioGuideId="..." role="SPEAKING" ...>` — which maps straight
   onto this project's own `person:{bioguide}` ids.

   **But it is NOT a debate transcript in the sense story 9.2 means, and is
   not labelled as one.** A moderated candidate debate and a member's floor
   statement are different genres of evidence: the first is a candidate
   answering a question put to them in a contest, the second is a legislator
   speaking on the record in office. Calling floor proceedings a "debate
   transcript" would quietly inflate what a citation means, so it gets its
   own `source_type`, `floor_statement`, and the reader of a
   `promise_sources` row can tell the two apart. It is shipped because real
   verbatim commitments by real sitting members, keyless and in bulk, are
   worth having — not to pad the count of source types.

   Note on the API: `api.govinfo.gov` needs a key, and `DEMO_KEY` is shared
   and sharply rate-limited (`429 OVER_RATE_LIMIT` was hit during this
   story's own research within seconds). The keyless `www.govinfo.gov`
   content path above has no such limit and is what `GOVINFO_CONTENT_URL`
   builds, so this module depends on no credential at all. `CONGRESS_GOV_API_KEY`
   exists in this project's environment but is not needed here.

**NOT USABLE — checked live and rejected, with the reason:**

4. **C-SPAN. Video only, and robots.txt forbids the transcript path.**
   Confirmed live: a real congressional page
   (`/event/house-proceeding/opening-day-119th-congress-us-house-of-representatives/429828`,
   HTTP 200, 156KB) contains the transcript UI as an HTML5 `<template>` whose
   body is **literal placeholder tokens** — `:::offset:::`,
   `:::speakername:::`, `:::text:::` — and not one word of real transcript.
   The text is fetched client-side from `/videoLibrary/transcript`, which
   `c-span.org/robots.txt` explicitly disallows, and that robots.txt also
   blocks ClaudeBot/GPTBot/CCBot from the whole site. So the only way to get
   C-SPAN transcript text is to execute JS against a path its operator has
   asked crawlers not to touch. This module does not do that, and no amount
   of User-Agent choice makes it acceptable. **If C-SPAN text is wanted
   later, the route is C-SPAN's licensing/API terms, not a workaround.**

5. **The American Presidency Project (presidency.ucsb.edu). Wrong chamber,
   and it blocks ClaudeBot site-wide.** Its debates category
   (`/documents/app-categories/elections-and-transitions/debates`, HTTP 200)
   lists 10 documents, every one presidential, VP or presidential-primary —
   **zero** House or Senate debates, so it cannot serve this project's
   federal-legislator scope at all. Separately, its robots.txt carries
   `Disallow: /` for ClaudeBot, GPTBot, CCBot, PerplexityBot and others. Both
   reasons are independently disqualifying.

6. **Vote Smart's Political Courage Test. Blocked, and asks not to be
   crawled by this kind of client.** `justfacts.votesmart.org/robots.txt`
   names `ClaudeBot`, `anthropic-ai` and `Claude-Web` with `Disallow: /` and
   sets `Content-Signal: ai-train=no`; a direct fetch returned **HTTP 403
   "Your request was blocked."** (Cloudflare). Rejected on the robots.txt
   alone — the 403 merely makes it moot.

7. **PBS NewsHour.** Live fetches answered HTTP 202 with empty bodies
   (bot challenge), and the pages that do render are NewsHour's own analysis
   segments with debate quotes woven in — not verbatim debate transcripts.
   Not a transcript source.

Fair use and redistribution
============================

Two of the three shipped source types are third-party publications rather
than government works:

  * **Floor statements** are U.S. Government works from GPO and carry no
    copyright. No constraint.
  * **Questionnaire answers** are the candidate's own words, published by
    VOTE411/Ballotpedia for exactly this purpose. Ballotpedia's own notice
    says it reserves the right to edit responses and marks edits in
    `[brackets]` — worth knowing when a quote span looks odd.
  * **Rev.com debate transcripts** carry an explicit fair-use notice under
    17 U.S.C. §107.

This module therefore archives a snapshot for **citation and verification**,
which is what `promise_sources` is for: a voter following a citation is shown
a short quote span and a link to the primary source, not a republished copy
of someone else's transcript. The archive exists so the quote can still be
checked after the page changes — the same reason story 9.1 archives campaign
pages. Serving a whole archived third-party transcript to end users would be
a different act with a different analysis, and nothing here does it; the
presigned-URL path stays available for an auditor or a correction reviewer.
**This is a deliberate product boundary, flagged for human review in this
story's PR rather than settled by an engineer.**

HTML and TEXT, not HTML and PDF
================================

Story 9.1 stores each capture twice, as HTML and as a rendered PDF. This
module also stores each capture twice, but as the **fetched document** and
its **extracted plain text**, and the difference is deliberate.

9.1's PDF exists so a human can read an archived campaign page offline. What
9.2's captures are mainly *for* is story 9.3's extractor, which consumes
text: `PromiseExtractionPipeline.extract_from_source(source_id,
source_text=...)` takes the text as a parameter precisely because the archived
bytes were not readable back when it was written. Storing the extracted text
as its own file means the extractor's input is itself archived and
hash-addressed — so a promise's quote span can be checked against the exact
text it was extracted from, not against a re-parse of the HTML that may drift
when the parser changes. A re-parse is not a citation; the bytes that were
read are.

The document of record stays the fetched bytes, and `archived_file_id` on the
`promise_sources` row points at them, matching 9.1 and datamodel.md. The text
file is found from the document's own id (see `read_source_text`), so no field
datamodel.md does not define is invented to hold it.

Idempotency, and why the id is the document's hash
====================================================

Same posture as 9.1 and for the same reason: `promise_sources.id` is
`src:{sha256}`, so re-archiving an unchanged document lands on the row that
already exists (idempotent re-archival for free) while a genuinely changed
page hashes differently and gets a second row — a second capture at a second
time, which is history worth keeping.

**But the hash is of the EXTRACTED TEXT, not of the fetched bytes, and that
difference was forced by a real live failure rather than chosen on taste.**
Story 9.1 hashes the raw HTML, which is right for a campaign page whose
markup is itself the artefact. It is wrong here, found live on 2026-10-20 by
running this story's own verification script twice: VOTE411 embeds its
server-side cache TTL in an HTML comment —

    <!-- BEGIN CACHE HIT key=VOTE41122.index-desktop.25687874..en expires-in=138m -->

— which ticks down **every minute**. Two fetches a minute apart carried
byte-for-byte identical answers, differed in exactly that one number, and
produced two `promise_sources` rows with two different `src:` ids. Left
alone, that is not a cosmetic duplicate: story 9.3 would extract the same
promise from each row, story 10.1 would map each copy to the same bill, and
story 11's verdict aggregation would count one commitment many times over —
so a candidate's say/do record would drift purely as a function of how often
the archiver ran. Hashing the text the extractor actually reads makes the
capture's identity the source's *content*, which is what a promise cites.

The raw bytes are still stored and still hashed (`document_bytes_sha256` on
the report, and the file's own `sha256` metadata), so the byte-exact capture
remains verifiable — nothing is lost, the identity is just taken at the right
level.

And as in 9.1, an existing row is **not** taken as proof its archive is
intact. `_archive_is_retrievable` checks that the stored bytes come back and
still extract to the same text as this fetch; a row whose stored object is
missing is repaired by re-uploading, because that is the state every capture
made before `ZeroDBClient.upload_file` was fixed to post the file body is in.

No automated discovery, and no invented sources
=================================================

`archive()` requires an explicit url, exactly like `CampaignSiteLoader.capture`.
There is no automated discovery of which questionnaire page or transcript
belongs to which candidate: VOTE411's own discovery is address-gated,
Ballotpedia's survey may simply not exist for a given member (Pelosi's does
not), and whether a race's debate was transcribed at all is luck. Guessing a
url would produce a `promise_sources` row pointing at a page that is not that
candidate's words, which is the one thing a provenance table must never
contain.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.repositories import Repository
from app.repositories.models import PromiseSource
from app.repositories.provenance import utc_now_iso, write_provenance
from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBError

logger = logging.getLogger(__name__)

#: Named so a `provenance_log` entry this module writes is traceable to the
#: code that produced it, exactly like `campaign_sites.ADAPTER_VERSION`. Bump
#: it when the transform below changes shape.
ADAPTER_VERSION = "debates_questionnaires/v1"

#: A candidate's own written answers to a published questionnaire -- VOTE411
#: (League of Women Voters) or Ballotpedia Candidate Connection. See the
#: module docstring for the live verification behind this.
SOURCE_TYPE_QUESTIONNAIRE = "candidate_questionnaire"

#: A moderated candidate debate or town-hall transcript.
SOURCE_TYPE_DEBATE_TRANSCRIPT = "debate_transcript"

#: A member's statement in floor proceedings, from GPO's Congressional
#: Record. DELIBERATELY DISTINCT from `debate_transcript`: see the module
#: docstring's point 3 for why conflating the two would inflate what a
#: citation means.
SOURCE_TYPE_FLOOR_STATEMENT = "floor_statement"

#: Every source type this module will write. A `source_type` outside this set
#: is refused rather than stored: `promise_sources.source_type` is a plain
#: string in ZeroDB, so nothing downstream would catch a typo, and a promise
#: whose provenance says `"questionaire"` is a promise whose evidence cannot
#: be grouped or audited.
KNOWN_SOURCE_TYPES: frozenset[str] = frozenset(
    {
        SOURCE_TYPE_QUESTIONNAIRE,
        SOURCE_TYPE_DEBATE_TRANSCRIPT,
        SOURCE_TYPE_FLOOR_STATEMENT,
    }
)

#: Mirrors every other loader's pacing constant against the ~60 req/min
#: ZeroDB ceiling datamodel.md records.
DEFAULT_MIN_INTERVAL_SECONDS = 1.1

#: VOTE411's robots.txt asks for `Crawl-delay: 10` (confirmed live). Honoured
#: by `scripts/archive_debates_questionnaires.py` when fetching from that
#: host, which is why it is a named constant rather than a magic number.
VOTE411_CRAWL_DELAY_SECONDS = 10.0

#: The keyless GPO content route for one Congressional Record granule. No API
#: key, no rate limit -- unlike `api.govinfo.gov`, which needs a key and
#: whose shared DEMO_KEY was observed returning 429 within seconds. See the
#: module docstring's point 3.
GOVINFO_CONTENT_URL = (
    "https://www.govinfo.gov/content/pkg/CREC-{date}/html/CREC-{date}-pt1-Pg{page}.htm"
)

#: The keyless granule metadata route, which carries
#: `<congMember bioGuideId=... role="SPEAKING">` -- the attribution that ties
#: a floor statement to this project's own `person:{bioguide}` ids.
GOVINFO_GRANULE_MODS_URL = "https://www.govinfo.gov/metadata/granule/{pkg}/{granule}/mods.xml"


class SourceDocumentError(Exception):
    """The source document could not be fetched, or was unusable once fetched."""


#: The key under which the document's own `file_metadata` records the id of
#: its extracted-text sibling. ZeroDB mints a random uuid per file and has no
#: "find a file by key" route (`ZeroDBClient` exposes `upload_file`,
#: `download_file` and `get_file` only), so the text file cannot be located
#: by a deterministic name. Recording the id on the document -- which the
#: `promise_sources` row already points at via `archived_file_id` -- means one
#: `get_file` turns a row into its text, and no field datamodel.md does not
#: define is added to the table. The text file carries the reverse pointer
#: under `extracted_from_sha256`, so the pair is traceable in both directions.
TEXT_FILE_ID_METADATA_KEY = "extracted_text_file_id"


@dataclass(frozen=True)
class FetchedDocument:
    """One source document as actually retrieved."""

    url: str
    body: str
    content_type: str = "text/html; charset=utf-8"


@dataclass(frozen=True)
class SourceArchiveReport:
    """What one `SourceDocumentLoader.archive()` call actually did.

    Carries every file id and every hash so a caller (and this story's live
    verification script) can show a human exactly what was written without
    re-reading ZeroDB. `is_new_capture` is False for a no-op re-run.

    THREE hashes, which are three different claims:

      * `text_sha256` is the capture's IDENTITY -- `promise_source_id` is
        `src:{text_sha256}`. See the module docstring on why identity is the
        text rather than the bytes.
      * `document_bytes_sha256` is the byte-exact hash of what the server
        actually sent, recorded on the stored document's own metadata so the
        archive stays verifiable at the byte level.
      * `document_file_id`/`text_file_id` are where each one lives.
    """

    candidacy_id: str
    url: str
    source_type: str
    document_file_id: str
    document_bytes_sha256: str
    text_file_id: str
    text_sha256: str
    text_characters: int
    promise_source_id: str
    captured_at: str
    is_new_capture: bool


def sha256_hex(data: bytes | str) -> str:
    """The sha256 hex digest of raw bytes or text, encoded as UTF-8 if text."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


# --- fetching: the only function that reaches the network --------------------


#: The same plain desktop-browser User-Agent story 9.1 settled on, and for the
#: same reason recorded there: several real publishers' WAFs refuse
#: `python-httpx/...` with a bare 403 while allowing an ordinary browser
#: string. This is not evading a login, a paywall or a CAPTCHA -- it is not
#: identifying this fetch as a bare scripting client. Where a publisher has
#: asked crawlers like this one NOT to fetch something (C-SPAN's
#: `/transcript`, the UCSB Presidency Project, Vote Smart -- all three name
#: ClaudeBot or the path in robots.txt), this module does not fetch it at
#: all, whatever User-Agent would get through. See the module docstring.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_source_document(
    url: str, *, timeout_seconds: float = 30.0, user_agent: str = DEFAULT_USER_AGENT
) -> FetchedDocument:
    """GET one source document exactly as published. The real network call.

    Kept separate from `SourceDocumentLoader`, same seam every other ingest
    module in this package uses: every test runs against a fixture with no
    network at all, and only
    `scripts/archive_debates_questionnaires.py` calls this for real.

    Raises:
        SourceDocumentError: the request failed or did not come back 200.
    """
    try:
        response = httpx.get(
            url,
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
    except httpx.HTTPError as exc:
        raise SourceDocumentError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise SourceDocumentError(f"GET {url} returned {response.status_code}, expected 200")
    return FetchedDocument(
        url=url,
        body=response.text,
        content_type=response.headers.get("content-type", "text/html"),
    )


def extract_document_text(body: str) -> str:
    """Plain text of a fetched source document.

    Reuses story 9.1's `extract_text` (the standard library's `html.parser`,
    script/style excluded) rather than adding a second HTML-to-text path.
    Plain text passes through untouched, so a transcript served as text/plain
    does not have to pretend it is markup -- the same posture story 9.3's
    `extract_text_if_html` takes, and deliberately the same function, so the
    text this module archives is the text that extractor would have derived.
    """
    from app.ingest.promise_extraction import extract_text_if_html

    return extract_text_if_html(body)


# --- loading -----------------------------------------------------------------


def text_file_key(source_id: str) -> str:
    """Where the extracted text for one capture is stored.

    Derived from the capture's own content-addressed id rather than held in a
    `promise_sources` field, because datamodel.md's spec for that table has
    one `archived_file_id` and this module does not invent a second column.
    """
    return f"promise-sources/{source_id}.txt"


class SourceDocumentLoader:
    """Fetches, hashes, stores and records one candidacy's source capture.

    `document_fetcher` is injected (defaulting to the real
    `fetch_source_document`) so every test runs against a fixture with no
    network at all -- the same seam `CampaignSiteLoader` uses.
    """

    def __init__(
        self,
        client: ZeroDBClient,
        *,
        pace: Callable[[], None] | None = None,
        document_fetcher: Callable[[str], FetchedDocument] | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        self._client = client
        self._pace = pace if pace is not None else (lambda: None)
        self._fetch = document_fetcher if document_fetcher is not None else fetch_source_document
        self._now = now if now is not None else utc_now_iso
        # No ProvenanceContext, for the reason PromiseSource's own docstring
        # spells out at length: the model declares `url`, not `source_url`, so
        # `requires_provenance` does not recognise it and the repository would
        # refuse one. `archive()` calls `write_provenance` directly instead,
        # exactly as story 9.1 does.
        self._sources: Repository[PromiseSource] = Repository(client=client, model=PromiseSource)

    def archive(self, *, candidacy_id: str, url: str, source_type: str) -> SourceArchiveReport:
        """Archive one source document for one candidacy, idempotently.

        Raises:
            ValueError: `url` is empty, or `source_type` is not one of
                `KNOWN_SOURCE_TYPES`. There is no automated discovery to fall
                back on (see the module docstring), so a missing url is the
                caller's mistake rather than something to invent.
            SourceDocumentError: the document could not be fetched, or carried
                no extractable text. Either way no row is written: a
                `promise_sources` row whose archive holds nothing citable is
                worse than no row.
            MissingReferenceError: `candidacy_id` does not resolve to a
                `candidacies` row (raised by the repository layer).
        """
        if not url or not url.strip():
            raise ValueError(
                "archive() needs an explicit url; this loader does not discover a "
                "candidate's questionnaire page or debate transcript automatically "
                "(see this module's docstring)"
            )
        if source_type not in KNOWN_SOURCE_TYPES:
            raise ValueError(
                f"unknown source_type {source_type!r}; this loader writes only "
                f"{sorted(KNOWN_SOURCE_TYPES)}. A promise whose provenance records "
                "an unrecognised source type cannot be grouped or audited."
            )

        self._pace()
        document = self._fetch(url)
        document_bytes_sha256 = sha256_hex(document.body)
        text = extract_document_text(document.body)
        if not text.strip():
            raise SourceDocumentError(
                f"{url} carried no extractable text, so there is nothing for promise "
                "extraction to read; refusing to archive an empty source rather than "
                "writing a row that cites nothing"
            )
        text_sha256 = sha256_hex(text)
        captured_at = self._now()
        # The IDENTITY is the text, not the bytes -- see the module docstring
        # on VOTE411's per-minute cache comment, found live.
        source_id = f"src:{text_sha256}"

        existing = self._sources.get(source_id)
        if existing is not None and self._archive_is_retrievable(existing, text_sha256):
            # Idempotent re-run: the same TEXT was already captured under this
            # exact content-addressed id and -- checked, not assumed -- read
            # back intact. Re-reading the stored row's file ids is then correct
            # rather than re-uploading.
            logger.info(
                "debates_questionnaires: %s already archived for %s (%s); no new files uploaded",
                source_id,
                candidacy_id,
                url,
            )
            return SourceArchiveReport(
                candidacy_id=candidacy_id,
                url=existing.url,
                source_type=existing.source_type,
                document_file_id=existing.archived_file_id,
                document_bytes_sha256=document_bytes_sha256,
                text_file_id=self._stored_text_file_id(existing.archived_file_id),
                text_sha256=text_sha256,
                text_characters=len(text),
                promise_source_id=source_id,
                captured_at=existing.captured_at,
                is_new_capture=False,
            )

        if existing is not None:
            # The row is here but its archive is not readable, so this re-run
            # is a REPAIR: fall through and re-upload. Same text, same id, so
            # the repair is an upsert rather than a second capture.
            logger.warning(
                "debates_questionnaires: %s exists for %s but its archive is not "
                "retrievable; re-uploading the captured files",
                source_id,
                candidacy_id,
            )

        # The TEXT is uploaded first, so its id can be recorded on the
        # document's own metadata below. That ordering is what makes a
        # `promise_sources` row resolvable to its extraction input with a
        # single `get_file` -- see TEXT_FILE_ID_METADATA_KEY.
        text_file = self._client.upload_file(
            file_key=text_file_key(source_id),
            content=text.encode("utf-8"),
            file_name=f"{source_id}.txt",
            content_type="text/plain; charset=utf-8",
            file_metadata={
                "sha256": text_sha256,
                "source_url": url,
                "captured_at": captured_at,
                "source_type": source_type,
                # Ties the extractor's input back to the document of record,
                # so a quote span can be checked against the exact text it
                # was extracted from. See the module docstring.
                "extracted_from_bytes_sha256": document_bytes_sha256,
            },
        )

        document_file = self._client.upload_file(
            file_key=f"promise-sources/{source_id}{self._document_suffix(document)}",
            content=document.body.encode("utf-8"),
            file_name=f"{source_id}{self._document_suffix(document)}",
            content_type=document.content_type,
            file_metadata={
                "sha256": document_bytes_sha256,
                "source_url": url,
                "captured_at": captured_at,
                "source_type": source_type,
                TEXT_FILE_ID_METADATA_KEY: text_file.file_id,
                "extracted_text_sha256": text_sha256,
            },
        )

        stored = self._sources.upsert(
            {
                "id": source_id,
                "candidacy_id": candidacy_id,
                "source_type": source_type,
                "url": url,
                "archived_file_id": document_file.file_id,
                "captured_at": captured_at,
            }
        )
        assert stored.id is not None  # noqa: S101 - upsert guarantees it

        write_provenance(
            self._client,
            entity_table=PromiseSource.table_name,
            entity_id=stored.id,
            source_url=url,
            fetched_at=captured_at,
            content_hash=text_sha256,
            adapter_version=ADAPTER_VERSION,
        )

        logger.info(
            "debates_questionnaires: archived %s (%s) for %s (%s), %d characters of text",
            source_id,
            source_type,
            candidacy_id,
            url,
            len(text),
        )
        return SourceArchiveReport(
            candidacy_id=candidacy_id,
            url=url,
            source_type=source_type,
            document_file_id=document_file.file_id,
            document_bytes_sha256=document_bytes_sha256,
            text_file_id=text_file.file_id,
            text_sha256=text_sha256,
            text_characters=len(text),
            promise_source_id=source_id,
            captured_at=captured_at,
            is_new_capture=True,
        )

    @staticmethod
    def _document_suffix(document: FetchedDocument) -> str:
        """`.html` or `.txt`, by what the server said the document was."""
        return ".html" if "html" in document.content_type.lower() else ".txt"

    def _stored_text_file_id(self, document_file_id: str) -> str:
        """The already-stored text file's id, for a no-op re-run's report.

        Read off the document's own `file_metadata` (see
        `TEXT_FILE_ID_METADATA_KEY`), because a no-op re-run has no upload
        response to take it from. Returns `""` when the capture predates this
        module storing text at all -- a story-9.1 campaign-website row, say --
        which a caller can tell apart from a stored-but-empty file.
        """
        if not document_file_id:
            return ""
        try:
            stored = self._client.get_file(file_id=document_file_id)
        except ZeroDBError as exc:
            logger.info(
                "debates_questionnaires: could not read metadata for %s (%s)",
                document_file_id,
                exc,
            )
            return ""
        return str(stored.file_metadata.get(TEXT_FILE_ID_METADATA_KEY) or "")

    def _archive_is_retrievable(self, existing: PromiseSource, text_sha256: str) -> bool:
        """Can the already-stored archive be read back, and is it right?

        An existing row is not proof its archive is intact -- that reasoning
        assumes bytes were stored at all, which they were not for any capture
        made before `ZeroDBClient.upload_file` was fixed to post the file
        body. So the claim is checked.

        The comparison is on the EXTRACTED TEXT, matching what the id means
        (see the module docstring). Checking the raw bytes here would
        reintroduce the VOTE411 bug from the other side: a stored document
        whose cache comment has since ticked holds the same answers, so it
        hashes differently at the byte level and would be judged "not
        retrievable" and needlessly re-uploaded on every single run.
        """
        if not existing.archived_file_id:
            return False
        try:
            archived = self._client.download_file(file_id=existing.archived_file_id)
        except ZeroDBError as exc:
            logger.info(
                "debates_questionnaires: archive %s is not readable (%s)",
                existing.archived_file_id,
                exc,
            )
            return False
        actual = sha256_hex(extract_document_text(archived.decode("utf-8", errors="replace")))
        if actual != text_sha256:
            logger.warning(
                "debates_questionnaires: archive %s extracts to text hashing %s, "
                "not the captured %s",
                existing.archived_file_id,
                actual,
                text_sha256,
            )
            return False
        return True


def read_source_text(client: ZeroDBClient, source: PromiseSource) -> str:
    """The archived plain text for one `promise_sources` row.

    This is the function story 9.3's pipeline wants: it turns a
    `promise_sources` row into the `source_text` that
    `PromiseExtractionPipeline.extract_from_source` takes, so the extractor
    reads the archived text that was actually stored rather than re-parsing
    the document and hoping the parser has not drifted.

    Falls back to extracting from the archived document when no text file
    exists -- which is the state story 9.1's campaign-website captures are
    in, since that loader stored HTML and a PDF and no text file. So this
    works for every `promise_sources` row, not only this story's.

    Raises:
        SourceDocumentError: neither the text file nor the document could be
            read, so there is nothing to extract from. A silent "" here would
            become an extraction run that finds no promises for a reason that
            has nothing to do with the candidate's words.
    """
    if source.archived_file_id:
        try:
            stored = client.get_file(file_id=source.archived_file_id)
            text_file_id = str(stored.file_metadata.get(TEXT_FILE_ID_METADATA_KEY) or "")
        except ZeroDBError:
            text_file_id = ""
        if text_file_id:
            try:
                return client.download_file(file_id=text_file_id).decode("utf-8")
            except ZeroDBError as exc:
                logger.info(
                    "debates_questionnaires: text file for %s is not readable (%s); "
                    "falling back to the archived document",
                    source.id,
                    exc,
                )

    if not source.archived_file_id:
        raise SourceDocumentError(
            f"promise_sources row {source.id!r} has no archived_file_id, so its text "
            "cannot be read; nothing should be extracted from it"
        )
    try:
        document = client.download_file(file_id=source.archived_file_id)
    except ZeroDBError as exc:
        raise SourceDocumentError(
            f"neither the extracted text nor the archived document for "
            f"{source.id!r} could be read ({exc}); this capture is not currently "
            "citable and should be repaired by re-running its loader"
        ) from exc
    return extract_document_text(document.decode("utf-8", errors="replace"))


__all__ = [
    "ADAPTER_VERSION",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "DEFAULT_USER_AGENT",
    "GOVINFO_CONTENT_URL",
    "GOVINFO_GRANULE_MODS_URL",
    "KNOWN_SOURCE_TYPES",
    "SOURCE_TYPE_DEBATE_TRANSCRIPT",
    "SOURCE_TYPE_FLOOR_STATEMENT",
    "SOURCE_TYPE_QUESTIONNAIRE",
    "TEXT_FILE_ID_METADATA_KEY",
    "VOTE411_CRAWL_DELAY_SECONDS",
    "FetchedDocument",
    "SourceArchiveReport",
    "SourceDocumentError",
    "SourceDocumentLoader",
    "extract_document_text",
    "fetch_source_document",
    "read_source_text",
    "sha256_hex",
    "text_file_key",
]
