#!/usr/bin/env python3
"""CLI entrypoint for the scheduled-job runner (Epic 16 infra).

    python scripts/run_scheduled_jobs.py

This is the one command stories 2.6, 5.1/5.6 and 12.4 all independently
flagged as missing: it knows every periodic job this codebase needs
(`app.scheduling.jobs.default_jobs`), checks each one's stored last-run
timestamp against its own cadence, runs whichever are due, and reports what
happened — safe to invoke from ANY real scheduler (a system cron entry, a
Railway cron service, the GitHub Actions `schedule:` trigger wired up in
`.github/workflows/scheduled-jobs.yml`) with no per-job awareness on that
scheduler's part. See `app.scheduling.registry`'s module docstring for the
full design argument; this file is intentionally thin, the same way
`scripts/bootstrap.py` is — all the logic it calls into is covered by
`backend/tests/features/scheduled_jobs.feature` and
`backend/tests/test_scheduled_jobs_unit.py` against the in-memory fake.

Reads ZeroDB credentials from the environment via `ZeroDBClient.from_env()`,
exactly like every other script here — this script never opens `local.env`
itself; the shell that runs it does (docs/environments.md). The two ingest
jobs additionally need `FEC_API_KEY` / `CONGRESS_GOV_API_KEY` to actually run;
neither exists in this environment today (see `app.ingest.candidacies`'s and
`app.ingest.house_rollcalls`'s module docstrings), so expect both to report
BLOCKED rather than RAN until a real key is configured — that is correct,
documented behaviour, not a bug in this script.

Never pass a credential on the command line: it would land in shell history
and in `ps` output on a shared host.

Exit codes:
    0  every due job ran successfully (jobs that were not due, or that are
       structurally blocked on a missing credential, do not affect this)
    1  at least one due job raised and failed, or the registry's own
       bookkeeping calls hit a ZeroDB error
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.observability.logging_config import configure_logging  # noqa: E402
from app.scheduling.jobs import default_jobs  # noqa: E402
from app.scheduling.registry import JobStatus, ScheduledJobRunner  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402


def main() -> int:
    # Structured JSON logs (story 16.5), consistently with the API process —
    # a scheduled run's own "job X failed" line is as useful to an
    # aggregator as a request log line, and gets the same plaintext-leak
    # guard for free. `LOG_LEVEL` matches `.env.example`'s convention.
    configure_logging(level=os.getenv("LOG_LEVEL", "INFO").upper())
    logger = logging.getLogger("run_scheduled_jobs")

    client = ZeroDBClient.from_env()
    try:
        logger.info("running scheduled jobs against project %r", client.project_id)
        report = ScheduledJobRunner(client, default_jobs()).run()
    except ZeroDBError as exc:
        logger.error("could not run the scheduled-job registry: %s", exc)
        return 1
    finally:
        client.close()

    for outcome in report.outcomes:
        if outcome.status is JobStatus.RAN:
            logger.info("%s: RAN (%s)", outcome.name, outcome.detail)
        elif outcome.status is JobStatus.SKIPPED_NOT_DUE:
            logger.info("%s: skipped, not due yet (%s)", outcome.name, outcome.detail)
        elif outcome.status is JobStatus.BLOCKED:
            logger.warning("%s: BLOCKED — %s", outcome.name, outcome.detail)
        else:
            logger.error("%s: FAILED — %s", outcome.name, outcome.detail)

    logger.info("run complete: %s", report.summary())

    if report.failed:
        logger.error(
            "%d job(s) failed; see above. Jobs that are merely BLOCKED on a "
            "missing credential are expected today (see this script's module "
            "docstring) and do not by themselves cause this exit code.",
            len(report.failed),
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
