#!/usr/bin/env python3
"""CLI entrypoint for story 3.1's issue-taxonomy seed.

    python scripts/seed_taxonomy.py

Writes the en-US issue taxonomy (20 topics, 60 subtopics, `taxonomy_version`
1) into `issue_topics`, idempotently. Run it after `scripts/bootstrap.py`, which
is what creates the table; running it twice is safe and leaves exactly one row
per topic id.

**It is a separate command from `scripts/bootstrap.py` on purpose.** Story
2.1's three reference rows are seeded by the bootstrap itself because
bootstrap's own acceptance criterion names them and they cost three writes;
this is ~80 writes against a project observed to throttle at 60 requests per
minute, so folding it in would add minutes to every bootstrap — including the
no-op re-runs — to restate editorial content that changes only when the
advisory panel revises wording. See `app/seed_taxonomy.py`'s module docstring.

The rows it writes are PERMANENT production reference data, not fixtures: every
voter interest, stance card and alignment score cites a topic id. Do not delete
them.

Reads ZeroDB credentials from the environment (`ZERODB_API_KEY`,
`ZERODB_PROJECT_ID`, …) via `ZeroDBClient.from_env()`, exactly like every other
piece of Informed's backend — this script does not open `local.env` itself; the
shell that runs it does (see docs/environments.md). Never pass a credential on
the command line: it would land in shell history and in `ps` output on a shared
host.

All the logic lives in `app.seed_taxonomy` so it is covered by the Gherkin
feature and unit tests against the in-memory fake; this file is intentionally
thin and has no logic of its own to assert on.

Exit codes:
    0  the taxonomy was seeded (whether or not any stored value changed)
    1  a ZeroDB error stopped the run partway through
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow `python scripts/seed_taxonomy.py` to find the `app` package without the
# caller having to set PYTHONPATH themselves, matching `scripts/bootstrap.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.seed_taxonomy import (  # noqa: E402
    EXPECTED_SUBTOPIC_COUNT,
    EXPECTED_TOPIC_COUNT,
    TAXONOMY_LOCALE,
    TAXONOMY_VERSION,
    TaxonomySeeder,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("seed_taxonomy")

    client = ZeroDBClient.from_env()
    try:
        logger.info(
            "Seeding the %s issue taxonomy v%d (%d topics, %d subtopics) "
            "into project %r. ~%d paced writes, so expect a few minutes.",
            TAXONOMY_LOCALE,
            TAXONOMY_VERSION,
            EXPECTED_TOPIC_COUNT,
            EXPECTED_SUBTOPIC_COUNT,
            client.project_id,
            EXPECTED_TOPIC_COUNT + EXPECTED_SUBTOPIC_COUNT,
        )
        # No `pace` argument: `TaxonomySeeder` defaults to the real pacer, so
        # this CLI cannot accidentally run 80 unpaced writes at the live
        # project's 60 req/min ceiling.
        report = TaxonomySeeder(client).run()
    except ZeroDBError as exc:
        logger.error("Taxonomy seed failed: %s", exc)
        return 1
    finally:
        client.close()

    logger.info(
        "Taxonomy seeded: %d row(s) — %d top-level topic(s), %d subtopic(s), "
        "locale %s, taxonomy_version %d. These rows are permanent reference "
        "data, not test fixtures: do not delete them.",
        report.row_count,
        len(report.topics_written),
        len(report.subtopics_written),
        TAXONOMY_LOCALE,
        TAXONOMY_VERSION,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
