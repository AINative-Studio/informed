#!/usr/bin/env python3
"""Verify methodology-version publishing against the LIVE "informed" project (6.1).

Every story from 1.3 on has found at least one discrepancy between what the
in-memory fake agrees to and what ZeroDB actually does, so story 6.1's two
acceptance criteria — "exactly one current version exists" and "publishing a
version emits `methodology.published`" — are checked against the real API as
well as against the fake.

!!! THIS SCRIPT WRITES TO THE LIVE PROJECT, AND THAT IS THE POINT !!!

Unlike story 12.2's verification (a read of a row 4.2 had already written),
this story's subject IS a write. `--publish <version>` publishes a real new
methodology version to the live project, which is legitimate permanent
reference data of exactly the kind 4.2 and 12.2 already treat it as: the row
the profile editor reads its tier weights from and the public page serves. A
methodology version is not test debris and must NOT be cleaned up afterwards —
deleting it would break the citation on anything scored under it, which is the
one thing `docs/methodology-changelog.md` promises a reader never happens.

Because of that, publishing is opt-in. Run with no flags and the script only
READS and reports — including whether the live table already satisfies this
story's invariants — so it is safe to run any time.

What it proves, in order:

  1. **Exactly one live row is marked `is_current`.** Story 6.1's first
     acceptance criterion, read over an unfiltered live scan rather than
     through the server-side filter (which matches nothing live — story 1.4).
  2. **The integrity check agrees**, via `check_current_uniqueness` — so the
     nightly job really would catch a second current version in production,
     not just in a fake.
  3. **Every live row validates** as story 4.2's `MethodologyVersion`.
  4. **The live current version's weights carry all three documented groups**,
     and `weights_not_yet_published` is empty. This is the gap the story
     closed: before it, the live `2026.1` row carried `tiers` only.
  5. **The weights round-trip as nested objects of real numbers.** The hazard
     12.2 pinned for `tiers` now has to hold for two more groups, with
     `evidence_blend`'s fractions being the interesting case: `0.15` coming
     back as `"0.15"` would render fine on the page and break the first time a
     scorer multiplied by it.
  6. **The evidence blend still sums to 1.0 after a live round trip** — a
     float-precision check on real stored bytes, not on the module constant.
  7. **`final_passage` is still the heaviest vote type live**, and no weight
     came back as zero.
  8. **A publish (with `--publish`) emits exactly one new
     `methodology.published` event** carrying the new version id and nothing
     else. The live events endpoint has its own response shape (a bare list,
     not an envelope — see `ZeroDBClient.list_events`), so "the event was
     actually created" is only provable here.
  9. **A publish leaves exactly one current row live**, with the previous
     version flipped to `is_current: False` and its own weights intact. The
     supersede is two non-atomic requests against the real API; this is the
     only place that ordering is exercised for real.
 10. **The superseded version is still fetchable at its own public path**,
     with the weights it was published with — the citation promise, live.
 11. **The public endpoints serve the new version with no credentials**,
     driven through FastAPI's TestClient over a LIVE ZeroDB client.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_methodology_versions_live.py
    python backend/scripts/verify_methodology_versions_live.py --publish 2026.2
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.integrity.checks import check_current_uniqueness  # noqa: E402
from app.main import create_app  # noqa: E402
from app.methodology.models import DOCUMENTED_WEIGHT_GROUPS  # noqa: E402
from app.methodology.publish import (  # noqa: E402
    DEFAULT_EVIDENCE_BLEND,
    DEFAULT_VOTE_TYPE_WEIGHTS,
    METHODOLOGY_PUBLISHED_TOPIC,
    MethodologyPublisher,
    MethodologyPublishError,
)
from app.methodology.router import zerodb_client_for_methodology  # noqa: E402
from app.methodology.service import PublicMethodologyService  # noqa: E402
from app.profiles.methodology import (  # noqa: E402
    TABLE_NAME,
    MethodologyVersion,
)
from app.zerodb import ZeroDBClient  # noqa: E402

#: How many rows the unfiltered live scan asks for. `methodology_versions`
#: grows by roughly one row per publication; a full page would mean the counts
#: below are reading a subset, which is reported rather than under-counted.
PAGE_SIZE = 500

#: Field names that would mean voter data reached an event or a public
#: response. Checked against serialized JSON, not against a model.
VOTER_WORDS = ("voter", "profile", "interest", "dealbreaker", "user_id", "stance")

logger = logging.getLogger("verify_methodology_versions_live")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish",
        metavar="VERSION",
        default=None,
        help=(
            "Publish this version id (e.g. 2026.2) to the LIVE project as the "
            "methodology in force. A real, permanent publication — not test "
            "data, and never cleaned up. Omitted, this script only reads."
        ),
    )
    return parser.parse_args()


def _numeric_offenders(group: object) -> dict[str, str]:
    """Which values in a weight group did not come back as numbers."""
    if not isinstance(group, dict):
        return {"<group>": type(group).__name__}
    return {
        key: type(value).__name__
        for key, value in group.items()
        if not isinstance(value, (int, float)) or isinstance(value, bool)
    }


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)

    service = PublicMethodologyService(client)
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        # --- 0. the live table, before anything -----------------------------
        before = client.query_rows(TABLE_NAME, limit=PAGE_SIZE)
        logger.info("live methodology_versions rows: %d", len(before.rows))
        if before.has_more or len(before.rows) >= PAGE_SIZE:
            logger.warning("a full page came back; the counts below may be reading a subset")
        for row in before.rows:
            logger.info(
                "        %s is_current=%s weight groups=%s",
                row.row_data.get("id"),
                row.row_data.get("is_current"),
                sorted(row.row_data.get("weights") or {}),
            )

        events_before = client.list_events(
            topic=METHODOLOGY_PUBLISHED_TOPIC, limit=PAGE_SIZE
        ).events
        logger.info("live %s events: %d", METHODOLOGY_PUBLISHED_TOPIC, len(events_before))

        # --- 1. exactly one current row, over an unfiltered live scan -------
        current_rows = [
            row.row_data for row in before.rows if row.row_data.get("is_current") is True
        ]
        check(
            "exactly one live methodology version is marked is_current",
            len(current_rows) == 1,
            f"{len(current_rows)} current: {[row.get('id') for row in current_rows]}",
        )

        # --- 2. the nightly integrity check agrees, live --------------------
        report = check_current_uniqueness(client)
        methodology_violations = [v for v in report.violations if v.table == TABLE_NAME]
        check(
            "the live integrity check reports no methodology_versions violation",
            not methodology_violations,
            "; ".join(v.detail for v in methodology_violations),
        )
        logger.info("        check 3 scanned %d row(s) across its tables", report.scanned)

        # --- 3. every live row validates as story 4.2's model ---------------
        invalid: list[str] = []
        for row in before.rows:
            try:
                MethodologyVersion.model_validate(row.row_data)
            except Exception as error:  # noqa: BLE001 - reported, not raised
                invalid.append(f"{row.row_data.get('id')}: {error}")
        check(
            "every live methodology_versions row validates",
            not invalid,
            "; ".join(invalid),
        )

        # --- optional: publish, for real ------------------------------------
        published_now: str | None = None
        if args.publish:
            logger.warning(
                "publishing methodology version %s to the LIVE project. This is "
                "PERMANENT published reference data and must NOT be cleaned up.",
                args.publish,
            )
            try:
                row = MethodologyPublisher(client).publish_new_version(args.publish)
                published_now = row.id
                logger.info("published %s at %s", row.id, row.published_at)
            except MethodologyPublishError as error:
                check(f"publishing {args.publish} succeeded", False, str(error))

        # --- 4. all three weight groups are in force ------------------------
        live_current = service.current()
        logger.info(
            "live current version: %s (published_at=%s)",
            live_current.version,
            live_current.published_at,
        )
        check(
            "the live current version carries all three documented weight groups",
            sorted(live_current.weights) == sorted(DOCUMENTED_WEIGHT_GROUPS),
            f"groups present: {sorted(live_current.weights)}",
        )
        check(
            "the live current version reports no weight group as unpublished",
            live_current.weights_not_yet_published == [],
            f"unpublished: {live_current.weights_not_yet_published}",
        )

        # --- 5. every group round-trips as an object of real numbers --------
        raw = client.get_row(TABLE_NAME, live_current.version)
        raw_weights = (raw.row_data if raw else {}).get("weights")
        check(
            "live `weights` round-trips as a nested object, not a JSON string",
            isinstance(raw_weights, dict),
            f"came back as {type(raw_weights).__name__}",
        )
        if isinstance(raw_weights, dict):
            for group in DOCUMENTED_WEIGHT_GROUPS:
                offenders = _numeric_offenders(raw_weights.get(group))
                check(
                    f"every live `{group}` weight round-trips as a number",
                    not offenders,
                    f"non-numeric: {offenders}",
                )

        # --- 6. the evidence blend still sums to 1.0 after a round trip -----
        live_blend = live_current.weights.get("evidence_blend") or {}
        blend_total = sum(live_blend.values())
        check(
            "the live evidence blend sums to 1.0",
            abs(blend_total - 1.0) < 1e-9,
            f"sums to {blend_total!r}: {live_blend}",
        )
        check(
            "the live evidence blend is prd.md's 70/15/15",
            live_blend == DEFAULT_EVIDENCE_BLEND,
            f"live blend is {live_blend}",
        )

        # --- 7. final passage is still the heaviest vote type ---------------
        live_vote_types = live_current.weights.get("vote_types") or {}
        heaviest = (
            max(live_vote_types, key=lambda name: live_vote_types[name])
            if live_vote_types
            else None
        )
        check(
            "final_passage is the heaviest live vote type",
            heaviest == "final_passage",
            f"heaviest is {heaviest!r}: {live_vote_types}",
        )
        check(
            "the live vote types cover datamodel.md's enum",
            sorted(live_vote_types) == sorted(DEFAULT_VOTE_TYPE_WEIGHTS),
            f"live vote types: {sorted(live_vote_types)}",
        )
        zeroed = [name for name, weight in live_vote_types.items() if not weight > 0]
        check(
            "no live vote type weight is zero",
            not zeroed,
            f"zero-weighted: {zeroed}",
        )

        # --- 8. the publish emitted exactly one event -----------------------
        events_after = client.list_events(topic=METHODOLOGY_PUBLISHED_TOPIC, limit=PAGE_SIZE).events
        logger.info("live %s events now: %d", METHODOLOGY_PUBLISHED_TOPIC, len(events_after))
        if published_now:
            new_events = len(events_after) - len(events_before)
            check(
                "publishing emitted exactly one new methodology.published event",
                new_events == 1,
                f"{new_events} new event(s)",
            )
            matching = [
                event
                for event in events_after
                if event.event_payload.get("version") == published_now
            ]
            check(
                "the live event names the version just published",
                len(matching) == 1,
                f"{len(matching)} event(s) naming {published_now}",
            )
            if matching:
                payload = matching[0].event_payload
                check(
                    "the live event payload is only the version",
                    set(payload) == {"version"},
                    f"payload keys: {sorted(payload)}",
                )
                serialized = json.dumps(payload).lower()
                leaked = [word for word in VOTER_WORDS if word in serialized]
                check(
                    "the live event payload carries nothing voter-derived",
                    not leaked,
                    f"leaked: {leaked}",
                )
        else:
            # Still worth asserting: if a version was published by an earlier
            # run of this script, its event has to still be readable, because
            # the event log is what proves a publication happened at all.
            check(
                "the live project has at least one methodology.published event",
                bool(events_after),
                "none found — no publication has ever been announced. Run with "
                "--publish to publish a real version.",
            )

        # --- 9. exactly one current row after the publish -------------------
        after = client.query_rows(TABLE_NAME, limit=PAGE_SIZE)
        current_after = [
            row.row_data for row in after.rows if row.row_data.get("is_current") is True
        ]
        check(
            "exactly one live version is marked is_current after the run",
            len(current_after) == 1,
            f"{len(current_after)} current: {[row.get('id') for row in current_after]}",
        )
        if published_now:
            check(
                "the version just published is the one in force",
                [row.get("id") for row in current_after] == [published_now],
                f"in force: {[row.get('id') for row in current_after]}",
            )
            superseded = [
                row.row_data for row in after.rows if row.row_data.get("id") != published_now
            ]
            still_current = [row.get("id") for row in superseded if row.get("is_current") is True]
            check(
                "every previously-current live version was flipped to is_current false",
                not still_current,
                f"still current: {still_current}",
            )
            # The append-only guarantee, live: a superseded row keeps its own
            # weights. `update_row` read-merge-writes; a bare PUT would have
            # replaced the row wholesale (story 1.2's finding).
            intact = [
                row.get("id")
                for row in superseded
                if isinstance(row.get("weights"), dict) and row.get("weights")
            ]
            check(
                "every superseded live version kept its own weights",
                len(intact) == len(superseded),
                f"{len(superseded) - len(intact)} superseded row(s) lost their weights",
            )

        # --- 10. a superseded version is still citable ----------------------
        superseded_ids = [
            str(row.row_data.get("id"))
            for row in after.rows
            if row.row_data.get("is_current") is not True
        ]
        if superseded_ids:
            target = superseded_ids[0]
            fetched = service.version(target)
            check(
                f"superseded live version {target} is still fetchable with its weights",
                fetched.is_current is False and bool(fetched.weights),
                f"is_current={fetched.is_current} weights={sorted(fetched.weights)}",
            )
        else:
            logger.info("SKIP  no superseded live version yet; nothing to check for citability")

        # --- 11. the public endpoints serve it, with no credentials ---------
        app = create_app()
        app.dependency_overrides[zerodb_client_for_methodology] = lambda: client
        with TestClient(app) as http:
            response = http.get("/v1/methodology")
            check(
                "GET /v1/methodology answers 200 over live ZeroDB with no credentials",
                response.status_code == 200,
                f"status={response.status_code} body={response.text[:200]}",
            )
            if response.status_code == 200:
                body = response.json()
                check(
                    "the public endpoint serves the live version in force",
                    body["version"] == live_current.version,
                    f"served {body['version']!r}, expected {live_current.version!r}",
                )
                check(
                    "the public endpoint publishes all three weight groups",
                    sorted(body["weights"]) == sorted(DOCUMENTED_WEIGHT_GROUPS)
                    and body["weights_not_yet_published"] == [],
                    f"groups={sorted(body['weights'])} "
                    f"unpublished={body['weights_not_yet_published']}",
                )
                serialized = json.dumps(body).lower()
                leaked = [word for word in VOTER_WORDS if word in serialized]
                check(
                    "the public methodology response carries nothing voter-derived",
                    not leaked,
                    f"leaked: {leaked}",
                )

            versions = http.get("/v1/methodology/versions")
            listed = versions.json().get("versions", []) if versions.status_code == 200 else []
            check(
                "GET /v1/methodology/versions answers 200 and lists every live version",
                versions.status_code == 200 and len(listed) == len(after.rows),
                f"status={versions.status_code} listed={len(listed)} stored={len(after.rows)}",
            )
            if versions.status_code == 200:
                listed_current = [entry for entry in listed if entry["is_current"]]
                check(
                    "exactly one listed live version is marked current",
                    len(listed_current) == 1,
                    f"{len(listed_current)} marked current",
                )

    except Exception:  # noqa: BLE001 - a live script reports rather than tracebacks
        logger.exception("verification aborted")
        return 1

    logger.info("")
    if published_now:
        logger.info(
            "PUBLISHED methodology version %s to the live project. This row and its "
            "methodology.published event are PERMANENT reference data — do not delete "
            "them. Add its entry to docs/methodology-changelog.md.",
            published_now,
        )
    if failures:
        logger.error("%d check(s) FAILED:", len(failures))
        for failure in failures:
            logger.error("  - %s", failure)
        return 1
    logger.info("all checks PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
