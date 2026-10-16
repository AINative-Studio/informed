#!/usr/bin/env python3
"""CLI entrypoint for story 3.3's grandfathering backfill.

    python scripts/backfill_panel_review.py

Marks every stored `issue_topics` and `stances` row `grandfathered` in the
`panel_reviews` ledger: **served, and never panel-reviewed.** Run it once per
project, after `scripts/seed_taxonomy.py` and `scripts/seed_stances.py`, and
before anything starts reading the gated paths.

What `grandfathered` means, and why it is not `approved`
========================================================

prd.md requires stance cards and the issue taxonomy to be "reviewed by a
cross-partisan advisory panel before each release". Stories 3.1 and 3.2
published 80 topics and 201 stances before story 3.3 built the workflow that
makes that checkable, so those rows have never been through a panel — and
prd.md itself lists "who sits on the cross-partisan advisory panel, and how
members are chosen" as an **open question**, so no panel exists to have done
it.

Marking them `approved` would therefore be a false entry in the one table
whose entire value is that its claims are true. Leaving them unmarked would
make every gated read empty and take the product down. `grandfathered` is the
third option and the honest one: it passes the gate, it is explicitly not
approval, and `GET /v1/panel/queue` reports every one of these rows as still
owing a real review until a named panel member decides on it.

So this script creates a **standing, countable debt**, not a clean slate. That
is the point. `reviewer_id` on every row it writes is the literal
`informed:story-3.3-backfill` — deliberately not plausible as a person's name,
so anyone reading the ledger can tell a migration's mark from a panel member's
decision at a glance.

Why these rows are PERMANENT
============================

Unlike a verification script's scratch rows, the decisions this writes are
meant to stay. They annotate permanent reference data (`app/seed_taxonomy.py`
and `app/seed_stances.py` both document why their corpora are permanent), they
are what keeps the gated reads serving, and they are the record of what was
published before the review workflow existed. Deleting them would un-publish
the whole taxonomy on any caller using the gated paths and would erase the
evidence that the corpus predates the panel. Do not clean them up.

Idempotent, and safe to re-run
==============================

Each decision's id is a sha256 over its own content (see
`app.panel_review.service.review_id`), so a second run lands on rows that
already exist and reports them as `already marked` rather than appending
duplicates. A re-run after a real panel pass does **not** downgrade anything: a
row already carrying a decision at its current version is skipped entirely, and
even if it were not, an approval is newer than this backfill's mark and wins.

Rate limiting
=============

281 rows, each an upsert costing a find and a write, against a project observed
to throttle at 60 requests per minute (datamodel.md "Open questions"). This
passes `pace=default_pacer()` explicitly — `PanelReviewService` defaults to a
no-op pacer because its other caller is a per-request HTTP handler, where a
deliberate sleep would be wrong. Expect roughly ten minutes for a cold run.

Reads ZeroDB credentials from the environment (`ZERODB_API_KEY`,
`ZERODB_PROJECT_ID`, …) via `ZeroDBClient.from_env()`, like every other piece
of Informed's backend — this script does not open `local.env` itself; the shell
that runs it does (see docs/environments.md). Never pass a credential on the
command line: it would land in shell history and in `ps` output on a shared
host.

All the logic lives in `app.panel_review.service` so it is covered by the
Gherkin feature and the unit tests against the in-memory fake; this file is
intentionally thin and has no logic of its own to assert on.

Exit codes:
    0  the corpus is marked (whether this run wrote anything or found it done)
    1  a ZeroDB error stopped the run partway through
    2  nothing is seeded in this project — run the two seed scripts first
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow `python scripts/backfill_panel_review.py` to find the `app` package
# without the caller setting PYTHONPATH, matching every other script here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap import default_pacer  # noqa: E402
from app.panel_review.service import BACKFILL_REVIEWER_ID, PanelReviewService  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("backfill_panel_review")

    client = ZeroDBClient.from_env()
    try:
        service = PanelReviewService(client, pace=default_pacer())
        logger.info(
            "Marking every stored issue_topics and stances row GRANDFATHERED in project "
            "%r, as %r. Grandfathered means SERVED and NEVER PANEL-REVIEWED — it is not "
            "approval, and every row marked here is reported by GET /v1/panel/queue as "
            "still owing a real cross-partisan review. Writes are paced for the live 60 "
            "req/min ceiling, so expect several minutes.",
            client.project_id,
            BACKFILL_REVIEWER_ID,
        )
        report = service.grandfather_existing_corpus()
    except ZeroDBError as exc:
        logger.error("Panel review backfill failed: %s", exc)
        return 1
    finally:
        client.close()

    if report.row_count == 0 and not report.already_marked:
        logger.error(
            "Nothing was marked and nothing was already marked, which means this project "
            "has no issue_topics and no stances rows. Run `python "
            "scripts/seed_taxonomy.py` and then `python scripts/seed_stances.py` first."
        )
        return 2

    logger.info(
        "Panel review backfill complete: %d topic(s) and %d stance(s) newly marked "
        "grandfathered, %d row(s) already carried a decision at their current version. "
        "These ledger rows are PERMANENT — they are what keeps the gated reads serving, "
        "and they are the record that this corpus predates the review workflow. Do not "
        "delete them. %d row(s) now owe a real panel review.",
        len(report.topics_marked),
        len(report.stances_marked),
        len(report.already_marked),
        report.row_count + len(report.already_marked),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
