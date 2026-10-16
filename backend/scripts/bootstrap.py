#!/usr/bin/env python3
"""CLI entrypoint for story 1.3's idempotent ZeroDB bootstrap.

    python scripts/bootstrap.py

Reads ZeroDB credentials from the environment (`ZERODB_API_KEY`,
`ZERODB_PROJECT_ID`, …) via `ZeroDBSettings` / `ZeroDBClient.from_env()`,
exactly like every other piece of Informed's backend — this script does not
open `local.env` itself; the shell that runs it does (see
docs/environments.md). Never pass a credential on the command line: it would
land in shell history and in `ps` output on a shared host.

All the logic lives in `app.bootstrap` so it is covered by the Gherkin
features and unit tests in `backend/tests/` against the in-memory fake; this
file is intentionally thin and untested beyond "it imports and wires
correctly" — it has no logic of its own to assert on.

Exit codes:
    0  bootstrap completed (whether or not anything needed creating)
    1  a ZeroDB error stopped the run partway through
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow `python scripts/bootstrap.py` to find the `app` package without the
# caller having to set PYTHONPATH themselves, matching how `pyproject.toml`'s
# `pythonpath = ["."]` already lets pytest find it from the backend root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap import Bootstrapper  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("bootstrap")

    client = ZeroDBClient.from_env()
    try:
        logger.info("Starting ZeroDB bootstrap for project %r", client.project_id)
        report = Bootstrapper(client).run()
    except ZeroDBError as exc:
        logger.error("Bootstrap failed: %s", exc)
        return 1
    finally:
        client.close()

    if report.is_noop:
        logger.info(
            "Bootstrap is a no-op: everything already exists at schema version %s.",
            report.schema_version,
        )
    else:
        logger.info(
            "Bootstrap complete. Tables created: %d (skipped %d). "
            "Namespaces indexed: %d (skipped %d). Ontology registered: %s "
            "(already current: %s). Schema version %s (changed: %s).",
            len(report.tables_created),
            len(report.tables_skipped),
            len(report.namespaces_indexed),
            len(report.namespaces_skipped),
            report.ontology_registered,
            report.ontology_already_current,
            report.schema_version,
            report.schema_version_changed,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
