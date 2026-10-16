#!/usr/bin/env python3
"""Verify address -> district -> legislator against the LIVE project (2.3).

The committed suite drives captured Census responses, because CI must not
depend on census.gov being reachable (CLAUDE.md) and because a capture is the
only way to pin a shape the live service will not reproduce on demand. That
leaves exactly the claims a fixture cannot make, and they are this story's
whole point:

  1. **The live geocoder really answers with these districts.** A fixture
     proves the parsing; only a live call proves the request. This script found
     a real bug that every fixture-driven test passed: `layers` must be sent as
     the service's *numeric* layer ids ("54,80"), because a layer *name* is
     silently mis-handled — `layers=States,Congressional Districts` comes back
     carrying only `States`, so every lookup fails with "we couldn't work out
     your congressional district" and nothing says why. See `CENSUS_LAYERS`.

  2. **The id joins story 2.2's live legislator rows.** Story 2.2 called its
     `terms[].district_id` scheme "provisional" and said 2.3 might have to
     migrate it. It does not — but that is a claim about 539 rows in the live
     project, not about this repo's code, so it is checked against them: every
     address below must resolve to an id that some real sitting member's term
     actually carries. datamodel.md has no joins, so these ids are compared as
     strings and a mismatch would silently produce an empty delegation with no
     error to explain it. That is the failure this check exists to catch.

  3. **The full `BallotService` answers 1 House member and 2 senators.** The
     end of the chain, through the production service against the production
     project.

  4. **The address is never persisted.** Asserted by reading back every row
     this script's lookups wrote and every log line they emitted, and searching
     both for the address — the structural guarantees (`ResolvedAddress` has no
     field for it; `DistrictStore.ensure` has no parameter for it) are already
     unit-tested, so what is added here is the live end-to-end check that no
     row in the real table and no log line from a real run carries it.

Addresses: real, public, non-residential landmarks (a stadium, a capitol, an
observation deck). prd.md forbids real voter data in a fixture, a log or test
output, and a home address is voter data — so none appears here, and the
addresses used are ones no voter lives at.

!!! NO CLEANUP, DELIBERATELY !!!

Like `verify_seed_live.py` and `verify_legislators_live.py`: the `districts`
rows this script creates are real congressional districts with real OCD-IDs and
the Census Geocoder's own names for them. They are permanent reference data,
the same category as story 2.1's `us-federal`/`us-house` rows, and emphatically
not the `zzz-live-check-*` debris stories 1.3-1.5 cleaned up. They are left in
place, and the first real voter to look up an address in one of these districts
reads the row this script wrote.

Run it with the live credentials sourced into the shell -- never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_districts_live.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ballot.service import BallotService  # noqa: E402
from app.districts.resolver import CensusAddressResolver  # noqa: E402
from app.districts.store import DistrictStore  # noqa: E402
from app.repositories import Repository  # noqa: E402
from app.repositories.models import Body, Person  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("verify_districts_live")

#: Real, public, non-residential addresses and the district each must resolve
#: to. The expected ids are written out rather than computed, so a regression in
#: `ocd_id_for` cannot make this script agree with itself.
#:
#: Chosen to cover every branch the id construction has: a padded single-digit
#: district (WA-07, the one that would break if the zero survived), a two-digit
#: one (NY-12), an at-large state (WY, geocoder code `00`), and a non-voting
#: delegate (DC, code `98`).
CASES: tuple[tuple[str, str, str], ...] = (
    (
        "400 Broad St, Seattle, WA 98109",
        "ocd-division/country:us/state:wa/cd:7",
        "Space Needle — padded single-digit district (CD120=07)",
    ),
    (
        "20 W 34th St, New York, NY 10001",
        "ocd-division/country:us/state:ny/cd:12",
        "Empire State Building — two-digit district",
    ),
    (
        "200 W 24th St, Cheyenne, WY 82002",
        "ocd-division/country:us/state:wy",
        "Wyoming State Capitol — at-large state (CD120=00)",
    ),
    (
        "1600 Pennsylvania Ave NW, Washington, DC 20500",
        "ocd-division/country:us/state:dc",
        "The White House — non-voting delegate (CD120=98)",
    ),
)

#: The chambers a `districts` row can reference. Checked before anything is
#: written, because `districts.body_id` is a declared reference as of this story
#: and an unseeded chamber is the one remaining 404 — better to say so here than
#: to report four confusing failures.
REQUIRED_BODY_IDS = ("us-house", "us-senate")


def live_district_rows(client: ZeroDBClient) -> list[dict[str, object]]:
    """Every `districts` row, read live and unfiltered.

    Unfiltered on purpose: story 1.4 measured the live server-side filter
    returning `total=0` for a filter that should match, so a filtered read here
    could "prove" the absence of a row that is plainly present.
    """
    return [dict(row.row_data) for row in client.query_rows("districts", limit=500).rows]


def scan_people(people: Repository[Person]) -> list[Person]:
    """Every `people` row, paged exactly as `BallotService` pages them."""
    rows: list[Person] = []
    skip = 0
    while True:
        page = people.query(limit=100, skip=skip)
        rows.extend(page)
        if len(page) < 100:
            return rows
        skip += 100


def main() -> int:  # noqa: C901 - a linear script of checks reads better as one
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

    # Captured so check 5 can prove no log line from a real run carried an
    # address. Attached to the `app` logger rather than the root one: the root
    # logger at DEBUG turns on httpx's wire logging, which prints every request
    # URL — and the geocoder's URL carries the address in a query parameter, so
    # raising the root level to inspect Informed's own logs would itself print
    # the address this check exists to prove is never printed.
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    app_logger = logging.getLogger("app")
    capture = _Capture(level=logging.DEBUG)
    app_logger.addHandler(capture)
    app_logger.setLevel(logging.DEBUG)

    try:
        bodies: Repository[Body] = Repository(client=client, model=Body)
        for body_id in REQUIRED_BODY_IDS:
            check(
                f"story 2.1's chamber {body_id} is present (a districts row references it)",
                bodies.get(body_id) is not None,
                "run scripts/verify_seed_live.py first",
            )
        if failures:
            logger.error("reference data missing; not writing districts rows")
            return 1

        people: Repository[Person] = Repository(client=client, model=Person)
        everyone = scan_people(people)
        logger.info("live people rows: %d", len(everyone))
        check(
            "story 2.2's legislators are loaded (the join has something to join to)",
            len(everyone) >= 500,
            f"only {len(everyone)} people rows; run scripts/load_legislators.py",
        )

        # --- the id scheme, measured against the live rows -----------------
        house_ids = {
            term.district_id
            for person in everyone
            for term in person.terms
            if term.body_id == "us-house" and term.district_id
        }
        senate_ids = {
            term.district_id
            for person in everyone
            for term in person.terms
            if term.body_id == "us-senate" and term.district_id
        }
        logger.info(
            "distinct live district_ids: %d House, %d Senate", len(house_ids), len(senate_ids)
        )
        check(
            "no live House district_id is zero-padded (the geocoder pads; these must not)",
            not [d for d in house_ids if "/cd:0" in d],
            f"padded ids found: {sorted(d for d in house_ids if '/cd:0' in d)[:5]}",
        )
        check(
            "no live district_id uses the `cd:at-large` spelling",
            not [d for d in house_ids | senate_ids if "at-large" in d],
            "a third spelling is live; the repo would carry two schemes",
        )

        resolver = CensusAddressResolver()
        store = DistrictStore(client=client)
        service = BallotService(client=client, resolver=resolver)

        for address, expected_id, label in CASES:
            logger.info("\n--- %s", label)

            # --- 1. the live geocoder's answer -----------------------------
            resolution = resolver.resolve_district(address)
            resolved = resolution.districts
            check(
                f"the live geocoder resolves it to {expected_id}",
                resolved.district_id == expected_id,
                f"got {resolved.district_id}",
            )
            logger.info('      geocoder name: "%s"', resolution.name)

            # --- 2. the id joins a real sitting legislator -----------------
            house = [
                person.canonical_name
                for person in everyone
                for term in person.terms
                if term.body_id == "us-house" and term.district_id == resolved.district_id
            ]
            check(
                "that id matches a real House member's term district_id from story 2.2",
                bool(house),
                f"no live person has a us-house term in {resolved.district_id}",
            )

            # --- 3. the row is created, idempotently -----------------------
            stored = store.ensure(resolved, name=resolution.name)
            check(
                "a districts row exists for it after resolution",
                stored.id == expected_id and stored.body_id == "us-house",
                f"stored row is {stored!r}",
            )
            store.ensure(resolved, name=resolution.name)
            duplicates = [r for r in live_district_rows(client) if r.get("id") == expected_id]
            check(
                "resolving twice leaves exactly one districts row (datamodel.md rule 1)",
                len(duplicates) == 1,
                f"{len(duplicates)} rows carry {expected_id}",
            )

            # --- 4. the whole service, end to end -------------------------
            ballot = service.ballot_for(address)
            house_seats = [o for o in ballot.officeholders if o.body_id == "us-house"]
            senate_seats = [o for o in ballot.officeholders if o.body_id == "us-senate"]
            logger.info(
                "      delegation: %s",
                ", ".join(f"{o.name} ({o.body_id})" for o in ballot.officeholders) or "none",
            )
            check(
                "the ballot names exactly one current House member",
                len(house_seats) == 1,
                f"got {[o.name for o in house_seats]}",
            )
            # DC and the five territories elect no senators at all, so the
            # honest expectation is "as many as that division actually has", and
            # the live rows are what say how many — not a constant here, and not
            # a flat 2 relaxed for everyone just to accommodate DC. A state that
            # should have two and does not still fails.
            expected_senators = len(
                {
                    person.id
                    for person in everyone
                    for term in person.terms
                    if term.body_id == "us-senate"
                    and term.district_id == resolved.senate_district_id
                }
            )
            check(
                f"the ballot names {expected_senators} current senator(s)",
                len(senate_seats) == expected_senators,
                f"got {[o.name for o in senate_seats]}",
            )

        # --- 5. the address reached neither a row nor a log ---------------
        logger.info("\n--- privacy: the address is never persisted")
        rows_blob = repr(live_district_rows(client)).lower()
        log_blob = " ".join(f"{record.getMessage()} {record.args}" for record in records).lower()
        for address, _, label in CASES:
            # Checked by distinctive fragment rather than whole string, because
            # the geocoder echoes the address normalised and upper-cased
            # ("400 BROAD ST, SEATTLE, WA, 98109") and a check for the submitted
            # spelling alone would miss a leak of that echo.
            #
            # Each fragment must be distinctive enough to mean something. A bare
            # house number is not: "20" and "400" occur in timestamps, ids and
            # row counts, so including them produced four confident false
            # positives on the first run. The number is therefore only ever
            # checked as part of "<number> <street>", and the street name and
            # ZIP stand on their own.
            street_line = address.split(",")[0]
            zip_code = address.rsplit(" ", 1)[-1]
            fragments = (
                address.lower(),
                street_line.lower(),
                street_line.split(" ", 1)[1].lower(),
                zip_code,
            )
            leaked_rows = [f for f in fragments if f in rows_blob]
            leaked_logs = [f for f in fragments if f in log_blob]
            check(
                f"no districts row carries any part of the address ({label.split(' —')[0]})",
                not leaked_rows,
                f"leaked: {leaked_rows}",
            )
            check(
                f"no log line carries any part of the address ({label.split(' —')[0]})",
                not leaked_logs,
                f"leaked: {leaked_logs}",
            )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        logging.getLogger().removeHandler(capture)
        logger.info(
            "\nLEFT IN PLACE (intentionally): districts rows %s — real "
            "congressional districts named by the Census Geocoder, permanent "
            "reference data, not test debris",
            ", ".join(expected for _, expected, _ in CASES),
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
