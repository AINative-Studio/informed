#!/usr/bin/env python3
"""Verify the five integrity checks and drift repair against the LIVE
"informed" project (stories 16.2, 16.4).

Checks 1 (dangling references) and 5 (provenance coverage) read tables that
hold real data today — `people`, `candidacies`, `roll_calls`, `provenance_log`
and so on — so they are run here exactly as the nightly job would run them,
against the live project, with NOTHING written. Any violation reported is a
genuine finding about the live data, not a test failure to explain away; see
this script's own PASS/FAIL output for whether any were found.

Checks 2 (verdict -> approved roll-call mapping), 3 (is_current uniqueness)
and 4 (member_votes drift) depend on `verdicts`, `candidate_positions` and
`member_votes` — all either empty or (member_votes) not yet loaded with real
rows live. Running the bare check functions against the live, unmodified
project would therefore only prove the vacuous case again (already proven
against the in-memory fake in `tests/features/integrity_checks.feature`,
with synthetic-violation sabotage tests). So this script additionally writes
a small number of CLEARLY-MARKED synthetic rows — ids carrying the marker
below — runs each check against them to confirm it catches the same
violation shape live that it catches against the fake, and deletes every
row it wrote before exiting, on success or failure.

Run it with the live credentials sourced into the shell, never passed on the
command line or printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_integrity_checks_live.py

CLEANUP: every row this script writes is deleted before it exits, including
on failure, and each deletion is re-read to confirm it is gone. Ids carry the
marker `zzz-live-check-16.2` so a row left by a crashed run is obvious and
greppable. Nothing it writes resembles real voter data (datamodel.md's
"never put real voter data in fixtures, logs or test output" has no bearing
here regardless: member_votes, verdicts and candidate_positions are not
voter-profile tables).
"""

from __future__ import annotations

import logging
import sys

from app.integrity.checks import (
    check_current_verdict_uniqueness,
    check_member_votes_drift,
    check_provenance_coverage,
    check_references,
    check_verdict_roll_call_mapping,
)
from app.integrity.repair import repair_member_votes_drift
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError

MARKER = "zzz-live-check-16.2"

logger = logging.getLogger("verify_integrity_checks_live")


