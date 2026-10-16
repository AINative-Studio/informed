#!/usr/bin/env python3
"""CLI entrypoint for story 2.2's legislator loader.

    python scripts/load_legislators.py

Fetches the real, live `legislators-current.yaml` from
unitedstates/congress-legislators, transforms it into `people` rows per
datamodel.md, and upserts every one through the story 1.4 repository layer,
paced against the ~60 req/min ceiling story 1.2 measured live (see
`app.ingest.legislators.LegislatorLoader`).

Reads ZeroDB credentials from the environment via `ZeroDBSettings` /
`ZeroDBClient.from_env()`, exactly like `scripts/bootstrap.py` -- this script
never opens `local.env` itself; the shell that runs it does
(docs/environments.md). Never pass a credential on the command line.

This script WRITES PERMANENT REFERENCE DATA. Every person it loads is a real,
currently sitting member of Congress, exactly like story 2.1's seeded
jurisdiction and chambers -- re-running this script is expected and safe
(idempotent upsert by bioguide id), and nothing it writes should be deleted
afterwards.

Exit codes:
    0  the load completed (even if some source records were skipped)
    1  the source could not be fetched/parsed, or a ZeroDB error stopped
       the run partway through
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.legislators import (  # noqa: E402
    DEFAULT_MIN_INTERVAL_SECONDS,
    LegislatorLoader,
    LegislatorSourceError,
    fetch_legislators_yaml,
    parse_legislators_yaml,
    transform_legislators,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402


def _pacer(min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS):
    """The same last-call-relative pacer `app.bootstrap` uses, inlined here
    rather than imported, because importing a private helper from another
    module's CLI script would couple two unrelated scripts' internals.
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


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("load_legislators")

    try:
        logger.info("fetching legislators-current.yaml")
        yaml_text = fetch_legislators_yaml()
        records = parse_legislators_yaml(yaml_text)
        logger.info("fetched %d source records", len(records))
        rows, skipped = transform_legislators(records)
        if skipped:
            logger.warning(
                "%d source record(s) could not be transformed and were skipped: %s",
                len(skipped),
                ", ".join(f"{s.bioguide or '<unknown>'} ({s.reason})" for s in skipped),
            )
    except LegislatorSourceError as exc:
        logger.error("could not load the source file: %s", exc)
        return 1

    client = ZeroDBClient.from_env()
    try:
        logger.info(
            "loading %d legislators into project %r (paced at >= %.1fs/call)",
            len(rows),
            client.project_id,
            DEFAULT_MIN_INTERVAL_SECONDS,
        )
        report = LegislatorLoader(client, pace=_pacer()).load(rows)
    except ZeroDBError as exc:
        logger.error("load failed: %s", exc)
        return 1
    finally:
        client.close()

    logger.info(
        "load complete: %d people written, %d source record(s) skipped. "
        "This is permanent reference data (like story 2.1's seed) -- do not delete it.",
        report.row_count,
        len(skipped),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
