#!/usr/bin/env python3
"""Verify House member positions (5.2) and bills (5.3) against the LIVE
"informed" ZeroDB project and the real Congress.gov API v3.

Unlike `verify_senate_rollcalls_live.py`, this script DOES have a House-side
sibling to compare notes with for the roll-call half: story 5.1's own
`house_rollcalls.py` never got a live run (no `CONGRESS_GOV_API_KEY` existed
yet), so this is also the first live-network, live-write proof that story
5.1's own module works against the real source -- see the live findings
below, several of which are bugs in 5.1's module that this run surfaced and
this story's PR fixed.

What this proves, in order:

  1. **A real House roll call (119th Congress, session 1, roll 240) fetches,
     transforms and writes with a real, non-zero tally.** This is 5.1's own
     live-source tally bug (`votePartyTotal` is a bare list, not the
     documented `{"item": [...]}` wrapper) proven fixed against the real API,
     not just the captured fixture already covering it in the BDD suite.
  2. **Every member's position on that roll call writes as a `member_votes`
     row carrying the five denormalized fields**, and the nightly drift check
     (`app.integrity.checks.check_member_votes_drift`) passes against the
     live project -- 5.2's stated acceptance criterion, checked for real.
  3. **A real bill (H.R. 3424, 119th Congress) ingests with its sponsor,
     cosponsors and CRS subjects** -- 5.3's stated acceptance criterion.
  4. **A real bill with NO cosponsors (H.R. 1) still ingests cleanly** --
     the live finding that the real detail response omits the `cosponsors`
     block entirely rather than reporting zero.
  5. **Once H.R. 3424 is ingested, re-ingesting roll 240 resolves a real,
     verified `bill_id`** via `resolve_roll_call_bill_id` -- proving the gap
     story 5.1 left null (no `bills` rows existed yet to point at) is now
     closed for a roll call ingested after its bill.
  6. **Running every load twice is idempotent**, proven by HTTP method (PUT,
     not a second POST .../rows), exactly like every other live verify
     script in this family.

CLEANUP: this run's own test data is deleted at the end, successfully.
Unlike `verify_senate_rollcalls_live.py`/`verify_legislators_live.py`
(whose subjects are the Senate's and House's own real, permanent voting
and legislator record, left in place on purpose), the specific roll call,
bill and the one synthetic `people` row this script seeds for H.R. 1's
sponsor are deleted here. The roll call and bill ARE real public record,
but running this verification script repeatedly must not accumulate
duplicate provenance_log entries or member_votes rows against the shared
live project every time CI or a developer runs it -- so this script owns
its own data's lifecycle end to end, confirmed in the final cleanup check.

Run it with the live credentials sourced into the shell -- never passed on
the command line, never printed:

    set -a; . /Users/aideveloper/informed/local.env; set +a
    python backend/scripts/verify_house_bills_live.py
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.house_bills import (  # noqa: E402
    HouseBillLoader,
    HouseBillSourceError,
    fetch_bill_cosponsors,
    fetch_bill_detail,
    fetch_bill_subjects,
    resolve_roll_call_bill_id,
    transform_bill,
)
from app.ingest.house_member_votes import (  # noqa: E402
    fetch_member_votes,
    transform_member_votes_for,
)
from app.ingest.house_rollcalls import (  # noqa: E402
    HouseRollCallLoader,
    HouseRollCallSourceError,
    fetch_roll_call_item,
    transform_roll_call,
)
from app.integrity.checks import check_member_votes_drift  # noqa: E402
from app.repositories import Repository  # noqa: E402
from app.repositories.models import Bill, MemberVote, Person, RollCall  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402
from app.zerodb.settings import ZeroDBSettings  # noqa: E402
from app.zerodb.transport import HttpxZeroDBTransport, ZeroDBResponse  # noqa: E402

CONGRESS = 119
SESSION = 1
ROLL_CALL_NUMBER = 240  # H.R. 3424, "On Motion to Suspend the Rules and Pass"

logger = logging.getLogger("verify_house_bills_live")


@dataclass
class _RecordingTransport:
    """Wraps a real transport, recording `(method, path)` for every call --
    identical in shape to every other live verify script in this family.
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
    """How many rows in `table` carry this natural-key id, read live."""
    page = client.query_rows(table, limit=500)
    return sum(1 for row in page.rows if row.row_data.get("id") == row_id)


