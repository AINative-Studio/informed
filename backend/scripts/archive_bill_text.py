#!/usr/bin/env python3
"""CLI entrypoint for story 5.4's bill-text archival.

    python scripts/archive_bill_text.py --bill us-house:119:hr-3424
    python scripts/archive_bill_text.py --bill us-house:119:hr-3424 --bill us-house:119:hr-1
    python scripts/archive_bill_text.py --all-bills
    python scripts/archive_bill_text.py --all-bills --limit 50 --dry-run

For each named bill, fetches its published text versions from
api.congress.gov, picks the latest-dated one, fetches that version's document
from www.congress.gov, stores the bytes in ZeroDB Files with their sha256, and
points `bills.text_file_id` at the stored file -- through
`app.ingest.bill_text.BillTextLoader`, idempotently (re-running for a bill
whose text has not changed uploads nothing; see that module's docstring).

Which bills, and why `--all-bills` is opt-in rather than the default
=======================================================================

`--bill` names them explicitly. `--all-bills` walks the `bills` table and
archives every row's text, which is the shape a scheduled job would take --
but it is not wired into `app.scheduling.jobs` by this story, deliberately:

  * Each bill costs two network fetches (the version listing, then the
    document), and the documents are large -- H.R. 1's enrolled HTML is
    1,022,332 bytes, measured live. A full 119th-Congress backfill is
    thousands of bills and gigabytes of egress from congress.gov, which is a
    capacity decision for a human, not something a story should start on its
    own.
  * `--limit` exists so a first run can be sized deliberately.

A bill with no published text yet is reported and skipped, not failed: that is
the ordinary state of a newly introduced bill (see `app.ingest.bill_text`).

A bill must already be ingested (`app.ingest.house_bills` writes `bills`
rows); naming one that is not raises `BillTextNotFoundError` and is reported as
a skip rather than inventing a row for the archive to hang off.

Reads ZeroDB credentials and `CONGRESS_GOV_API_KEY` from the environment,
exactly like `scripts/load_house_rollcalls.py` -- this script never opens
`local.env` itself; the shell that runs it does (docs/environments.md). Never
pass a credential on the command line.

THIS SCRIPT WRITES PERMANENT REFERENCE DATA. Every document it archives is
real public record -- the text of a real bill -- and is what a vote citation
opens. Re-running it is expected and safe (idempotent on the content-addressed
file key), and nothing it writes should be deleted afterwards.

Exit codes:
    0  the run completed (even if some bills were skipped)
    1  no bill was named, the API key is missing, or a ZeroDB error stopped
       the run partway through
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.bill_text import (  # noqa: E402
    DEFAULT_MIN_INTERVAL_SECONDS,
    BillTextLoader,
    BillTextNotFoundError,
    BillTextSourceError,
)
from app.repositories.errors import RepositoryError  # noqa: E402
from app.repositories.models import Bill  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("archive_bill_text")


def _pacer(min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS):
    """The same last-call-relative pacer every other load script inlines."""
    state: dict[str, float | None] = {"last": None}

    def pace() -> None:
        last = state["last"]
        if last is not None:
            remaining = min_interval_seconds - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        state["last"] = time.monotonic()

    return pace


def all_bill_ids(client: ZeroDBClient, *, limit: int | None = None) -> list[str]:
    """Every stored `bills` id, oldest-first by page order.

    Paged rather than fetched whole: datamodel.md's own note that any read
    which could be large must page, and a full Congress is thousands of bills.
    """
    ids: list[str] = []
    skip = 0
    page_size = 200
    while True:
        page = client.query_rows(Bill.table_name, limit=page_size, skip=skip)
        if not page.rows:
            break
        for row in page.rows:
            row_id = row.row_data.get("id")
            if row_id:
                ids.append(str(row_id))
                if limit is not None and len(ids) >= limit:
                    return ids
        if len(page.rows) < page_size:
            break
        skip += page_size
    return ids


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bill",
        action="append",
        default=[],
        metavar="BILL_ID",
        help='A bills id to archive, e.g. "us-house:119:hr-3424". Repeatable.',
    )
    parser.add_argument(
        "--all-bills",
        action="store_true",
        help="Archive the text of every stored bills row. See the module "
        "docstring on why this is opt-in and sized with --limit.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="With --all-bills, stop after N bills.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve which bills would be archived and write nothing.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if not args.bill and not args.all_bills:
        logger.error("name at least one --bill, or pass --all-bills")
        return 1

    api_key = os.environ.get("CONGRESS_GOV_API_KEY")
    if not api_key:
        logger.error(
            "CONGRESS_GOV_API_KEY is not set. Source it into the shell "
            "(see docs/environments.md); never pass it on the command line."
        )
        return 1

    client = ZeroDBClient.from_env()
    bill_ids = list(args.bill)
    if args.all_bills:
        discovered = all_bill_ids(client, limit=args.limit)
        logger.info("found %d stored bill(s)", len(discovered))
        bill_ids.extend(b for b in discovered if b not in bill_ids)

    if args.dry_run:
        logger.info("dry run: would archive text for %d bill(s)", len(bill_ids))
        for bill_id in bill_ids:
            logger.info("  %s", bill_id)
        return 0

    loader = BillTextLoader(client, pace=_pacer(), api_key=api_key)

    uploaded = 0
    unchanged = 0
    no_text = 0
    skipped: list[tuple[str, str]] = []
    for bill_id in bill_ids:
        try:
            report = loader.archive(bill_id=bill_id)
        except (BillTextNotFoundError, BillTextSourceError, RepositoryError) as exc:
            skipped.append((bill_id, str(exc)))
            logger.warning("skipping %s: %s", bill_id, exc)
            continue
        if not report.archived:
            no_text += 1
            logger.info("%s: no text archived (%s)", bill_id, report.reason)
            continue
        if report.uploaded:
            uploaded += 1
            logger.info(
                "%s: archived %s (%s, %s, %d bytes, sha256=%s)",
                bill_id,
                report.text_file_id,
                report.version_type,
                report.format_type,
                report.size_bytes or 0,
                report.sha256,
            )
        else:
            unchanged += 1
            logger.info("%s: already archived as %s, unchanged", bill_id, report.text_file_id)

    logger.info(
        "%d archived, %d already current, %d with no published text, %d skipped (of %d)",
        uploaded,
        unchanged,
        no_text,
        len(skipped),
        len(bill_ids),
    )
    for bill_id, reason in skipped:
        logger.info("  skipped %s: %s", bill_id, reason)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ZeroDBError as exc:
        logger.error("ZeroDB error: %s", exc)
        sys.exit(1)
