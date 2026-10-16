#!/usr/bin/env python3
"""Verify `GET /v1/ballot` against the LIVE "informed" project (story 2.4).

Story 2.4's two acceptance criteria are "response lists races with incumbents
flagged" and "p95 latency under 800 ms". The committed suite proves the first
outright and defends the second by counting round trips, because a millisecond
number measured against the in-memory fake measures the machine and one
measured against the live geocoder would put census.gov in CI (CLAUDE.md).

**This script is where the millisecond number is actually claimed.** It calls
the production endpoint, over the production repository and transport, against
the real project with its 539 real legislators and the real Census Geocoder,
and reports a measured p95 against the 800 ms budget.

It replaces story 1.5's version of this script wholesale. That one seeded
synthetic `ZZ`-prefixed legislators, asserted against them and deleted them
afterwards, which was right when `people` was empty — and it has been broken
since story 2.3 removed `StubAddressResolver`, which it imported. There is
nothing left to seed: stories 2.1-2.3 put real bodies, real legislators and
real district rows in the project, so this script **writes nothing at all** and
has no cleanup. It is a pure read, which is also what makes its timings
representative of a voter's request rather than of a request plus a fixture.

What it checks
==============

  1. **Latency.** `RUNS` lookups per address, reported as median/p95/max per
     address and pooled, against the 800 ms budget. The pooled p95 is the
     number story 2.4's acceptance criterion is about.
  2. **Races with incumbents flagged.** Every address returns a House race for
     its district; a state address also returns a Senate race with two
     senators; every candidate carries `is_incumbent: true`; `data_state` is
     `incumbents_only`.
  3. **No regression in story 2.3's at-large fix.** Wyoming (at-large, code
     `00`) must return one representative *and* two senators, and DC (delegate,
     code `98`) one delegate and **no Senate race** — the chambers' divisions
     are the same OCD-ID in both, which is the collision 2.3 fixed and which the
     race grouping could have reintroduced.
  4. **Privacy.** The address appears in no response body and no log line
     (prd.md, privacy first; story 2.3's "never stored").

Addresses: real, public, non-residential landmarks, reused from story 2.3's
live verification so the two stories' results are comparable. prd.md forbids
real voter data in a fixture, a log or test output, and a home address is voter
data — so none appears here.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_ballot_live.py
"""

from __future__ import annotations

import logging
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.ballot.models import DataState  # noqa: E402
from app.ballot.service import HOUSE_BODY_ID, SENATE_BODY_ID  # noqa: E402
from app.main import create_app, zerodb_client  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("verify_ballot_live")

#: Story 2.4's acceptance criterion, in milliseconds.
BUDGET_MS = 800.0

#: Lookups per address. Enough that a p95 means something and the snapshot's
#: warm path (which is the steady state a real voter hits) dominates, without
#: hammering a free public geocoder.
RUNS = 6

#: (address, label, expected senators). The senator count is written out rather
#: than derived, so a regression cannot make this script agree with itself —
#: and DC's 0 is the at-large/delegate case story 2.3 fixed.
CASES: tuple[tuple[str, str, int], ...] = (
    ("400 Broad St, Seattle, WA 98109", "Space Needle — WA-07, padded single digit", 2),
    ("20 W 34th St, New York, NY 10001", "Empire State Building — NY-12", 2),
    ("200 W 24th St, Cheyenne, WY 82002", "Wyoming State Capitol — at-large (CD=00)", 2),
    ("1600 Pennsylvania Ave NW, Washington, DC 20500", "White House — delegate (CD=98)", 0),
)


def percentile(values: list[float], p: float) -> float:
    """The p-th percentile by nearest-rank, which is honest at this sample size.

    Interpolating between two of six samples would report a number no request
    actually took. Nearest-rank reports one that did.
    """
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(p / 100 * len(ordered) + 0.5))))
    return ordered[rank - 1]


