#!/usr/bin/env python3
"""CLI entrypoint for story 5.8's 118th/119th Congress House backfill.

    set -a; . local.env; set +a
    python backend/scripts/backfill_house_rollcalls.py            # all four sessions
    python backend/scripts/backfill_house_rollcalls.py --congress 118 --session 1
    python backend/scripts/backfill_house_rollcalls.py --reconcile-only

    backlog.md 5.8: "As a voter, I want an incumbent's full current-era
    record, so that the score isn't based on a few weeks of votes."
    Acceptance: "Backfill completes; row counts match the source totals."

Why this is a separate script from `load_house_rollcalls.py`
=============================================================

`load_house_rollcalls.py` is story 5.1's bounded sampler: its `--limit`
defaults to 20 and its own docstring says a full backfill "is explicitly
story 5.8's job, not this story's". This is that job. The difference is not
just the limit:

  * it walks `BACKFILL_CONGRESS_SESSIONS` (118-1, 118-2, 119-1, 119-2)
    rather than one congress/session;
  * it RECONCILES, which is the acceptance criterion. "The backfill ran
    without raising" is not the criterion and would not have caught what
    this story actually found (see below);
  * it is resumable. ~1,917 item fetches paced at >=1.1s is over 35 minutes
    of wall time, so a run WILL be interrupted sooner or later. Because
    every write is an idempotent upsert keyed by a deterministic id, a
    re-run is safe and `--skip-stored` makes it cheap: already-stored roll
    calls are not re-fetched at all.

What the reconciliation is checked against
===========================================

The source's own `pagination.count` on the list endpoint — not a number
hard-coded here, and not this script's own fetch count (which would make the
check circular: a loader that silently skips every record would "match" its
own zero). Confirmed live 2026-10-19:

    118-1: 724    119-1: 362
    118-2: 517    119-2: 314       total 1,917

THE DEFECTS THIS STORY FOUND, which is why reconciliation and not "it ran"
==========================================================================

This is the first story in this repo to have a working
`CONGRESS_GOV_API_KEY` — story 5.1 shipped fully tested against a fixture
built from the endpoint's DOCUMENTATION and explicitly never ran live. The
documentation is wrong in two places, and both failures are SILENT:

  1. `votePartyTotal` is a bare list, not `{"item": [...]}`. Every live roll
     call would have stored `yea 0 / nay 0 / present 0 / not_voting 0` — a
     valid `RollCall` row that scoring would read as a unanimously
     abstaining House.
  2. No record carries a `url` field at all. `transform_roll_call` required
     it, so `transform_roll_calls` collected 100% of live records as skips
     and the loader wrote nothing, while the process exited 0.

Under defect 2 a "backfill completed" message was reachable with zero rows
written. The reconciliation is what turns that into
`0 stored / 724 at source -- MISMATCH (+724)` and a nonzero exit.

Exit codes:
    0  every requested congress/session reconciles against the source total
    1  a source fetch failed, no CONGRESS_GOV_API_KEY was set, a ZeroDB error
       stopped the run, or a reconciliation did NOT match
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.house_rollcalls import (  # noqa: E402
    BACKFILL_CONGRESS_SESSIONS,
    DEFAULT_MIN_INTERVAL_SECONDS,
    HouseRollCallLoader,
    HouseRollCallSourceError,
    Reconciliation,
    fetch_roll_call_item,
    fetch_roll_call_numbers,
    fetch_source_total,
    reconcile_congress_session,
    roll_call_id,
    transform_roll_calls,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("backfill_house_rollcalls")


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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--congress",
        type=int,
        default=None,
        help="Only this congress (default: every congress in the backfill plan)",
    )
    parser.add_argument(
        "--session",
        type=int,
        default=None,
        choices=(1, 2),
        help="Only this session (default: both)",
    )
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Fetch no items and write nothing; just report stored vs source counts",
    )
    parser.add_argument(
        "--skip-stored",
        action="store_true",
        default=True,
        help="Do not re-fetch roll calls already stored (default: on, makes a resumed run cheap)",
    )
    parser.add_argument(
        "--no-skip-stored",
        dest="skip_stored",
        action="store_false",
        help="Re-fetch and re-upsert every roll call, even already-stored ones "
        "(use after a transform fix, to repair rows written under the old shape)",
    )
    parser.add_argument(
        "--limit-per-session",
        type=int,
        default=None,
        help="Cap how many roll calls are loaded per congress/session (for a "
        "smoke run; a capped run will correctly NOT reconcile)",
    )
    return parser.parse_args(argv)


def _targets(args: argparse.Namespace) -> list[tuple[int, int]]:
    targets = [
        (congress, session)
        for congress, session in BACKFILL_CONGRESS_SESSIONS
        if (args.congress is None or congress == args.congress)
        and (args.session is None or session == args.session)
    ]
    if not targets:
        logger.error(
            "no congress/session in the backfill plan %s matches --congress %s --session %s",
            list(BACKFILL_CONGRESS_SESSIONS),
            args.congress,
            args.session,
        )
    return targets


def _stored_ids(client: ZeroDBClient, *, congress: int, session: int) -> set[str]:
    """Which roll call ids for this congress/session are already stored.

    One paged scan up front rather than a `get_row` per roll number: a
    `--skip-stored` run over 724 roll calls would otherwise cost 724 extra
    reads to decide what to skip, which is most of what skipping was meant
    to save.
    """
    from app.ingest.house_rollcalls import HOUSE_BODY
    from app.repositories.models import RollCall

    prefix = f"{HOUSE_BODY}:{congress}-{session}:"
    found: set[str] = set()
    skip = 0
    while True:
        page = client.query_rows(RollCall.table_name, limit=1000, skip=skip)
        if not page.rows:
            break
        for row in page.rows:
            row_id = row.row_data.get("id")
            if isinstance(row_id, str) and row_id.startswith(prefix):
                found.add(row_id)
        skip += len(page.rows)
        if not page.has_more:
            break
    return found


def _backfill_one(
    client: ZeroDBClient,
    *,
    congress: int,
    session: int,
    api_key: str,
    skip_stored: bool,
    limit_per_session: int | None,
) -> None:
    """Fetch and load every roll call for one congress/session."""
    pace = _pacer()
    total = fetch_source_total(congress=congress, session=session, api_key=api_key)
    numbers = fetch_roll_call_numbers(
        congress=congress, session=session, api_key=api_key, pace=pace
    )
    logger.info(
        "congress %d session %d: source reports %d roll call(s), listing returned %d",
        congress,
        session,
        total,
        len(numbers),
    )

    already = _stored_ids(client, congress=congress, session=session) if skip_stored else set()
    wanted = [
        n
        for n in sorted(numbers)
        if roll_call_id(congress=congress, session=session, roll_call_number=n) not in already
    ]
    if skip_stored and already:
        logger.info(
            "congress %d session %d: %d already stored, %d to fetch",
            congress,
            session,
            len(already),
            len(wanted),
        )
    if limit_per_session is not None:
        wanted = wanted[:limit_per_session]

    loader = HouseRollCallLoader(client, pace=_pacer())
    written = 0
    skipped_records = 0
    events = 0
    # Loaded in batches rather than one list of ~700 envelopes, so an
    # interrupted run has already committed everything it fetched instead of
    # losing the whole session's work at the final `load`.
    batch: list[dict] = []
    for index, number in enumerate(wanted, start=1):
        pace()
        try:
            batch.append(
                fetch_roll_call_item(
                    congress=congress,
                    session=session,
                    roll_call_number=number,
                    api_key=api_key,
                )
            )
        except HouseRollCallSourceError as exc:
            # One unreachable roll call must not abandon the other ~700.
            # Counted and logged; the reconciliation at the end is what
            # decides whether the gap matters.
            logger.warning(
                "congress %d session %d roll %d could not be fetched: %s",
                congress,
                session,
                number,
                exc,
            )
            skipped_records += 1
            continue

        if len(batch) >= 25 or index == len(wanted):
            rows, skips = transform_roll_calls(batch)
            skipped_records += len(skips)
            for skip_record in skips:
                logger.warning(
                    "congress %d session %d roll %s skipped: %s",
                    congress,
                    session,
                    skip_record.roll_call_number,
                    skip_record.reason,
                )
            report = loader.load(rows)
            written += report.row_count
            events += len(report.events_emitted)
            batch = []
            logger.info(
                "congress %d session %d: %d/%d fetched, %d written so far",
                congress,
                session,
                index,
                len(wanted),
                written,
            )

    logger.info(
        "congress %d session %d: %d roll call(s) written, %d skipped, %d event(s) emitted",
        congress,
        session,
        written,
        skipped_records,
        events,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = parse_args(argv if argv is not None else sys.argv[1:])

    targets = _targets(args)
    if not targets:
        return 1

    api_key = os.environ.get("CONGRESS_GOV_API_KEY")
    if not api_key:
        logger.error(
            "CONGRESS_GOV_API_KEY is not set. Get a free key at "
            "https://api.data.gov/signup/ and export it (set -a; . local.env; set +a) "
            "before running this script."
        )
        return 1

    client = ZeroDBClient.from_env()
    reconciliations: list[Reconciliation] = []
    try:
        for congress, session in targets:
            if not args.reconcile_only:
                try:
                    _backfill_one(
                        client,
                        congress=congress,
                        session=session,
                        api_key=api_key,
                        skip_stored=args.skip_stored,
                        limit_per_session=args.limit_per_session,
                    )
                except HouseRollCallSourceError as exc:
                    logger.error(
                        "congress %d session %d: could not load the source: %s",
                        congress,
                        session,
                        exc,
                    )
                    return 1

            reconciliation = reconcile_congress_session(
                client, congress=congress, session=session, api_key=api_key
            )
            reconciliations.append(reconciliation)
            logger.info("reconciliation: %s", reconciliation.summary())
    except ZeroDBError as exc:
        logger.error("backfill failed: %s", exc)
        return 1
    finally:
        client.close()

    stored_total = sum(r.stored for r in reconciliations)
    expected_total = sum(r.expected for r in reconciliations)
    mismatched = [r for r in reconciliations if not r.matches]

    logger.info(
        "backfill complete: %d roll call(s) stored against %d at source across "
        "%d congress/session(s). This is permanent public-record data -- do not delete it.",
        stored_total,
        expected_total,
        len(reconciliations),
    )
    if mismatched:
        for reconciliation in mismatched:
            logger.error("DOES NOT MATCH SOURCE: %s", reconciliation.summary())
        logger.error(
            "story 5.8's acceptance criterion is 'row counts match the source totals'; "
            "%d of %d congress/session(s) do not match",
            len(mismatched),
            len(reconciliations),
        )
        if args.limit_per_session is not None:
            logger.error(
                "note: --limit-per-session %d was set, so a mismatch is expected for a capped run",
                args.limit_per_session,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
