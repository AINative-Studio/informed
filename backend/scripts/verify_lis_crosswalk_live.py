#!/usr/bin/env python3
"""Verify the LIS-to-bioguide crosswalk against the LIVE project (story 8.2).

This story's acceptance criterion -- "100% of sitting senators map; an
unmapped id raises an alert" -- is a claim about the REAL current Senate and
the REAL live `people` table, not about a fixture. A green BDD suite proves
the code is correct; only this script can prove the claim is true.

What it proves, in order:

  1. **The live roster names a real, plausible sitting Senate.** Fetched live
     from `legislators-current.yaml` -- the source chosen after fetching all
     four candidates (see `app.ingest.lis_crosswalk`'s module docstring for
     why senate.gov's own `senators_cfm.xml` and Congress.gov's member API
     both turned out to carry no LIS id at all). Checked for exactly 100
     senators, the Senate's own seat count, and reported if it differs
     (a genuine vacancy is real and is reported, not papered over).
  2. **Every sitting senator's LIS id writes onto their real `people` row**,
     merged into `external_ids` rather than replacing it -- the live rows
     carry `govtrack`, `fec`, `thomas` and more from story 2.2's roster
     load, and ZeroDB's `PUT` replaces a row wholesale, so a crosswalk
     refresh that did not merge would destroy them. Checked by re-reading
     one real row and asserting its other ids survived.
  3. **The crosswalk, read back from the live `people` table, covers 100% of
     the sitting Senate.** `check_coverage` against the live roster. This is
     the acceptance criterion, measured.
  4. **Every LIS id in story 8.1's real live `member_votes` rows resolves.**
     8.1 wrote 500 real rows for 5 real roll calls (`us-senate:119-2:252`
     through `:256`). This re-fetches those 5 roll calls' XML live from
     senate.gov, pulls every `lis_member_id` out, and resolves each against
     the crosswalk read from ZeroDB -- the real end-to-end question this
     story exists to answer. The real mapping rate is reported as a number,
     whatever it is. It then cross-checks the other direction: every
     `person_id` actually present in the live Senate `member_votes` rows
     must be one the crosswalk now resolves to.
  5. **An unmapped id actually alerts rather than being silently dropped.**
     A deliberately bogus LIS id is resolved with alerting on; the counter
     and the strict `require_bioguide_id` raise are both checked, and the
     rendered Prometheus gauge is printed.
  6. **A second refresh is idempotent**: the same senators map, and reading
     the crosswalk back finds no duplicate entry.

Live run, 2026-10-19, against project bb8c620a (every check PASSed):

    live roster: 539 records, 100 sitting senators
    refresh: 100 mapped, 0 without an LIS id, 0 with no people row
    live crosswalk holds 100 mappings
    LIVE COVERAGE: 100 of 100 sitting senators map (100.0%)
    story 8.1's live roll calls: 500 member positions, 100 distinct LIS ids,
      100 resolve to a bioguide id
    live member_votes rows for us-senate roll calls: 500, 0 orphans

!!! NO CLEANUP, DELIBERATELY !!!

Same posture as `verify_senate_rollcalls_live.py` and
`verify_legislators_live.py`: what this script writes is a REAL, PERMANENT
public identifier mapping (each sitting senator's own LIS id, onto their own
`people` row), not synthetic test debris. It is left in place -- it is, in
fact, this story's deliverable. The one exception is the bogus LIS id in
check 5, which is never written anywhere: it is resolved in memory only.

No voter data is read, written or logged.

Run it with the live credentials sourced into the shell -- never passed on
the command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_lis_crosswalk_live.py
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.lis_crosswalk import (  # noqa: E402
    LIS_EXTERNAL_ID_KEY,
    LisCrosswalkSourceError,
    LisCrosswalkWriter,
    UnmappedLisMemberIdError,
    check_coverage,
    fetch_roster,
    load_crosswalk,
    render_coverage_prometheus_text,
    sitting_senators_from_roster,
)
from app.ingest.senate_rollcalls import (  # noqa: E402
    SENATE_LIS_BASE_URL,
    SenateRollCallSourceError,
    fetch_roll_call_xml,
    roll_call_id,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import Person  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402
from app.zerodb.settings import ZeroDBSettings  # noqa: E402

logger = logging.getLogger("verify_lis_crosswalk_live")

#: The Senate's statutory seat count. Not padded to and not enforced -- a
#: real vacancy is real -- but a roster that names materially fewer is a
#: finding worth seeing rather than silently accepting.
SENATE_SEATS = 100

#: Story 8.1's real live roll calls (confirmed live: 500 `member_votes` rows
#: across these five). Re-fetched here so check 4 resolves the ids that are
#: actually in the live data, not ids this script chose.
STORY_8_1_CONGRESS = 119
STORY_8_1_SESSION = 2
STORY_8_1_VOTE_NUMBERS = (252, 253, 254, 255, 256)

#: A LIS id no senator has or will have, for check 5. Resolved in memory
#: only; never written.
BOGUS_LIS_ID = "S-zzz-live-check-8.2"

#: The real `people` row whose other external ids are checked for survival in
#: check 2. Any sitting senator would do; this one is picked because it is
#: long-serving and so has an unusually full `id:` map in the source.
MERGE_CHECK_LIS_ID = "S313"


def _lis_ids_from_live_8_1_roll_calls() -> dict[str, set[str]]:
    """`{roll_call_id: {lis_member_id, ...}}` for story 8.1's real roll calls,
    re-fetched live from senate.gov.
    """
    by_roll_call: dict[str, set[str]] = {}
    for vote_number in STORY_8_1_VOTE_NUMBERS:
        xml = fetch_roll_call_xml(
            congress=STORY_8_1_CONGRESS,
            session=STORY_8_1_SESSION,
            vote_number=vote_number,
            base_url=SENATE_LIS_BASE_URL,
        )
        ids = set(re.findall(r"<lis_member_id>([^<]+)</lis_member_id>", xml))
        by_roll_call[
            roll_call_id(
                congress=STORY_8_1_CONGRESS, session=STORY_8_1_SESSION, vote_number=vote_number
            )
        ] = ids
    return by_roll_call


def main() -> int:  # noqa: C901 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings = ZeroDBSettings()
    client = ZeroDBClient.from_env(settings)
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
        # --- 1. the live roster ------------------------------------------
        logger.info("fetching the live roster (legislators-current.yaml)")
        records = fetch_roster()
        senators = sitting_senators_from_roster(records)
        logger.info(
            "live roster: %d records, %d sitting senators (latest term is a Senate term)",
            len(records),
            len(senators),
        )
        check("the live roster names at least one sitting senator", len(senators) > 0)
        check(
            f"the live roster names {SENATE_SEATS} sitting senators (the Senate's seat count)",
            len(senators) == SENATE_SEATS,
            f"named {len(senators)} — a real vacancy is possible and is a finding, not a bug",
        )
        without_lis = [s.person_id for s in senators if s.lis_member_id is None]
        check(
            "every sitting senator in the live roster carries an id.lis",
            not without_lis,
            f"no LIS id for: {without_lis}",
        )

        # --- 2. the write, merged ----------------------------------------
        before = next(
            (people.get(s.person_id) for s in senators if s.lis_member_id == MERGE_CHECK_LIS_ID),
            None,
        )
        other_ids_before = (
            {k: v for k, v in before.external_ids.items() if k != LIS_EXTERNAL_ID_KEY}
            if before is not None
            else {}
        )
        logger.info("refreshing the crosswalk onto %d live people rows (paced)", len(senators))
        report = LisCrosswalkWriter(client).refresh(senators)
        logger.info(
            "refresh: %d mapped, %d without an LIS id, %d with no people row",
            len(report.mapped),
            len(report.without_lis_id),
            len(report.missing_people_rows),
        )
        check(
            "every sitting senator has a live people row to write onto",
            not report.missing_people_rows,
            f"missing people rows: {report.missing_people_rows} "
            f"— run scripts/load_legislators.py (story 2.2)",
        )
        check(
            f"all {len(senators)} sitting senators' LIS ids were written",
            len(report.mapped) == len(senators),
            f"mapped {len(report.mapped)} of {len(senators)}",
        )

        if other_ids_before:
            after = next(
                (
                    people.get(s.person_id)
                    for s in senators
                    if s.lis_member_id == MERGE_CHECK_LIS_ID
                ),
                None,
            )
            check(
                "the refresh merged into external_ids rather than replacing it "
                "(story 2.2's other ids survived)",
                after is not None
                and all(after.external_ids.get(k) == v for k, v in other_ids_before.items()),
                f"before: {sorted(other_ids_before)}, "
                f"after: {sorted(after.external_ids) if after else None}",
            )
        else:
            logger.info(
                "SKIP  external_ids merge check — the live row for LIS %s carried no "
                "other external ids to preserve",
                MERGE_CHECK_LIS_ID,
            )

        # --- 3. the acceptance criterion, measured -----------------------
        logger.info("reading the crosswalk back from the live people table")
        crosswalk = load_crosswalk(client)
        logger.info("live crosswalk holds %d mappings", len(crosswalk))
        coverage = check_coverage(crosswalk, senators)
        logger.info(
            "LIVE COVERAGE: %d of %d sitting senators map (%.1f%%)",
            coverage.mapped_count,
            coverage.sitting_count,
            coverage.percentage,
        )
        if coverage.unmapped_person_ids:
            logger.error("unmapped sitting senators: %s", coverage.unmapped_person_ids)
        if coverage.unmapped_lis_ids:
            logger.info(
                "%d crosswalk entries belong to no sitting senator (former senators, "
                "whose mappings are deliberately kept so historical roll calls still "
                "join): %s",
                len(coverage.unmapped_lis_ids),
                coverage.unmapped_lis_ids[:10],
            )
        check(
            "100% of sitting senators map (story 8.2's acceptance criterion)",
            coverage.is_complete,
            f"{coverage.mapped_count} of {coverage.sitting_count}; "
            f"unmapped: {coverage.unmapped_person_ids}",
        )
        logger.info("rendered coverage metrics:\n%s", render_coverage_prometheus_text(coverage))

        # --- 4. story 8.1's real live member_votes rows ------------------
        logger.info(
            "re-fetching story 8.1's %d real roll calls from senate.gov to resolve the "
            "LIS ids actually present in the live member_votes data",
            len(STORY_8_1_VOTE_NUMBERS),
        )
        by_roll_call = _lis_ids_from_live_8_1_roll_calls()
        total_positions = sum(len(ids) for ids in by_roll_call.values())
        all_lis_ids: set[str] = set().union(*by_roll_call.values()) if by_roll_call else set()
        resolved = {lis_id for lis_id in all_lis_ids if crosswalk.resolve_bioguide_id(lis_id)}
        unresolved = sorted(all_lis_ids - resolved)
        logger.info(
            "story 8.1's live roll calls: %d member positions, %d distinct LIS ids, "
            "%d resolve to a bioguide id",
            total_positions,
            len(all_lis_ids),
            len(resolved),
        )
        for rc_id, ids in sorted(by_roll_call.items()):
            logger.info("  %s: %d positions", rc_id, len(ids))
        check(
            "every LIS id in story 8.1's real live roll calls resolves to a bioguide id",
            not unresolved,
            f"unresolved: {unresolved}",
        )

        # Cross-check the other direction: the person_id actually written into
        # the live Senate member_votes rows must be one the crosswalk resolves
        # to. This is what proves the crosswalk and 8.1's in-memory resolution
        # agree on the real data, rather than each being internally consistent.
        live_member_votes = [
            row.row_data
            for row in client.query_rows("member_votes", limit=1000).rows
            if str(row.row_data.get("roll_call_id", "")).startswith("us-senate:")
        ]
        logger.info("live member_votes rows for us-senate roll calls: %d", len(live_member_votes))
        live_person_ids = {str(row.get("person_id")) for row in live_member_votes}
        crosswalk_person_ids = {f"person:{b}" for b in crosswalk.bioguide_ids}
        orphans = sorted(live_person_ids - crosswalk_person_ids)
        check(
            "every person_id in the live Senate member_votes rows is one the crosswalk "
            "now resolves to",
            not orphans,
            f"{len(orphans)} person_id(s) no crosswalk entry maps to: {orphans[:10]}",
        )

        # --- 5. an unmapped id alerts ------------------------------------
        before_count = crosswalk.unmapped_alert_count
        crosswalk.resolve_bioguide_id(BOGUS_LIS_ID, alert=True)
        check(
            "an unmapped LIS id is counted as an alert, not silently dropped",
            crosswalk.unmapped_alert_count == before_count + 1,
            f"count went {before_count} -> {crosswalk.unmapped_alert_count}",
        )
        raised = False
        try:
            crosswalk.require_bioguide_id(BOGUS_LIS_ID)
        except UnmappedLisMemberIdError:
            raised = True
        check("the strict path raises on an unmapped LIS id", raised)

        # --- 6. idempotency ----------------------------------------------
        second = LisCrosswalkWriter(client).refresh(senators)
        check(
            "a second refresh maps the same senators (idempotent)",
            len(second.mapped) == len(report.mapped),
            f"first {len(report.mapped)}, second {len(second.mapped)}",
        )
        recount = load_crosswalk(client)
        check(
            "a second refresh produced no duplicate crosswalk entry",
            len(recount) == len(crosswalk),
            f"before {len(crosswalk)}, after {len(recount)}",
        )

    except LisCrosswalkSourceError as exc:
        failures.append(f"roster source error: {exc}")
        logger.error("FAIL  live roster fetch failed: %s", exc)
    except SenateRollCallSourceError as exc:
        failures.append(f"senate.gov source error: {exc}")
        logger.error("FAIL  live senate.gov fetch failed: %s", exc)
    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)
    finally:
        logger.info(
            "LEFT IN PLACE (intentionally): each sitting senator's own LIS id on their "
            "own people row — real public identifier data, this story's deliverable, "
            "not test debris. The bogus id %r was never written anywhere.",
            BOGUS_LIS_ID,
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
