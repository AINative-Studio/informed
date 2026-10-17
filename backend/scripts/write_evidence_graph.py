#!/usr/bin/env python3
"""Write the evidence graph from the rows that exist today (story 13.1).

datamodel.md's evidence graph is "written by the ingest and verdict agents".
Those agents do not exist as such — what exists is this codebase's ingest
modules, and this script is the one that walks what they have already loaded
and writes the corresponding graph. It is the production caller of
`app.evidence_graph.EvidenceGraphWriter`, and it is deliberately thin: every
rule about what may be written lives in the writer, which refuses an edge the
ontology does not know (see that module's docstring).

===============================================================================
WHAT IT CAN ACTUALLY WRITE TODAY, AND WHAT IS BLOCKED
===============================================================================

**`person -> ran_in -> candidacy`: real, writable now.** Both endpoints exist
in the live project — the `people` rows story 2.2 loaded and the `candidacies`
rows story 2.6 loaded — so this is the predicate this script writes from real
data, one edge per candidacy row.

**`candidacy -> made_promise -> promise` and everything downstream of a
promise: writable the moment promises exist.** The `promises` table is created
(story 1.3) and empty: story 9.1 archived 1,318 real campaign pages into
`promise_sources`, and story 9.3's extractor — in flight in a separate PR — is
what turns those into rows. This script reads `promises` and writes an edge per
row it finds, which is zero today and will be the real count with no code
change here.

**`person -> voted -> roll_call`: blocked on data that does not exist.** A
`voted` edge needs a `member_votes` row, which needs a roll call from
Congress.gov, which needs `CONGRESS_GOV_API_KEY` — absent in every environment
(`app.scheduling.jobs` registers `load_house_rollcalls` and reports it BLOCKED
for exactly this reason). The writer's `voted` path exists and is guarded by
datamodel.md's "only where evidence needs them" rule, and this script will
write those edges when the votes land. **No synthetic vote is written to make
the graph look fuller.** An uncitable edge in the evidence graph is worse than
a missing one: the agent's whole contract is that every claim resolves to a
primary source.

So a run today reports real `ran_in` edges and zeros for the rest, and the
zeros are the honest answer rather than a failure.

===============================================================================
RUNNING IT
===============================================================================

With the live credentials sourced into the shell — never passed on the command
line, never printed::

    set -a; . local.env; set +a

    # See what would be written, writing nothing:
    python backend/scripts/write_evidence_graph.py --dry-run

    # Write for real, bounded to a sample while checking it out:
    python backend/scripts/write_evidence_graph.py --limit 5

    # Write every edge the live rows support:
    python backend/scripts/write_evidence_graph.py

Idempotent: ZeroDB's `graph_upsert_entity` is a real upsert and
`graph_create_edge` does not duplicate an existing triple, so re-running
converges rather than accumulating. Paced at ~1.1s per call like every other
load script in this codebase, because the live ZeroDB tier throttles around 60
req/min (datamodel.md's story-1.2 finding).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evidence_graph import EvidenceGraphWriter  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("write_evidence_graph")

#: Rows per read. Well under the 100-row cap the memory endpoint has and
#: comfortable for the table endpoints, which accept more.
PAGE_SIZE = 100


def _pacer(min_interval_seconds: float = 1.1):
    """The same last-call-relative pacer every load script here inlines."""
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


def _all_rows(client: ZeroDBClient, table: str, *, limit: int | None, pace) -> list[dict]:
    """Every row of one table, paged, up to `limit`.

    Unfiltered reads only: datamodel.md's open question records that ZeroDB's
    server-side `/rows` filter matches nothing, so a filtered read runs on the
    client's fallback. This script needs whole tables anyway.
    """
    rows: list[dict] = []
    skip = 0
    while True:
        pace()
        page = client.query_rows(table, limit=PAGE_SIZE, skip=skip)
        if not page.rows:
            return rows
        for row in page.rows:
            rows.append(row.row_data)
            if limit is not None and len(rows) >= limit:
                return rows
        if not page.has_more:
            return rows
        skip += len(page.rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be written and write nothing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="write at most this many edges per predicate (for a first look)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = ZeroDBClient.from_env()
    writer = EvidenceGraphWriter(client)
    pace = _pacer()

    logger.info("project: %s", client.project_id)

    # The ontology has to be right before a single edge is written: an edge
    # validated against a constant that does not match what is registered
    # would be refused server-side at best and silently mistyped at worst.
    ontology = writer.verify_ontology()
    logger.info("ontology: %s", ontology.summary())
    if not ontology.ok:
        logger.error("refusing to write: the registered ontology is not the documented one")
        logger.error("run `python backend/scripts/bootstrap.py` to re-register it")
        return 1

    written_edges = 0
    skipped: list[str] = []

    # --- person -> ran_in -> candidacy, from real candidacy rows ----------
    candidacies = _all_rows(client, "candidacies", limit=args.limit, pace=pace)
    logger.info("read %d candidacy row(s)", len(candidacies))
    for row in candidacies:
        person_id = row.get("person_id")
        candidacy_id = row.get("id")
        if not person_id or not candidacy_id:
            skipped.append(f"candidacy {candidacy_id!r} has no person_id")
            continue
        if args.dry_run:
            written_edges += 1
            continue
        try:
            pace()
            writer.write_candidacy_evidence(
                person_id=person_id,
                candidacy_id=candidacy_id,
                party=row.get("party"),
                is_incumbent=bool(row.get("is_incumbent")),
            )
        except (ZeroDBError, ValueError) as exc:
            # One bad row must not abandon the rest — the same tolerance every
            # loader in this codebase applies to a per-record failure.
            skipped.append(f"candidacy {candidacy_id}: {exc}")
            continue
        written_edges += 1

    # --- candidacy -> made_promise -> promise, zero today -----------------
    promises = _all_rows(client, "promises", limit=args.limit, pace=pace)
    logger.info("read %d promise row(s)", len(promises))
    for row in promises:
        promise_id = row.get("id")
        candidacy_id = row.get("candidacy_id")
        if not promise_id or not candidacy_id:
            skipped.append(f"promise {promise_id!r} has no candidacy_id")
            continue
        if args.dry_run:
            written_edges += 1
            continue
        try:
            pace()
            writer.write_promise_evidence(
                candidacy_id=candidacy_id,
                promise_id=promise_id,
                source_id=row.get("source_id") or "",
                captured_at=row.get("captured_at") or "",
                stance_id=row.get("stance_id"),
                stance_confidence=float(row.get("confidence") or 1.0),
            )
        except (ZeroDBError, ValueError) as exc:
            skipped.append(f"promise {promise_id}: {exc}")
            continue
        written_edges += 1

    # --- person -> voted -> roll_call, blocked on Congress.gov ------------
    member_votes = _all_rows(client, "member_votes", limit=args.limit, pace=pace)
    if member_votes:
        logger.info(
            "read %d member_vote row(s); a voted edge is written only where a promise "
            "or bill-stance edge reaches the roll call",
            len(member_votes),
        )
        for row in member_votes:
            result = writer.write_vote_evidence(
                person_id=row.get("person_id", ""),
                roll_call_id=row.get("roll_call_id", ""),
                position=row.get("position", ""),
            )
            if result.written:
                written_edges += 1
    else:
        logger.info(
            "read 0 member_vote row(s): no voted edges to write. This is blocked on "
            "CONGRESS_GOV_API_KEY, not on this code — see the module docstring."
        )

    logger.info(
        "%s %d edge(s); %d row(s) skipped",
        "would write" if args.dry_run else "wrote",
        written_edges,
        len(skipped),
    )
    for reason in skipped[:20]:
        logger.warning("skipped: %s", reason)
    if len(skipped) > 20:
        logger.warning("... and %d more", len(skipped) - 20)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
