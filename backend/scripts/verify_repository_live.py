#!/usr/bin/env python3
"""Verify the repository layer against the LIVE "informed" ZeroDB project (1.4).

Stories 1.2 and 1.3 both learned the same lesson the hard way: the in-memory
fake agreed with the code, the code was wrong, and only a live call found it
out. ZeroDB's `POST /rows` appends rather than upserting, its `PUT` replaces a
row wholesale, its `{row_id}` path segment takes an internal UUID, and its
server-side `/query` filter was observed silently dropping matching rows. Every
one of those is load-bearing for this layer, so story 1.4's two acceptance
criteria are checked against the real API as well as the fake.

What it proves, in order, and what it would catch:

  0. **Decoy rows, first.** Several unrelated rows are written into `people`
     before the row under test, and the table's row count is asserted to be
     greater than one. Without this the rest of the script is a false green:
     the first live run of this story passed against a repository that *did*
     duplicate rows, because `people` was empty, the one row written was the
     only row, and the client's filter workaround only ever inspected one page.
     Re-running with decoys reported `live row count ... is 2, expected 1` —
     a real duplicate in the real table.
  1. **Re-ingest produces no duplicate.** Two `upsert` calls with the same
     natural-key id, then a live count. Catches the upsert's find step failing
     to find — which is exactly what the server-side filter bug does cause,
     and what would quietly double every table on a second ingest run.
  2. **A row referencing a missing id is rejected.** A `member_votes` row whose
     `person_id` does not exist. Catches a reference check that only works
     because the fake happened to answer the way the code expected.
  3. **A row whose references resolve is written.** The same row with a real
     person, so the check is proven to permit as well as refuse.
  4. **Append-only versioning marks exactly one row current.** Two verdict
     versions, then a live read of both rows. Catches the superseded row losing
     its fields to ZeroDB's wholesale PUT, which would destroy the historical
     record the table exists for.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_repository_live.py

CLEANUP: every row it writes is deleted before it exits, including on failure,
and it then re-reads each one to confirm it is gone. It writes to the real
`people`, `roll_calls`, `member_votes` and `verdicts` tables, so each test id
carries the `zzz-live-check-1.4` marker to make a stray row obvious and
greppable. Nothing it writes resembles real data, and it writes no voter data
at all.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from app.repositories import MissingReferenceError, Repository
from app.repositories.models import MemberVote, Person, RollCall, Verdict
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError

#: Marker embedded in every id this script writes, so a row left behind by a
#: crashed run is obvious in the live project and trivially greppable.
MARKER = "zzz-live-check-1.4"

PERSON_ID = f"person:{MARKER}"
ROLL_CALL_ID = f"us-house:119-2:{MARKER}"
MISSING_PERSON_ID = f"person:{MARKER}-nobody"
PROMISE_ID = f"prm:{MARKER}"

#: Rows written into `people` BEFORE the row under test, so that row is not the
#: table's first. See the `0.` block in `main`: without decoys this whole script
#: passes against a repository whose find step only ever inspects row 0, which
#: is exactly the false green story 1.4's first live run produced.
DECOY_COUNT = 3

logger = logging.getLogger("verify_repository_live")


def person_row(name: str) -> dict[str, Any]:
    return {
        "id": PERSON_ID,
        "canonical_name": name,
        "external_ids": {"marker": MARKER},
        "terms": [{"body_id": "us-house", "party": "Synthetic", "start": "2025-01-03"}],
    }


def roll_call_row() -> dict[str, Any]:
    return {
        "id": ROLL_CALL_ID,
        "voted_at": "2026-03-04T15:00:00Z",
        "question": f"Synthetic question ({MARKER})",
        "vote_type": "final_passage",
        "result": "Passed",
        "tally": {"yea": 1, "nay": 0, "present": 0, "not_voting": 0},
        "topic_ids": [],
    }


def member_vote_row(person_id: str) -> dict[str, Any]:
    return {
        "roll_call_id": ROLL_CALL_ID,
        "person_id": person_id,
        "position": "yea",
        "voted_at": "2026-03-04T15:00:00Z",
        "vote_type": "final_passage",
        "topic_ids": [],
    }


def verdict_row(verdict: str) -> dict[str, Any]:
    return {
        "promise_id": PROMISE_ID,
        "person_id": PERSON_ID,
        "verdict": verdict,
        "confidence": 0.9,
        "methodology_version": f"{MARKER}-2026.1",
        "qa_status": "approved",
    }


def live_row_count(client: ZeroDBClient, table: str) -> int:
    """How many rows the table holds, read unfiltered.

    Used to prove the idempotency check below is not vacuous. The client works
    around ZeroDB's broken server-side filter by walking the table page by
    page, so a check run against a table holding ONE row cannot distinguish a
    correct walk from the old one-page version that only ever inspected row 0 —
    which is exactly how this story's first live run came back green against a
    repository that duplicated rows.

    Row *position* was the first thing tried here and is not usable: a live
    probe during this story showed ZeroDB's unfiltered row order is not
    insertion order and not otherwise specified (a pre-existing row came back
    between two rows inserted before and after it), so "is this row first" is
    not a property a test can depend on. Row *count* is, and it is what
    actually determines whether more than one page must be read.
    """
    page = client.query_rows(table, limit=500)
    return len(page.rows)


def count_live_rows(client: ZeroDBClient, table: str, row_id: str) -> int:
    """How many rows in `table` carry this natural-key id, read from live.

    Deliberately NOT `Repository.get`, which stops at the first match: the
    whole point of check 1 is counting duplicates, and a method that returns
    one row cannot tell one from two.
    """
    page = client.query_rows(table, filter={"id": row_id}, limit=100)
    return sum(1 for row in page.rows if row.row_data.get("id") == row_id)


def main() -> int:  # noqa: C901 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)

    people: Repository[Person] = Repository(client=client, model=Person)
    roll_calls: Repository[RollCall] = Repository(client=client, model=RollCall)
    member_votes: Repository[MemberVote] = Repository(client=client, model=MemberVote)
    verdicts: Repository[Verdict] = Repository(client=client, model=Verdict)

    #: (table, natural-key id) pairs to delete in the finally block.
    written: list[tuple[str, str]] = []
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        # --- 0. make every check below non-vacuous ------------------------
        #
        # This story's FIRST live run passed while the repository was broken,
        # because `people` was empty: the one row written was row 0, and the
        # client's client-side filter fallback only ever read row 0. Seeding
        # decoys FIRST puts the row under test behind other rows, which is the
        # only arrangement that actually exercises the find step. Without this
        # block the run below is a false green.
        for index in range(DECOY_COUNT):
            decoy_id = f"{PERSON_ID}-decoy-{index}"
            client.upsert_row("people", decoy_id, {"canonical_name": f"Decoy {index}"})
            written.append(("people", decoy_id))
        logger.info("seeded %d decoy rows ahead of the row under test", DECOY_COUNT)

        # --- 1. re-ingesting the same record produces no duplicate --------
        people.upsert(person_row("Synthetic Live Check"))
        written.append(("people", PERSON_ID))
        people.upsert(person_row("Synthetic Live Check (re-ingested)"))

        row_count = live_row_count(client, "people")
        check(
            "the table holds several rows, so the find step is really exercised",
            row_count > 1,
            f"people holds {row_count} row(s); with one row this check is vacuous",
        )

        count = count_live_rows(client, "people", PERSON_ID)
        check(
            "re-ingesting the same record produces no duplicate",
            count == 1,
            f"live row count for {PERSON_ID} is {count}, expected 1",
        )

        stored = people.get(PERSON_ID)
        check(
            "the second upsert updated the one row rather than being ignored",
            stored is not None and stored.canonical_name.endswith("(re-ingested)"),
            f"canonical_name is {stored.canonical_name!r}" if stored else "row not found",
        )

        # --- 2. a row referencing a missing id is rejected ----------------
        roll_calls.upsert(roll_call_row())
        written.append(("roll_calls", ROLL_CALL_ID))

        missing_exists = client.get_row("people", MISSING_PERSON_ID) is not None
        check(
            "the id used for the negative case really is absent live",
            not missing_exists,
            f"{MISSING_PERSON_ID} unexpectedly exists; the next check would be vacuous",
        )

        try:
            member_votes.upsert(member_vote_row(MISSING_PERSON_ID))
        except MissingReferenceError as exc:
            check(
                "a row referencing a missing id is rejected",
                exc.referenced_table == "people" and exc.referenced_id == MISSING_PERSON_ID,
                f"rejected, but naming {exc.referenced_table}/{exc.referenced_id}",
            )
        else:
            orphan_id = f"{ROLL_CALL_ID}:{MARKER}-nobody"
            written.append(("member_votes", orphan_id))
            check(
                "a row referencing a missing id is rejected",
                False,
                "the write was ACCEPTED — the reference check does not work live",
            )

        # --- 3. a row whose references resolve is written ------------------
        vote = member_votes.upsert(member_vote_row(PERSON_ID))
        assert vote.id is not None
        written.append(("member_votes", vote.id))
        vote_count = count_live_rows(client, "member_votes", vote.id)
        check(
            "a row whose references all resolve is written exactly once",
            vote_count == 1,
            f"live row count for {vote.id} is {vote_count}, expected 1",
        )
        check(
            "the member vote's id is the deterministic natural key",
            vote.id == f"{ROLL_CALL_ID}:{MARKER}",
            f"id is {vote.id!r}",
        )

        # --- 4. append-only versioning marks exactly one row current ------
        # No `promises` row is seeded, so the promise reference is skipped for
        # this check: writing one would mean cleaning up a fifth table, and the
        # reference check itself is already proven by check 2. The person
        # reference is still checked, and is real.
        first = verdicts.append_version(verdict_row("kept"), check_references=False)
        assert first.id is not None
        written.append(("verdicts", first.id))
        second = verdicts.append_version(verdict_row("broken"), check_references=False)
        assert second.id is not None
        written.append(("verdicts", second.id))

        check(
            "append-only versioning numbers versions in sequence",
            (first.id, second.id) == (f"{PROMISE_ID}:v1", f"{PROMISE_ID}:v2"),
            f"got {first.id} then {second.id}",
        )

        page = client.query_rows("verdicts", filter={"promise_id": PROMISE_ID}, limit=100)
        rows = [row.row_data for row in page.rows if row.row_data.get("promise_id") == PROMISE_ID]
        current = [row for row in rows if row.get("is_current") is True]
        superseded = [row for row in rows if row.get("is_current") is False]

        check(
            "both versions are stored, so history is kept",
            len(rows) == 2,
            f"found {len(rows)} verdict rows, expected 2",
        )
        check(
            "exactly one row is current",
            len(current) == 1,
            f"found {len(current)} current rows, expected 1",
        )
        check(
            "the current row is the newest version",
            len(current) == 1 and current[0].get("verdict") == "broken",
            f"current verdict is {current[0].get('verdict')!r}" if current else "none current",
        )
        check(
            "the superseded row survived the is_current flip intact",
            len(superseded) == 1
            and superseded[0].get("verdict") == "kept"
            and superseded[0].get("id") == f"{PROMISE_ID}:v1"
            and superseded[0].get("methodology_version") == f"{MARKER}-2026.1",
            f"superseded row is {superseded[0] if superseded else None}",
        )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        # --- cleanup, then confirm it took ------------------------------
        logger.info("cleaning up %d live rows", len(written))
        for table, row_id in reversed(written):
            try:
                client.delete_row(table, row_id)
            except ZeroDBNotFoundError:
                logger.info("  already gone: %s/%s", table, row_id)
            except ZeroDBError as exc:
                failures.append(f"could not delete {table}/{row_id}: {exc}")
                logger.error("  FAILED to delete %s/%s: %s", table, row_id, exc)

        leftover = []
        for table, row_id in written:
            remaining = count_live_rows(client, table, row_id)
            if remaining:
                leftover.append(f"{table}/{row_id} ({remaining} row(s))")
        if leftover:
            failures.append(f"live rows left behind: {', '.join(leftover)}")
            logger.error("FAIL  cleanup left rows behind: %s", ", ".join(leftover))
        else:
            logger.info("PASS  every row written was deleted and confirmed gone")

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
