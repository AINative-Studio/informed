#!/usr/bin/env python3
"""CLI entrypoint for story 3.4's topic-and-stance embedding pipeline.

    python scripts/embed_taxonomy.py

Embeds every live `issue_topics` row into the `topics` vector namespace and
every live `stances` row into the `stances` namespace, idempotently: running
this twice with nothing changed embeds nothing a second time, and a row whose
`taxonomy_version`/`wording_version` has moved on since the last run is
rebuilt (its stale vector deleted, a fresh one written) rather than left stale
or duplicated.

**Run `scripts/seed_taxonomy.py` and `scripts/seed_stances.py` first.** This
module only reads `issue_topics`/`stances`; it writes nothing to either table,
and there is nothing to embed from an empty one.

The vectors it writes are PERMANENT production reference data, not fixtures:
story 3.5's free-text interest matching and the promise/roll-call classifier
both depend on every topic and stance being embedded. Do not delete them.

Reads ZeroDB credentials from the environment (`ZERODB_API_KEY`,
`ZERODB_PROJECT_ID`, …) via `ZeroDBClient.from_env()`, exactly like every other
piece of Informed's backend. Never pass a credential on the command line.

All the logic lives in `app.embed_taxonomy` so it is covered by the Gherkin
feature and unit tests against the in-memory fake; this file is intentionally
thin and has no logic of its own to assert on.

Exit codes:
    0  the embedding pipeline ran to completion (whether or not it wrote
       anything new)
    1  a ZeroDB error stopped the run partway through
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow `python scripts/embed_taxonomy.py` to find the `app` package without
# the caller having to set PYTHONPATH themselves, matching every other script
# in this directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.embed_taxonomy import STANCES_NAMESPACE, TOPICS_NAMESPACE, TaxonomyEmbedder  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("embed_taxonomy")

    client = ZeroDBClient.from_env()
    try:
        logger.info(
            "Embedding issue_topics into %r and stances into %r for project %r. "
            "Each row costs a listing read plus up to one delete and one embed call, "
            "paced, so expect several minutes on a first run.",
            TOPICS_NAMESPACE,
            STANCES_NAMESPACE,
            client.project_id,
        )
        # No `pace` argument: `TaxonomyEmbedder` defaults to the real pacer,
        # same reasoning as `TaxonomySeeder`/`StanceSeeder`.
        topics_report, stances_report = TaxonomyEmbedder(client).run()
    except ZeroDBError as exc:
        logger.error("Embedding pipeline failed: %s", exc)
        return 1
    finally:
        client.close()

    logger.info(
        "topics: %d current (%d newly written, %d rebuilt, %d already up to date)",
        topics_report.row_count,
        len(topics_report.written) - len(topics_report.rebuilt),
        len(topics_report.rebuilt),
        len(topics_report.skipped),
    )
    logger.info(
        "stances: %d current (%d newly written, %d rebuilt, %d already up to date)",
        stances_report.row_count,
        len(stances_report.written) - len(stances_report.rebuilt),
        len(stances_report.rebuilt),
        len(stances_report.skipped),
    )
    logger.info(
        "These vectors are permanent reference data, not test fixtures: do not delete them."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
