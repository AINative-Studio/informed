#!/usr/bin/env python3
"""CLI entrypoint for story 8.1's Senate roll-call loader.

    python scripts/load_senate_rollcalls.py --congress 119 --session 2 --limit 20

Fetches Senate roll-call votes from the real, live senate.gov LIS XML feed
(`app.ingest.senate_rollcalls`), transforms them into `roll_calls` AND
`member_votes` rows per datamodel.md, and upserts every one through the
story 1.4 repository layer, writing a `provenance_log` entry alongside each
roll call, paced against the ~60 req/min ceiling datamodel.md records.

**Unlike `scripts/load_house_rollcalls.py`, this script needs NO API key.**
The senate.gov LIS feed is public and keyless, confirmed live during this
story by fetching real, current XML directly from both the index
(`roll_call_lists/vote_menu_{congress}_{session}.xml`) and item
(`roll_call_votes/vote{congress}{session}/vote_{congress}_{session}_{roll:05d}.xml`)
endpoints -- see `app.ingest.senate_rollcalls`'s module docstring for the
full research trail. This script IS runnable today, which is this story's
whole point relative to 5.1's House loader (still blocked on
`CONGRESS_GOV_API_KEY`).

`--limit` defaults to a SMALL bounded sample deliberately, same reasoning as
`load_house_rollcalls.py`: this script's job is to prove the pipeline works
end to end on real data, not to backfill every roll call since the 118th
Congress in one run (a full backfill is explicitly story 5.8's job for the
House side, and this story's own scope note says the same for the Senate:
"Bounded live verification, not a full historical backfill").

Reads ZeroDB credentials from the environment via `ZeroDBClient.from_env()`,
exactly like every other load script in this codebase -- this script never
opens `local.env` itself; the shell that runs it does
(docs/environments.md). Never pass either credential on the command line as
a literal where it could be logged; export it in the shell instead.

This script WRITES PERMANENT REFERENCE DATA, like story 2.1's seed, story
2.2's legislator load, and (once a key exists) story 5.1's House load:
every roll call and member vote it loads is a real, recorded Senate vote.
Re-running it is expected and safe (idempotent upsert on `roll_calls` by id;
idempotent upsert on `member_votes` by its own compound id; content-
addressed, insert-if-absent writes on `provenance_log`), and nothing it
writes should be deleted afterwards.

A senator whose `lis_member_id` has no bioguide mapping in the fetched
`legislators-current.yaml` crosswalk is skipped (not written with a guessed
or null `person_id`) and logged by name at the end of the run -- see
`app.ingest.senate_rollcalls`'s module docstring's "Member positions"
section for why this loader does not itself write a persistent crosswalk
(that is story 8.2's own job).

Exit codes:
    0  the load completed (even if some source records or member votes were
       skipped)
    1  the source could not be fetched/parsed, or a ZeroDB error stopped the
       run partway through
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.senate_rollcalls import (  # noqa: E402
    DEFAULT_MIN_INTERVAL_SECONDS,
    EARLIEST_CONGRESS,
    SenateRollCallLoader,
    SenateRollCallSourceError,
    fetch_lis_to_bioguide_map,
    fetch_roll_call_xml,
    fetch_vote_numbers,
    roll_call_item_url,
    transform_member_votes_for,
    transform_roll_calls,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

SENATE_LIS_BASE_URL = "https://www.senate.gov/legislative/LIS"


def _pacer(min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS):
    """The same last-call-relative pacer every load script in this codebase
    inlines; see `scripts/load_house_rollcalls.py`'s own copy for why it is
    inlined rather than imported.
    """
    state = {"last": None}

    def pace() -> None:
        now = time.monotonic()
        last = state["last"]
        if last is not None:
            elapsed = now - last
            remaining = min_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        state["last"] = time.monotonic()

    return pace


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--congress",
        type=int,
        default=119,
        help=f"Congress number to load (>= {EARLIEST_CONGRESS}, the 118th; default: 119)",
    )
    parser.add_argument(
        "--session", type=int, default=2, choices=(1, 2), help="Session number (1 or 2)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="How many of the most recently listed roll calls to fetch and load "
        "(default: 20 -- a bounded sample, not a full backfill; see story 5.8's "
        "equivalent scope note for the House side)",
    )
    parser.add_argument(
        "--skip-member-votes",
        action="store_true",
        help="Load roll_calls only, skipping the member_votes write and the "
        "legislators-current.yaml crosswalk fetch it needs. Useful for a "
        "quick roll_calls-only smoke test.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger = logging.getLogger("load_senate_rollcalls")
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.congress < EARLIEST_CONGRESS:
        logger.error(
            "congress %d is before this story's coverage (118th Congress onward)", args.congress
        )
        return 1

    pace = _pacer()
    try:
        logger.info("listing roll calls for congress=%d session=%d", args.congress, args.session)
        numbers = fetch_vote_numbers(congress=args.congress, session=args.session)
        logger.info(
            "source lists %d roll call(s); loading the %d most recent", len(numbers), args.limit
        )
        sample = sorted(numbers, reverse=True)[: args.limit]

        documents: list[tuple[str, str]] = []
        for vote_number in sample:
            pace()
            xml = fetch_roll_call_xml(
                congress=args.congress, session=args.session, vote_number=vote_number
            )
            source_url = roll_call_item_url(
                congress=args.congress,
                session=args.session,
                vote_number=vote_number,
                base_url=SENATE_LIS_BASE_URL,
            )
            documents.append((xml, source_url))

        rows, skipped = transform_roll_calls(documents)
        if skipped:
            logger.warning(
                "%d source record(s) could not be transformed and were skipped: %s",
                len(skipped),
                ", ".join(f"{s.vote_number or '<unknown>'} ({s.reason})" for s in skipped),
            )

        member_vote_rows_by_roll_call: dict[str, list[dict]] = {}
        all_member_skips = []
        if not args.skip_member_votes:
            logger.info("fetching the LIS-to-bioguide crosswalk (legislators-current.yaml)")
            bioguide_by_lis_id = fetch_lis_to_bioguide_map()
            logger.info("crosswalk covers %d legislator(s)", len(bioguide_by_lis_id))

            xml_by_source_url = {source_url: xml for xml, source_url in documents}
            for row in rows:
                xml = xml_by_source_url.get(row["source_url"])
                if xml is None:
                    continue
                member_rows, member_skipped = transform_member_votes_for(
                    row, xml, bioguide_by_lis_id=bioguide_by_lis_id
                )
                member_vote_rows_by_roll_call[row["id"]] = member_rows
                all_member_skips.extend(member_skipped)
            if all_member_skips:
                logger.warning(
                    "%d member vote(s) could not be resolved to a bioguide id and were "
                    "skipped (story 8.2's crosswalk is the fix): %s",
                    len(all_member_skips),
                    ", ".join(
                        f"{s.lis_member_id} on {s.roll_call_id}" for s in all_member_skips[:10]
                    )
                    + (" ..." if len(all_member_skips) > 10 else ""),
                )
    except SenateRollCallSourceError as exc:
        logger.error("could not load the source: %s", exc)
        return 1

    client = ZeroDBClient.from_env()
    try:
        logger.info(
            "loading %d roll call(s) into project %r (paced at >= %.1fs/call)",
            len(rows),
            client.project_id,
            DEFAULT_MIN_INTERVAL_SECONDS,
        )
        report = SenateRollCallLoader(client, pace=_pacer()).load(
            rows, member_vote_rows_by_roll_call=member_vote_rows_by_roll_call
        )
    except ZeroDBError as exc:
        logger.error("load failed: %s", exc)
        return 1
    finally:
        client.close()

    logger.info(
        "load complete: %d roll call(s) written, %d new provenance entry(ies), "
        "%d member_vote(s) written, %d source record(s) skipped, %d member vote(s) "
        "skipped for lack of a bioguide mapping. This is permanent reference data "
        "(like story 2.1's seed and story 2.2's legislator load) -- do not delete it.",
        report.row_count,
        len(report.provenance_written),
        len(report.member_votes_written),
        len(skipped),
        len(all_member_skips),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
