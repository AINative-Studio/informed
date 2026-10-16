#!/usr/bin/env python3
"""Erase contact emails on corrections closed over 90 days ago (story 12.4).

datamodel.md, "Privacy, retention and integrity": `corrections.contact_email`
is retained "90 days after resolution". This script is how that promise is
kept today.

!!! THIS IS THE SCHEDULED JOB THAT IS NOT YET SCHEDULED !!!

There is no scheduler, worker, cron or queue in this repository — see
`app/corrections/retention.py`'s module docstring for the full argument, and
`app/ingest/candidacies.py` for the same gap stated for weekly candidacy
refreshes. Until one exists, the 90-day erasure happens when a human runs this.
An address that should have gone on day 90 sits there until the next run. That
is a real gap between the documented promise and the deployed behaviour, and
it is flagged as a follow-up in story 12.4's PR rather than left in a backlog.

The sweep is written to be scheduled with no code change: it is idempotent,
holds no cursor or watermark, and is a pure function of the stored rows and the
clock. A cron entry, a Railway cron service or a GitHub Actions `schedule:`
trigger can call it as-is. Daily is the natural cadence — the window is 90 days,
so a day's granularity is well inside the promise.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a

    # See what would be erased, writing nothing:
    python backend/scripts/sweep_correction_emails.py --dry-run

    # Erase for real:
    python backend/scripts/sweep_correction_emails.py

What it reports, and what it deliberately does not: the counts and the row ids
it touched. **Never an email address** — not to stdout, not to a log, not in
the exit summary. The whole point of this script is that an address stops
existing, and a run log quoting the addresses it just erased would defeat it.
`--dry-run` is the same: it reports how many and which rows, never whose.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.corrections.retention import (  # noqa: E402
    RETENTION_DAYS,
    sweep_expired_contact_emails,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("sweep_correction_emails")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report what would be erased and write nothing. Worth doing on a "
            "first run against a live project: 'how many addresses is this "
            "about to delete' deserves an answer before they are deleted."
        ),
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)
    logger.info(
        "retention window: %d days after a correction is resolved or rejected", RETENTION_DAYS
    )
    if args.dry_run:
        logger.info("DRY RUN — nothing will be written")

    try:
        result = sweep_expired_contact_emails(client, dry_run=args.dry_run)
    except ZeroDBError as exc:
        logger.error("FAILED: %s", exc)
        return 1
    finally:
        client.close()

    logger.info("%s", result.summary())
    for row_id in result.erased_ids:
        # The row id, which is not sensitive. Never the address.
        logger.info(
            "  %s contact email on %s",
            "would erase" if args.dry_run else "erased",
            row_id,
        )
    if args.dry_run and result.erased:
        logger.info("re-run without --dry-run to erase these %d address(es)", result.erased)
    return 0


if __name__ == "__main__":
    sys.exit(main())
