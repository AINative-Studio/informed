#!/usr/bin/env python3
"""Verify the time-bounded district lookup against the LIVE project (2.7).

The committed suite proves the query logic against story 1.2's in-memory fake,
including a synthetic two-version district that no real data provides. That
leaves exactly the claims a fake cannot make, and they are what this script
checks:

  1. **The live `districts` rows already carry a usable `valid_from`.** Story
     2.3 wrote one on every row it created and story 2.7 backfills nothing, so
     the premise of the whole story is a fact about live data rather than about
     this repo's code. If any live row's `valid_from` were missing or
     unparseable, a date-qualified lookup would answer None for a district
     Informed can perfectly well resolve — the failure this check exists to
     catch.
  2. **Every live row is open-ended.** `valid_to` null is what "this map is
     still current" means, and it is what keeps the plain OCD-ID holding the
     current version. A live row with a `valid_to` set would mean a superseded
     map is sitting at the id the ballot endpoint reads.
  3. **No live row is a version-suffixed id.** Nothing has been redistricted
     yet, so the table should hold exactly one version per district. This is
     the check that would notice a half-finished redistricting.
  4. **A date-qualified lookup answers for real ids against the real table.**
     Run for an election date (2026-11-03), for the first day the current map
     applied, and for a date before it — the last of which must answer None,
     because Informed holds no map for 2024 and saying otherwise would be an
     invented answer.
  5. **The ballot endpoint is unregressed.** The dateless path through the real
     `BallotService` against the real project, for the same districts, so a
     change to the store cannot have quietly broken story 2.4.

!!! READ-ONLY, DELIBERATELY !!!

Unlike `verify_districts_live.py`, this script **writes nothing**. There is no
real redistricting to record, so a write would either invent a boundary change
(fabricated evidence — prd.md) or leave synthetic debris in a table story 2.3
deliberately keeps as permanent reference data. `record_version` and
`redistrict` are proven against the fake, where a two-version district can be
built honestly and then thrown away.

Run it with the live credentials sourced into the shell -- never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_redistricting_live.py
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ballot.service import BallotService  # noqa: E402
from app.districts.resolver import CensusAddressResolver  # noqa: E402
from app.districts.store import (  # noqa: E402
    VERSION_SEPARATOR,
    DistrictStore,
    base_id_of,
    contains,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import District  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402

logger = logging.getLogger("verify_redistricting_live")

#: The federal general election this cycle. The date the acceptance criterion
#: is about: "a lookup with an election date returns the district valid on
#: that date".
ELECTION_DATE = date(2026, 11, 3)

#: A date before any map Informed holds. The 119th Congress convened on
#: 2025-01-03, so nothing in the table claims 2024 — and a lookup for it must
#: answer None rather than stretch the current map backwards over an election
#: it was not in force for.
BEFORE_ANY_MAP = date(2024, 11, 5)

#: A real, public, non-residential address, for the end-to-end ballot check.
#: prd.md forbids real voter data anywhere, and a home address is voter data.
BALLOT_ADDRESS = "400 Broad St, Seattle, WA 98109"
BALLOT_EXPECTED_ID = "ocd-division/country:us/state:wa/cd:7"


def read_all(districts: Repository[District]) -> list[District]:
    """Every live `districts` row, paged."""
    rows: list[District] = []
    skip = 0
    while True:
        page = districts.query(limit=100, skip=skip)
        rows.extend(page)
        if len(page) < 100:
            return rows
        skip += 100


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    districts: Repository[District] = Repository(client=client, model=District)
    store = DistrictStore(client=client)

    rows = read_all(districts)
    logger.info("live districts rows: %d", len(rows))
    check(
        "the live districts table is populated (story 2.3 created rows on real lookups)",
        bool(rows),
        "run scripts/verify_districts_live.py first",
    )
    if not rows:
        return 1

    # --- 1-3. the shape of what is already live --------------------------

    undated = [r.id for r in rows if not r.valid_from]
    check(
        "every live row carries a valid_from",
        not undated,
        f"rows with no valid_from: {undated[:5]}",
    )

    unreadable = [r.id for r in rows if not contains(r, date(2099, 1, 1))]
    check(
        "every live row's valid_from parses and its range reaches today",
        not unreadable,
        f"rows whose range cannot be read or has expired: {unreadable[:5]}",
    )

    closed = [(r.id, r.valid_to) for r in rows if r.valid_to is not None]
    check(
        "every live row is open-ended (valid_to null means the map is current)",
        not closed,
        f"rows with a valid_to set: {closed[:5]}",
    )

    versioned = [r.id for r in rows if VERSION_SEPARATOR in (r.id or "")]
    check(
        "no live row is a superseded version (nothing has been redistricted yet)",
        not versioned,
        f"version-suffixed ids found: {versioned[:5]}",
    )

    bases = [base_id_of(r.id or "") for r in rows]
    check(
        "each district has exactly one live version",
        len(set(bases)) == len(bases),
        "two rows share an OCD-ID, so a dated lookup would have two answers",
    )

    for row in rows:
        logger.info("  %s  %s..%s  %s", row.id, row.valid_from, row.valid_to or "open", row.name)

    # --- 4. the date-qualified lookup, against the real table ------------

    for row in rows:
        district_id = row.id or ""
        logger.info("\n--- %s", district_id)

        on_election_day = store.get(district_id, as_of=ELECTION_DATE)
        check(
            f"a lookup as of {ELECTION_DATE} returns the current map",
            on_election_day is not None and on_election_day.id == district_id,
            f"got {on_election_day.id if on_election_day else None}",
        )

        # The boundary day itself: `valid_from` is inclusive, so the first day
        # a map applies must resolve to it rather than fall in the gap before.
        first_day = date.fromisoformat(row.valid_from)
        on_first_day = store.get(district_id, as_of=first_day)
        check(
            f"a lookup on its first valid day ({row.valid_from}) returns it",
            on_first_day is not None and on_first_day.id == district_id,
            f"got {on_first_day.id if on_first_day else None}",
        )

        check(
            f"a lookup as of {BEFORE_ANY_MAP} finds nothing (no map is claimed for then)",
            store.get(district_id, as_of=BEFORE_ANY_MAP) is None,
            "the current map is being stretched back over an election it did not govern",
        )

        dateless = store.get(district_id)
        check(
            "the dateless lookup still returns the current row unchanged (story 2.3)",
            dateless is not None and dateless.id == district_id,
            f"got {dateless.id if dateless else None}",
        )

        check(
            "versions() lists exactly this one version",
            [v.id for v in store.versions(district_id)] == [district_id],
            f"got {[v.id for v in store.versions(district_id)]}",
        )

    # --- 5. no regression in story 2.4's ballot endpoint -----------------

    logger.info("\n--- the ballot endpoint, end to end")
    service = BallotService(client=client, resolver=CensusAddressResolver())
    ballot = service.ballot_for(BALLOT_ADDRESS)
    check(
        f"the ballot still resolves to {BALLOT_EXPECTED_ID}",
        ballot.district.id == BALLOT_EXPECTED_ID,
        f"got {ballot.district.id}",
    )
    check(
        "the ballot still names a district and at least one race",
        bool(ballot.district.name) and bool(ballot.races),
        f"name={ballot.district.name!r} races={len(ballot.races)}",
    )
    logger.info(
        "  %s — %d races, %d officeholders",
        ballot.district.name,
        len(ballot.races),
        len(ballot.officeholders),
    )

    if failures:
        logger.error("\n%d check(s) failed:", len(failures))
        for failure in failures:
            logger.error("  - %s", failure)
        return 1

    logger.info("\nall checks passed; nothing was written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
