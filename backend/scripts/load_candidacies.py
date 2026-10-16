#!/usr/bin/env python3
"""CLI entrypoint for story 2.6's candidacy loader.

    python scripts/load_candidacies.py                      # every state
    python scripts/load_candidacies.py --state PA --state DE
    python scripts/load_candidacies.py --dry-run            # fetch + transform only

Seeds the upcoming federal election (if it is not already there), fetches
federal House and Senate candidate filings from the live OpenFEC
`/v1/candidates/` endpoint for that election's year, transforms them into
`candidacies` rows per datamodel.md, and upserts every one through the story
1.4 repository layer -- creating the `districts` and `people` rows each
candidacy references as it goes. Paced against the ~60 req/min ZeroDB ceiling
story 1.2 measured live (see `app.ingest.candidacies.CandidacyLoader`).

Reads ZeroDB credentials from the environment via `ZeroDBSettings` /
`ZeroDBClient.from_env()`, exactly like `scripts/bootstrap.py` and the other
load scripts -- this script never opens `local.env` itself; the shell that runs
it does (docs/environments.md). Never pass a credential on the command line.

>>> AN FEC API KEY IS REQUIRED AND WAS NOT AVAILABLE DURING STORY 2.6 <<<

Set `FEC_API_KEY` to a free key from https://api.data.gov/signup/. The public
`DEMO_KEY` is accepted by OpenFEC and is rate-limited to **10 requests per
hour**, which is enough to inspect the schema (which story 2.6 did, live) and
nowhere near enough to load a cycle -- a national pull is ~110 state/office
queries plus paging. This script has therefore NOT been run end to end; see
`app/ingest/candidacies.py`'s module docstring and story 2.6's PR.

This script WRITES PERMANENT REFERENCE DATA. Every candidacy it loads is a
real federal candidate filing, exactly like story 2.1's seeded jurisdiction
and story 2.2's legislators -- re-running it is expected and safe (idempotent
upsert on deterministic ids, which is what makes the acceptance criterion's
"refreshed weekly" true), and nothing it writes should be deleted afterwards.

Exit codes:
    0  the load completed (even if some source records were skipped)
    1  no API key, the source could not be fetched/parsed, or a ZeroDB error
       stopped the run partway through
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.candidacies import (  # noqa: E402
    DEFAULT_MIN_INTERVAL_SECONDS,
    INGESTED_OFFICES,
    CandidacyLoader,
    CandidacySourceError,
    candidacies_from_results,
    fetch_candidates,
)
from app.seed_elections import UPCOMING_ELECTIONS, ElectionSeeder  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

#: Every state and territory that elects a voting member or delegate to
#: Congress, as the two-letter codes OpenFEC's `state` filter accepts. Issued
#: per state rather than as one national query so each response stays small
#: and one state's failure can be retried without re-fetching the other 55.
# fmt: off
# Kept as a hand-wrapped block: the formatter would otherwise put each of the
# 56 codes on its own line, turning a scannable list into three screens.
ALL_STATES: tuple[str, ...] = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "GU",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND",
    "MP", "OH", "OK", "OR", "PA", "PR", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "VI", "WA", "WV", "WI", "WY", "AS",
)
# fmt: on


def _pacer(min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS):
    """The same last-call-relative pacer the other load scripts use, inlined
    for the same reason they inline it: this is a CLI script's own concern,
    not shared library code.
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
        "--state",
        action="append",
        dest="states",
        metavar="XX",
        help="Two-letter state code to load; repeatable. Default: every state "
        "and territory that elects to Congress.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and transform but write nothing. Useful for confirming a "
        "key works and seeing the per-state counts before a real load.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("load_candidacies")
    args = parse_args(argv if argv is not None else sys.argv[1:])

    api_key = os.environ.get("FEC_API_KEY")
    if not api_key:
        logger.error(
            "FEC_API_KEY is not set. Get a free key at https://api.data.gov/signup/ "
            "and export it before running this script. The public DEMO_KEY is "
            "accepted by OpenFEC but allows only 10 requests/hour, which cannot "
            "complete even one state's load. See app/ingest/candidacies.py's "
            "module docstring: no real key was available during story 2.6, so this "
            "script has never been run end to end -- this is exactly the blocker "
            "it hits without a key."
        )
        return 1

    election = UPCOMING_ELECTIONS[0]
    election_year = int(str(election.election_date)[:4])
    states = [state.upper() for state in (args.states or ALL_STATES)]

    try:
        client = ZeroDBClient.from_env()
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator as a message
        logger.error("could not build a ZeroDB client from the environment: %s", exc)
        return 1

    pace = _pacer()

    if not args.dry_run:
        logger.info("seeding election %s", election.id)
        try:
            ElectionSeeder(client, pace=pace).run()
        except ZeroDBError as exc:
            logger.error("could not seed the election: %s", exc)
            return 1

    loader = CandidacyLoader(client, pace=pace)
    total_written = 0
    total_people = 0
    total_districts = 0
    total_skipped = 0

    for state in states:
        for office in INGESTED_OFFICES:
            try:
                pace()
                records = fetch_candidates(
                    election_year=election_year,
                    office=office,
                    state=state,
                    api_key=api_key,
                )
            except CandidacySourceError as exc:
                # One state/office failing must not abandon the other 110
                # pulls: the run is resumable and idempotent, so logging and
                # continuing leaves strictly more correct data than aborting.
                logger.error("could not fetch %s/%s: %s", state, office, exc)
                total_skipped += 1
                continue

            rows, skipped = candidacies_from_results(records, election=election)
            logger.info(
                "%s/%s: %d filing(s) fetched, %d row(s) to write, %d skipped",
                state,
                office,
                len(records),
                len(rows),
                len(skipped),
            )
            for skip in skipped:
                logger.info("  skipped %s: %s", skip.candidate_id or "<unknown>", skip.reason)
            total_skipped += len(skipped)

            if args.dry_run:
                continue

            try:
                report = loader.load(rows)
            except ZeroDBError as exc:
                logger.error("ZeroDB error while loading %s/%s: %s", state, office, exc)
                return 1
            total_written += report.row_count
            total_people += len(report.people_written)
            total_districts += len(report.districts_written)
            total_skipped += len(report.skipped)
            for skip in report.skipped:
                logger.warning(
                    "  could not write %s: %s", skip.candidate_id or "<unknown>", skip.reason
                )

    if args.dry_run:
        logger.info("dry run complete: nothing written; %d record(s) skipped", total_skipped)
        return 0

    logger.info(
        "load complete: %d candidacy row(s), %d new person row(s), "
        "%d new district row(s), %d skipped",
        total_written,
        total_people,
        total_districts,
        total_skipped,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
