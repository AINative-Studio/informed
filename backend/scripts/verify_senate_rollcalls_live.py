#!/usr/bin/env python3
"""Verify the Senate roll-call load against the LIVE "informed" ZeroDB project (8.1).

Loads a small, bounded, REAL sample of the most recently cast Senate roll
calls -- fetched live from senate.gov's public, keyless LIS XML feed -- into
the real "informed" project, through the exact same `SenateRollCallLoader`
the scheduled job and `scripts/load_senate_rollcalls.py` use. A small sample
rather than a full historical backfill, for the same reason
`verify_legislators_live.py` loads three people rather than the full ~539
roster and `verify_seed_live.py` seeds a handful of rows rather than the
whole dataset: this script's job is to prove the pipeline end to end, not to
backfill (`scripts/load_senate_rollcalls.py --limit <large N>` or a future
story 5.8-equivalent full-backfill job is the right tool for that).

Unlike `verify_legislators_live.py`'s own House-side sibling concern, there
is no `verify_house_rollcalls_live.py` to compare notes with: story 5.1
never got this far because no `CONGRESS_GOV_API_KEY` ever existed. This
script is the first live-network, live-write verification run for either
chamber's own roll-call ingest.

What it proves, in order:

  1. **A real, current sample of Senate roll calls fetches and transforms
     with no skips.** The most recently listed roll numbers for the current
     congress/session, fetched live from senate.gov -- not a fixture.
  2. **Every fetched roll call writes to `roll_calls` under the documented
     id pattern**, with a real `source_url` and `content_hash`, and a
     matching `provenance_log` entry -- this story's acceptance criterion,
     checked against the live project rather than the in-memory fake the
     BDD suite uses.
  3. **At least one roll call's `vote_type` is actually `cloture`** among
     the sample, if the current session has cast one recently (logged as an
     informational note, not a hard failure, if the bounded sample happens
     to contain none -- cloture frequency varies by week, and this script's
     job is to prove the classifier is WIRED, not to wait for one to occur).
  4. **`member_votes` rows are written for every senator this run could
     resolve to a bioguide id**, via a real fetch of
     `legislators-current.yaml` -- and the run's own skip count for
     unmapped senators is reported, which is this story's honest
     measurement of how much of 8.2's crosswalk work remains.
  5. **Running the same sample twice is idempotent**, proven by HTTP method
     (PUT, not a second POST .../rows) exactly like `verify_legislators_live.py`'s
     own check 3, extended here to both `roll_calls` and `member_votes`.

!!! NO CLEANUP, DELIBERATELY !!!

Exactly like `verify_legislators_live.py` and `verify_seed_live.py`: every
roll call and member vote this script loads is REAL, PERMANENT public
voting record (the Senate's own official record of how it voted), not
synthetic test debris -- see this story's own scope note and CLAUDE.md's
posture on live data from 5.1/9.1/2.6. It is left in place.

Run it with the live credentials sourced into the shell -- never passed on
the command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_senate_rollcalls_live.py
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.senate_rollcalls import (  # noqa: E402
    SenateRollCallLoader,
    SenateRollCallSourceError,
    fetch_lis_to_bioguide_map,
    fetch_roll_call_xml,
    fetch_vote_numbers,
    roll_call_item_url,
    transform_member_votes_for,
    transform_roll_calls,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import MemberVote, Person, RollCall  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402
from app.zerodb.settings import ZeroDBSettings  # noqa: E402
from app.zerodb.transport import HttpxZeroDBTransport, ZeroDBResponse  # noqa: E402

SENATE_LIS_BASE_URL = "https://www.senate.gov/legislative/LIS"
CONGRESS = 119
SESSION = 2
SAMPLE_SIZE = 5

logger = logging.getLogger("verify_senate_rollcalls_live")


@dataclass
class _RecordingTransport:
    """Wraps a real transport, recording `(method, path)` for every call --
    identical in shape to `verify_legislators_live.py`'s own, applied here to
    both `roll_calls` and `member_votes` writes.
    """

    inner: HttpxZeroDBTransport
    calls: list[tuple[str, str]] = field(default_factory=list)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> ZeroDBResponse:
        self.calls.append((method, path))
        return self.inner.request(method, path, json=json, params=params, files=files, data=data)

    def close(self) -> None:
        self.inner.close()


def live_row_count(client: ZeroDBClient, table: str, row_id: str) -> int:
    """How many rows in `table` carry this natural-key id, read live --
    identical to `verify_legislators_live.py`'s own helper."""
    page = client.query_rows(table, limit=500)
    return sum(1 for row in page.rows if row.row_data.get("id") == row_id)


