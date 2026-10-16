#!/usr/bin/env python3
"""Verify provenance enforcement against the LIVE "informed" project (16.3).

Story 16.3 turns datamodel.md's integrity check — "every NoSQL row with a
`source_url` has a matching `provenance_log` entry, so no evidence goes
untraced" — into a property of the write rather than a step an adapter has to
remember. The in-memory fake agrees with that code; stories 1.2, 1.3 and 1.4
each learned the hard way that the fake agreeing proves nothing. The two
behaviours this story rests on are exactly the two ZeroDB has surprised this
codebase about before:

  * **the content-addressed check-then-insert finds the existing row.** It is a
    `get_row` by `prov:{sha256}`, and `get_row` goes through the client-side
    filter fallback that ZeroDB's broken server-side `/query` filter forces
    (story 1.4). If that find fails live, every re-ingest appends a duplicate
    provenance entry and the log stops being a record of distinct fetches.
  * **`insert_row` into `provenance_log` actually appends**, so a genuinely
    changed record gets a SECOND entry rather than replacing the first — the
    append-only guarantee datamodel.md's retention table makes for this table.

What it checks, in order:

  0. **A live audit of every table, first.** Every row in the project is read
     and checked for a `source_url` with no matching `provenance_log` entry.
     This is the retrofit question stories 2.1/2.2/3.1 raise, answered against
     the real data rather than from the schema, and it runs before anything is
     written so the answer is about the project as it stands.
  1. **Decoy provenance rows, so the rest is not vacuous.** Several unrelated
     rows are written into `provenance_log` ahead of the row under test, and
     the table's count is asserted greater than one. Without this the find step
     below is satisfied by a table whose only row IS the row under test — the
     false green story 1.4 documents.
  2. **A sourced row's write produces its provenance entry**, with the right
     entity_table, entity_id, source_url and adapter_version.
  3. **Re-writing identical content adds no second entry.**
  4. **Re-writing changed content appends a second entry and keeps the first.**
  5. **A sourced write with no ProvenanceContext is refused**, and leaves
     neither the entity row nor a provenance entry behind.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_provenance_live.py

CLEANUP: every row it writes is deleted before it exits, including on failure,
and each deletion is re-read to confirm it is gone. Ids carry the marker
`zzz-live-check-16.3` so a row left by a crashed run is obvious and greppable.
Nothing it writes resembles real data, and it writes no voter data at all.

One wrinkle the cleanup has to handle: `Repository.delete` refuses append-only
tables (`provenance_log` is retained indefinitely per datamodel.md), which is
correct for production and unhelpful for a test's teardown — so the provenance
rows this script writes are deleted through `ZeroDBClient.delete_row`
directly. That is the one place in this codebase that is allowed to, and it is
allowed because these rows document a fetch that never happened.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from app.repositories import MissingProvenanceError, ProvenanceContext, Repository
from app.repositories.models import RollCall
from app.repositories.provenance import PROVENANCE_TABLE, provenance_id
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError

MARKER = "zzz-live-check-16.3"

ROLL_CALL_ID = f"us-house:119-2:{MARKER}"
SOURCE_URL = f"https://api.congress.gov/v3/house-vote/119/2/{MARKER}"
ADAPTER_VERSION = f"{MARKER}/v1"

#: Two distinct content hashes: the same record fetched twice unchanged, then
#: changed. 64 hex chars each, like a real sha256.
#:
#: Deliberately disjoint from `_decoy_hash` below. The first live run of this
#: script failed four checks because `HASH_FIRST` was `"1" * 64` and decoy 1
#: was too: the decoy claimed `prov:111…` first, the real write's
#: check-then-insert correctly found it and wrote nothing, and the script
#: reported the mechanism broken when the mechanism was right and the fixture
#: was wrong. A content-addressed id means two "different" test rows with the
#: same hash ARE the same row — which is the property under test, so the
#: fixtures have to respect it.
HASH_FIRST = "a1" * 32
HASH_SECOND = "b2" * 32

#: Provenance rows written BEFORE the row under test, so the find step is not
#: satisfied by a single-row table. See the module docstring's check 1.
DECOY_COUNT = 3

#: How many rows one page of the audit reads. The audit pages to the end of
#: every table, so this is a page size and not a cap.
AUDIT_PAGE_SIZE = 200

logger = logging.getLogger("verify_provenance_live")


def _decoy_hash(index: int) -> str:
    """A 64-hex-char hash for decoy `index`, disjoint from HASH_FIRST/SECOND."""
    return f"d{index}" * 32


def roll_call_row(content_hash: str, result: str = "Passed") -> dict[str, Any]:
    return {
        "id": ROLL_CALL_ID,
        "voted_at": "2026-03-04T15:00:00Z",
        "question": f"Synthetic question ({MARKER})",
        "vote_type": "final_passage",
        "result": result,
        "tally": {"yea": 1, "nay": 0, "present": 0, "not_voting": 0},
        "topic_ids": [],
        "source_url": SOURCE_URL,
        "content_hash": content_hash,
    }


def all_rows(client: ZeroDBClient, table: str) -> list[dict[str, Any]]:
    """Every row in one table, paged to the end.

    Paged rather than one big read: `people` already holds 539 rows and the
    audit's whole claim is "every row", which a single capped read cannot
    make.
    """
    rows: list[dict[str, Any]] = []
    skip = 0
    while True:
        page = client.query_rows(table, limit=AUDIT_PAGE_SIZE, skip=skip)
        if not page.rows:
            break
        rows.extend(row.row_data for row in page.rows)
        skip += len(page.rows)
        if not page.has_more:
            break
    return rows


def audit_live_project(client: ZeroDBClient) -> tuple[int, list[str]]:
    """Every live row with a `source_url` and no provenance entry.

    Returns (rows audited, violations). The retrofit question for stories
    2.1/2.2/3.1, answered from the data: a table whose rows carry no
    `source_url` at all is exempt from the rule, and this is what establishes
    which tables those are rather than assuming it from the schema.
    """
    provenance_by_entity: dict[tuple[str, str], int] = {}
    for row in all_rows(client, PROVENANCE_TABLE):
        key = (str(row.get("entity_table")), str(row.get("entity_id")))
        provenance_by_entity[key] = provenance_by_entity.get(key, 0) + 1

    audited = 0
    violations: list[str] = []
    for table in sorted(table.table_name for table in client.list_tables(limit=200)):
        if table == PROVENANCE_TABLE:
            continue
        rows = all_rows(client, table)
        sourced = [row for row in rows if row.get("source_url")]
        audited += len(rows)
        logger.info("  %-24s %4d row(s), %d with a source_url", table, len(rows), len(sourced))
        for row in sourced:
            row_id = str(row.get("id"))
            if (table, row_id) not in provenance_by_entity:
                violations.append(
                    f"{table} row {row_id!r} carries source_url "
                    f"{row.get('source_url')!r} with no provenance_log entry"
                )
    return audited, violations


def provenance_entries_for(client: ZeroDBClient, entity_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in all_rows(client, PROVENANCE_TABLE)
        if row.get("entity_table") == "roll_calls" and row.get("entity_id") == entity_id
    ]


def main() -> int:  # noqa: C901 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)

    sourced: Repository[RollCall] = Repository(
        client=client,
        model=RollCall,
        provenance=ProvenanceContext(adapter_version=ADAPTER_VERSION),
    )
    unprovenanced: Repository[RollCall] = Repository(client=client, model=RollCall)

    roll_calls_written: list[str] = []
    provenance_written: list[str] = []
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        # --- 0. audit the project as it stands ----------------------------
        logger.info("auditing every live table for untraced source_url rows:")
        audited, violations = audit_live_project(client)
        check(
            "no live row carries a source_url without a provenance_log entry",
            not violations,
            "; ".join(violations[:5]),
        )
        logger.info("  audited %d live row(s)", audited)

        # --- 1. decoys, so the find step is really exercised --------------
        for index in range(DECOY_COUNT):
            decoy_id = provenance_id(_decoy_hash(index))
            client.insert_row(
                PROVENANCE_TABLE,
                {
                    "id": decoy_id,
                    "entity_table": "roll_calls",
                    "entity_id": f"{ROLL_CALL_ID}-decoy-{index}",
                    "source_url": f"{SOURCE_URL}-decoy-{index}",
                    "fetched_at": "2026-10-16T00:00:00+00:00",
                    "adapter_version": ADAPTER_VERSION,
                },
            )
            provenance_written.append(decoy_id)
        total = len(all_rows(client, PROVENANCE_TABLE))
        check(
            "provenance_log holds several rows, so the find step is exercised",
            total > 1,
            f"the table holds {total} row(s); with one row the checks below are vacuous",
        )

        # --- 2. a sourced write produces its provenance entry -------------
        stored = sourced.upsert(roll_call_row(HASH_FIRST))
        roll_calls_written.append(str(stored.id))
        provenance_written.append(provenance_id(HASH_FIRST))

        entries = provenance_entries_for(client, ROLL_CALL_ID)
        check(
            "writing a row with a source_url also wrote its provenance entry",
            len(entries) == 1,
            f"found {len(entries)} entries, expected 1",
        )
        if entries:
            entry = entries[0]
            check(
                "the entry records the right source_url and adapter",
                entry.get("source_url") == SOURCE_URL
                and entry.get("adapter_version") == ADAPTER_VERSION
                and bool(entry.get("fetched_at")),
                f"entry is {entry!r}",
            )
            check(
                "the entry's id is the documented content-addressed pattern",
                entry.get("id") == provenance_id(HASH_FIRST),
                f"id is {entry.get('id')!r}, expected {provenance_id(HASH_FIRST)!r}",
            )

        # --- 3. identical content adds nothing ----------------------------
        sourced.upsert(roll_call_row(HASH_FIRST))
        entries = provenance_entries_for(client, ROLL_CALL_ID)
        check(
            "re-writing identical content adds no second provenance entry",
            len(entries) == 1,
            f"found {len(entries)} entries, expected 1 — the content-addressed "
            "find step is failing live, so every re-ingest would duplicate",
        )

        # --- 4. changed content appends, keeping the first ----------------
        sourced.upsert(roll_call_row(HASH_SECOND, result="Agreed to"))
        provenance_written.append(provenance_id(HASH_SECOND))
        entries = provenance_entries_for(client, ROLL_CALL_ID)
        check(
            "changed content appends a second provenance entry",
            len(entries) == 2,
            f"found {len(entries)} entries, expected 2",
        )
        check(
            "the first entry survived, so the log is append-only live",
            {entry.get("id") for entry in entries}
            == {provenance_id(HASH_FIRST), provenance_id(HASH_SECOND)},
            f"ids are {sorted(str(entry.get('id')) for entry in entries)}",
        )

        # --- 5. a sourced write with no context is refused ----------------
        refused_id = f"{ROLL_CALL_ID}-refused"
        try:
            unprovenanced.upsert({**roll_call_row(HASH_FIRST), "id": refused_id})
        except MissingProvenanceError as exc:
            check(
                "a sourced write with no ProvenanceContext is refused",
                exc.table == "roll_calls",
                f"refused, but naming table {exc.table!r}",
            )
        else:
            roll_calls_written.append(refused_id)
            check(
                "a sourced write with no ProvenanceContext is refused",
                False,
                "the write was ACCEPTED — untraceable evidence reached live storage",
            )
        check(
            "the refused write left no row behind",
            client.get_row("roll_calls", refused_id) is None,
            f"{refused_id} exists live",
        )

    except ZeroDBError as exc:
        logger.exception("the run failed against live ZeroDB: %s", exc)
        failures.append(f"unexpected ZeroDBError: {exc}")
    finally:
        logger.info("cleaning up %d row(s)", len(roll_calls_written) + len(provenance_written))
        for row_id in roll_calls_written:
            _delete_and_confirm(client, "roll_calls", row_id, failures)
        # `Repository.delete` refuses append-only tables, correctly; these rows
        # document a fetch that never happened, so they go through the client.
        for row_id in provenance_written:
            _delete_and_confirm(client, PROVENANCE_TABLE, row_id, failures)

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
