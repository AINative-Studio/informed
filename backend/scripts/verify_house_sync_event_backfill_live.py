#!/usr/bin/env python3
"""Verify stories 5.6/5.7/5.8 against the LIVE Congress.gov API and ZeroDB project.

    set -a; . local.env; set +a
    python backend/scripts/verify_house_sync_event_backfill_live.py

**This is the first live verification of the House roll-call pipeline in this
repo's history.** Story 5.1 built this adapter against a fixture derived from
the endpoint's documentation and said plainly that no `CONGRESS_GOV_API_KEY`
was available and the loader "has NOT been run against the live API". A real,
working key exists now, and running this script is what turned up the two
silent defects documented in `scripts/backfill_house_rollcalls.py` and fixed
in this branch.

What it proves, in order:

  1. **The live list endpoint is reachable and reports a real total for every
     congress/session in the backfill plan** (story 5.8's `pagination.count`,
     which is the only honest thing to reconcile against).
  2. **The paged listing collects every roll call number the source claims**,
     not the first 250. This is the defect story 5.8 found in
     `fetch_roll_call_numbers`: one `limit=250` call against a 724-roll
     session silently returned 34% of it.
  3. **A real, current roll call fetches, transforms with no skips, and
     writes** to `roll_calls` with a real `source_url`, `content_hash` and a
     matching `provenance_log` entry — against the live project, not the
     in-memory fake the BDD suite uses.
  4. **The live record's tally is non-zero**, which is the `votePartyTotal`
     bare-list defect: under story 5.1's transform every live roll call
     stored yea 0 / nay 0 and nothing raised.
  5. **Story 5.7's `rollcall.ingested` event is emitted exactly once per new
     roll call** and NOT re-emitted when the same unchanged record is
     ingested again — the "exactly once" half of 5.7's criterion, measured
     against the live event log.
  6. **Story 6.2's consumer reads the event this producer emitted.** The
     point of 5.7 is closing a gap two stories flagged, so this runs the real
     `PositionRecomputeConsumer` against the real emitted event.
  7. **Story 5.8's reconciliation reports real stored-vs-source counts** for
     every congress/session in the plan.

!!! NO CLEANUP, DELIBERATELY !!!

Every roll call this script writes is REAL, PERMANENT public record (the
House's own record of how it voted), exactly like `verify_legislators_live.py`
and `verify_senate_rollcalls_live.py`. It is left in place.

Never pass credentials on the command line and never print them. Source them
into the shell (`set -a; . local.env; set +a`); this script reads
`CONGRESS_GOV_API_KEY` and ZeroDB's own credentials from the environment and
prints neither.

Exit codes:
    0  every check passed
    1  a check failed (what failed is printed, with the real numbers)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.house_rollcalls import (  # noqa: E402
    ADAPTER_VERSION,
    BACKFILL_CONGRESS_SESSIONS,
    DEFAULT_MIN_INTERVAL_SECONDS,
    HouseRollCallLoader,
    HouseRollCallSourceError,
    fetch_roll_call_item,
    fetch_roll_call_numbers,
    fetch_source_total,
    reconcile_congress_session,
    transform_roll_calls,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import RollCall  # noqa: E402
from app.scoring.positions import (  # noqa: E402
    ROLLCALL_INGESTED_TOPIC,
    PositionRecomputeConsumer,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("verify_house_sync_event_backfill_live")


@dataclass
class Checks:
    """Pass/fail accumulator, so one failure does not hide the others."""

    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def ok(self, message: str) -> None:
        logger.info("PASS: %s", message)
        self.passed.append(message)

    def bad(self, message: str) -> None:
        logger.error("FAIL: %s", message)
        self.failed.append(message)

    def check(self, condition: bool, message: str) -> bool:
        if condition:
            self.ok(message)
        else:
            self.bad(message)
        return condition


def _pacer(min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS):
    state = {"last": None}

    def pace() -> None:
        now = time.monotonic()
        last = state["last"]
        if last is not None:
            remaining = min_interval_seconds - (now - last)
            if remaining > 0:
                time.sleep(remaining)
        state["last"] = time.monotonic()

    return pace


def _events_for(client: ZeroDBClient, roll_call_id: str) -> int:
    """How many `rollcall.ingested` events name this roll call.

    Reads the live event log rather than trusting the loader's own report, so
    "exactly once" is measured where the consumer will actually see it.
    """
    page = client.list_events(topic=ROLLCALL_INGESTED_TOPIC, limit=1000)
    return sum(
        1
        for event in page.events
        if (event.event_payload or {}).get("roll_call_id") == roll_call_id
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx logs every request at INFO as "HTTP Request: GET <full url>", and
    # Congress.gov takes its credential as a QUERY PARAMETER -- so leaving
    # this at INFO prints the live API key into the terminal and into any CI
    # log or pasted transcript. Observed doing exactly that on this script's
    # first live run. Silenced to WARNING here rather than left to the
    # operator to remember: CLAUDE.md's rule is that a credential is never
    # logged, and a verification script is precisely the thing whose output
    # gets pasted into a PR.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    checks = Checks()

    api_key = os.environ.get("CONGRESS_GOV_API_KEY")
    if not api_key:
        logger.error(
            "CONGRESS_GOV_API_KEY is not set. Source it into the shell "
            "(set -a; . local.env; set +a) -- never pass it on the command line."
        )
        return 1

    pace = _pacer()

    # --- check 1: live totals for every congress/session in the plan --------
    totals: dict[tuple[int, int], int] = {}
    for congress, session in BACKFILL_CONGRESS_SESSIONS:
        try:
            pace()
            total = fetch_source_total(congress=congress, session=session, api_key=api_key)
        except HouseRollCallSourceError as exc:
            checks.bad(f"congress {congress} session {session}: could not read a total: {exc}")
            continue
        totals[(congress, session)] = total
        checks.check(
            total > 0,
            f"congress {congress} session {session}: source reports {total} roll call(s)",
        )
    logger.info(
        "source totals across the backfill plan: %s (sum %d)",
        {f"{c}-{s}": t for (c, s), t in totals.items()},
        sum(totals.values()),
    )

    # --- check 2: the paged listing collects the whole session ---------------
    # Checked against the LARGEST session in the plan, which is the one a
    # single unpaged limit=250 call would have truncated most severely.
    if totals:
        biggest = max(totals, key=lambda key: totals[key])
        congress, session = biggest
        expected = totals[biggest]
        try:
            numbers = fetch_roll_call_numbers(
                congress=congress, session=session, api_key=api_key, pace=pace
            )
            checks.check(
                len(numbers) == expected,
                f"paged listing for {congress}-{session} collected {len(numbers)} "
                f"roll call number(s) against the source's {expected}",
            )
            checks.check(
                len(set(numbers)) == len(numbers),
                f"paged listing for {congress}-{session} returned no duplicates",
            )
            checks.check(
                len(numbers) > 250,
                f"paged listing for {congress}-{session} returned more than one "
                f"page's 250 rows ({len(numbers)}), which an unpaged call could not",
            )
        except HouseRollCallSourceError as exc:
            checks.bad(f"paged listing for {congress}-{session} failed: {exc}")
            numbers = []
    else:
        numbers = []

    if not numbers:
        logger.error("no roll call numbers listed; cannot run the write checks")
        _report(checks)
        return 1

    # --- checks 3-6: ingest one real, current roll call ----------------------
    client = ZeroDBClient.from_env()
    try:
        newest = numbers[0]  # the listing is most-recent-first
        pace()
        envelope = fetch_roll_call_item(
            congress=congress, session=session, roll_call_number=newest, api_key=api_key
        )
        rows, skipped = transform_roll_calls([envelope])

        checks.check(
            not skipped and len(rows) == 1,
            f"a real {congress}-{session} roll call (#{newest}) transformed with no skips "
            f"({len(skipped)} skip(s)) -- story 5.1 skipped every live record for want "
            "of a 'url' field",
        )
        if not rows:
            _report(checks)
            return 1

        row = rows[0]
        tally = row["tally"]
        checks.check(
            (tally.yea + tally.nay + tally.present + tally.not_voting) > 0,
            f"the live record's tally is non-zero (yea {tally.yea}, nay {tally.nay}, "
            f"present {tally.present}, not_voting {tally.not_voting}) -- story 5.1's "
            "transform read the documented votePartyTotal shape and stored all zeroes",
        )
        checks.check(
            bool(row["source_url"]) and row["source_url"].startswith("https://"),
            f"the live record carries a real source_url ({row['source_url']})",
        )

        events_before = _events_for(client, row["id"])
        report = HouseRollCallLoader(client, pace=_pacer()).load(rows)
        checks.check(
            report.row_count == 1,
            f"the roll call wrote to the live project as {row['id']}",
        )

        repo: Repository[RollCall] = Repository(client=client, model=RollCall)
        stored = repo.get(row["id"])
        checks.check(stored is not None, f"{row['id']} reads back from the live project")
        if stored is not None:
            checks.check(
                bool(stored.content_hash) and len(stored.content_hash or "") == 64,
                f"{row['id']} stored a sha256 content_hash",
            )

        provenance_rows = client.query_rows("provenance_log", limit=1000)
        matching = [
            prov
            for prov in provenance_rows.rows
            if prov.row_data.get("entity_table") == "roll_calls"
            and prov.row_data.get("entity_id") == row["id"]
        ]
        checks.check(
            bool(matching),
            f"a provenance_log entry exists for {row['id']} (adapter_version {ADAPTER_VERSION})",
        )

        # --- story 5.7: exactly once -----------------------------------------
        events_after_first = _events_for(client, row["id"])
        emitted_now = events_after_first - events_before
        if events_before > 0:
            # Already ingested by an earlier run of this script or the hourly
            # job: an unchanged re-ingest must emit NOTHING, which is the
            # same criterion from the other direction and is just as good a
            # proof. Reported honestly rather than presented as a first run.
            checks.check(
                emitted_now == 0 and not report.events_emitted,
                f"{row['id']} was already ingested ({events_before} event(s) on "
                "record); re-ingesting it unchanged emitted no further event "
                "(story 5.7's 'exactly once')",
            )
        else:
            checks.check(
                emitted_now == 1 and report.events_emitted == [row["id"]],
                f"ingesting {row['id']} for the first time emitted exactly 1 "
                f"{ROLLCALL_INGESTED_TOPIC} event",
            )

            # Re-ingest the identical record: must add no second event.
            HouseRollCallLoader(client, pace=_pacer()).load(rows)
            events_after_second = _events_for(client, row["id"])
            checks.check(
                events_after_second == events_after_first,
                f"re-ingesting the unchanged {row['id']} emitted no second event "
                f"({events_after_second} total) -- story 5.6's 'reruns are no-ops' "
                "and 5.7's 'exactly once' are the same mechanism",
            )

        # --- check 6: story 6.2's consumer reads what 5.7 emitted ------------
        try:
            outcome = PositionRecomputeConsumer(client, pace=_pacer()).run(limit=50)
            checks.ok(
                f"story 6.2's PositionRecomputeConsumer read the live event log and "
                f"completed: {outcome.people_recomputed} person(s) touched, "
                f"{outcome.positions_written} position(s) written "
                f"(0 is correct until story 5.5 fills topic_ids)"
            )
        except Exception as exc:  # noqa: BLE001 - this check is "does it survive"
            checks.bad(f"story 6.2's consumer raised on a real emitted event: {exc}")

        # --- check 7: story 5.8's reconciliation -----------------------------
        logger.info("--- story 5.8 reconciliation (stored vs source) ---")
        for congress_n, session_n in BACKFILL_CONGRESS_SESSIONS:
            pace()
            try:
                reconciliation = reconcile_congress_session(
                    client, congress=congress_n, session=session_n, api_key=api_key
                )
            except HouseRollCallSourceError as exc:
                checks.bad(f"could not reconcile {congress_n}-{session_n}: {exc}")
                continue
            logger.info("reconciliation: %s", reconciliation.summary())
            # NOT asserted as a pass/fail here: whether the backfill has
            # actually been run to completion is the backfill script's
            # business, and this script must report the real number rather
            # than fail for a backfill nobody has started yet.
    except ZeroDBError as exc:
        checks.bad(f"a live ZeroDB call failed: {exc}")
    finally:
        client.close()

    return _report(checks)


def _report(checks: Checks) -> int:
    logger.info("--- %d passed, %d failed ---", len(checks.passed), len(checks.failed))
    for failure in checks.failed:
        logger.error("still failing: %s", failure)
    return 1 if checks.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
