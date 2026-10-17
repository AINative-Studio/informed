#!/usr/bin/env python3
"""Verify story 9.3's extraction pipeline against REAL archived sources.

Runs the real pipeline — real `promise_sources` rows read from the real
"informed" ZeroDB project, real HTTP fetches of the real campaign sites those
rows recorded, real segmentation, real quote-span verification — over story
9.1's three real captures:

    Jon Ossoff (D-GA), sitting U.S. Senator      -- https://electjon.com/
    Nancy Pelosi (D-CA-11), sitting U.S. Rep.    -- https://www.pelosiforcongress.org/
    Mike Collins (R-GA-10), sitting U.S. Rep.    -- https://mikecollinsga.com/

    python backend/scripts/verify_promise_extraction_live.py

===============================================================================
WHAT THIS RUN PROVED, AND WHAT IT DISPROVED
===============================================================================

Run live 2026-10-17 against the real project (unlike story 9.1's own
verification script, which could not reach it — credentials exist now, and all
three of its `promise_sources` rows are present and correct).

PROVED — the pipeline's plumbing, against real text:

  * all three real `promise_sources` rows read back and validate;
  * real pages fetch and convert: 189,137 / 39,722 / 91,784 HTML bytes ->
    3,448 / 784 / 1,948 chars of text -> 163 / 19 / 67 candidate passages;
  * every quote span produced is a verbatim substring of its passage
    (`verify_quote_span` passes on all of them);
  * every commitment converts into a `promises` row that passes `Promise`'s
    validators;
  * ids are deterministic — a second run produces byte-identical ids, so a
    re-run is an upsert and not a duplicate.

DISPROVED — any notion that the stand-in extractor is usable as an extractor.
Against these three real pages it scored **0 for 7**:

  * 7 "commitments" found, 0 testable, and all 7 are false positives:
    "Chip in to support Jon's campaign!", "Donate Today to Support Our Work",
    "Follow Jon on Twitter and Bluesky...", a Pelosi sentence CRITICISING
    opponents read as her own commitment, and a Collins biography sentence;
  * it MISSED the one genuinely testable commitment in the real text —
    "fighting to cap insulin costs for seniors at $35 a month" on electjon.com
    — because "fighting to cap" is not in its commitment-verb list.

That is the expected result for a regex stand-in and is exactly why it is
labeled scaffolding everywhere it appears (see
`app.ingest.promise_extraction.RuleBasedStubExtractor`). It is recorded here
rather than quietly omitted because it is the concrete evidence for two
decisions in this story:

  1. why **nothing was written to the live `promises` table** (see below), and
  2. why story 9.3's "at least 90% precision on a 300-promise labeled set"
     acceptance criterion is reported as NOT MET rather than approximated.

===============================================================================
NOTHING IS WRITTEN TO THE LIVE PROJECT, AND WHY THAT WAS THE CALL
===============================================================================

This script is read-and-report only. The live `promises` table was empty
before this run and is empty after it.

The alternative — writing the stub's output with
`extractor_version="stub-rule-based-v0"` — was considered and rejected.
The label is honest and would be preserved, but it only protects a consumer
that filters on it, and the two stories that next read `promises` (9.4
embedding, 10.1 bill mapping) do not filter on `extractor_version` and have no
reason to. Writing would therefore place seven fabricated promises —
attributed to three real, named public figures, two of them sitting members of
Congress — into the system of record of an app whose entire claim is cited
evidence. prd.md's evidence-over-opinion rule is not satisfied by a row that
is clearly labeled wrong; it is satisfied by not writing a wrong row.

The table stays empty until a real extractor exists. That is a visible,
honest gap rather than an invisible, labeled one.

===============================================================================
ANOTHER LIVE FINDING (SINCE FIXED): THE ARCHIVED BYTES DID NOT EXIST
===============================================================================

Found by this script's own live run on 2026-10-17: `generate_presigned_url`
minted a real signed URL for all three of story 9.1's archived files, and a
GET of each returned `404 NoSuchBucket`. The cause was in the client —
`ZeroDBClient.upload_file` posted file metadata to ZeroDB's metadata-only
route and never sent any bytes, so story 9.1 registered three file records
whose contents had never been stored.

That is fixed: `upload_file` posts the bytes as multipart to ZeroDB's binary
endpoint, `download_file` reads them back, and story 9.1's three real captures
were re-archived. The per-source "archived bytes" check below reads them
through `download_file` and verifies the sha256 against what the capture
recorded, so this script reports the real state of the live data on every run
rather than restating a conclusion from one.

This script still extracts from a live re-fetch of each row's own recorded
`url` rather than from the archive. That is deliberate for a *verification*
script — it exercises the same `fetch_campaign_page` path story 9.1 used, so a
site that has started refusing us shows up here. Extraction for real goes
through `scripts/extract_promises.py --require-archive`, which reads the
archive itself.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.campaign_sites import CampaignSiteError, fetch_campaign_page  # noqa: E402
from app.ingest.promise_extraction import (  # noqa: E402
    STUB_EXTRACTOR_VERSION,
    QuoteSpanError,
    RuleBasedStubExtractor,
    extract_text_if_html,
    passages_with_context,
    promise_id_for,
    segment_sentences,
    verify_quote_span,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import Promise, PromiseSource  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402

logger = logging.getLogger("verify_promise_extraction")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        client = ZeroDBClient.from_env()
    except Exception as exc:  # noqa: BLE001
        logger.error("no ZeroDB credentials in the environment: %s", exc)
        logger.error("source local.env into the shell and re-run.")
        return 1

    logger.info("connected to the live project %s", client.project_id)

    sources = Repository(client=client, model=PromiseSource).query(limit=50)
    logger.info("%d promise_sources row(s) on file (story 9.1 wrote these)\n", len(sources))
    if not sources:
        logger.error("no promise_sources rows; run scripts/archive_campaign_sites.py first")
        return 1

    stub = RuleBasedStubExtractor()
    total_found = 0
    total_testable = 0
    spans_verified = 0

    for source in sources:
        logger.info("=" * 78)
        logger.info("%s", source.id)
        logger.info("  url:       %s", source.url)
        logger.info("  candidacy: %s", source.candidacy_id)

        # Are the archived bytes readable, and are they the right bytes? Both
        # checked live, not assumed. Reading back through BOTH paths matters:
        # `download_file` is what a server-side reader uses, and the presigned
        # URL is what the UI hands a browser, and story 9.1 shipped with the
        # second one 404ing while nothing ever ran it.
        try:
            archived_bytes = client.download_file(file_id=source.archived_file_id)
            # The row's id is `src:<sha256 of the capture>`, so this is an
            # end-to-end integrity check and not just a reachability one.
            expected = source.id.removeprefix("src:")
            actual = hashlib.sha256(archived_bytes).hexdigest()
            logger.info(
                "  archived bytes: READABLE via download_file (%d bytes), sha256 %s",
                len(archived_bytes),
                "MATCHES the capture" if actual == expected else f"MISMATCH ({actual})",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("  archived bytes: UNREADABLE via download_file (%s)", exc)

        try:
            presigned = client.generate_presigned_url(file_id=source.archived_file_id)
            archived = httpx.get(presigned.url, timeout=30, follow_redirects=True)
            if archived.status_code == 200:
                logger.info(
                    "  archived bytes: READABLE via presigned URL (%d bytes)",
                    len(archived.content),
                )
            else:
                logger.warning(
                    "  archived bytes: UNREADABLE via presigned URL (HTTP %d, %s)",
                    archived.status_code,
                    archived.text[:60].replace("\n", " "),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("  archived bytes: presigned URL failed (%s)", type(exc).__name__)

        try:
            page = fetch_campaign_page(source.url)
        except CampaignSiteError as exc:
            logger.warning("  FETCH FAILED: %s", exc)
            continue

        text = extract_text_if_html(page.html)
        passages = passages_with_context(segment_sentences(text))
        logger.info(
            "  %d html bytes -> %d chars of text -> %d candidate passage(s)",
            len(page.html),
            len(text),
            len(passages),
        )

        for passage, context in passages:
            for commitment in stub(passage, context=context):
                # Quote-span exactness, verified against real text.
                try:
                    verify_quote_span(commitment.quote_span, source_text=passage)
                except QuoteSpanError as exc:
                    logger.error("  QUOTE SPAN FAILED: %s", exc)
                    return 1
                spans_verified += 1

                promise_id = promise_id_for(str(source.id), commitment)
                # The row the pipeline WOULD write, validated but not written.
                Promise.model_validate(
                    {
                        "id": promise_id,
                        "candidacy_id": source.candidacy_id,
                        "person_id": "person:placeholder-not-written",
                        "source_id": source.id,
                        "quote_span": commitment.quote_span,
                        "actor": commitment.actor,
                        "action": commitment.action,
                        "object": commitment.object,
                        "direction": commitment.direction,
                        "testable": commitment.testable,
                        "extractor_version": STUB_EXTRACTOR_VERSION,
                        "confidence": commitment.confidence,
                    }
                )
                total_found += 1
                total_testable += int(commitment.testable)
                logger.info(
                    "   [%s] %s\n     actor=%s action=%s object=%s dir=%s\n     quote: %s",
                    "TESTABLE" if commitment.testable else "not testable",
                    promise_id,
                    commitment.actor,
                    commitment.action,
                    str(commitment.object)[:60],
                    commitment.direction,
                    commitment.quote_span[:150],
                )

    logger.info("=" * 78)
    logger.info(
        "stand-in extractor found %d 'commitment(s)' across %d real source(s), "
        "%d testable; %d quote span(s) verified verbatim",
        total_found,
        len(sources),
        total_testable,
        spans_verified,
    )
    logger.info(
        "\nThe PIPELINE is verified against real text. The EXTRACTOR is a labeled "
        "stand-in and its output is not promise data -- see this script's docstring "
        "for the 0-for-7 result and why nothing is written to the live project."
    )

    stored = Repository(client=client, model=Promise).query(limit=50)
    logger.info("live promises table holds %d row(s) -- unchanged by this run", len(stored))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
