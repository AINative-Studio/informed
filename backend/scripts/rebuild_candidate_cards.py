#!/usr/bin/env python3
"""CLI entrypoint for story 7.2's manual `candidate_cards` rebuild.

    python scripts/rebuild_candidate_cards.py                  # every candidacy
    python scripts/rebuild_candidate_cards.py --candidacy <id> # just one

The read model is normally rebuilt by events — `app.scheduling.jobs`'
`rebuild_candidate_cards` job polls `verdict.published` and
`positions.recomputed` every 2.5 minutes. This script exists for the two cases
an event-driven rebuild cannot cover:

  * **A cold start.** A card appears when an event about its candidate
    arrives, so a project whose candidacies were ingested before this story
    shipped has candidacies and no cards. There is no event to replay for
    them, because the positions and verdicts they would summarise predate the
    consumer.
  * **A repair.** A card is derived data: if one is wrong, restating it from
    the source tables is the fix, and waiting for the candidate's next
    recompute is not.

**It is a script and not a startup hook, deliberately**, for the reason every
other load in this codebase is: an unattended full-table write on boot is how
a deployment surprises its operator, and this one writes a row per candidacy.

**What a rebuilt card will and will not contain today.** The honest answer,
because running this against the live project and seeing null fields should
not look like a bug:

  * `person_name`, `party`, `district`, `photo_url` — populated, from
    `candidacies`, `districts` and `people`.
  * `positions_summary`, `methodology_version` — populated only for a
    candidate with current `candidate_positions` rows. The live project holds
    **zero** (measured 2026-10-19): story 6.2's scorer matches a vote to a
    stance through `member_votes.topic_ids`, and story 5.5's classifier
    measured 54.2% precision against an 85% bar and therefore accepts nothing
    automatically, so no roll call carries a topic yet. Null here is the
    correct answer, not a gap in this script.
  * `verdict_counts`, `say_do_gap` — null for every candidacy, because
    `verdicts` holds zero live rows: epic 11's verdict engine is unbuilt.

So on today's live data this script writes the identity half of each card and
honestly leaves the computed half absent. It becomes more interesting with
every epic that lands, and needs no change to do so.

Reads ZeroDB credentials from the environment (`ZERODB_API_KEY`,
`ZERODB_PROJECT_ID`, …) via `ZeroDBClient.from_env()`, exactly like every
other piece of Informed's backend — this script does not open `local.env`
itself; the shell that runs it does (see docs/environments.md). Never pass a
credential on the command line: it would land in shell history and in `ps`
output on a shared host.

All the logic lives in `app.ballot.cards` so it is covered by the Gherkin
feature and the unit tests against the in-memory fake; this file is
intentionally thin and has no logic of its own to assert on.

Exit codes:
    0  every card asked for was rebuilt
    1  a ZeroDB error stopped the run partway through
    3  the run finished but some candidacies were skipped (see the log)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

# Allow `python scripts/rebuild_candidate_cards.py` to find the `app` package
# without the caller having to set PYTHONPATH, matching `scripts/bootstrap.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ballot.cards import CandidateCardBuilder, RebuildResult  # noqa: E402
from app.repositories import Repository  # noqa: E402
from app.repositories.models import Candidacy  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

#: Rows of `candidacies` one page asks for. The live table holds 3 rows today
#: and will hold a few thousand once story 2.6's FEC load has a key, which is
#: still one or two pages — and a page is still a page.
PAGE_SIZE = 500

#: Minimum seconds between writes. The live project was observed to throttle
#: at ~60 requests per minute (story 1.2), and a rebuild is several reads plus
#: one write per candidacy, so this paces the loop rather than the request.
#: The same last-call-relative pacer every load script in this codebase
#: inlines; see `scripts/load_candidacies.py` for why it is inlined.
MIN_SECONDS_BETWEEN_CARDS = 1.1


def _pacer(min_interval_seconds: float) -> Callable[[], None]:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidacy",
        help="Rebuild one candidacy's card by its id, instead of every candidacy.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("rebuild_candidate_cards")

    client = ZeroDBClient.from_env()
    pace = _pacer(MIN_SECONDS_BETWEEN_CARDS)
    builder = CandidateCardBuilder(client)
    rebuilt: list[str] = []
    skipped: list[str] = []

    try:
        if args.candidacy:
            candidacy_ids = [args.candidacy]
            logger.info("Rebuilding 1 card in project %r.", client.project_id)
        else:
            candidacy_ids = _all_candidacy_ids(client)
            logger.info(
                "Rebuilding %d card(s) in project %r, paced at ~1/s.",
                len(candidacy_ids),
                client.project_id,
            )

        for candidacy_id in candidacy_ids:
            pace()
            result: RebuildResult = builder.rebuild_candidacy(candidacy_id)
            rebuilt.extend(result.candidacy_ids)
            skipped.extend(result.skipped)
    except ZeroDBError as exc:
        logger.error("Card rebuild failed: %s", exc)
        return 1
    finally:
        client.close()

    logger.info(
        "candidate_cards rebuilt: %d card(s), %d skipped. Computed fields "
        "(verdict_counts, say_do_gap, positions_summary) are absent where "
        "nothing has computed them — see this script's docstring for which, "
        "and why that is the correct answer rather than a gap.",
        len(rebuilt),
        len(skipped),
    )
    for candidacy_id in skipped:
        logger.warning("skipped candidacy %s; see the errors above for why", candidacy_id)
    return 3 if skipped else 0


def _all_candidacy_ids(client: ZeroDBClient) -> list[str]:
    """Every candidacy id, paged.

    Sorted so a run's order is reproducible — which matters only for readable
    logs when a rebuild is interrupted halfway, but costs nothing. Live
    ZeroDB's row order is unspecified (story 1.4).
    """
    repository: Repository[Candidacy] = Repository(client=client, model=Candidacy)
    ids: list[str] = []
    skip = 0
    while True:
        page = repository.query(limit=PAGE_SIZE, skip=skip)
        ids.extend(row.id for row in page if row.id)
        if len(page) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
    return sorted(set(ids))


if __name__ == "__main__":
    raise SystemExit(main())
