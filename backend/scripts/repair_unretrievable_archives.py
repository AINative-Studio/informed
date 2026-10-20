#!/usr/bin/env python3
"""Find and mend `promise_sources` rows whose archive stores no bytes.

WHAT THIS REPAIRS
=================

`ZeroDBClient.upload_file` used to POST file *metadata* to ZeroDB's
metadata-only route and never send the file body. Every capture made through
it — story 9.1's three real campaign-site archives, and the six `files` rows
behind them — therefore has a perfectly good metadata row (right `file_key`,
right sha256, right `source_url`) with no stored object behind it, and every
download of one answers `404 NoSuchKey`. Confirmed live against the real
"informed" project on 2026-10-17, for all six.

An archive that cannot be read back is not an archive. prd.md's
evidence-over-opinion rule is not satisfied by a `promise_sources` row that
*names* a snapshot; it is satisfied by a snapshot a reader can fetch.

THE TWO OUTCOMES, AND WHY ONLY ONE IS AUTOMATED
================================================

A capture's id is `src:<sha256 of the captured HTML>`, so re-archiving the
same url yields the SAME id if and only if the page is byte-identical to what
was captured. That makes the state of each broken row an observable fact, not
a judgment call:

  * **MENDABLE.** The live page still hashes to the row's own sha256, so a
    re-fetch reproduces the exact captured bytes. `CampaignSiteLoader.capture`
    re-uploads them under the same id and the row is whole again — its
    `archived_file_id` now points at real, retrievable content whose hash
    matches what the row always claimed. This script does that with `--apply`:
    an in-place repair of permanent reference data, the same posture as
    `app/corrections/retention.py` sweeping live rows.

  * **UNMENDABLE.** The page has changed since capture, so the original bytes
    are **gone and cannot be reconstructed by anyone** — not by this script,
    not by a later one. (Where archive.org happens to hold a snapshot, that is
    a different capture with different bytes and a different hash; presenting
    it under this row's id would be a fabricated provenance claim, so nothing
    here does that.) The row is a citation that can never be honoured.

    **This script reports those rows and does not delete them.** Deleting
    live reference data is a decision for a human with the backlog in front of
    them, not something a repair pass does on its way past — so the report
    gives the exact list, with the evidence for each, and stops there.

    The plumbing is no longer the obstacle it was: `ZeroDBClient.delete_file`
    now exists (and `delete_row` always did), so executing that decision is
    mechanically possible today. These particular rows name files whose bytes
    were never stored, so `delete_file` on them requires its explicit
    `allow_missing_content=True` — which is exactly the deliberate, named
    opt-in a retirement like this should have to pass through. What is still
    missing is the human decision, and `promise_sources` deletion at the
    repository layer to carry it out tidily.

Re-running `scripts/archive_campaign_sites.py` captures the pages as they are
TODAY, under new ids. That is a NEW capture, not a repair of a lost one, and
it leaves the unmendable rows in place beside it. Both facts are reported.

    python scripts/repair_unretrievable_archives.py              # report only
    python scripts/repair_unretrievable_archives.py --apply      # mend what can be mended

Reads ZeroDB credentials from the environment via `ZeroDBClient.from_env()`,
like every other script here; it never opens `local.env` itself
(docs/environments.md).

Exit codes:
    0  the run completed and every row is retrievable
    1  no credentials, a ZeroDB error, or at least one row is still
       unretrievable (mendable but not applied, or unmendable)
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.campaign_sites import (  # noqa: E402
    CampaignSiteError,
    CampaignSiteLoader,
    fetch_campaign_page,
    sha256_hex,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import PromiseSource  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("repair_unretrievable_archives")

#: Every outcome that leaves the row citable. Anything else is a gap.
OK_OUTCOMES = frozenset({"retrievable", "mended"})


@dataclass(frozen=True)
class RowOutcome:
    """What this run found, and did or would do, for one `promise_sources` row."""

    source_id: str
    url: str
    #: "retrievable", "mended", "mendable", "unmendable", or "unknown".
    outcome: str
    detail: str = ""


def archive_reads_back(client: ZeroDBClient, source: PromiseSource) -> bool:
    """Is this row's archive readable AND the bytes its sha256 claims?

    Both halves matter. A readable object that hashes to something else is not
    this capture, and treating it as one would make the citation wrong rather
    than merely missing.
    """
    if not source.archived_file_id:
        return False
    try:
        archived = client.download_file(file_id=source.archived_file_id)
    except ZeroDBError:
        return False
    assert source.id is not None  # noqa: S101 - a stored row always carries one
    return sha256_hex(archived) == source.id.removeprefix("src:")


def check_and_repair(
    client: ZeroDBClient,
    source: PromiseSource,
    *,
    apply: bool,
    loader: CampaignSiteLoader | None = None,
) -> RowOutcome:
    """Classify one row and, with `apply`, mend it if its bytes are recoverable."""
    assert source.id is not None  # noqa: S101
    captured_sha256 = source.id.removeprefix("src:")

    if archive_reads_back(client, source):
        return RowOutcome(source.id, source.url, "retrievable", "already readable")

    # Can the captured bytes still be obtained? Only the live page can say.
    try:
        page = fetch_campaign_page(source.url)
    except CampaignSiteError as exc:
        return RowOutcome(
            source.id,
            source.url,
            "unknown",
            f"archive unreadable and the page cannot be re-fetched: {exc}",
        )

    live_sha256 = sha256_hex(page.html)
    if live_sha256 != captured_sha256:
        return RowOutcome(
            source.id,
            source.url,
            "unmendable",
            f"the page now hashes to {live_sha256[:16]}..., not the captured "
            f"{captured_sha256[:16]}...; the captured bytes are gone and cannot "
            "be reconstructed",
        )

    if not apply:
        return RowOutcome(
            source.id,
            source.url,
            "mendable",
            "the page is byte-identical to the capture; --apply would re-upload it",
        )

    # Same bytes, same id, so the loader's repair path re-uploads under the
    # existing id rather than creating a second capture.
    report = (loader or CampaignSiteLoader(client)).capture(
        candidacy_id=source.candidacy_id, url=source.url
    )
    repo: Repository[PromiseSource] = Repository(client=client, model=PromiseSource)
    reread = repo.get(source.id)
    if reread is None or not archive_reads_back(client, reread):
        return RowOutcome(source.id, source.url, "unknown", "re-upload did not become readable")
    return RowOutcome(
        source.id,
        source.url,
        "mended",
        f"re-uploaded under the same id (file {report.html_file_id})",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="re-upload the archives whose bytes are still recoverable",
    )
    parser.add_argument("--limit", type=int, default=500, help="promise_sources rows to scan")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        client = ZeroDBClient.from_env()
    except Exception as exc:  # noqa: BLE001
        logger.error("no ZeroDB credentials: %s", exc)
        return 1

    sources: Repository[PromiseSource] = Repository(client=client, model=PromiseSource)
    rows = sources.query(limit=args.limit)
    if not rows:
        logger.info("no promise_sources rows to check")
        return 0

    if not args.apply:
        logger.info("REPORT ONLY — nothing will be changed. Re-run with --apply to mend.")

    loader = CampaignSiteLoader(client)
    outcomes = [check_and_repair(client, row, apply=args.apply, loader=loader) for row in rows]

    logger.info("")
    logger.info("=== %d row(s) checked ===", len(outcomes))
    for outcome in outcomes:
        logger.log(
            logging.INFO if outcome.outcome in OK_OUTCOMES else logging.WARNING,
            "%-12s %s (%s) — %s",
            outcome.outcome,
            outcome.source_id,
            outcome.url,
            outcome.detail,
        )

    gaps = [o for o in outcomes if o.outcome not in OK_OUTCOMES]
    if gaps:
        logger.error("")
        logger.error(
            "%d of %d row(s) still name an archive that cannot be fetched. These "
            "are citations pointing at nothing and need a deliberate decision — "
            "see this script's docstring on UNMENDABLE rows.",
            len(gaps),
            len(outcomes),
        )
        return 1

    logger.info("")
    logger.info("every promise_sources row's archive reads back and matches its sha256")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ZeroDBError as exc:
        logging.getLogger("repair_unretrievable_archives").error("ZeroDB error: %s", exc)
        sys.exit(1)
