#!/usr/bin/env python3
"""Verify the panel review workflow against the LIVE "informed" project (3.3).

Every story from 1.3 on has found at least one discrepancy between what the
in-memory fake agrees to and what ZeroDB actually does, so story 3.3's three
acceptance criteria are checked against the real API as well as the fake.

What it proves, in order:

  1. **The `panel_reviews` table exists live**, and the gate has something to
     read. A missing table would make every gated read 404 rather than
     withhold, which is a different failure with a different fix.
  2. **Every live `issue_topics` and `stances` row is marked grandfathered**,
     counted over an unfiltered live read rather than over what the backfill
     thinks it wrote.
  3. **No grandfathered row reads as approved.** The honesty check, asserted
     from the stored bytes: `decision` is literally `grandfathered`,
     `reviewer_id` is the backfill's literal id, and `is_panel_approved` is
     false for every one of them. If this check ever passes while the ledger
     says `approved`, the live database is making a claim about a review that
     did not happen.
  4. **Grandfathered rows still serve.** The gated reads return the live
     corpus, so installing the gate did not take the product down.
  5. **An unreviewed row is withheld live.** A synthetic stance is written,
     confirmed absent from the gated read, approved, and confirmed present —
     the first acceptance criterion, proven against real rows rather than
     against the fake's filter.
  6. **An approved edit bumps the version, keeps the id, and stays served.**
     The second acceptance criterion, end to end against the live API, on the
     synthetic card rather than on a real one.
  7. **A reviewer cannot approve their own proposal live.**
  8. **The review history reads back in order with the wording before and
     after**, from live rows. The third acceptance criterion.
  9. **The ledger is append-only in practice**: a second decision about the
     same row leaves the first readable, and re-running the backfill writes no
     duplicate — counted from an unfiltered live read rather than through
     `Repository.get`, which stops at the first match and so cannot tell one
     row from two.
 10. **A decision about a row that does not exist is refused live.**

!!! WHAT IS CLEANED UP AND WHAT IS NOT !!!

**Permanent, deliberately left in place:** the ~281 `grandfathered` decisions
over the real taxonomy and stance corpus. They are what keeps the gated reads
serving, and they are the standing record that the corpus predates the review
workflow. Deleting them would un-publish the taxonomy for any caller on the
gated paths and erase the evidence of the gap. `scripts/backfill_panel_review.py`
writes the same rows and this script's re-run is a no-op on them.

**Cleaned up, because it is test debris:** the synthetic stance card checks 5-9
operate on, and every ledger row about it. A proposal-and-review cycle over a
made-up card is not production content, and leaving it would put a fake stance
in the live menu the moment the voter-facing endpoints move onto the gated
path. The cleanup is in a `finally`, so it runs even when a check fails.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_panel_review_live.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.panel_review.service import (  # noqa: E402
    BACKFILL_REVIEWER_ID,
    PanelReviewService,
    SelfReviewError,
    UnknownSubjectError,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import IssueTopic, PanelReview, Stance  # noqa: E402
from app.seed_stances import WORDING_VERSION  # noqa: E402
from app.seed_taxonomy import TAXONOMY_LOCALE, TAXONOMY_VERSION  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError  # noqa: E402

logger = logging.getLogger("verify_panel_review_live")

#: The real subtopic the synthetic card hangs off. prd.md's own anchor topic,
#: so a reader can tell at a glance that the id is a real one.
ANCHOR_TOPIC = f"{TAXONOMY_LOCALE}:prescription-drug-pricing:v{TAXONOMY_VERSION}"

#: The synthetic card checks 5-9 operate on. `-live-check` in the slug so that
#: a row left behind by a crashed run is unmistakably debris rather than
#: something a later reader might take for real editorial content.
SYNTHETIC_STANCE = f"{ANCHOR_TOPIC}:panel-review-live-check"

#: A subtopic that does not exist, for check 10's negative case.
MISSING_STANCE = f"{ANCHOR_TOPIC}:no-such-card-live-check"

REVIEWER_A = "panelist:live-check-a"
REVIEWER_B = "panelist:live-check-b"

#: One page big enough for the whole ledger plus the whole corpus.
PAGE = 5000


# A single long linear function on purpose: the checks build on each other in
# order (the synthetic card must be written before it can be withheld, approved,
# edited and cleaned up), and splitting it into helpers would hide that order
# behind call sites while gaining nothing — same shape as every other
# verify_*_live.py script here.
def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    failures: list[str] = []

    def check(what: str, ok: bool, detail: str = "") -> None:
        if ok:
            logger.info("PASS  %s", what)
        else:
            failures.append(f"{what}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s%s", what, f" — {detail}" if detail else "")

    client = ZeroDBClient.from_env()
    panel = PanelReviewService(client)
    stances: Repository[Stance] = Repository(client=client, model=Stance)
    topics: Repository[IssueTopic] = Repository(client=client, model=IssueTopic)
    reviews: Repository[PanelReview] = Repository(client=client, model=PanelReview)
    synthetic_written = False

    try:
        logger.info("verifying the panel review workflow against project %r", client.project_id)

        # --- 1. the ledger table exists ----------------------------------
        tables = {table.table_name for table in client.list_tables(limit=200)}
        check(
            "the panel_reviews table exists live",
            "panel_reviews" in tables,
            "run `python scripts/bootstrap.py` first",
        )
        if "panel_reviews" not in tables:
            return 1

        live_topics = topics.query(limit=PAGE)
        live_stances = stances.query(limit=PAGE)
        check(
            "the live project holds the seeded corpus to review",
            bool(live_topics) and bool(live_stances),
            f"{len(live_topics)} topic(s), {len(live_stances)} stance(s)",
        )
        logger.info(
            "live corpus: %d issue_topics row(s), %d stances row(s)",
            len(live_topics),
            len(live_stances),
        )

        # --- 2. the corpus is grandfathered ------------------------------
        # Run the backfill rather than assume someone has: it is idempotent,
        # so a project already marked reports every row as already covered and
        # nothing is written twice.
        report = panel.grandfather_existing_corpus()
        logger.info(
            "backfill: %d newly marked, %d already carried a decision",
            report.row_count,
            len(report.already_marked),
        )

        unmarked: list[str] = []
        not_publishable: list[str] = []
        falsely_approved: list[str] = []
        for table, rows in (("issue_topics", live_topics), ("stances", live_stances)):
            for row in rows:
                status = panel.status_for(subject_table=table, subject_id=str(row.id))  # type: ignore[arg-type]
                if status.decision is None:
                    unmarked.append(str(row.id))
                    continue
                if not status.is_publishable:
                    not_publishable.append(str(row.id))
                if status.is_panel_approved:
                    falsely_approved.append(str(row.id))

        check(
            "every live topic and stance carries a review decision",
            not unmarked,
            f"{len(unmarked)} unmarked, e.g. {unmarked[:3]}",
        )
        check(
            "every live topic and stance is publishable, so the app is not dark",
            not not_publishable,
            f"{len(not_publishable)} withheld, e.g. {not_publishable[:3]}",
        )

        # --- 3. grandfathered is NOT approved ----------------------------
        check(
            "no grandfathered row claims a panel approved it",
            not falsely_approved,
            f"{len(falsely_approved)} row(s) falsely report a panel approval, "
            f"e.g. {falsely_approved[:3]}",
        )

        ledger = reviews.query(limit=PAGE)
        grandfathered = [row for row in ledger if row.decision == "grandfathered"]
        check(
            "the grandfathering decisions are stored with the literal decision value",
            bool(grandfathered),
            "no row in panel_reviews carries decision='grandfathered'",
        )
        wrong_reviewer = [
            str(row.id) for row in grandfathered if row.reviewer_id != BACKFILL_REVIEWER_ID
        ]
        check(
            "every grandfathering decision names the backfill, not a person",
            not wrong_reviewer,
            f"{len(wrong_reviewer)} row(s) name someone else, e.g. {wrong_reviewer[:3]}",
        )
        silent = [
            str(row.id) for row in grandfathered if "NOT panel-approved" not in (row.reason or "")
        ]
        check(
            "every grandfathering decision says in its own reason that it is not approval",
            not silent,
            f"{len(silent)} row(s) carry no such reason, e.g. {silent[:3]}",
        )

        queued = {entry.subject_id for entry in panel.queue()}
        expected_debt = {str(row.id) for row in live_topics} | {str(row.id) for row in live_stances}
        check(
            "the review queue reports the whole grandfathered corpus as owing a review",
            expected_debt <= queued,
            f"the queue forgets {len(expected_debt - queued)} row(s)",
        )
        logger.info("standing review debt: %d row(s) owe a real panel review", len(queued))

        # --- 4. grandfathered rows still serve ---------------------------
        served = {str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)}
        expected_served = {str(row.id) for row in live_stances if row.topic_id == ANCHOR_TOPIC}
        check(
            "the gated stance read serves every live card for the anchor subtopic",
            expected_served <= served and bool(expected_served),
            f"withheld {sorted(expected_served - served)}",
        )
        tree = panel.approved_topic_tree(locale=TAXONOMY_LOCALE, taxonomy_version=TAXONOMY_VERSION)
        check(
            "the gated topic tree serves the live taxonomy",
            len(tree.topics) > 0,
            "the gated tree is empty, so the picker would be blank",
        )
        logger.info(
            "gated tree: %d top-level topic(s), %d subtopic(s)",
            len(tree.topics),
            sum(len(topic.subtopics) for topic in tree.topics),
        )

        # --- 5. an unreviewed row is withheld live -----------------------
        stances.upsert(
            {
                "id": SYNTHETIC_STANCE,
                "topic_id": ANCHOR_TOPIC,
                "label": "Live-check placeholder, deleted by this script",
                "description": "A synthetic stance card written by "
                "scripts/verify_panel_review_live.py to prove the review gate withholds "
                "unreviewed wording live. It is deleted at the end of the run.",
                "direction_code": "0",
                "wording_version": WORDING_VERSION,
            }
        )
        synthetic_written = True

        served = {str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)}
        check(
            "an unreviewed stance written live is withheld from the gated read",
            SYNTHETIC_STANCE not in served,
            "unreviewed wording reached a gated read against the live API",
        )
        check(
            "the ungated read still sees it, so the gate withheld rather than lost it",
            stances.get(SYNTHETIC_STANCE) is not None,
        )

        panel.record_decision(
            subject_table="stances",
            subject_id=SYNTHETIC_STANCE,
            subject_version=WORDING_VERSION,
            reviewer_id=REVIEWER_A,
            decision="approved",
        )
        served = {str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)}
        check(
            "approving it live publishes it",
            SYNTHETIC_STANCE in served,
            "an approved card is still withheld",
        )

        # --- 6. an approved edit bumps the version and keeps the id ------
        proposal = panel.propose(
            subject_table="stances",
            subject_id=SYNTHETIC_STANCE,
            author_id=REVIEWER_A,
            label="Live-check placeholder, reworded then deleted",
        )
        before = stances.get(SYNTHETIC_STANCE)
        check(
            "a proposal leaves the live wording and version untouched",
            before is not None
            and before.label == "Live-check placeholder, deleted by this script"
            and before.wording_version == WORDING_VERSION,
            f"the live row moved to {before!r}",
        )

        # --- 7. self-review is refused live ------------------------------
        try:
            panel.decide(
                proposal_id=proposal.proposal_id, reviewer_id=REVIEWER_A, decision="approved"
            )
        except SelfReviewError:
            check("a reviewer cannot approve their own proposal live", True)
        else:
            check(
                "a reviewer cannot approve their own proposal live",
                False,
                "the self-approval was ACCEPTED — the cross-partisan rule does not hold live",
            )

        panel.decide(proposal_id=proposal.proposal_id, reviewer_id=REVIEWER_B, decision="approved")
        after = stances.get(SYNTHETIC_STANCE)
        check(
            "approving the proposal applies the wording and bumps wording_version live",
            after is not None
            and after.label == "Live-check placeholder, reworded then deleted"
            and after.wording_version == WORDING_VERSION + 1,
            f"the stored row is {after!r}",
        )
        check(
            "the bump did not change the row's id",
            after is not None and str(after.id) == SYNTHETIC_STANCE,
        )
        page = client.query_rows("stances", limit=PAGE)
        matching = [row for row in page.rows if row.row_data.get("id") == SYNTHETIC_STANCE]
        check(
            "exactly one live row carries the id after the bump",
            len(matching) == 1,
            f"{len(matching)} rows carry {SYNTHETIC_STANCE}",
        )
        served = {str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)}
        check(
            "the approved edit is served at its new version",
            SYNTHETIC_STANCE in served,
            "the approved edit bumped past its own approval and is now withheld",
        )

        # --- 8. the history reads back, in order, with the diff ----------
        history = panel.history_for(subject_table="stances", subject_id=SYNTHETIC_STANCE)
        check(
            "the live review history holds every decision about the row",
            len(history) >= 4,
            f"{len(history)} entries: {[row.decision for row in history]}",
        )
        timestamps = [row.decided_at for row in history]
        check(
            "the live history is newest first",
            timestamps == sorted(timestamps, reverse=True),
            f"order is {timestamps}",
        )
        approvals = [row for row in history if row.decision == "approved" and row.proposal_id]
        check(
            "the live history shows the wording before and after the change",
            bool(approvals)
            and approvals[0].previous_label == "Live-check placeholder, deleted by this script"
            and approvals[0].proposed_label == "Live-check placeholder, reworded then deleted",
            f"the approval entry is {approvals[0] if approvals else None!r}",
        )
        incomplete = [
            str(row.id)
            for row in history
            if not row.reviewer_id or not row.decided_at or row.subject_version < 1
        ]
        check(
            "every live history entry names its reviewer, version and timestamp",
            not incomplete,
            f"{len(incomplete)} incomplete entries",
        )

        # --- 9. append-only in practice ----------------------------------
        panel.record_decision(
            subject_table="stances",
            subject_id=SYNTHETIC_STANCE,
            subject_version=WORDING_VERSION + 1,
            reviewer_id=REVIEWER_B,
            decision="rejected",
            reason="Live-check rejection, recorded to prove the ledger is append-only.",
        )
        after_rejection = panel.history_for(subject_table="stances", subject_id=SYNTHETIC_STANCE)
        check(
            "a later decision does not erase the earlier ones live",
            len(after_rejection) == len(history) + 1,
            f"{len(after_rejection)} entries after adding one to {len(history)}",
        )
        check(
            "the newest decision is the one in force live",
            panel.status_for(subject_table="stances", subject_id=SYNTHETIC_STANCE).decision
            == "rejected",
        )
        served = {str(card.id) for card in panel.approved_stances_for_topic(ANCHOR_TOPIC)}
        check(
            "a rejected row is withheld live",
            SYNTHETIC_STANCE not in served,
            "rejected wording is still being served",
        )

        # Re-running the backfill must not duplicate a decision. Counted from
        # an unfiltered live read rather than through `Repository.get`, which
        # stops at the first match and so cannot tell one row from two — the
        # whole question idempotency asks.
        before_rows = len(client.query_rows("panel_reviews", limit=PAGE).rows)
        second = panel.grandfather_existing_corpus()
        after_rows = len(client.query_rows("panel_reviews", limit=PAGE).rows)
        check(
            "re-running the backfill writes no duplicate decision live",
            second.row_count == 0 and after_rows == before_rows,
            f"the second run wrote {second.row_count} row(s); the table went from "
            f"{before_rows} to {after_rows}",
        )
        check(
            "re-running the backfill did not downgrade the synthetic card's rejection",
            panel.status_for(subject_table="stances", subject_id=SYNTHETIC_STANCE).decision
            == "rejected",
            "the backfill overwrote a real decision",
        )

        # --- 10. a decision about a missing row is refused ---------------
        check(
            "the row used for the negative case really is absent live",
            stances.get(MISSING_STANCE) is None,
            f"{MISSING_STANCE} unexpectedly exists; the next check would be vacuous",
        )
        try:
            panel.record_decision(
                subject_table="stances",
                subject_id=MISSING_STANCE,
                subject_version=1,
                reviewer_id=REVIEWER_A,
                decision="approved",
            )
        except UnknownSubjectError:
            check("a decision about a row that does not exist is refused live", True)
        else:
            check(
                "a decision about a row that does not exist is refused live",
                False,
                "the decision was ACCEPTED — the ledger can approve nothing",
            )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        # The synthetic card and every ledger row about it are test debris and
        # go. The ~281 grandfathering decisions over the REAL corpus stay —
        # see this module's docstring.
        if synthetic_written:
            logger.info("cleaning up the synthetic card and its ledger rows")
            try:
                client.delete_row("stances", SYNTHETIC_STANCE)
                logger.info("  deleted stances/%s", SYNTHETIC_STANCE)
            except ZeroDBNotFoundError:
                logger.info("  already gone: stances/%s", SYNTHETIC_STANCE)
            except ZeroDBError as exc:
                failures.append(f"could not delete stances/{SYNTHETIC_STANCE}: {exc}")
                logger.error("  FAILED to delete stances/%s: %s", SYNTHETIC_STANCE, exc)

            try:
                debris = [
                    row
                    for row in reviews.query(limit=PAGE)
                    if row.subject_id in {SYNTHETIC_STANCE, MISSING_STANCE}
                ]
            except ZeroDBError as exc:
                failures.append(f"could not read panel_reviews to clean up: {exc}")
                debris = []
            for row in debris:
                try:
                    client.delete_row("panel_reviews", str(row.id))
                except ZeroDBNotFoundError:
                    pass
                except ZeroDBError as exc:
                    failures.append(f"could not delete panel_reviews/{row.id}: {exc}")
                    logger.error("  FAILED to delete panel_reviews/%s: %s", row.id, exc)
            logger.info("  deleted %d synthetic ledger row(s)", len(debris))

        logger.info(
            "LEFT IN PLACE (intentionally): the grandfathering decisions over the real "
            "taxonomy and stance corpus, in panel_reviews. They are what keeps the gated "
            "reads serving and the standing record that this corpus predates the review "
            "workflow — reviewer_id %r on every one, which is NOT a panel approval. Every "
            "one of those rows still owes a real cross-partisan review.",
            BACKFILL_REVIEWER_ID,
        )
        client.close()

    if failures:
        logger.error("\n%d check(s) FAILED:", len(failures))
        for failure in failures:
            logger.error("  - %s", failure)
        return 1
    logger.info("\nall live checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
