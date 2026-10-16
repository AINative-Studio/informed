#!/usr/bin/env python3
"""Verify the reference-data seed against the LIVE "informed" project (2.1).

Stories 1.3, 1.4 and 1.5 each found a real discrepancy between what the fake
agreed to and what ZeroDB actually does, so story 2.1's acceptance criterion
("rows `us-federal`, `us-house` and `us-senate` exist after bootstrap") is
checked against the real API as well as the fake.

What it proves, in order:

  1. **All three rows exist with the right fields.** Read back through the
     repository, so the stored row is also proven to still validate against
     the model rather than merely being present.
  2. **Running the seed twice leaves exactly three rows.** Counted from an
     unfiltered live read, not through `Repository.get` — `get` stops at the
     first match and so cannot tell one row from two, which is the whole
     question idempotency asks. This is the check that catches ZeroDB's
     always-appending `POST /rows` leaking through the upsert's find step.
  3. **A chamber whose jurisdiction is missing is refused live.** The
     reference check (datamodel.md rule 5) is what makes the seed's ordering
     load-bearing, so it is proven live rather than assumed from the fake.

!!! NO CLEANUP, DELIBERATELY !!!

Unlike `verify_repository_live.py` and `verify_ballot_live.py`, this script
does **not** delete what it wrote, and that is the point. Those scripts wrote
synthetic `zzz-live-check-*` rows that are debris once the check passes. These
three rows are permanent production reference data: every later story (2.2's
legislator terms, 2.3's districts, 2.4's ballot) references them by id, and the
next bootstrap would simply write them back. A `us-federal` row in the live
project is this seed's output, not forgotten test debris.

The ONE row this script does clean up is the deliberately-orphaned chamber of
check 3 — and only if the reference check failed to refuse it, in which case
the row is real debris and the script reports a failure.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_seed_live.py
"""

from __future__ import annotations

import logging
import sys

from app.repositories import MissingReferenceError, Repository
from app.repositories.models import Body, Jurisdiction
from app.seed import FEDERAL_BODIES, US_FEDERAL, Seeder, seeded_row_ids
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError

#: The one row this script writes that is NOT permanent seed data: a chamber
#: pointing at a jurisdiction that does not exist, used to prove the reference
#: check refuses it. If the check works the row is never written at all; the
#: marker is here so that a row left behind by a *failing* check is obvious.
ORPHAN_BODY_ID = "zzz-live-check-2.1-orphan"
MISSING_JURISDICTION_ID = "zzz-live-check-2.1-nowhere"

logger = logging.getLogger("verify_seed_live")


def count_live_rows(client: ZeroDBClient, table: str, row_id: str) -> int:
    """How many rows in `table` carry this natural-key id, read live.

    Deliberately not `Repository.get`: that stops at the first match, and
    counting duplicates is exactly what check 2 needs.

    Reads a generous page and counts client-side because live ZeroDB's
    server-side filter matches nothing (story 1.4's finding) — the client's own
    paging fallback is what actually does the filtering.
    """
    page = client.query_rows(table, limit=500)
    return sum(1 for row in page.rows if row.row_data.get("id") == row_id)


def main() -> int:  # noqa: C901 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)

    jurisdictions: Repository[Jurisdiction] = Repository(client=client, model=Jurisdiction)
    bodies: Repository[Body] = Repository(client=client, model=Body)

    failures: list[str] = []
    orphan_written = False

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        # --- 1. seed once, then read all three rows back ------------------
        logger.info("seeding (run 1 of 2)")
        first = Seeder(client).run()
        check(
            "the seed reports three rows written",
            first.row_count == 3,
            f"reported {first.row_count}",
        )

        live_jurisdiction = jurisdictions.get("us-federal")
        check(
            "us-federal exists live with the seeded fields",
            live_jurisdiction is not None and live_jurisdiction == US_FEDERAL,
            f"stored row is {live_jurisdiction!r}",
        )

        for expected in FEDERAL_BODIES:
            assert expected.id is not None
            live_body = bodies.get(expected.id)
            check(
                f"{expected.id} exists live with the seeded fields",
                live_body is not None and live_body == expected,
                f"stored row is {live_body!r}",
            )

        # --- 2. seed a SECOND time: still exactly three rows -------------
        logger.info("seeding (run 2 of 2) — this is the idempotency check")
        second = Seeder(client).run()
        check(
            "the second run reports the same three rows",
            second.row_count == 3,
            f"reported {second.row_count}",
        )

        for table, row_id in sorted(seeded_row_ids()):
            count = count_live_rows(client, table, row_id)
            check(
                f"exactly one live row carries {table}/{row_id} after two runs",
                count == 1,
                f"live row count is {count}, expected 1",
            )

        # The fields must also survive the second write, not just the count:
        # an upsert that found the row and replaced it with something else
        # would pass the count check and still be wrong.
        re_read = jurisdictions.get("us-federal")
        check(
            "us-federal still carries the seeded fields after the second run",
            re_read is not None and re_read == US_FEDERAL,
            f"stored row is {re_read!r}",
        )

        # --- 3. a chamber with a missing jurisdiction is refused ---------
        already_there = client.get_row("jurisdictions", MISSING_JURISDICTION_ID) is not None
        check(
            "the jurisdiction used for the negative case really is absent live",
            not already_there,
            f"{MISSING_JURISDICTION_ID} unexpectedly exists; the next check would be vacuous",
        )

        try:
            bodies.upsert(
                {
                    "id": ORPHAN_BODY_ID,
                    "jurisdiction_id": MISSING_JURISDICTION_ID,
                    "name": "Orphan Chamber (live check)",
                    "chamber": "lower",
                    "seats": 1,
                }
            )
        except MissingReferenceError as exc:
            check(
                "a chamber referencing a missing jurisdiction is refused live",
                exc.referenced_table == "jurisdictions"
                and exc.referenced_id == MISSING_JURISDICTION_ID,
                f"refused, but naming {exc.referenced_table}/{exc.referenced_id}",
            )
        else:
            orphan_written = True
            check(
                "a chamber referencing a missing jurisdiction is refused live",
                False,
                "the write was ACCEPTED — the reference check does not work live",
            )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        # The seeded rows are PERMANENT and are deliberately left in place —
        # see this module's docstring. The only thing cleaned up is the orphan
        # row, which exists only if check 3 failed.
        if orphan_written:
            logger.info("cleaning up the orphan row left by the failed reference check")
            try:
                client.delete_row("bodies", ORPHAN_BODY_ID)
            except ZeroDBNotFoundError:
                logger.info("  already gone: bodies/%s", ORPHAN_BODY_ID)
            except ZeroDBError as exc:
                failures.append(f"could not delete bodies/{ORPHAN_BODY_ID}: {exc}")
                logger.error("  FAILED to delete bodies/%s: %s", ORPHAN_BODY_ID, exc)

        logger.info(
            "LEFT IN PLACE (intentionally): %s — permanent reference data, not test debris",
            ", ".join(f"{table}/{row_id}" for table, row_id in sorted(seeded_row_ids())),
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
