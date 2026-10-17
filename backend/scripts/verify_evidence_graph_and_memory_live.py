#!/usr/bin/env python3
"""Verify stories 13.1, 13.3 and 13.5 against the LIVE "informed" project.

Three things this proves that no test against the in-memory fake can:

  1. **13.1 — the registered ontology IS datamodel.md's predicate table.** Read
     from the live project, compared field by field against
     `app.evidence_graph.DOCUMENTED_PREDICATE_TABLE`. A fake can only prove the
     comparison logic works; only this proves the live project is correct.
  2. **13.1 — real graph edges from real rows, and they traverse.** Reads a
     handful of the live `candidacies` rows, writes their
     `person -> ran_in -> candidacy` edges through `EvidenceGraphWriter`, then
     traverses from each person and confirms the candidacy comes back BY ITS
     NoSQL ROW ID — which is the "canonical names are NoSQL ids" criterion
     made true against the live graph rather than asserted. **These edges are
     left in place**: they are correct, public-record evidence derived from
     real rows, and story 13.1's whole point is that they exist. Nothing here
     is synthetic.
  3. **13.5 — the 1-day window and the anonymous-mode refusal, live.** Writes
     synthetic turns under fresh session uuids (one dated now, one backdated
     past the window), confirms the read guard hides the expired one, confirms
     the sweep deletes it and keeps the fresh one, confirms an anonymous call
     makes no request at all, and DELETES EVERY TURN IT WROTE before exiting —
     on success or failure.

Story 13.3's policy layer needs no live verification: it is a pure function
with no I/O, and the 50-prompt red-team suite exercises it in CI. Running it
here anyway, once, confirms the deployed module behaves as the suite says.

Run it with the live credentials sourced into the shell, never passed on the
command line or printed::

    set -a; . local.env; set +a
    python backend/scripts/verify_evidence_graph_and_memory_live.py

CLEANUP: every memory row this script writes is deleted before it exits,
including on failure, and the session is re-read to confirm it is empty. The
content is marked `zzz-live-check-13.5` so a row left by a crashed run is
obvious and greppable. **No real voter data is written** — the turns are two
fixed strings about a bill, which is what prd.md's "never put real voter data
in fixtures, logs or test output" requires.

The graph edges are NOT cleaned up, deliberately — see point 2 above. If you
need to re-run from a clean graph, that is a ZeroDB-side operation; the writer
is idempotent so a re-run converges rather than duplicating.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent_memory import (  # noqa: E402
    RETENTION_DAYS,
    SessionMemoryService,
    sweep_expired_session_memory,
)
from app.agent_policy import classify  # noqa: E402
from app.agent_policy.red_team import RED_TEAM_PROMPTS  # noqa: E402
from app.evidence_graph import DOCUMENTED_PREDICATE_TABLE, EvidenceGraphWriter  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError  # noqa: E402

MARKER = "zzz-live-check-13.5"

#: How many real candidacies to write graph edges for. Small: the point is to
#: prove the write and the traversal work against the live API, and
#: `scripts/write_evidence_graph.py` is the thing that writes them all.
SAMPLE_CANDIDACIES = 3

logger = logging.getLogger("verify_evidence_graph_and_memory_live")


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - a linear script of checks reads better whole
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)

    failures: list[str] = []
    written_memory_ids: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            logger.error("FAIL  %s%s", label, f" — {detail}" if detail else "")
            failures.append(label)

    try:
        # =================================================================
        # 13.1 — the live ontology is datamodel.md's predicate table
        # =================================================================
        logger.info("")
        logger.info("--- 13.1: the registered ontology ---")
        writer = EvidenceGraphWriter(client)
        report = writer.verify_ontology()
        logger.info("%s", report.summary())
        check("an ontology is registered on the live project", report.registered)
        check(
            "every documented predicate is registered with its documented types",
            not report.missing_predicates and not report.mistyped_predicates,
            f"missing={report.missing_predicates} mistyped={report.mistyped_predicates}",
        )
        check(
            "no predicate outside datamodel.md's table is registered",
            not report.unexpected_predicates,
            f"unexpected={report.unexpected_predicates}",
        )
        check(
            "every entity type either side of a documented predicate is registered",
            not report.missing_entity_types,
            f"missing={report.missing_entity_types}",
        )

        live = client.graph_get_ontology()
        if live is not None:
            logger.info(
                "live ontology: %d entity types, %d predicates",
                len(live.entity_types),
                len(live.predicates),
            )
            for predicate in sorted(DOCUMENTED_PREDICATE_TABLE):
                spec = DOCUMENTED_PREDICATE_TABLE[predicate]
                registered = live.predicates.get(predicate)
                logger.info(
                    "  %-13s %s -> %s  [live: %s]",
                    predicate,
                    spec["source"],
                    spec["target"],
                    registered,
                )

        # =================================================================
        # 13.1 — real ran_in edges from real candidacies, and they traverse
        # =================================================================
        logger.info("")
        logger.info("--- 13.1: real graph edges from real candidacy rows ---")
        page = client.query_rows("candidacies", limit=SAMPLE_CANDIDACIES)
        rows = [row.row_data for row in page.rows]
        logger.info(
            "live candidacies table holds %d row(s); writing edges for %d",
            page.total,
            len(rows),
        )
        check("there are real candidacy rows to write edges from", bool(rows))

        pairs: list[tuple[str, str]] = []
        for row in rows:
            person_id = str(row.get("person_id") or "")
            candidacy_id = str(row.get("id") or "")
            if not person_id or not candidacy_id:
                continue
            writer.write_candidacy_evidence(
                person_id=person_id,
                candidacy_id=candidacy_id,
                party=row.get("party"),
                is_incumbent=bool(row.get("is_incumbent")),
            )
            pairs.append((person_id, candidacy_id))
            logger.info("wrote %s -ran_in-> %s", person_id, candidacy_id)

        check("real ran_in edges were written", bool(pairs))

        for person_id, candidacy_id in pairs:
            traversal = client.graph_traverse(entity=person_id, max_hops=1)
            reached = {node.entity for node in traversal.nodes}
            check(
                f"traversing from {person_id} reaches its candidacy by row id",
                candidacy_id in reached,
                f"reached {sorted(reached)[:5]}",
            )
            # "Canonical names are NoSQL ids" made concrete: the label the
            # traversal returned is read back as a row id, with no mapping.
            resolved = client.get_row("candidacies", candidacy_id)
            check(
                f"the traversed name {candidacy_id} resolves to a candidacies row",
                resolved is not None,
            )

        # =================================================================
        # 13.3 — the policy layer, run once against the deployed module
        # =================================================================
        logger.info("")
        logger.info("--- 13.3: the no-recommendations policy ---")
        refused = [prompt for prompt in RED_TEAM_PROMPTS if not classify(prompt).allowed]
        check(
            f"all {len(RED_TEAM_PROMPTS)} red-team prompts are refused",
            len(refused) == len(RED_TEAM_PROMPTS),
            f"{len(RED_TEAM_PROMPTS) - len(refused)} slipped through",
        )
        allowed_sample = [
            "Who voted for the 2025 appropriations bill?",
            "How did my representative vote on the infrastructure bill?",
            "Why did this candidate score 62 on housing?",
        ]
        check(
            "legitimate evidence questions are not refused",
            all(classify(prompt).allowed for prompt in allowed_sample),
        )
        decision = classify("Who should I vote for?")
        logger.info("refusal: %s", decision.refusal)

        # =================================================================
        # 13.5 — the 1-day window and the anonymous refusal, live
        # =================================================================
        logger.info("")
        logger.info("--- 13.5: session memory, 1-day retention, anonymous mode ---")
        service = SessionMemoryService(client)
        now = datetime.now(UTC)
        fresh_session = service.new_session_id()
        stale_session = service.new_session_id()
        anon_session = service.new_session_id()

        fresh = service.remember(
            session_id=fresh_session,
            role="user",
            content=f"{MARKER}: how did the member vote on the bill?",
            anonymous=False,
            now=now,
        )
        check("a turn in an identified session is stored", fresh.stored)
        if fresh.memory_id:
            written_memory_ids.append(fresh.memory_id)
        logger.info("fresh turn expires_at: %s", fresh.expires_at)
        expected_prefix = (now + timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M")
        check(
            f"the stored expiry is exactly {RETENTION_DAYS} day out",
            fresh.expires_at is not None and fresh.expires_at.startswith(expected_prefix),
            f"got {fresh.expires_at}, expected to start {expected_prefix}",
        )

        stale = service.remember(
            session_id=stale_session,
            role="user",
            content=f"{MARKER}: an older question about the same bill",
            anonymous=False,
            now=now - timedelta(hours=25),
        )
        if stale.memory_id:
            written_memory_ids.append(stale.memory_id)

        # NOTE on how the backdating actually lands live: the endpoint stamps
        # its own `created_at` and ignores one sent in the body, so a backdated
        # turn's age lives ONLY in the `expires_at` marker in
        # `memory_metadata`. That is fine and in fact is the path worth
        # exercising — `expiry_of` reads the marker first and falls back to
        # `created_at` — but it means this check proves the marker path, not
        # the fallback path. The fallback is covered against the fake, where a
        # row's `created_at` can be set.
        before = client.list_memories(session_id=fresh_session)
        check("the fresh turn is in ZeroDB", before.total == 1, f"total={before.total}")

        recalled_fresh = service.recall(session_id=fresh_session, anonymous=False, now=now)
        check("the read guard returns the fresh turn", len(recalled_fresh) == 1)

        recalled_stale = service.recall(session_id=stale_session, anonymous=False, now=now)
        check(
            "the read guard hides a turn past its 1-day window, while the row still exists",
            recalled_stale == [],
            f"got {len(recalled_stale)} turn(s)",
        )
        still_there = client.list_memories(session_id=stale_session)
        check(
            "...and the row genuinely does still exist at that point",
            still_there.total == 1,
            f"total={still_there.total}",
        )

        anon = service.remember(
            session_id=anon_session,
            role="user",
            content=f"{MARKER}: this must never be written",
            anonymous=True,
            now=now,
        )
        check("an anonymous turn is not stored", anon.stored is False)
        check("...and the refusal names anonymous mode", anon.reason == "anonymous mode")
        anon_rows = client.list_memories(session_id=anon_session)
        check(
            "nothing at all was written for the anonymous session",
            anon_rows.total == 0,
            f"total={anon_rows.total}",
        )
        check(
            "an anonymous recall returns nothing",
            service.recall(session_id=anon_session, anonymous=True, now=now) == [],
        )

        # The sweep, against the live project. Scoped by checking the two
        # sessions afterwards rather than by trusting the counts, since the
        # live project holds other memory rows this sweep legitimately also
        # ages (see the summary line it prints).
        result = sweep_expired_session_memory(client, now=now)
        logger.info("sweep: %s", result.summary())
        after_stale = client.list_memories(session_id=stale_session)
        after_fresh = client.list_memories(session_id=fresh_session)
        check(
            "the sweep deleted the turn past its window",
            after_stale.total == 0,
            f"total={after_stale.total}",
        )
        check(
            "the sweep kept the turn inside its window",
            after_fresh.total == 1,
            f"total={after_fresh.total}",
        )

        second = sweep_expired_session_memory(client, now=now)
        logger.info("second sweep: %s", second.summary())
        check(
            "the sweep is idempotent (a second run deletes nothing)",
            second.deleted == 0,
            f"deleted={second.deleted}",
        )

        forgotten = service.forget_session(session_id=fresh_session)
        check("forget_session deletes the session's turns", forgotten >= 1)
        check(
            "...and the session is then empty",
            client.list_memories(session_id=fresh_session).total == 0,
        )

    except ZeroDBError as exc:
        logger.exception("a live call failed: %s", exc)
        failures.append(f"live call failed: {exc}")

    finally:
        # =================================================================
        # CLEANUP: every memory row this script wrote, on success or failure.
        # =================================================================
        logger.info("")
        logger.info("--- cleanup ---")
        remaining = 0
        for memory_id in written_memory_ids:
            try:
                client.delete_memory(memory_id=memory_id)
                logger.info("deleted memory row %s", memory_id)
            except ZeroDBNotFoundError:
                logger.info("memory row %s was already gone", memory_id)
            except ZeroDBError as exc:
                remaining += 1
                logger.error("could not delete memory row %s: %s", memory_id, exc)
        if remaining:
            failures.append(f"{remaining} memory row(s) could not be deleted")
        else:
            logger.info("every memory row this run wrote is gone")

        # Belt and braces: nothing carrying this run's marker should survive.
        leftovers = [
            record.memory_id
            for record in client.list_memories(limit=100).memories
            if MARKER in (record.content or "")
        ]
        if leftovers:
            logger.warning("rows still carrying %s: %s", MARKER, leftovers)
            for memory_id in leftovers:
                try:
                    client.delete_memory(memory_id=memory_id)
                    logger.info("deleted leftover %s", memory_id)
                except ZeroDBError as exc:
                    logger.error("could not delete leftover %s: %s", memory_id, exc)

    logger.info("")
    if failures:
        logger.error("%d check(s) FAILED:", len(failures))
        for failure in failures:
            logger.error("  - %s", failure)
        return 1
    logger.info("every live check PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