def main() -> int:  # noqa: C901 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = ZeroDBSettings()
    recording = _RecordingTransport(
        inner=HttpxZeroDBTransport(
            base_url=settings.base_url,
            api_key=settings.api_key.get_secret_value(),
            timeout_seconds=settings.timeout_seconds,
        )
    )
    client = ZeroDBClient(
        transport=recording, project_id=settings.project_id, max_attempts=settings.max_attempts
    )
    logger.info("project: %s", client.project_id)

    roll_calls: Repository[RollCall] = Repository(client=client, model=RollCall)
    member_votes: Repository[MemberVote] = Repository(client=client, model=MemberVote)
    people: Repository[Person] = Repository(client=client, model=Person)

    failures: list[str] = []
    loaded_roll_call_ids: list[str] = []
    loaded_member_vote_ids: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        # --- fetch a real, current, bounded sample live from senate.gov ----
        logger.info(
            "fetching the %d most recent roll call(s) for congress=%d session=%d from "
            "senate.gov (live, keyless)",
            SAMPLE_SIZE,
            CONGRESS,
            SESSION,
        )
        numbers = fetch_vote_numbers(congress=CONGRESS, session=SESSION)
        check("the live vote index lists at least one roll call", len(numbers) > 0)
        sample = sorted(numbers, reverse=True)[:SAMPLE_SIZE]
        logger.info("sample roll numbers: %s", sample)

        documents: list[tuple[str, str]] = []
        for vote_number in sample:
            xml = fetch_roll_call_xml(congress=CONGRESS, session=SESSION, vote_number=vote_number)
            source_url = roll_call_item_url(
                congress=CONGRESS,
                session=SESSION,
                vote_number=vote_number,
                base_url=SENATE_LIS_BASE_URL,
            )
            documents.append((xml, source_url))

        rows, skipped = transform_roll_calls(documents)
        check(
            f"all {len(sample)} sample roll call(s) transform with no skips",
            not skipped,
            f"skipped: {skipped}",
        )

        logger.info("fetching the live LIS-to-bioguide crosswalk (legislators-current.yaml)")
        bioguide_by_lis_id = fetch_lis_to_bioguide_map()
        # A real finding from this story's own live run: `id.lis` is a
        # SENATE-ONLY identifier (the Senate's own Legislative Information
        # System) -- confirmed live here, since every record carrying one in
        # legislators-current.yaml carries a bioguide too, and the count is
        # ~100, not ~535. House members never get an `id.lis` at all, so the
        # right expectation is "close to 100" (the Senate's own seat count),
        # not "close to the full chamber total" as this check's first draft
        # assumed before this live run corrected it.
        check(
            "the live crosswalk covers close to a full Senate's worth of legislators",
            90 <= len(bioguide_by_lis_id) <= 100,
            f"covers {len(bioguide_by_lis_id)} legislator(s), expected 90-100 "
            "(id.lis is Senate-only; the Senate has 100 seats, some of which "
            "may be briefly vacant)",
        )

        xml_by_source_url = {source_url: xml for xml, source_url in documents}
        member_vote_rows_by_roll_call: dict[str, list[dict]] = {}
        total_member_skips = 0
        for row in rows:
            xml = xml_by_source_url[row["source_url"]]
            member_rows, member_skipped = transform_member_votes_for(
                row, xml, bioguide_by_lis_id=bioguide_by_lis_id
            )
            member_vote_rows_by_roll_call[row["id"]] = member_rows
            total_member_skips += len(member_skipped)
            # Every senator this run resolves to a bioguide id must already
            # exist as a `people` row for member_votes' own reference check
            # to pass -- story 2.2's legislators ingest is what writes those
            # for real; this script seeds only the ones its own sample
            # touches, exactly as `verify_legislators_live.py` upserts its
            # own three fixture people rather than assuming the full roster
            # is already live.
            for member_row in member_rows:
                if people.get(member_row["person_id"]) is None:
                    people.upsert(
                        {
                            "id": member_row["person_id"],
                            "canonical_name": f"Senator {member_row['person_id']}",
                        }
                    )

        cloture_votes = [r for r in rows if r["vote_type"] == "cloture"]
        if cloture_votes:
            logger.info(
                "PASS  at least one sampled roll call classified cloture (%s)",
                [r["id"] for r in cloture_votes],
            )
        else:
            logger.info(
                "NOTE  no cloture vote in this %d-roll-call sample (not a failure -- "
                "cloture frequency varies week to week; the classifier itself is unit- "
                "and BDD-tested against a real captured cloture sample in "
                "tests/fixtures/senate_rollcalls_sample.json)",
                len(sample),
            )

        # --- 1. load once: roll_calls + member_votes, with provenance ------
        logger.info("loading sample (run 1 of 2)")
        first = SenateRollCallLoader(client).load(
            rows, member_vote_rows_by_roll_call=member_vote_rows_by_roll_call
        )
        check(
            f"the loader reports {len(rows)} roll call(s) written",
            first.row_count == len(rows),
            f"reported {first.row_count}",
        )
        loaded_roll_call_ids = list(first.roll_calls_written)
        loaded_member_vote_ids = list(first.member_votes_written)
        logger.info(
            "member_votes: %d written, %d skipped (no bioguide mapping -- story 8.2's "
            "crosswalk is the fix)",
            len(loaded_member_vote_ids),
            total_member_skips,
        )

        for roll_call_id in loaded_roll_call_ids:
            stored = roll_calls.get(roll_call_id)
            check(
                f"{roll_call_id} exists live under the documented id pattern",
                stored is not None and stored.id == roll_call_id,
                f"stored row is {stored!r}",
            )
            check(
                f"{roll_call_id} has a real source_url and content_hash",
                bool(stored and stored.source_url and stored.content_hash),
                f"source_url={stored.source_url if stored else None!r}",
            )

        for member_vote_id in loaded_member_vote_ids[:10]:  # sample, not every one
            stored_mv = member_votes.get(member_vote_id)
            check(
                f"{member_vote_id} exists live with denormalized vote_type",
                stored_mv is not None and bool(stored_mv.vote_type),
                f"stored row is {stored_mv!r}",
            )

        # --- 2. load again: idempotent, proven by HTTP method ---------------
        recording.calls.clear()
        logger.info("loading sample (run 2 of 2) — this is the idempotency check")
        second = SenateRollCallLoader(client).load(
            rows, member_vote_rows_by_roll_call=member_vote_rows_by_roll_call
        )
        check(
            "the second run reports the same roll call count",
            second.row_count == len(rows),
            f"reported {second.row_count}",
        )

        for roll_call_id in loaded_roll_call_ids:
            count = live_row_count(client, "roll_calls", roll_call_id)
            check(
                f"exactly one live row carries {roll_call_id} after two runs",
                count == 1,
                f"live row count is {count}, expected 1",
            )

        write_calls = [
            (method, path)
            for method, path in recording.calls
            if method in {"POST", "PUT"} and "/rows" in path and "/query" not in path
        ]
        post_row_writes = [
            (m, p) for m, p in write_calls if m == "POST" and p.rstrip("/").endswith("/rows")
        ]
        put_writes = [(m, p) for m, p in write_calls if m == "PUT"]
        check(
            "the second run's roll_calls/member_votes writes were PUTs (updates), "
            "not a second POST .../rows per row",
            len(post_row_writes) == 0 and len(put_writes) >= len(rows),
            f"POST .../rows calls: {len(post_row_writes)}, PUT calls: {len(put_writes)}",
        )

    except SenateRollCallSourceError as exc:
        failures.append(f"senate.gov source error: {exc}")
        logger.error("FAIL  live fetch failed: %s", exc)
    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        logger.info(
            "LEFT IN PLACE (intentionally): %d roll call(s) and %d member vote(s) — "
            "real Senate voting record, permanent reference data, not test debris. "
            "roll_call ids: %s",
            len(loaded_roll_call_ids),
            len(loaded_member_vote_ids),
            loaded_roll_call_ids,
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
