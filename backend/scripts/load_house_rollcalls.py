#!/usr/bin/env python3
"""CLI entrypoint for story 5.1's House roll-call loader.

    CONGRESS_GOV_API_KEY=... python scripts/load_house_rollcalls.py \\
        --congress 119 --session 2 --limit 20

Fetches House roll-call votes from the real, live Congress.gov API v3
house-vote endpoints (`app.ingest.house_rollcalls`), transforms them into
`roll_calls` rows per datamodel.md, and upserts every one through the story
1.4 repository layer, writing a `provenance_log` entry alongside each one,
paced against the ~60 req/min ceiling datamodel.md records.

**This script's blocker is cleared.** Story 5.1 shipped it with "THIS SCRIPT
HAS NOT BEEN RUN AGAINST THE LIVE API" at the top, for want of a
`CONGRESS_GOV_API_KEY`. A real key exists as of 2026-10-19 and the pipeline
this script drives has been run live (stories 5.6/5.7/5.8) -- which is also
how the two silent transform defects story 5.1 could not have found were
found and fixed. See `app/ingest/house_rollcalls.py`'s module docstring.

`--limit` defaults to a SMALL bounded sample deliberately: this script's job
is to prove the pipeline works end to end on real data, not to backfill every
roll call since the 118th Congress in one run. The full backfill now exists
as its own script -- `scripts/backfill_house_rollcalls.py` (story 5.8), which
pages, resumes and reconciles against the source's own totals.

Reads ZeroDB credentials from the environment via `ZeroDBClient.from_env()`,
exactly like `scripts/load_legislators.py` -- this script never opens
`local.env` itself; the shell that runs it does (docs/environments.md). Never
pass either credential on the command line as a literal where it could be
logged; export it in the shell instead.

This script WRITES PERMANENT REFERENCE DATA, like story 2.1's seed and story
2.2's legislator load: every roll call it loads is a real, recorded House
vote. Re-running it is expected and safe (idempotent upsert on `roll_calls`
by id; content-addressed, insert-if-absent writes on `provenance_log`), and
nothing it writes should be deleted afterwards.

Exit codes:
    0  the load completed (even if some source records were skipped)
    1  the source could not be fetched/parsed, no CONGRESS_GOV_API_KEY was
       set, or a ZeroDB error stopped the run partway through
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.house_rollcalls import (  # noqa: E402
    DEFAULT_MIN_INTERVAL_SECONDS,
    EARLIEST_CONGRESS,
    HouseRollCallLoader,
    HouseRollCallSourceError,
    fetch_roll_call_item,
    fetch_roll_call_numbers,
    transform_roll_calls,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402


def _pacer(min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS):
    """The same last-call-relative pacer `scripts/load_legislators.py` uses,
    inlined rather than imported for the same reason that script inlines its
    own copy: this is a CLI script's own concern, not shared library code.
    """
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--congress",
        type=int,
        default=119,
        help=f"Congress number to load (>= {EARLIEST_CONGRESS}, the 118th; default: 119)",
    )
    parser.add_argument(
        "--session", type=int, default=2, choices=(1, 2), help="Session number (1 or 2)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="How many of the most recently listed roll calls to fetch and load "
        "(default: 20 -- a bounded sample, not a full backfill; see story 5.8 for that)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("load_house_rollcalls")
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.congress < EARLIEST_CONGRESS:
        logger.error(
            "congress %d is before this story's coverage (118th Congress onward)", args.congress
        )
        return 1

    api_key = os.environ.get("CONGRESS_GOV_API_KEY")
    if not api_key:
        logger.error(
            "CONGRESS_GOV_API_KEY is not set. Get a free key at "
            "https://api.data.gov/signup/ and export it before running this script. "
            "See app/ingest/house_rollcalls.py's module docstring: no real key was "
            "available during story 5.1, so this script is untested against the live "
            "API -- this is exactly the blocker it will hit without a key."
        )
        return 1

    try:
        logger.info("listing roll calls for congress=%d session=%d", args.congress, args.session)
        numbers = fetch_roll_call_numbers(
            congress=args.congress, session=args.session, api_key=api_key
        )
        logger.info(
            "source lists %d roll call(s); loading the %d most recent", len(numbers), args.limit
        )
        sample = sorted(numbers, reverse=True)[: args.limit]

        pace = _pacer()
        envelopes = []
        for roll_number in sample:
            pace()
            envelopes.append(
                fetch_roll_call_item(
                    congress=args.congress,
                    session=args.session,
                    roll_call_number=roll_number,
                    api_key=api_key,
                )
            )
        rows, skipped = transform_roll_calls(envelopes)
        if skipped:
            logger.warning(
                "%d source record(s) could not be transformed and were skipped: %s",
                len(skipped),
                ", ".join(f"{s.roll_call_number or '<unknown>'} ({s.reason})" for s in skipped),
            )
    except HouseRollCallSourceError as exc:
        logger.error("could not load the source: %s", exc)
        return 1

    client = ZeroDBClient.from_env()
    try:
        logger.info(
            "loading %d roll call(s) into project %r (paced at >= %.1fs/call)",
            len(rows),
            client.project_id,
            DEFAULT_MIN_INTERVAL_SECONDS,
        )
        report = HouseRollCallLoader(client, pace=_pacer()).load(rows)
    except ZeroDBError as exc:
        logger.error("load failed: %s", exc)
        return 1
    finally:
        client.close()

    logger.info(
        "load complete: %d roll call(s) written, %d new provenance entry(ies), "
        "%d source record(s) skipped. This is permanent reference data "
        "(like story 2.1's seed and story 2.2's legislator load) -- do not delete it.",
        report.row_count,
        len(report.provenance_written),
        len(skipped),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
