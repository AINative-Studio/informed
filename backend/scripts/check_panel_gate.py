#!/usr/bin/env python3
"""The panel gate CI check (story 12.5).

backlog.md, story 12.5: "The CI check fails on unapproved wording versions."
This is that check, runnable two ways:

    python scripts/check_panel_gate.py            # --fake (the default)
    python scripts/check_panel_gate.py --live      # against real ZeroDB

`app.panel_review.gate.check_panel_gate` holds all the logic; this script is
the thin CLI wrapper around it, in the same spirit as every other
`scripts/*.py` entrypoint here (`scripts/backfill_panel_review.py`,
`scripts/run_scheduled_jobs.py`): no logic of its own to assert on, so the
Gherkin feature and the unit tests are where this gate's behaviour is proven.

=============================================================================
WHY THIS SCRIPT HAS TWO MODES, AND WHICH ONE RUNS IN GITHUB ACTIONS TODAY
=============================================================================

Story 12.5's acceptance criterion is about the real, deployed panel_reviews
ledger and the real, deployed issue_topics/stances corpus — the thing that
actually ships to voters. Checking THAT requires live ZeroDB credentials
(ZERODB_WORKSPACE_ID, ZERODB_PROJECT_ID, ZERODB_API_KEY), and this repo's CI
workflow has never had them: `.github/workflows/scheduled-jobs.yml`'s own
module docstring already documents that gap for the nightly integrity checks
and the ingest jobs, and nothing has changed since. An agent session cannot
create a GitHub Environment or set its secrets through the UI; a human with
repo admin access has to do both.

So `--live` is real, tested against the actual "informed" project during
this story's development (see this story's PR), and it is NOT what
`.github/workflows/ci.yml` runs on every push — it CANNOT be, with no
credentials to give it. What CI runs instead, every push and PR, with no
credentials required, is `--fake` (the default), in two parts run back to
back:

  1. **the self-check.** The exact same `check_panel_gate` function, run
     against an in-memory fake transport seeded with the real taxonomy and
     stance seeders, grandfathered exactly as
     `scripts/backfill_panel_review.py` would, PLUS one sabotage row written
     with no review at all — and this part is REQUIRED to report that
     sabotage row as a violation. If it does not, the script exits 1: that
     means the gate's failing path is broken (someone silently widened
     `GATE_PASSING_DECISIONS`, or broke the ledger/version match), and a
     broken gate that rubber-stamps everything is worse than no gate, so
     this fails loudly rather than quietly reporting a clean pass.
  2. **the real check**, against the same seeded-and-grandfathered fixture
     with the sabotage row removed again. THIS is the number CI's exit code
     reflects: a healthy build exits 0 here because the fixture, like the
     live corpus today, is fully grandfathered and nothing is actually
     unapproved.

Splitting these two is what makes `--fake` usable as an actual required CI
check rather than a step that is permanently and uninformatively red: a
script that always exits 1 (because it always contains a known-bad row)
would train everyone to ignore it, which is the opposite of what a release
gate is for. The self-check is the proof that failing still works; the real
check is the thing CI actually gates on.

This is "correctly built but currently vacuous against live data" in exactly
the sense `app.integrity.checks`'s module docstring uses that phrase for
checks 2 and 3 there: the logic is proven against synthetic data standing in
for a real violation, and it will run against the real ledger the day a
`prod` GitHub Environment with ZeroDB credentials exists — no code change
required, just `--live` instead of the default, which is exactly the shape
`scheduled-jobs.yml` already uses this script's `--live` mode for (see the
"wire it into CI" section of this story's PR).

=============================================================================
DEPLOYMENT-READINESS ITEM (same pattern as scheduled-jobs.yml's gap)
=============================================================================

Before this check can protect a REAL release (as opposed to proving its own
logic in CI), a human needs to:

  1. Set up a GitHub Environment (e.g. `prod`) carrying ZERODB_WORKSPACE_ID,
     ZERODB_PROJECT_ID and ZERODB_API_KEY as environment secrets.
  2. Add a workflow step (or a separate workflow) that runs
     `python scripts/check_panel_gate.py --live`, with those three secrets
     exported, on whatever trigger the team wants release-blocking checks
     on (a deploy workflow, a scheduled pre-release gate, or a manual
     `workflow_dispatch` run before cutting a release).

Nothing here needs a code change to go live the day those secrets exist —
the exact same "flip a flag once credentials exist" posture
`scheduled-jobs.yml` already documents for the nightly jobs.

Exit codes:
    0  every current row passed the gate
    1  the gate found at least one unapproved row (or `--live` hit a ZeroDB error)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.panel_review.gate import GateReport, check_panel_gate  # noqa: E402
from app.panel_review.service import PanelReviewService  # noqa: E402
from app.seed_stances import StanceSeeder  # noqa: E402
from app.seed_taxonomy import TaxonomySeeder  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402
from app.zerodb.testing import FakeZeroDBTransport  # noqa: E402

logger = logging.getLogger("check_panel_gate")

#: The real anchor subtopic the sabotage row hangs off, matching the live
#: verifier's and the BDD feature's own anchor.
ANCHOR_TOPIC = "en-US:prescription-drug-pricing:v1"

#: Deliberately unmistakable as test debris, matching
#: `verify_panel_review_live.py`'s own naming convention for synthetic rows.
SABOTAGE_STANCE = f"{ANCHOR_TOPIC}:ci-gate-self-check-sabotage"

_TABLES = ("issue_topics", "stances", "panel_reviews")


def _seeded_and_grandfathered_client() -> ZeroDBClient:
    """An in-memory ZeroDB seeded with the real corpus and fully grandfathered.

    The baseline fixture both `--fake` steps build on: the actual taxonomy and
    stance seeders, then `PanelReviewService.grandfather_existing_corpus()` —
    the exact method `scripts/backfill_panel_review.py` calls against the
    live project. This mirrors today's real, live state (every row
    grandfathered, nothing yet panel-approved), which is what makes a clean
    run of this fixture the right thing for CI to gate on.
    """
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-ci-self-check", backoff_base=0.0, sleep=lambda _: None
    )
    for table in _TABLES:
        client.create_table(table, {"id": "string"})

    TaxonomySeeder(client, pace=lambda: None).run()
    StanceSeeder(client, pace=lambda: None).run()

    PanelReviewService(client).grandfather_existing_corpus()
    return client


def _write_sabotage_row(client: ZeroDBClient) -> None:
    """Write one stance with no panel review of any kind — not even
    grandfathered. The same sabotage shape
    `tests/step_defs/test_panel_gate_in_release_steps.py` already proves the
    gate catches, written here too so the CLI path CI actually executes
    re-proves it on every run, not only a pytest file someone could forget
    to keep green.
    """
    from app.repositories import Repository
    from app.repositories.models import Stance
    from app.seed_stances import WORDING_VERSION

    stances: Repository[Stance] = Repository(client=client, model=Stance)
    stances.upsert(
        {
            "id": SABOTAGE_STANCE,
            "topic_id": ANCHOR_TOPIC,
            "label": "CI self-check sabotage card: proves the gate still catches this",
            "description": "Written by scripts/check_panel_gate.py's own self-check on every "
            "CI run, with no panel review attached, specifically so a regression that breaks "
            "the gate's failing path turns this CI step red immediately rather than only a "
            "separate pytest file.",
            "direction_code": "0",
            "wording_version": WORDING_VERSION,
        }
    )


def _report_and_exit(report: GateReport, *, live: bool) -> int:
    mode = "LIVE" if live else "FAKE (in-memory, CI-safe)"
    logger.info("panel gate check [%s]: %s", mode, report.summary())
    if report.grandfathered:
        logger.warning(
            "%d row(s) pass only because they are grandfathered — served, NEVER "
            "panel-reviewed, and still owed a real cross-partisan review. This is a "
            "deliberate, known, visible debt this gate tolerates; it is not endorsement. "
            "See app.panel_review.gate's module docstring.",
            report.grandfathered,
        )
    if report.ok:
        logger.info("PASS — every current row has an approved or grandfathered decision")
        return 0

    logger.error(
        "FAIL — %d row(s) have no publishable panel decision at their current version:",
        len(report.violations),
    )
    for violation in report.violations:
        logger.error("  - %s", violation)
    return 1


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Check the REAL ZeroDB project named by ZERODB_WORKSPACE_ID/ZERODB_PROJECT_ID/"
            "ZERODB_API_KEY, instead of the default self-contained in-memory fake. Source "
            "local.env into the shell first; never pass credentials on the command line."
        ),
    )
    args = parser.parse_args()

    if args.live:
        client = ZeroDBClient.from_env()
        try:
            logger.info(
                "checking the panel gate against LIVE project %r — this requires "
                "panel_reviews, issue_topics and stances to already be seeded/backfilled",
                client.project_id,
            )
            report = check_panel_gate(client)
        except ZeroDBError as exc:
            logger.error("panel gate check failed to run: %s", exc)
            return 1
        finally:
            client.close()
        return _report_and_exit(report, live=True)

    logger.info(
        "checking the panel gate against an in-memory fake (the CI-safe default) — this "
        "proves the gate's logic is sound, not that the live ledger currently passes. Run "
        "with --live and real ZeroDB credentials to check the live project."
    )

    # Step 1: the self-check. A sabotage row with no review at all MUST be
    # caught, or the gate's failing path is broken and nothing below this
    # point can be trusted — see the module docstring for why this fails
    # loudly rather than letting a broken gate report a clean pass.
    self_check_client = _seeded_and_grandfathered_client()
    try:
        _write_sabotage_row(self_check_client)
        self_check_report = check_panel_gate(self_check_client)
    finally:
        self_check_client.close()

    sabotage_caught = any(
        v.table == "stances" and v.row_id == SABOTAGE_STANCE for v in self_check_report.violations
    )
    if not sabotage_caught:
        logger.error(
            "SELF-CHECK FAILED: a stance written with NO panel review at all "
            "(%s) was NOT flagged by check_panel_gate. The gate's failing path is broken — "
            "it would rubber-stamp a real unreviewed release. Refusing to report a real "
            "result until this is fixed.",
            SABOTAGE_STANCE,
        )
        return 1
    logger.info(
        "self-check passed: an unreviewed stance with no panel decision of any kind is "
        "correctly caught by the gate"
    )

    # Step 2: the real check, against the same fixture with the sabotage row
    # gone. This is the number CI's exit code reflects.
    client = _seeded_and_grandfathered_client()
    try:
        report = check_panel_gate(client)
    finally:
        client.close()
    return _report_and_exit(report, live=False)


if __name__ == "__main__":
    raise SystemExit(main())