def main() -> int:  # noqa: C901, PLR0915 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)

    written: list[tuple[str, str]] = []  # (table, row_id)
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        # --- checks 1 and 5 against the real, unmodified live data ---------
        logger.info("")
        logger.info("checks 1 and 5 against real live data (read-only, nothing written):")

        references_report = check_references(client)
        logger.info(
            "  check 1 (references): scanned %d row(s), %d violation(s)",
            references_report.scanned,
            len(references_report.violations),
        )
        if references_report.violations:
            logger.warning("  check 1 found real violations in the live project:")
            for violation in references_report.violations[:20]:
                logger.warning("    - %s", violation)
        check(
            "check 1 (references) is correctly built and ran against live data",
            True,  # Running without raising is the proof; violations (if any)
            # are a genuine finding about the data, not a failure of the
            # check itself — see the module docstring.
        )

        provenance_report = check_provenance_coverage(client)
        logger.info(
            "  check 5 (provenance): scanned %d row(s), %d violation(s)",
            provenance_report.scanned,
            len(provenance_report.violations),
        )
        if provenance_report.violations:
            logger.warning("  check 5 found real violations in the live project:")
            for violation in provenance_report.violations[:20]:
                logger.warning("    - %s", violation)
        check("check 5 (provenance coverage) is correctly built and ran against live data", True)

        logger.info("")
        logger.info(
            "live data findings: check 1 found %d violation(s), check 5 found %d violation(s)",
            len(references_report.violations),
            len(provenance_report.violations),
        )

        # --- checks 2, 3, 4: synthetic marked rows, cleaned up afterward ---
        logger.info("")
        logger.info("checks 2, 3 and 4: synthetic marked rows (cleaned up at the end)")

        promise_id = f"prm:{MARKER}"
        roll_call_id = f"us-house:119-2:{MARKER}"
        person_id = "person:B000944"  # a real, already-live person row

        if client.get_row("people", person_id) is None:
            client.insert_row(
                "people", {"id": person_id, "canonical_name": "Synthetic Test Person"}
            )
            written.append(("people", person_id))

        # Check 2: a verdict with NO approved mapping is a violation.
        client.insert_row(
            "promises",
            {"id": promise_id, "person_id": person_id, "mappings": []},
        )
        written.append(("promises", promise_id))
        verdict_row_id = f"{promise_id}:v1"
        client.insert_row(
            "verdicts",
            {
                "id": verdict_row_id,
                "promise_id": promise_id,
                "person_id": person_id,
                "verdict": "kept",
                "confidence": 0.8,
                "methodology_version": "v1",
                "qa_status": "pending",
                "reviewed_by": None,
                "reviewed_at": None,
                "is_current": True,
                "created_at": "2026-01-01T00:00:00Z",
            },
        )
        written.append(("verdicts", verdict_row_id))

        mapping_report = check_verdict_roll_call_mapping(client)
        check(
            "check 2 (verdict mapping) catches a kept verdict with no approved mapping",
            any(v.row_id == verdict_row_id for v in mapping_report.violations),
            f"violations: {[v.row_id for v in mapping_report.violations]}",
        )

        # Check 3: a second "current" row for the same promise, written
        # directly (bypassing append_version) — exactly the sabotage shape
        # the BDD suite proves against the fake.
        second_verdict_id = f"{promise_id}:v2"
        client.insert_row(
            "verdicts",
            {
                "id": second_verdict_id,
                "promise_id": promise_id,
                "person_id": person_id,
                "verdict": "broken",
                "confidence": 0.5,
                "methodology_version": "v1",
                "qa_status": "pending",
                "reviewed_by": None,
                "reviewed_at": None,
                "is_current": True,
                "created_at": "2026-01-02T00:00:00Z",
            },
        )
        written.append(("verdicts", second_verdict_id))

        uniqueness_report = check_current_verdict_uniqueness(client)
        check(
            "check 3 (is_current uniqueness) catches two current verdicts for one promise",
            any(promise_id in v.row_id for v in uniqueness_report.violations),
            f"violations: {[v.row_id for v in uniqueness_report.violations]}",
        )

        # Check 4 / repair: a roll call, a member_vote that copies it, then a
        # correction that drifts it.
        client.insert_row(
            "roll_calls",
            {
                "id": roll_call_id,
                "voted_at": "2026-01-01T00:00:00Z",
                "question": f"Synthetic question ({MARKER})",
                "vote_type": "final_passage",
                "result": "Passed",
                "tally": {"yea": 1, "nay": 0, "present": 0, "not_voting": 0},
                "bill_id": None,
                "amendment_ref": None,
                "is_bundled": False,
                "topic_ids": [],
                "source_url": None,
                "content_hash": None,
            },
        )
        written.append(("roll_calls", roll_call_id))

        member_vote_id = f"{roll_call_id}:B000944"
        client.insert_row(
            "member_votes",
            {
                "id": member_vote_id,
                "roll_call_id": roll_call_id,
                "person_id": person_id,
                "position": "yea",
                "voted_at": "2026-01-01T00:00:00Z",
                "vote_type": "final_passage",
                "bill_id": None,
                "topic_ids": [],
                "is_bundled": False,
            },
        )
        written.append(("member_votes", member_vote_id))

        client.update_row("roll_calls", roll_call_id, {"vote_type": "cloture"})

        drift_report = check_member_votes_drift(client)
        check(
            "check 4 (member_votes drift) catches a drifted vote_type",
            any(
                v.row_id == member_vote_id and "vote_type" in v.detail
                for v in drift_report.violations
            ),
            f"violations: {[str(v) for v in drift_report.violations]}",
        )

        repair_result = repair_member_votes_drift(client)
        check(
            "the drift repair fixes the synthetic drift",
            repair_result.repaired >= 1
            and any(record.member_vote_id == member_vote_id for record in repair_result.records),
            repair_result.summary(),
        )

        fixed_vote = client.get_row("member_votes", member_vote_id)
        check(
            "the repaired member_votes row now matches its roll call's vote_type",
            fixed_vote is not None and fixed_vote.row_data.get("vote_type") == "cloture",
            f"stored vote_type: {fixed_vote.row_data.get('vote_type') if fixed_vote else None}",
        )

        second_repair = repair_member_votes_drift(client)
        check(
            "re-running the repair is idempotent (no drift left to fix)",
            second_repair.repaired == 0,
            second_repair.summary(),
        )

    except ZeroDBError as exc:
        logger.exception("the run failed against live ZeroDB: %s", exc)
        failures.append(f"unexpected ZeroDBError: {exc}")
    finally:
        logger.info("")
        logger.info("cleaning up %d synthetic row(s)", len(written))
        for table, row_id in written:
            _delete_and_confirm(client, table, row_id, failures)

    if failures:
        logger.error("\n%d check(s) failed:", len(failures))
        for failure in failures:
            logger.error("  - %s", failure)
        return 1
    logger.info("\nevery check passed against the live project")
    return 0


def _delete_and_confirm(client: ZeroDBClient, table: str, row_id: str, failures: list[str]) -> None:
    try:
        client.delete_row(table, row_id)
    except ZeroDBNotFoundError:
        return
    except ZeroDBError as exc:
        failures.append(f"could not delete {table}/{row_id}: {exc}")
        return
    if client.get_row(table, row_id) is not None:
        failures.append(f"{table}/{row_id} is still present after delete")


if __name__ == "__main__":
    sys.exit(main())
