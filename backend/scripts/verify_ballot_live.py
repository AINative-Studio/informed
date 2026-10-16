#!/usr/bin/env python3
"""Verify `GET /v1/ballot` end to end against the LIVE "informed" project (1.5).

Story 1.5's acceptance criterion is "address input returns 1 House member and
2 senators **from ZeroDB**". The committed test suite proves that against the
in-memory fake, which is the right thing for CI but is not the criterion: every
story in this iteration so far has found at least one way the fake and the live
API disagree (`POST /rows` appends rather than upserts, `PUT` replaces wholesale,
the `{row_id}` segment takes an internal UUID, the server-side `/query` filter
matches nothing). So this script runs the real FastAPI handler, through the real
repository layer and the real HTTP transport, against the real project.

What it does, in order:

  0. **Decoy rows first.** Several unrelated `people` rows are written before
     the three under test, in another state and another district. Story 1.4
     learned this the hard way: with an empty table, the one row written is
     row 0, the client's one-page filter fallback finds it, and a broken read
     looks green. Here the decoys also prove the ballot *excludes* people it
     should exclude, which an empty table can never prove.
  1. **Seed the district and the delegation.** One `districts` row and three
     `people` rows — one House member, two senators — shaped exactly as
     datamodel.md's `people` spec says, with `terms[]` tying each to a
     `district_id` and a `body_id`.
  2. **Call the endpoint.** Through `TestClient` against the app from
     `create_app()`, with the ZeroDB dependency pointed at the live project.
     This is the full path the acceptance criterion names, minus the browser:
     HTTP -> handler -> service -> repository -> client -> live ZeroDB.
  3. **Check the answer.** Exactly 1 House member and exactly 2 senators, the
     right people, the decoys absent, and the address absent from the response.

CLEANUP: every row it writes is deleted before it exits, including on failure,
and each one is then re-read with an **unfiltered** live query to confirm it is
gone — unfiltered because a filtered read is exactly the thing that was
measured not to work, so using it to confirm a deletion would prove nothing.

Nothing it writes resembles real data. The legislators are synthetic, with
`ZZ`-prefixed bioguide-shaped ids and names that say so; real legislator data
arrives in story 2.2. It writes no voter data at all, and the address it
submits is the White House's, which is nobody's home.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_ballot_live.py
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from fastapi.testclient import TestClient

from app.ballot.service import HOUSE_BODY_ID, SENATE_BODY_ID
from app.districts.resolver import StubAddressResolver, senate_division_for
from app.main import create_app, zerodb_client
from app.repositories import Repository
from app.repositories.models import District, Person
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError

#: Marker in every id this script writes, so a row left behind by a crashed run
#: is obvious in the live project and trivially greppable.
MARKER = "zzz-live-check-1.5"

HOUSE_DISTRICT_ID = StubAddressResolver.DISTRICT_ID
SENATE_DIVISION_ID = senate_division_for(HOUSE_DISTRICT_ID)

#: A decoy district nobody's address resolves to, for the exclusion checks.
OTHER_DISTRICT_ID = "ocd-division/country:us/state:tx/cd:1"
OTHER_SENATE_DIVISION_ID = "ocd-division/country:us/state:tx"

#: The White House. A real address that is nobody's home, so no live run ever
#: submits a real person's address (prd.md, privacy first).
ADDRESS = "1600 Pennsylvania Avenue NW, Washington, DC 20500"

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("verify-ballot-1.5")


def person_row(
    suffix: str,
    *,
    body_id: str,
    district_id: str,
    name: str,
) -> dict[str, Any]:
    """A `people` row per datamodel.md: canonical_name, external_ids, terms[]."""
    bioguide = f"ZZ{suffix}"
    return {
        "id": f"person:{bioguide}",
        "canonical_name": name,
        "external_ids": {"bioguide": bioguide, "live_check": MARKER},
        "terms": [
            {
                "body_id": body_id,
                "district_id": district_id,
                "party": "Independent",
                "start": "2023-01-03",
                "end": "2029-01-03",
            }
        ],
    }


#: The delegation under test: 1 House member + 2 senators (the criterion).
DELEGATION = [
    person_row(
        "0001-house",
        body_id=HOUSE_BODY_ID,
        district_id=HOUSE_DISTRICT_ID,
        name=f"Synthetic Representative ({MARKER})",
    ),
    person_row(
        "0002-senate",
        body_id=SENATE_BODY_ID,
        district_id=SENATE_DIVISION_ID,
        name=f"Synthetic Senator One ({MARKER})",
    ),
    person_row(
        "0003-senate",
        body_id=SENATE_BODY_ID,
        district_id=SENATE_DIVISION_ID,
        name=f"Synthetic Senator Two ({MARKER})",
    ),
]

#: Written BEFORE the delegation, so the rows under test are not row 0, and so
#: the ballot is proven to exclude as well as include.
DECOYS = [
    person_row(
        "9001-decoy-house",
        body_id=HOUSE_BODY_ID,
        district_id=OTHER_DISTRICT_ID,
        name=f"Synthetic Rep Elsewhere ({MARKER})",
    ),
    person_row(
        "9002-decoy-senate",
        body_id=SENATE_BODY_ID,
        district_id=OTHER_SENATE_DIVISION_ID,
        name=f"Synthetic Senator Elsewhere ({MARKER})",
    ),
    person_row(
        "9003-decoy-wrong-chamber",
        # Right division, wrong chamber: a bad row that must not be presented
        # as a member of a chamber this person does not sit in.
        body_id=SENATE_BODY_ID,
        district_id=HOUSE_DISTRICT_ID,
        name=f"Synthetic Chamber Mismatch ({MARKER})",
    ),
]

DISTRICT_ROW = {
    "id": HOUSE_DISTRICT_ID,
    "body_id": HOUSE_BODY_ID,
    "name": f"Synthetic District 19 ({MARKER})",
    "state": "CA",
    "valid_from": "2023-01-03",
}


def count_live_rows(client: ZeroDBClient, table: str, row_id: str) -> int:
    """How many rows in `table` carry `row_id`, read UNFILTERED.

    Unfiltered on purpose: the server-side filter was measured not to match
    (story 1.4), so confirming a deletion with it would prove nothing. Pages
    through the table and counts by hand.
    """
    found = 0
    skip = 0
    page_size = 100
    while True:
        page = client.query_rows(table, limit=page_size, skip=skip)
        for row in page.rows:
            if row.row_data.get("id") == row_id:
                found += 1
        if len(page.rows) < page_size:
            return found
        skip += page_size
        if skip > 10_000:  # pragma: no cover - a runaway table, not a real case
            logger.error("stopped counting %s at %d rows", table, skip)
            return found


def main() -> int:
    failures: list[str] = []
    written: list[tuple[str, str]] = []

    def check(description: str, passed: bool, detail: str = "") -> None:
        if passed:
            logger.info("PASS  %s", description)
        else:
            failures.append(f"{description}: {detail}")
            logger.error("FAIL  %s — %s", description, detail)

    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)

    districts: Repository[District] = Repository(client=client, model=District)
    people: Repository[Person] = Repository(client=client, model=Person)

    try:
        # --- 0. decoys first, so nothing under test is row 0 ---------------
        for row in DECOYS:
            written.append(("people", people.upsert(row).id or ""))
        logger.info("seeded %d decoy people rows", len(DECOYS))

        # --- 1. seed the district and the delegation -----------------------
        written.append(("districts", districts.upsert(DISTRICT_ROW).id or ""))
        for row in DELEGATION:
            written.append(("people", people.upsert(row).id or ""))
        logger.info("seeded 1 district and %d delegation rows", len(DELEGATION))

        check(
            "the district row round-trips from the live project",
            (live_district := districts.get(HOUSE_DISTRICT_ID)) is not None
            and live_district.state == "CA",
            f"read back {live_district!r}",
        )

        # --- 2. call the endpoint over the live project --------------------
        app = create_app()
        app.dependency_overrides[zerodb_client] = lambda: client
        with TestClient(app) as api:
            response = api.get("/v1/ballot", params={"address": ADDRESS})

        check(
            "the endpoint answers 200",
            response.status_code == 200,
            f"status {response.status_code}: {response.text[:300]}",
        )
        if response.status_code != 200:
            raise RuntimeError("no payload to check")

        body = response.json()
        officeholders = body["officeholders"]
        house = [o for o in officeholders if o["body_id"] == HOUSE_BODY_ID]
        senate = [o for o in officeholders if o["body_id"] == SENATE_BODY_ID]

        logger.info("live response: %s", body)

        # --- 3. the acceptance criterion, checked ---------------------------
        check(
            "the response names the voter's House district",
            body["district"]["id"] == HOUSE_DISTRICT_ID and MARKER in body["district"]["name"],
            f"district is {body['district']}",
        )
        check(
            "exactly 1 House member comes back from ZeroDB",
            len(house) == 1,
            f"got {len(house)}: {[o['name'] for o in house]}",
        )
        check(
            "exactly 2 senators come back from ZeroDB",
            len(senate) == 2,
            f"got {len(senate)}: {[o['name'] for o in senate]}",
        )
        check(
            "they are the three seeded people, and only those",
            {o["person_id"] for o in officeholders} == {row["id"] for row in DELEGATION},
            f"got {sorted(o['person_id'] for o in officeholders)}",
        )
        check(
            "the decoys in another state and district are excluded",
            not {o["person_id"] for o in officeholders} & {row["id"] for row in DECOYS[:2]},
            f"a decoy leaked in: {sorted(o['person_id'] for o in officeholders)}",
        )
        check(
            "the chamber-mismatched row is excluded",
            DECOYS[2]["id"] not in {o["person_id"] for o in officeholders},
            "a row whose body_id and district_id disagree was presented anyway",
        )
        check(
            "the submitted address is nowhere in the response",
            "Pennsylvania" not in response.text and "20500" not in response.text,
            "the address was echoed back",
        )
        check(
            "the response makes no recommendation",
            not any(
                word in response.text.lower()
                for word in ("recommend", "endorse", "vote for", "best match")
            ),
            "the payload contains recommendation language",
        )

        # --- a blank address is still refused over the live project --------
        with TestClient(app) as api:
            blank = api.get("/v1/ballot", params={"address": "   "})
        check(
            "a blank address is refused with 422",
            blank.status_code == 422,
            f"status {blank.status_code}",
        )

    except (ZeroDBError, RuntimeError) as exc:
        failures.append(f"live call failed: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        # --- cleanup, then confirm it took, unfiltered --------------------
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
            logger.info("PASS  every row written was deleted and confirmed gone (unfiltered read)")

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
