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

  1. **Latency.** `ROUNDS` paced, round-robin lookups across the addresses,
     reported per address and pooled, against the 800 ms budget.

     Two things about the methodology are load-bearing, and both were learned
     by getting them wrong first. **Round-robin, not N-in-a-row per address:**
     the first version of this script ran six lookups per address in sequence,
     which meant five of every six hit a warm district cache for that one
     district, and reported an optimistic p95 (705 ms) that a round-robin run
     over six addresses did not reproduce (1066 ms). **Paced, not back to
     back:** the free Census Geocoder throttles a tight loop — the same code
     measured a geocode p95 of 621 ms when called at a human pace and 1543 ms
     with a 4404 ms outlier when hammered — so an unpaced run measures
     census.gov's rate limiting rather than Informed's latency.

     The **cold-start** request (the first of a fresh process, paying TCP and
     TLS setup to two hosts plus both cold caches) is reported separately
     rather than dropped. It is real and a voter can hit it after a deploy; it
     is simply not what a steady-state p95 describes.

     What remains after all of that is the geocode, which is one external call
     to a free public service with an occasional multi-second outlier that no
     caching on Informed's side can remove — the response times in
     `app.ballot.service`'s docstring are what the endpoint controls.
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

#: Rounds over the whole address list. Requests go **round-robin** across
#: addresses rather than N times per address, which matters: hitting one address
#: six times in a row flatters the result, because the second through sixth all
#: hit a warm district cache for that one district. Round-robin exercises the
#: caches the way a stream of real voters would — many districts, one shared
#: `people` snapshot — and was what first revealed that the per-address loop had
#: been reporting an optimistic p95.
#:
#: Ten rounds over four addresses is 40 samples, chosen so that **one** geocoder
#: outlier does not define the p95. At 19 samples it did: a single 1500 ms
#: geocode in an otherwise 440 ms median / 578 ms p90 run *was* the 95th
#: percentile, and the script reported a failure that said nothing about
#: Informed's code. A p95 over so few samples is really "the worst one or two
#: requests", which for an endpoint fronting a free public geocoder measures
#: that service's weather.
ROUNDS = 10

#: Seconds between requests. Not padding: measured, the free Census Geocoder
#: starts adding multi-second delays to a tight benchmark loop (p95 1543 ms,
#: max 4404 ms back-to-back at one point), which measures its rate limiting
#: rather than Informed's latency. Paced at roughly a request a second the same
#: code measured a p95 of 605 ms. A second is still far faster than real voter
#: traffic to one process.
REQUEST_GAP_SECONDS = 1.0

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

    #: address -> every timing for it, and the last payload it returned.
    timings_by_address: dict[str, list[float]] = {address: [] for address, _, _ in CASES}
    payload_by_address: dict[str, dict] = {}
    cold_start_ms: float | None = None
    bodies: list[str] = []

    try:
        with TestClient(app) as api:
            # --- phase 1: latency, round-robin --------------------------------
            #
            # Round-robin rather than N-in-a-row per address, and paced; see
            # ROUNDS and REQUEST_GAP_SECONDS for why both matter to whether the
            # resulting p95 means anything.
            logger.info("--- %d rounds over %d addresses, round-robin", ROUNDS, len(CASES))
            for _round in range(ROUNDS):
                for address, label, _ in CASES:
                    if cold_start_ms is not None:
                        time.sleep(REQUEST_GAP_SECONDS)
                    start = time.perf_counter()
                    response = api.get("/v1/ballot", params={"address": address})
                    elapsed = (time.perf_counter() - start) * 1000
                    if cold_start_ms is None:
                        # The very first request of a fresh process pays TCP and
                        # TLS setup to two hosts plus both cold caches. Recorded
                        # and reported separately rather than quietly dropped:
                        # it is real, a voter can hit it after a deploy, and it
                        # is not what a steady-state p95 describes.
                        cold_start_ms = elapsed
                    if response.status_code != 200:
                        check(
                            f"the endpoint answers 200 ({label})",
                            False,
                            f"status {response.status_code}: {response.text[:200]}",
                        )
                        continue
                    timings_by_address[address].append(elapsed)
                    bodies.append(response.text)
                    payload_by_address[address] = response.json()

            # --- phase 2: the response's shape --------------------------------
            for address, label, expected_senators in CASES:
                logger.info("--- %s", label)
                timings = timings_by_address[address]
                if timings:
                    logger.info(
                        "      %d runs: median %.0fms  p95 %.0fms  max %.0fms",
                        len(timings),
                        statistics.median(timings),
                        percentile(timings, 95),
                        max(timings),
                    )
                payload = payload_by_address.get(address)
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
        all_timings = [t for timings in timings_by_address.values() for t in timings]
        steady = [t for t in all_timings if t != cold_start_ms]
        pooled_p95 = percentile(steady, 95)
        logger.info(
            "      %d lookups across %d addresses:"
            "  median %.0fms  p90 %.0fms  p95 %.0fms  max %.0fms",
            len(steady),
            len(CASES),
            statistics.median(steady),
            percentile(steady, 90),
            pooled_p95,
            max(steady),
        )
        logger.info(
            "      cold start (first request of the process): %.0fms — reported "
            "separately, not in the p95 above",
            cold_start_ms or 0.0,
        )
        over = [t for t in steady if t >= BUDGET_MS]
        if over:
            # Listed individually rather than left inside the percentile,
            # because the honest account of this budget is "the geocoder
            # occasionally takes seconds and nothing on Informed's side can
            # cache that away". Seeing *which* requests were slow is what tells
            # an operator whether the endpoint regressed or census.gov had a
            # bad minute.
            logger.info(
                "      %d of %d lookups were at or over budget: %s",
                len(over),
                len(steady),
                ", ".join(f"{t:.0f}ms" for t in sorted(over, reverse=True)[:8]),
            )
        check(
            f"the steady-state p95 is under {BUDGET_MS:.0f}ms",
            pooled_p95 < BUDGET_MS,
            f"p95 was {pooled_p95:.0f}ms",
        )
        # Reported alongside, because it is the more stable statistic at this
        # sample size and the one that moves if Informed's own work gets slower.
        # A p90 inside budget with a p95 outside it is the signature of a
        # geocoder spike, not of a regression here.
        check(
            f"the steady-state p90 is under {BUDGET_MS:.0f}ms",
            percentile(steady, 90) < BUDGET_MS,
            f"p90 was {percentile(steady, 90):.0f}ms — this one is about "
            "Informed's own work, so investigate rather than re-running",
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
