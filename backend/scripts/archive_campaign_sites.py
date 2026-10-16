#!/usr/bin/env python3
"""CLI entrypoint for story 9.1's campaign-site archival pipeline.

    python scripts/archive_campaign_sites.py --input candidate_sites.csv
    python scripts/archive_campaign_sites.py --input candidate_sites.csv --dry-run

Reads a small, human-curated CSV of `candidacy_id,url` pairs and, for each
row, fetches the campaign page, stores it as HTML and a rendered PDF in
ZeroDB Files, and writes the matching `promise_sources` + `provenance_log`
rows -- through `app.ingest.campaign_sites.CampaignSiteLoader`, idempotently
(re-running for an unchanged site writes nothing new; see that module's
docstring, "once per election cycle").

WHY A CSV, NOT A DISCOVERY STEP
================================

There is no automated way to discover a candidate's campaign website from
any free federal data source this project has -- checked live against the
real OpenFEC `/v1/candidates/` response during this story, which does not
carry a website field at all (FEC Form 2 never collects one). See
`docs/spikes/9.1-candidate-website-discovery.md` for the full finding. So
this script's input IS the discovery step, done by a person: find the url for
a candidacy the same way a human looking for a candidate's site would (a web
search), and add one line. There is deliberately no crawler, no
pattern-guessed url, and no fallback that invents one -- `CampaignSiteLoader`
raises on an empty url rather than skip it silently, and this script passes
that error through.

CSV format, one header row required:

    candidacy_id,url
    us-2026-11-03-general:ocd-division/country:us/state:pa/cd:2:person:fec-H6PA02205,https://arriaga-for-congress.example/

`candidacy_id` must already exist as a `candidacies` row (story 2.6's loader
writes those); an id that does not resolve raises `MissingReferenceError` and
is reported as a skip, exactly like every other loader in this package.

Reads ZeroDB credentials from the environment via `ZeroDBSettings` /
`ZeroDBClient.from_env()`, exactly like `scripts/load_candidacies.py` -- this
script never opens `local.env` itself; the shell that runs it does
(docs/environments.md). Never pass a credential on the command line.

Wayback (Internet Archive Save Page Now) submission is attempted only when
`WAYBACK_ACCESS_KEY`/`WAYBACK_SECRET_KEY` are set (a free archive.org
account's S3-like keys -- see `app.ingest.campaign_sites`'s module docstring
for why this is required and not, as this story's brief assumed, free and
keyless). Omitting them is fine: the HTML/PDF archival this story's acceptance
criteria require does not depend on Wayback succeeding.

THIS SCRIPT WRITES PERMANENT REFERENCE DATA when run against a real CSV of
real candidates. Every capture it makes is a real campaign page for a real
candidacy, exactly like story 2.6's candidacies -- re-running it is expected
and safe (idempotent on the content-addressed `promise_sources` id), and
nothing it writes should be deleted afterwards.

Exit codes:
    0  the run completed (even if some rows were skipped)
    1  the input CSV could not be read, or a ZeroDB error stopped the run
       partway through
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.campaign_sites import (  # noqa: E402
    DEFAULT_MIN_INTERVAL_SECONDS,
    CampaignSiteError,
    CampaignSiteLoader,
)
from app.repositories.errors import RepositoryError  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402


def _pacer(min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS):
    """The same last-call-relative pacer every other load script inlines."""
    state = {"last": None}

    def pace() -> None:
        now = time.monotonic()
        last = state["last"]
        if last is not None:
            elapsed = now - last
            remaining = min_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        state["last"] = time.monotonic()

    return pace


def read_rows(path: Path) -> list[tuple[str, str]]:
    """`(candidacy_id, url)` pairs from the input CSV, header row required."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = {"candidacy_id", "url"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} is missing column(s): {sorted(missing)}")
        return [
            (row["candidacy_id"].strip(), row["url"].strip())
            for row in reader
            if row.get("candidacy_id", "").strip()
        ]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        metavar="PATH",
        help="CSV of candidacy_id,url pairs to archive. See the module "
        "docstring for the format and why this is a curated list rather "
        "than an automated discovery step.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and validate the CSV but write nothing.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("archive_campaign_sites")
    args = parse_args(argv if argv is not None else sys.argv[1:])

    try:
        rows = read_rows(args.input)
    except (OSError, ValueError) as exc:
        logger.error("could not read %s: %s", args.input, exc)
        return 1

    logger.info("read %d candidacy/url pair(s) from %s", len(rows), args.input)
    if args.dry_run:
        for candidacy_id, url in rows:
            logger.info("[dry-run] would archive %s <- %s", candidacy_id, url)
        return 0

    if not os.environ.get("WAYBACK_ACCESS_KEY"):
        logger.info(
            "WAYBACK_ACCESS_KEY not set; Internet Archive Save Page Now submission "
            "will be skipped this run (best-effort only -- see "
            "app.ingest.campaign_sites's module docstring). HTML/PDF archival to "
            "ZeroDB Files does not depend on this."
        )

    client = ZeroDBClient.from_env()
    loader = CampaignSiteLoader(client, pace=_pacer())

    archived = 0
    skipped: list[tuple[str, str]] = []
    for candidacy_id, url in rows:
        try:
            report = loader.capture(candidacy_id=candidacy_id, url=url)
        except (ValueError, CampaignSiteError, RepositoryError) as exc:
            skipped.append((candidacy_id, str(exc)))
            logger.warning("skipping %s (%s): %s", candidacy_id, url, exc)
            continue
        archived += 1
        status = "new capture" if report.is_new_capture else "already archived this cycle"
        logger.info(
            "%s: %s (%s), promise_source=%s, wayback=%s",
            candidacy_id,
            status,
            url,
            report.promise_source_id,
            "ok" if report.wayback.ok else "not recorded",
        )

    logger.info("archived %d/%d candidacy site(s); %d skipped", archived, len(rows), len(skipped))
    for candidacy_id, reason in skipped:
        logger.info("  skipped %s: %s", candidacy_id, reason)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ZeroDBError as exc:
        logging.getLogger("archive_campaign_sites").error("ZeroDB error: %s", exc)
        sys.exit(1)
