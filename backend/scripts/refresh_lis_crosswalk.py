#!/usr/bin/env python3
"""CLI entrypoint for story 8.2's LIS-to-bioguide crosswalk refresh.

    python scripts/refresh_lis_crosswalk.py
    python scripts/refresh_lis_crosswalk.py --check-only

Fetches the live roster (`legislators-current.yaml`), works out which records
are sitting senators, and writes each one's own LIS member id onto their
`people` row under `external_ids["lis"]` -- the crosswalk story 8.1's Senate
roll-call ingest needs to join `member_votes` to `people`, and that stories
8.3/8.4/8.5 read back with `app.ingest.lis_crosswalk.load_crosswalk`.

**Needs NO API key**, same as `scripts/load_senate_rollcalls.py`: the roster
source is public and keyless. See `app.ingest.lis_crosswalk`'s module
docstring for the four candidate sources this story actually fetched and why
this is the only one publishing both ids (senate.gov's own `senators_cfm.xml`
carries bioguide and no LIS id; Congress.gov's member API carries no LIS id
either).

`--check-only` measures the acceptance criterion ("100% of sitting senators
map") against the crosswalk currently in ZeroDB and writes nothing. That is
the mode a monitoring job or a pre-ingest gate should run: it exits 1 when
coverage is incomplete, naming every unmapped senator, and prints the
`lis_crosswalk_*` Prometheus gauges for a scrape.

Reads ZeroDB credentials from the environment via `ZeroDBClient.from_env()`,
exactly like every other script here -- this script never opens `local.env`
itself; the shell that runs it does (docs/environments.md).

This script WRITES PERMANENT REFERENCE DATA: each sitting senator's own
public LIS identifier, onto their own `people` row, merged into whatever
`external_ids` story 2.2's roster load already wrote there. Re-running it is
expected and safe (idempotent upsert by `person:{bioguide}`, and a row whose
mapping is already correct is not rewritten at all), and nothing it writes
should be deleted afterwards.

A sitting senator with no `people` row is reported, never invented: story
2.2's `scripts/load_legislators.py` is what creates those rows, and
conjuring a thin one here would hide an out-of-date roster behind a
crosswalk that looks complete.

Exit codes:
    0  the refresh (or check) completed and 100% of sitting senators map
    1  coverage is incomplete, the source could not be fetched/parsed, or a
       ZeroDB error stopped the run partway through
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.lis_crosswalk import (  # noqa: E402
    DEFAULT_MIN_INTERVAL_SECONDS,
    LisCrosswalkSourceError,
    LisCrosswalkWriter,
    check_coverage,
    fetch_roster,
    load_crosswalk,
    render_coverage_prometheus_text,
    sitting_senators_from_roster,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402


def _pacer(min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS):
    """The same last-call-relative pacer every load script in this codebase
    inlines; see `scripts/load_house_rollcalls.py`'s own copy for why it is
    inlined rather than imported.
    """
    state: dict[str, float | None] = {"last": None}

    def pace() -> None:
        now = time.monotonic()
        last = state["last"]
        if last is not None:
            remaining = min_interval_seconds - (now - last)
            if remaining > 0:
                time.sleep(remaining)
        state["last"] = time.monotonic()

    return pace


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Measure coverage against the crosswalk already in ZeroDB and write "
        "nothing. Exits 1 when coverage is below 100%%.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("refresh_lis_crosswalk")
    args = parse_args(argv if argv is not None else sys.argv[1:])

    try:
        client = ZeroDBClient.from_env()
    except Exception as exc:  # noqa: BLE001 - a missing credential is a usage error
        logger.error("could not build a ZeroDB client: %s", exc)
        logger.error("source local.env into the shell first: set -a; . local.env; set +a")
        return 1

    try:
        logger.info("fetching the live roster")
        senators = sitting_senators_from_roster(fetch_roster())
        logger.info("roster names %d sitting senators", len(senators))

        if not args.check_only:
            report = LisCrosswalkWriter(client, pace=_pacer()).refresh(senators)
            logger.info(
                "refresh: %d mapped, %d with no LIS id in the roster, %d with no people row",
                len(report.mapped),
                len(report.without_lis_id),
                len(report.missing_people_rows),
            )
            if report.missing_people_rows:
                logger.error(
                    "no people row for %d sitting senator(s): %s -- run "
                    "scripts/load_legislators.py (story 2.2) first",
                    len(report.missing_people_rows),
                    report.missing_people_rows,
                )

        coverage = check_coverage(load_crosswalk(client), senators)
        logger.info(
            "coverage: %d of %d sitting senators map (%.1f%%)",
            coverage.mapped_count,
            coverage.sitting_count,
            coverage.percentage,
        )
        print(render_coverage_prometheus_text(coverage), end="")  # noqa: T201

        if not coverage.is_complete:
            logger.error(
                "COVERAGE INCOMPLETE -- story 8.2's acceptance criterion is 100%%. Unmapped: %s",
                coverage.unmapped_person_ids,
            )
            return 1
        # No `%`-style args, so this format string is used verbatim -- a
        # doubled `%%` would render literally here, unlike in the lines above.
        logger.info("100% of sitting senators map")
        return 0

    except LisCrosswalkSourceError as exc:
        logger.error("roster source error: %s", exc)
        return 1
    except ZeroDBError as exc:
        logger.error("ZeroDB error: %s", exc)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