def main() -> int:  # noqa: C901 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    # Captured so the privacy check can prove no log line from a real run
    # carried an address. Attached to the `app` logger rather than the root one:
    # the root logger at DEBUG turns on httpx's wire logging, which prints every
    # request URL — and the geocoder's URL carries the address in a query
    # parameter, so raising the root level to inspect Informed's own logs would
    # itself print the thing this check exists to prove is never printed.
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    app_logger = logging.getLogger("app")
    capture = _Capture(level=logging.DEBUG)
    app_logger.addHandler(capture)
    app_logger.setLevel(logging.DEBUG)

    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)
    logger.info("NO WRITES: this script only reads. Nothing to clean up.\n")

    # The production app, with only the ZeroDB client pointed at the live
    # project. The resolver is NOT overridden: the real Census Geocoder is most
    # of the latency being measured, so faking it would measure nothing.
    app = create_app()
    app.dependency_overrides[zerodb_client] = lambda: client

    all_timings: list[float] = []
    bodies: list[str] = []

    try:
        with TestClient(app) as api:
            for address, label, expected_senators in CASES:
                logger.info("--- %s", label)
                timings: list[float] = []
                payload: dict | None = None

                for _ in range(RUNS):
                    start = time.perf_counter()
                    response = api.get("/v1/ballot", params={"address": address})
                    timings.append((time.perf_counter() - start) * 1000)
                    if response.status_code != 200:
                        check(
                            f"the endpoint answers 200 ({label})",
                            False,
                            f"status {response.status_code}: {response.text[:200]}",
                        )
                        break
                    bodies.append(response.text)
                    payload = response.json()

                all_timings.extend(timings)
                logger.info(
                    "      %d runs: median %.0fms  p95 %.0fms  max %.0fms  (first %.0fms)",
                    len(timings),
                    statistics.median(timings),
                    percentile(timings, 95),
                    max(timings),
                    timings[0],
                )
                if payload is None:
                    continue

                races = {race["body_id"]: race for race in payload["races"]}
                logger.info(
                    "      races: %s",
                    "; ".join(
                        f"{race['office_name']} ({len(race['candidates'])}): "
                        + ", ".join(c["name"] for c in race["candidates"])
                        for race in payload["races"]
                    )
                    or "none",
                )

                # --- races with incumbents flagged ------------------------
                house = races.get(HOUSE_BODY_ID)
                check(
                    f"a House race is listed for the district ({label})",
                    house is not None and house["district_id"] == payload["district"]["id"],
                    f"races were {sorted(races)}",
                )
                if house is not None:
                    check(
                        f"the House race names exactly one sitting member ({label})",
                        len(house["candidates"]) == 1,
                        f"got {[c['name'] for c in house['candidates']]}",
                    )

                senate = races.get(SENATE_BODY_ID)
                if expected_senators:
                    # The at-large regression check: in WY the House and Senate
                    # divisions are the same OCD-ID, so a grouping keyed on the
                    # division alone merges the chambers and loses a seat.
                    check(
                        f"a Senate race is listed with {expected_senators} senators ({label})",
                        senate is not None and len(senate["candidates"]) == expected_senators,
                        f"senate race was {senate}",
                    )
                    if senate is not None and house is not None:
                        check(
                            f"the House and Senate races are distinct ({label})",
                            senate["id"] != house["id"],
                            "the two chambers collapsed into one race",
                        )
                else:
                    # DC and the five territories elect no senators. An empty
                    # Senate race would claim a contest that does not exist.
                    check(
                        f"no Senate race is listed where there are no senators ({label})",
                        senate is None,
                        f"an empty Senate race was listed: {senate}",
                    )

                candidates = [c for race in payload["races"] for c in race["candidates"]]
                check(
                    f"every candidate is flagged as an incumbent ({label})",
                    bool(candidates) and all(c["is_incumbent"] is True for c in candidates),
                    f"unflagged: {[c for c in candidates if not c['is_incumbent']]}",
                )
                check(
                    f"every candidate names a real person row ({label})",
                    all(c["person_id"].startswith("person:") and c["name"] for c in candidates),
                    f"got {candidates}",
                )
                check(
                    f"the response declares that only incumbents are listed ({label})",
                    payload["data_state"] == DataState.INCUMBENTS_ONLY.value,
                    f"data_state is {payload['data_state']!r}",
                )
                check(
                    f"the flat officeholders list agrees with the races ({label})",
                    {o["person_id"] for o in payload["officeholders"]}
                    == {c["person_id"] for c in candidates},
                    "the two views of the same people disagree",
                )

            # --- a blank address is still refused -------------------------
            blank = api.get("/v1/ballot", params={"address": "   "})
            check(
                "a blank address is refused with 422",
                blank.status_code == 422,
                f"{blank.status_code}",
            )

        # --- the acceptance criterion: p95 under 800 ms -------------------
        logger.info("\n--- latency against story 2.4's 800 ms budget")
        pooled_p95 = percentile(all_timings, 95)
        logger.info(
            "      %d lookups across %d addresses: median %.0fms  p95 %.0fms  max %.0fms",
            len(all_timings),
            len(CASES),
            statistics.median(all_timings),
            pooled_p95,
            max(all_timings),
        )
        check(
            f"the pooled p95 is under {BUDGET_MS:.0f}ms",
            pooled_p95 < BUDGET_MS,
            f"p95 was {pooled_p95:.0f}ms",
        )

        # --- privacy -------------------------------------------------------
        logger.info("\n--- privacy: the address reaches no response and no log")
        log_blob = " ".join(f"{r.getMessage()} {r.args}" for r in records).lower()
        body_blob = " ".join(bodies).lower()
        for address, label, _ in CASES:
            # Checked by distinctive fragment rather than whole string, because
            # the geocoder echoes the address normalised and upper-cased. A bare
            # house number is not distinctive — "20" and "400" occur in
            # timestamps, ids and counts — so the number is only checked as part
            # of "<number> <street>", while the street name and ZIP stand alone.
            street_line = address.split(",")[0]
            fragments = (
                address.lower(),
                street_line.lower(),
                street_line.split(" ", 1)[1].lower(),
                address.rsplit(" ", 1)[-1],
            )
            check(
                f"no response body carries any part of the address ({label.split(' —')[0]})",
                not [f for f in fragments if f in body_blob],
                f"leaked: {[f for f in fragments if f in body_blob]}",
            )
            check(
                f"no log line carries any part of the address ({label.split(' —')[0]})",
                not [f for f in fragments if f in log_blob],
                f"leaked: {[f for f in fragments if f in log_blob]}",
            )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        app_logger.removeHandler(capture)
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