def main() -> int:  # noqa: C901 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    api_key = os.environ.get("CONGRESS_GOV_API_KEY")
    if not api_key:
        logger.error(
            "CONGRESS_GOV_API_KEY is not set. source local.env first: "
            "set -a; . /Users/aideveloper/informed/local.env; set +a"
        )
        return 1

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
    bills: Repository[Bill] = Repository(client=client, model=Bill)
    people: Repository[Person] = Repository(client=client, model=Person)

    failures: list[str] = []
    seeded_person_ids: set[str] = set()
    loaded_roll_call_id: str | None = None
    loaded_member_vote_ids: list[str] = []
    loaded_bill_ids: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        # --- 5.2: fetch & transform a real roll call + its member positions
        logger.info(
            "fetching House roll call %d-%d:%d live from api.congress.gov",
            CONGRESS,
            SESSION,
            ROLL_CALL_NUMBER,
        )
        roll_envelope = fetch_roll_call_item(
            congress=CONGRESS, session=SESSION, roll_call_number=ROLL_CALL_NUMBER, api_key=api_key
        )
        roll_row = transform_roll_call(roll_envelope)
        loaded_roll_call_id = roll_row["id"]
        check(
            "the roll call transforms under the documented id pattern",
            roll_row["id"] == "us-house:119-1:240",
            f"got {roll_row['id']!r}",
        )
        tally = roll_row["tally"]
        check(
            "the tally is non-zero (5.1's live bare-list votePartyTotal bug, fixed)",
            (tally.yea + tally.nay + tally.present + tally.not_voting) > 0,
            f"tally={tally!r}",
        )

        logger.info("fetching every member's position on roll %d live", ROLL_CALL_NUMBER)
        members_envelope = fetch_member_votes(
            congress=CONGRESS, session=SESSION, roll_call_number=ROLL_CALL_NUMBER, api_key=api_key
        )
        member_rows, member_skipped = transform_member_votes_for(roll_row, members_envelope)
        check(
            "member positions transform with few or no skips",
            len(member_skipped) < len(member_rows) * 0.05,
            f"{len(member_skipped)} skipped of {len(member_rows)} total",
        )
        check(
            "the live members response is unpaginated and carries the whole House",
            len(member_rows) + len(member_skipped) > 400,
            f"got {len(member_rows) + len(member_skipped)} total positions",
        )

        # member_votes.person_id must resolve to a real `people` row. This
        # script does not seed the whole chamber (that's story 2.2's job,
        # already live) -- it only checks every member in the sample already
        # resolves, and reports how many don't (an honest measurement, same
        # posture as verify_senate_rollcalls_live.py's own crosswalk-coverage
        # check).
        unresolved = [
            row["person_id"] for row in member_rows if people.get(row["person_id"]) is None
        ]
        check(
            "nearly every sampled representative already has a people row "
            "(story 2.2's legislators ingest)",
            len(unresolved) < len(member_rows) * 0.05,
            f"{len(unresolved)} of {len(member_rows)} have no people row yet: "
            f"{unresolved[:5]}{'...' if len(unresolved) > 5 else ''}",
        )

        # --- load run 1: roll_calls + member_votes --------------------------
        logger.info("loading roll call + member positions (run 1 of 2)")
        rc_loader = HouseRollCallLoader(client)
        first_rc = rc_loader.load(
            [roll_row], member_vote_rows_by_roll_call={roll_row["id"]: member_rows}
        )
        check(
            "the loader reports the roll call written",
            first_rc.row_count == 1,
            f"reported {first_rc.row_count}",
        )
        loaded_member_vote_ids = list(first_rc.member_votes_written)
        check(
            "member_votes written count matches the resolvable positions "
            "(len(member_rows) minus any with no people row)",
            len(loaded_member_vote_ids) == len(member_rows) - len(unresolved),
            f"wrote {len(loaded_member_vote_ids)}, expected "
            f"{len(member_rows) - len(unresolved)}",
        )
        check(
            "no member_vote write was itself skipped by the reference check "
            "beyond the unresolved set already measured",
            len(first_rc.member_votes_skipped) == len(unresolved),
            f"loader skipped {len(first_rc.member_votes_skipped)}, expected {len(unresolved)}",
        )

        stored_roll = roll_calls.get(loaded_roll_call_id)
        check(
            f"{loaded_roll_call_id} exists live with a real source_url and content_hash",
            bool(stored_roll and stored_roll.source_url and stored_roll.content_hash),
            f"stored row is {stored_roll!r}",
        )

        drift_report = check_member_votes_drift(client)
        check(
            "the member_votes denormalization drift check passes against the live project",
            drift_report.ok,
            drift_report.summary() if not drift_report.ok else "",
        )

        for member_vote_id in loaded_member_vote_ids[:5]:  # sample, not every one
            stored_mv = member_votes.get(member_vote_id)
            check(
                f"{member_vote_id} exists live with denormalized vote_type",
                stored_mv is not None and bool(stored_mv.vote_type),
                f"stored row is {stored_mv!r}",
            )

        # --- 5.3: fetch & transform two real bills ---------------------------
        logger.info("fetching H.R. 3424 (with cosponsors) and H.R. 1 (without) live")
        samples = [
            ("HR", "3424", "us-house:119:hr-3424"),
            ("HR", "1", "us-house:119:hr-1"),
        ]
        bill_rows: list[dict[str, Any]] = []
        for bill_type, number, expected_id in samples:
            detail = fetch_bill_detail(
                congress=CONGRESS, bill_type=bill_type, bill_number=number, api_key=api_key
            )
            cosponsors = fetch_bill_cosponsors(
                congress=CONGRESS, bill_type=bill_type, bill_number=number, api_key=api_key
            )
            subjects = fetch_bill_subjects(
                congress=CONGRESS, bill_type=bill_type, bill_number=number, api_key=api_key
            )
            row, sponsor_skips = transform_bill(
                detail=detail, cosponsors=cosponsors, subjects=subjects
            )
            check(
                f"{bill_type} {number} transforms under the documented id pattern",
                row["id"] == expected_id,
                f"got {row['id']!r}",
            )
            check(
                f"{bill_type} {number} has at least one sponsor",
                len(row["sponsors"]) >= 1,
                f"sponsors={row['sponsors']!r}",
            )
            # Every sponsor/cosponsor this script's own two bills touch must
            # resolve to a people row for the bill's own reference check to
            # pass -- seed exactly the ones this run needs (mirrors
            # verify_senate_rollcalls_live.py's own posture), and remember
            # them for cleanup since they are this script's own test data,
            # not a pre-existing live person.
            for sponsor in row["sponsors"]:
                if people.get(sponsor["person_id"]) is None:
                    people.upsert(
                        {
                            "id": sponsor["person_id"],
                            "canonical_name": f"Rep. {sponsor['person_id']}",
                        }
                    )
                    seeded_person_ids.add(sponsor["person_id"])
            bill_rows.append(row)
            if sponsor_skips:
                logger.info(
                    "NOTE  %s %s: %d sponsor(s) skipped in transform (no bioguideId): %s",
                    bill_type,
                    number,
                    len(sponsor_skips),
                    sponsor_skips,
                )

        check(
            "H.R. 3424 has cosponsors (the ordinary case)",
            any(
                r["id"] == "us-house:119:hr-3424" and len(r["sponsors"]) > 1 for r in bill_rows
            ),
        )
        check(
            "H.R. 3424 has a CRS policy_area",
            any(r["id"] == "us-house:119:hr-3424" and r["policy_area"] for r in bill_rows),
        )
        check(
            "H.R. 1 ingests with its sponsor alone (the real detail response omits "
            "the cosponsors block entirely rather than reporting zero)",
            any(r["id"] == "us-house:119:hr-1" and len(r["sponsors"]) >= 1 for r in bill_rows),
        )

        # --- load run 1: bills ------------------------------------------------
        logger.info("loading both bills (run 1 of 2)")
        bill_loader = HouseBillLoader(client)
        first_bills = bill_loader.load(bill_rows)
        check(
            "the loader reports both bills written",
            first_bills.row_count == len(bill_rows),
            f"reported {first_bills.row_count}",
        )
        loaded_bill_ids = list(first_bills.bills_written)

        for expected_id in (row["id"] for row in bill_rows):
            stored_bill = bills.get(expected_id)
            check(
                f"{expected_id} exists live with a real source_url and content_hash",
                bool(stored_bill and stored_bill.source_url and stored_bill.content_hash),
                f"stored row is {stored_bill!r}",
            )

        # --- 5.3 closes 5.1's gap: a verified bill_id on the roll call ------
        resolved_bill_id = resolve_roll_call_bill_id(roll_envelope, client=client)
        check(
            "resolve_roll_call_bill_id finds the now-ingested H.R. 3424",
            resolved_bill_id == "us-house:119:hr-3424",
            f"got {resolved_bill_id!r}",
        )
        if resolved_bill_id is not None:
            roll_row_with_bill = dict(roll_row)
            roll_row_with_bill["bill_id"] = resolved_bill_id
            rc_loader.load([roll_row_with_bill])
            restamped = roll_calls.get(loaded_roll_call_id)
            check(
                f"{loaded_roll_call_id} carries the resolved bill_id after re-ingest",
                bool(restamped and restamped.bill_id == "us-house:119:hr-3424"),
                f"stored bill_id={restamped.bill_id if restamped else None!r}",
            )

        # --- idempotency: load everything a second time ---------------------
        recording.calls.clear()
        logger.info("loading roll call, member positions and bills again (run 2 of 2)")
        second_rc = rc_loader.load(
            [roll_row], member_vote_rows_by_roll_call={roll_row["id"]: member_rows}
        )
        second_bills = bill_loader.load(bill_rows)
        check(
            "the second roll-call run reports the same count",
            second_rc.row_count == 1,
            f"reported {second_rc.row_count}",
        )
        check(
            "the second bills run reports the same count",
            second_bills.row_count == len(bill_rows),
            f"reported {second_bills.row_count}",
        )

        count = live_row_count(client, "roll_calls", loaded_roll_call_id)
        check(
            f"exactly one live row carries {loaded_roll_call_id} after two runs",
            count == 1,
            f"live row count is {count}, expected 1",
        )
        for bill_row_id in (row["id"] for row in bill_rows):
            count = live_row_count(client, "bills", bill_row_id)
            check(
                f"exactly one live row carries {bill_row_id} after two runs",
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
            "the second run's roll_calls/bills writes were PUTs (updates), "
            "not a second POST .../rows per row",
            len(post_row_writes) == 0 and len(put_writes) >= 1,
            f"POST .../rows calls: {len(post_row_writes)}, PUT calls: {len(put_writes)}",
        )

    except (HouseRollCallSourceError, HouseBillSourceError) as exc:
        failures.append(f"Congress.gov source error: {exc}")
        logger.error("FAIL  live fetch failed: %s", exc)
    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        # --- cleanup: this script's own test data, confirmed ------------
        logger.info("cleaning up this run's own test data")
        if loaded_roll_call_id:
            for member_vote_id in loaded_member_vote_ids:
                client.delete_row(MemberVote.table_name, member_vote_id)
            client.delete_row(RollCall.table_name, loaded_roll_call_id)
        for bill_row_id in loaded_bill_ids:
            client.delete_row(Bill.table_name, bill_row_id)
        for person_id in seeded_person_ids:
            client.delete_row(Person.table_name, person_id)

        cleanup_failures: list[str] = []
        if loaded_roll_call_id and live_row_count(
            client, RollCall.table_name, loaded_roll_call_id
        ):
            cleanup_failures.append(f"roll call {loaded_roll_call_id} still present")
        for member_vote_id in loaded_member_vote_ids:
            if live_row_count(client, MemberVote.table_name, member_vote_id):
                cleanup_failures.append(f"member_vote {member_vote_id} still present")
        for bill_row_id in loaded_bill_ids:
            if live_row_count(client, Bill.table_name, bill_row_id):
                cleanup_failures.append(f"bill {bill_row_id} still present")
        for person_id in seeded_person_ids:
            if live_row_count(client, Person.table_name, person_id):
                cleanup_failures.append(f"seeded person {person_id} still present")

        if cleanup_failures:
            failures.extend(cleanup_failures)
            logger.error("FAIL  cleanup incomplete: %s", cleanup_failures)
        else:
            logger.info(
                "PASS  cleanup confirmed: 1 roll_call, %d member_votes, %d bills, "
                "%d seeded person(s) all removed",
                len(loaded_member_vote_ids),
                len(loaded_bill_ids),
                len(seeded_person_ids),
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
