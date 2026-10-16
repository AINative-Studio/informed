#!/usr/bin/env python3
"""Verify the legislator load against the LIVE "informed" ZeroDB project (2.2).

Loads a small, deterministic subset of real legislators from the fixture
roster (tests/fixtures/legislators_current_sample.yaml) against the real
"informed" project, through the exact same `LegislatorLoader` the full live
run uses. A small, known subset rather than the full ~539-person roster is
what this script loads, so it runs in seconds and leaves an auditable,
easily-reasoned-about footprint ahead of the full load.

What it proves, in order:

  1. **All three fixture people load with bioguide ids.** Read back through
     the repository, so the stored row is also proven to still validate
     against the `Person` model.
  2. **Terms cover the 118th and 119th Congress.** Checked on the senator
     fixture (C000127, Maria Cantwell), whose fixture terms span exactly
     that boundary, matching story 2.2's acceptance criterion literally.
  3. **Running the load twice is idempotent, proven by HTTP method, not just
     row count** -- same proof pattern story 2.1's `verify_seed_live.py`
     uses for row count, extended here to assert *how* ZeroDB was asked to
     write the second time. `_RecordingTransport` wraps the real HTTPS
     transport and records every `(method, path)` pair exactly like the fake
     does for contract tests; the second load's writes for an
     already-existing person must be `PUT` (ZeroDBClient._replace_row), never
     a second `POST .../rows` (which would mean `upsert_row`'s find step
     missed the existing row and silently duplicated it -- precisely the bug
     story 1.4's live run caught for `people` itself).

!!! NO CLEANUP, DELIBERATELY !!!

Exactly like `verify_seed_live.py`: these three rows are real, permanent
legislator data (Maria Cantwell, Eleanor Holmes Norton, Bernie Sanders, all
real sitting or recently-sitting members of Congress with real bioguide ids),
not synthetic test debris. They are left in place. The *full* roster is loaded
separately by `scripts/load_legislators.py` against the same project; running
both against the same `people` table is expected and is itself a second,
larger idempotency proof (the full run's upsert of these same three bioguide
ids must not duplicate what this script already wrote).

Run it with the live credentials sourced into the shell -- never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_legislators_live.py
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.legislators import (  # noqa: E402
    LegislatorLoader,
    parse_legislators_yaml,
    transform_legislators,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import Person  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402
from app.zerodb.settings import ZeroDBSettings  # noqa: E402
from app.zerodb.transport import HttpxZeroDBTransport, ZeroDBResponse  # noqa: E402

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "legislators_current_sample.yaml"
)

CANTWELL_ID = "person:C000127"
NORTON_ID = "person:N000147"
SANDERS_ID = "person:S000033"
FIXTURE_PERSON_IDS = (CANTWELL_ID, NORTON_ID, SANDERS_ID)

logger = logging.getLogger("verify_legislators_live")


@dataclass
class _RecordingTransport:
    """Wraps a real transport, recording `(method, path)` for every call.

    Same shape as `app.zerodb.testing.FakeZeroDBTransport`'s call recording,
    applied to the REAL HTTPS transport instead of a fake one, so this script
    can assert on how the live API was actually addressed -- PUT vs POST --
    not just on the row count afterwards. See the module docstring's check 3.
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
    ) -> ZeroDBResponse:
        self.calls.append((method, path))
        return self.inner.request(method, path, json=json, params=params)

    def close(self) -> None:
        self.inner.close()


def live_row_count(client: ZeroDBClient, table: str, row_id: str) -> int:
    """How many rows in `table` carry this natural-key id, read live.

    Deliberately not `Repository.get`, which stops at the first match --
    counting duplicates is exactly what the idempotency check asks, same as
    `verify_seed_live.py`'s `count_live_rows`.
    """
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

    people: Repository[Person] = Repository(client=client, model=Person)
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        records = parse_legislators_yaml(FIXTURE_PATH.read_text())
        rows, skipped = transform_legislators(records)
        check("the fixture roster transforms with no skips", not skipped, f"skipped: {skipped}")

        # --- 1. load once: all three fixture people exist with bioguide ids -
        logger.info("loading fixture roster (run 1 of 2)")
        first = LegislatorLoader(client).load(rows)
        check(
            "the loader reports three people written",
            first.row_count == 3,
            f"reported {first.row_count}",
        )

        for person_id in FIXTURE_PERSON_IDS:
            stored = people.get(person_id)
            check(
                f"{person_id} exists live with a bioguide-based id",
                stored is not None and stored.id == person_id,
                f"stored row is {stored!r}",
            )

        # --- 2. terms cover the 118th and 119th Congress --------------------
        cantwell = people.get(CANTWELL_ID)
        has_118th_term = cantwell is not None and any(
            t.body_id == "us-senate" and t.start == "2019-01-03" and t.end == "2025-01-03"
            for t in cantwell.terms
        )
        has_119th_term = cantwell is not None and any(
            t.body_id == "us-senate" and t.start == "2025-01-03" and t.end == "2031-01-03"
            for t in cantwell.terms
        )
        check(
            f"{CANTWELL_ID} has a term covering the 118th Congress",
            has_118th_term,
            f"terms={cantwell.terms!r}" if cantwell else "person not found",
        )
        check(
            f"{CANTWELL_ID} has a term covering the 119th Congress",
            has_119th_term,
            f"terms={cantwell.terms!r}" if cantwell else "person not found",
        )

        # --- 3. load again: idempotent, proven by HTTP method --------------
        recording.calls.clear()
        logger.info("loading fixture roster (run 2 of 2) — this is the idempotency check")
        second = LegislatorLoader(client).load(rows)
        check(
            "the second run reports the same three people",
            second.row_count == 3,
            f"reported {second.row_count}",
        )

        for person_id in FIXTURE_PERSON_IDS:
            count = live_row_count(client, "people", person_id)
            check(
                f"exactly one live row carries {person_id} after two runs",
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
            "the second run's writes were PUTs (updates), not a second POST .../rows per person",
            len(post_row_writes) == 0 and len(put_writes) >= 3,
            f"POST .../rows calls: {len(post_row_writes)}, PUT calls: {len(put_writes)} "
            f"(all write calls: {write_calls})",
        )

        re_read = people.get(CANTWELL_ID)
        check(
            f"{CANTWELL_ID} still carries the same terms after the second run",
            re_read is not None and re_read.terms == cantwell.terms,
            f"stored row is {re_read!r}",
        )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        logger.info(
            "LEFT IN PLACE (intentionally): %s — real legislator data, permanent "
            "reference data, not test debris",
            ", ".join(FIXTURE_PERSON_IDS),
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
