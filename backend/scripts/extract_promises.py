#!/usr/bin/env python3
"""CLI entrypoint for story 9.3's promise-extraction pipeline.

    python scripts/extract_promises.py --dry-run                  # the default posture
    python scripts/extract_promises.py --source-id src:abc...     # one source only
    python scripts/extract_promises.py --write                    # actually write rows

Reads `promise_sources` rows (story 9.1 writes those), runs
`app.ingest.promise_extraction.PromiseExtractionPipeline` over each one's
text, and upserts the resulting `promises` rows — idempotently, since a
promise's id is content-addressed on the commitment itself (see
`app.repositories.models.Promise`).

===============================================================================
THIS SCRIPT IS READY TO RUN AND IS BLOCKED ONLY ON AN LLM CREDENTIAL
===============================================================================

**No LLM credential exists in this environment**, confirmed three ways during
story 9.3 — `.env.example` has no LLM entry, the real `local.env` holds only
`ZERODB_*` variables, and `ZeroDBClient` has embedding/retrieval methods but
no text-generation method at all. See
`app/ingest/promise_extraction.py`'s module docstring for the full finding and
`.env.example`'s "Promise extraction (story 9.3)" section.

So this script currently wires `RuleBasedStubExtractor` — **explicitly-labeled
scaffolding, not the real extractor** — and `--dry-run` is the default
precisely because of what that stub does to real text. Measured live against
all three of story 9.1's real archived campaign sites (2026-10-17):

    electjon.com           5 "commitments", 0 testable — ALL FIVE false
                           positives: "Chip in to support Jon's campaign!",
                           "Donate Today to Support Our Work", "Follow Jon on
                           Twitter and Bluesky..."
    pelosiforcongress.org  1 "commitment", 0 testable — a sentence
                           CRITICISING opponents, read as the candidate's own
                           commitment
    mikecollinsga.com      1 "commitment", 0 testable — a biography sentence

    And it MISSED the one genuinely testable commitment present in the real
    text: "fighting to cap insulin costs for seniors at $35 a month" — because
    "fighting to cap" is not in its commitment-verb list.

That is a 0-for-7 precision result against real pages, in both directions.
Writing those rows into the live `promises` table would put seven fabricated
"promises" — attributed to three real, named public figures, two of them
sitting members of Congress — into the system of record for an app whose
entire claim is cited evidence. The `extractor_version="stub-rule-based-v0"`
label is honest, but it only protects consumers that filter on it, and story
9.4 (embedding) and 10.1 (bill mapping) both read `promises` and neither
filters on `extractor_version` today. So the default here is to report, not
to write.

**Once a real `PromiseExtractor` exists**, change exactly one thing — the
`_build_extractor` function below — and run with `--write`. Nothing else in
this script or in the pipeline needs to change; that is what the
`PromiseExtractor` seam is for. Use
`extractor_version="claude-promise-extractor-v1"` (or the real model's
identifier) so the stub rows and the real rows are distinguishable forever.

Reads ZeroDB credentials from the environment via `ZeroDBClient.from_env()`,
exactly like `scripts/archive_campaign_sites.py` — this script never opens
`local.env` itself; the shell that runs it does (docs/environments.md).

WHERE THE SOURCE TEXT COMES FROM
=================================

Two paths, and `--require-archive` picks the correct one:

  * **`--require-archive` (correct, now that it works)** reads the archived
    snapshot through `ZeroDBClient.download_file` and checks that what comes
    back hashes to the sha256 the `promise_sources` row's id records. A quote
    span then cites the bytes that were captured.
  * **The default** re-fetches each row's OWN recorded `url` with the same
    `fetch_campaign_page` story 9.1 used. Honest, and available when an
    archive predates the fix below — but it is NOT the archived snapshot: a
    page that changed since capture yields different text, and the quote spans
    would cite the live page rather than the archive.

`--require-archive` was unconditionally broken when this script was written,
for the reason story 9.3 flagged: `ZeroDBClient.upload_file` posted file
metadata to ZeroDB's metadata-only route and never sent any bytes, so story
9.1's archives were rows with no object behind them and every presigned GET
answered 404. That is fixed (`upload_file` now posts the bytes as multipart to
the binary endpoint, and `download_file` reads them back), and story 9.1's
three real captures were re-archived, so `--require-archive` is the right flag
to pass against current data.

Exit codes:
    0  the run completed (even if some sources were skipped)
    1  a ZeroDB error stopped the run, or --require-archive could not be met
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.campaign_sites import CampaignSiteError, fetch_campaign_page  # noqa: E402
from app.ingest.promise_extraction import (  # noqa: E402
    DEFAULT_MIN_INTERVAL_SECONDS,
    STUB_EXTRACTOR_VERSION,
    PromiseExtractionError,
    PromiseExtractionPipeline,
    PromiseExtractor,
    RuleBasedStubExtractor,
    extract_text_if_html,
    passages_with_context,
    promise_id_for,
    segment_sentences,
    verify_quote_span,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import PromiseSource  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("extract_promises")


def _build_extractor() -> tuple[PromiseExtractor, str]:
    """The one function to change when a real LLM extractor exists.

    Returns `(extractor, extractor_version)`. Today it returns the
    explicitly-labeled stand-in, because no LLM credential exists in this
    environment — see this script's docstring.

    To wire a real one: implement the `PromiseExtractor` protocol as a
    callable that posts `EXTRACTION_PROMPT_TEMPLATE` (formatted with the
    passage and its context) to the model, parses the JSON array back into
    `ExtractedCommitment` records, and returns them. Then return it here with
    a real version string. The pipeline is unchanged.
    """
    return RuleBasedStubExtractor(), STUB_EXTRACTOR_VERSION


def _source_text_for(source: PromiseSource, *, require_archive: bool, client: ZeroDBClient) -> str:
    """The text to extract from. See this script's docstring on re-fetching.

    `--require-archive` reads the archived snapshot itself, which is the
    correct source: it is the bytes the `promise_sources` row's sha256 was
    computed over, so a quote span cites what was captured rather than
    whatever the page says today. This path was dead until the file-upload
    retrievability fix (story 9.1's archives stored no bytes at all); it works
    now, and a source whose archive is still unreadable fails loudly here
    rather than silently substituting a re-fetch.

    Raises:
        PromiseExtractionError: `--require-archive` was given and the archived
            bytes could not be read back, or the bytes read back do not hash
            to what the capture recorded.
    """
    if require_archive:
        try:
            archived = client.download_file(file_id=source.archived_file_id)
        except ZeroDBError as exc:
            raise PromiseExtractionError(
                f"--require-archive was given, but the archived bytes for "
                f"{source.id} (file {source.archived_file_id}) could not be "
                f"read back: {exc}. A capture made before the file-upload "
                "retrievability fix stores no object; re-archive it with "
                "scripts/archive_campaign_sites.py, or re-run without "
                "--require-archive to extract from a live re-fetch of the "
                "row's own url instead."
            ) from exc
        # The row's id IS the capture's sha256 (`src:<sha256>`), so this is a
        # free end-to-end integrity check on the archive we just read.
        expected = source.id.removeprefix("src:")
        actual = hashlib.sha256(archived).hexdigest()
        if expected != actual:
            raise PromiseExtractionError(
                f"the archived bytes for {source.id} hash to {actual}, not the "
                f"{expected} its id records — the stored object does not match "
                "the capture, so nothing extracted from it would be citable"
            )
        return archived.decode("utf-8", errors="replace")
    page = fetch_campaign_page(source.url)
    return page.html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source-id",
        action="append",
        default=None,
        help="extract from this promise_sources id only (repeatable); default: all",
    )
    write_group = parser.add_mutually_exclusive_group()
    write_group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="report what WOULD be extracted and write nothing (the default)",
    )
    write_group.add_argument(
        "--write",
        dest="dry_run",
        action="store_false",
        help="actually upsert promises rows. Read this script's docstring first: "
        "with the stand-in extractor still wired, this writes fabricated "
        "promises attributed to real public figures",
    )
    parser.add_argument(
        "--require-archive",
        action="store_true",
        help="refuse to extract from a live re-fetch; demand the archived bytes",
    )
    parser.add_argument("--limit", type=int, default=100, help="how many sources to read")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    extractor, extractor_version = _build_extractor()
    if extractor_version == STUB_EXTRACTOR_VERSION:
        logger.warning(
            "USING THE STAND-IN EXTRACTOR (%s). This is NOT the real extractor and "
            "its output is not trustworthy as promise data -- measured 0-for-7 "
            "against real campaign pages. See this script's docstring.",
            extractor_version,
        )
        if not args.dry_run:
            logger.warning(
                "--write was given WITH the stand-in extractor. This writes "
                "fabricated promises attributed to real, named public figures into "
                "the system of record. This is almost certainly not what you want."
            )

    try:
        client = ZeroDBClient.from_env()
    except Exception as exc:  # noqa: BLE001
        logger.error("no ZeroDB credentials: %s", exc)
        return 1

    sources_repo: Repository[PromiseSource] = Repository(client=client, model=PromiseSource)
    if args.source_id:
        sources = [row for row in (sources_repo.get(sid) for sid in args.source_id) if row]
    else:
        sources = sources_repo.query(limit=args.limit)

    if not sources:
        logger.error("no promise_sources rows to extract from (story 9.1 writes those)")
        return 1

    pipeline = PromiseExtractionPipeline(
        client,
        extractor=extractor,
        extractor_version=extractor_version,
        pace=lambda: time.sleep(DEFAULT_MIN_INTERVAL_SECONDS),
    )

    total_commitments = 0
    total_testable = 0
    for source in sources:
        assert source.id is not None  # noqa: S101 - a stored row always carries one
        logger.info("--- %s (%s)", source.id, source.url)
        try:
            source_text = _source_text_for(
                source, require_archive=args.require_archive, client=client
            )
        except PromiseExtractionError as exc:
            logger.error("%s", exc)
            return 1
        except CampaignSiteError as exc:
            logger.warning("skipped %s: %s", source.id, exc)
            continue

        if args.dry_run:
            found, testable = _report_dry_run(source, source_text, extractor)
            total_commitments += found
            total_testable += testable
            continue

        try:
            report = pipeline.extract_from_source(source.id, source_text=source_text)
        except PromiseExtractionError as exc:
            logger.warning("skipped %s: %s", source.id, exc)
            continue
        logger.info(
            "  %d promise(s): %d new, %d updated, %d testable, %d not testable",
            report.total_promises,
            report.new_promises,
            report.updated_promises,
            report.testable_promises,
            report.non_testable_promises,
        )
        total_commitments += report.total_promises
        total_testable += report.testable_promises

    logger.info(
        "%s %d commitment(s) across %d source(s), %d testable",
        "would extract" if args.dry_run else "extracted",
        total_commitments,
        len(sources),
        total_testable,
    )
    if args.dry_run:
        logger.info("DRY RUN -- nothing was written. Use --write to store rows.")
    return 0


def _report_dry_run(
    source: PromiseSource, source_text: str, extractor: PromiseExtractor
) -> tuple[int, int]:
    """Print what would be extracted, without writing anything.

    Runs the same segmentation, the same extractor and the same quote-span
    verification the pipeline does — so a dry run exercises every step except
    the write.
    """
    text = extract_text_if_html(source_text)
    passages = passages_with_context(segment_sentences(text))
    found = 0
    testable = 0
    for passage, context in passages:
        for commitment in extractor(passage, context=context):
            verify_quote_span(commitment.quote_span, source_text=passage)
            found += 1
            testable += int(commitment.testable)
            logger.info(
                "  [%s] %s | actor=%s action=%s object=%s | %s",
                "TESTABLE" if commitment.testable else "not testable",
                promise_id_for(str(source.id), commitment),
                commitment.actor,
                commitment.action,
                str(commitment.object)[:60],
                commitment.quote_span[:120],
            )
    logger.info("  %d passage(s) -> %d commitment(s), %d testable", len(passages), found, testable)
    return found, testable


if __name__ == "__main__":
    raise SystemExit(main())
