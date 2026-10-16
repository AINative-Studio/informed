#!/usr/bin/env python3
"""CLI entrypoint for story 3.2's stance-card seed.

    python scripts/seed_stances.py

Writes every balanced stance card (201 rows across story 3.1's 60 subtopics,
`wording_version` 1) into `stances`, idempotently. Running it twice is safe and
leaves exactly one row per stance id.

**Run `scripts/seed_taxonomy.py` first.** `Stance.topic_id` is a declared
reference, so every card's write resolves its subtopic out of `issue_topics`
before it is stored (datamodel.md rule 5). Against a project with no taxonomy
the first card is refused and the run stops — deliberately, because a stance
pointing at a topic that does not exist is unusable and 201 orphans is a worse
outcome than an immediate failure. The order between the two seeds is the one
ordering constraint in this story; `app.seed_stances.stance_rows()` has no
internal ordering requirement of its own, unlike 3.1's parents-before-children.

**It is a separate command from `scripts/bootstrap.py` on purpose**, for the
same reason 3.1's seed is: 201 upserts, each a find-then-write, against a
project observed to throttle at 60 requests per minute would add minutes to
every bootstrap — including the no-op re-runs — to restate editorial content
that changes only when the advisory panel revises wording. See
`app/seed_stances.py`'s module docstring.

The rows it writes are PERMANENT production reference data, not fixtures. A
voter's encrypted profile stores `stance_id`s from this corpus, so deleting a
row orphans that profile, every `candidate_positions` row computed against it
and every promise classified under it. Do not delete them.

Reads ZeroDB credentials from the environment (`ZERODB_API_KEY`,
`ZERODB_PROJECT_ID`, …) via `ZeroDBClient.from_env()`, exactly like every other
piece of Informed's backend — this script does not open `local.env` itself; the
shell that runs it does (see docs/environments.md). Never pass a credential on
the command line: it would land in shell history and in `ps` output on a shared
host.

All the logic lives in `app.seed_stances` so it is covered by the Gherkin
feature and unit tests against the in-memory fake; this file is intentionally
thin and has no logic of its own to assert on.

Exit codes:
    0  the stance cards were seeded (whether or not any stored value changed)
    1  a ZeroDB error stopped the run partway through
    2  a card's subtopic is missing — run scripts/seed_taxonomy.py first
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow `python scripts/seed_stances.py` to find the `app` package without the
# caller having to set PYTHONPATH themselves, matching `scripts/bootstrap.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.repositories import MissingReferenceError  # noqa: E402
from app.seed_stances import (  # noqa: E402
    MINIMUM_STANCES_PER_SUBTOPIC,
    STANCES,
    WORDING_VERSION,
    StanceSeeder,
)
from app.seed_taxonomy import EXPECTED_SUBTOPIC_COUNT  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("seed_stances")

    client = ZeroDBClient.from_env()
    try:
        logger.info(
            "Seeding %d stance card(s) at wording_version %d across %d subtopics "
            "(at least %d each) into project %r. ~%d paced writes, so expect "
            "several minutes.",
            len(STANCES),
            WORDING_VERSION,
            EXPECTED_SUBTOPIC_COUNT,
            MINIMUM_STANCES_PER_SUBTOPIC,
            client.project_id,
            len(STANCES),
        )
        # No `pace` argument: `StanceSeeder` defaults to the real pacer, so this
        # CLI cannot accidentally run 201 unpaced writes at the live project's
        # 60 req/min ceiling.
        report = StanceSeeder(client).run()
    except MissingReferenceError as exc:
        logger.error(
            "Stance seed failed: %s. The issue taxonomy is not seeded in this "
            "project — run `python scripts/seed_taxonomy.py` first.",
            exc,
        )
        return 2
    except ZeroDBError as exc:
        logger.error("Stance seed failed: %s", exc)
        return 1
    finally:
        client.close()

    logger.info(
        "Stance cards seeded: %d row(s) across %d subtopic(s) at wording_version "
        "%d. These rows are permanent reference data, not test fixtures: a voter's "
        "saved profile cites these stance ids. Do not delete them.",
        report.row_count,
        report.topics_covered,
        WORDING_VERSION,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
