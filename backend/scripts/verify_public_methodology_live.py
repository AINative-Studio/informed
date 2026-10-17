#!/usr/bin/env python3
"""Verify the public methodology page against the LIVE "informed" project (12.2).

Every story from 1.3 on has found at least one discrepancy between what the
in-memory fake agrees to and what ZeroDB actually does, so story 12.2's
acceptance criterion — that a methodology version is publicly fetchable and
citable — is checked against the real API as well as against the fake.

This story is almost entirely a READ, which makes it an unusual verification:
the row it publishes was written by story 4.2 and is already permanent
reference data. So the question here is not "did my write land" but "is what
4.2 actually stored servable, unchanged, through the public endpoint" —
including the parts a fake cannot tell you, like whether ZeroDB round-trips
the nested `weights` object as a real object with float values rather than a
JSON string or a dict of strings.

What it proves, in order:

  1. **The live `methodology_versions` row exists and still validates** as
     story 4.2's `MethodologyVersion` — read through the public service, so
     the model, the service and the live bytes are all exercised at once.
  2. **`weights` round-trips as a nested object with numeric values.** The one
     thing in this story's wire shape that a dict-of-dicts could plausibly
     come back from a NoSQL store as something else. `3.0` arriving as the
     string `"3.0"` would still render on the page and would break the moment
     epic 6's scorer multiplied by it.
  3. **`is_current` round-trips as a real boolean**, not `"true"` or `1` — the
     same hazard story 2.1 pinned for `Body.seats` and 3.1 for
     `taxonomy_version`. The whole "which version is in force" read turns on
     this one field.
  4. **The live tier weights are prd.md's published numbers** (primary 3.0,
     secondary 1.5, nice_to_have 0.5), read from the database rather than
     from the module constant — so a row edited by hand in the live project
     is caught rather than papered over by a comparison against our own
     default.
  5. **The row's id matches datamodel.md's documented pattern** (`2026.1`),
     and therefore passes the public endpoint's own path-parameter check. A
     live id of some other shape would be unreachable through
     `/v1/methodology/{version}` — fetchable by the service but 422 at the
     edge, which is exactly the sort of gap only a live read finds.
  6. **The published document carries nothing voter-derived**, checked
     against the serialized JSON a caller would actually receive.
  7. **A specific version is fetchable by id, live**, and reports the same
     weights as the current read — the citation target resolving for real.
  8. **An unpublished version id is a clean "not found", live**, rather than
     a 500 or an empty document. `ZeroDBNotFoundError` is raised by this
     story's service; what the live check adds is that the underlying query
     for a missing id really does come back empty rather than erroring.
  9. **The changelog lists every live version, newest first**, over an
     unfiltered live read — and reports exactly one row as current. More than
     one `is_current` row in the live project would mean story 4.2's seed or
     some later hand-edit left the table in a state where "the methodology in
     force" has no single answer, which no amount of unit testing can tell
     you.
 10. **The whole HTTP surface answers with no credentials**, driven through
     FastAPI's TestClient over a LIVE ZeroDB client — the end-to-end shape a
     journalist gets, not just the service layer.

!!! NO CLEANUP NEEDED, AND ALMOST NOTHING WRITTEN !!!

This script performs no writes of its own. The only write that can happen is
story 4.2's seed, via `MethodologyVersionService.current_version()`, and only
if the live project somehow has no `methodology_versions` row at all — in
which case seeding it is the correct outcome, not test debris: it is permanent
published reference data that the profile editor and this page both read, and
the script reports loudly when it had to.

OPTIONAL, EXPLICIT CHANGELOG BACKFILL: pass `--set-changelog-url <url>` to
update the live current row's `changelog_url` to a real published location.
That is a legitimate edit to permanent reference data (the field exists in
datamodel.md precisely to hold it) and is therefore opt-in rather than
something this script does on its own — a verification script must not quietly
rewrite production rows.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_public_methodology_live.py
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.methodology.models import DOCUMENTED_WEIGHT_GROUPS, VERSION_ID_PATTERN  # noqa: E402
from app.methodology.router import zerodb_client_for_methodology  # noqa: E402
from app.methodology.service import PublicMethodologyService  # noqa: E402
from app.profiles.methodology import (  # noqa: E402
    DEFAULT_TIER_WEIGHTS,
    TABLE_NAME,
    MethodologyVersionService,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError  # noqa: E402

#: A version id that must never have been published. Deliberately in the shape
#: the endpoint accepts (`\d{4}\.\d+`) so that check 8 exercises the real
#: ZeroDB miss path rather than being turned away by the path-parameter check.
UNPUBLISHED_VERSION = "1999.9"

#: Field names that would mean voter data reached a public, uncredentialed
#: response. Checked against the serialized document, not the model.
VOTER_WORDS = ("voter", "profile", "interest", "payload", "dealbreaker", "user_id", "stance")

#: How many rows the unfiltered live read asks for. `methodology_versions`
#: holds a handful of rows; a full page would mean the counts below are reading
#: a subset, which is reported rather than silently under-counted.
PAGE_SIZE = 500

logger = logging.getLogger("verify_public_methodology_live")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--set-changelog-url",
        metavar="URL",
        default=None,
        help=(
            "Update the live CURRENT row's changelog_url to this value. An "
            "explicit, opt-in edit to permanent reference data; omitted, this "
            "script writes nothing."
        ),
    )
    return parser.parse_args()


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)

    service = PublicMethodologyService(client)
    failures: list[str] = []
    seeded = False
    backfilled: str | None = None

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        # --- 0. was there anything to publish? ----------------------------
        # Reported separately from check 1 so the log distinguishes "4.2's row
        # was already live" (the expected case) from "this script had to seed
        # it", which is a fact about the environment worth seeing.
        before = client.query_rows(TABLE_NAME, limit=PAGE_SIZE)
        logger.info("live methodology_versions rows before this run: %d", len(before.rows))
        if not before.rows:
            logger.warning(
                "no methodology_versions row live; story 4.2's seed will write one. "
                "That row is PERMANENT published reference data, not test debris."
            )
            MethodologyVersionService(client).current_version()
            seeded = True

        # --- optional, explicit changelog backfill -------------------------
        if args.set_changelog_url:
            target = service.current().version
            logger.info("updating changelog_url on live row %s (explicitly requested)", target)
            client.update_row(TABLE_NAME, target, {"changelog_url": args.set_changelog_url})
            backfilled = target

        # --- 1. the live row reads back and still validates ----------------
        published = service.current()
        check(
            "the live current methodology reads back and validates",
            bool(published.version),
            f"version={published.version!r}",
        )
        logger.info(
            "        version=%s published_at=%s is_current=%s",
            published.version,
            published.published_at,
            published.is_current,
        )

        # --- 2. weights round-trips as a nested object of floats ----------
        raw = client.get_row(TABLE_NAME, published.version)
        raw_weights = (raw.row_data if raw else {}).get("weights")
        check(
            "live `weights` round-trips as a nested object, not a JSON string",
            isinstance(raw_weights, dict),
            f"came back as {type(raw_weights).__name__}: {raw_weights!r}",
        )
        raw_tiers = raw_weights.get("tiers") if isinstance(raw_weights, dict) else None
        non_numeric = (
            {k: type(v).__name__ for k, v in raw_tiers.items() if not isinstance(v, (int, float))}
            if isinstance(raw_tiers, dict)
            else {"<tiers>": type(raw_tiers).__name__}
        )
        check(
            "every live tier weight round-trips as a number, not a string",
            not non_numeric,
            f"non-numeric value(s): {non_numeric}",
        )

        # --- 3. is_current round-trips as a real boolean -------------------
        raw_is_current = (raw.row_data if raw else {}).get("is_current")
        check(
            "live `is_current` round-trips as a bool, not a string or int",
            isinstance(raw_is_current, bool),
            f"came back as {type(raw_is_current).__name__}: {raw_is_current!r}",
        )

        # --- 4. the live numbers are prd.md's published ones ---------------
        check(
            "the live tier weights are prd.md's published numbers "
            "(primary 3.0, secondary 1.5, nice_to_have 0.5)",
            published.weights.get("tiers") == DEFAULT_TIER_WEIGHTS,
            f"live tiers are {published.weights.get('tiers')!r}",
        )

        # --- 5. the id matches datamodel.md's pattern AND the route -------
        check(
            "the live version id matches datamodel.md's `2026.1` pattern, so the "
            "public per-version route can serve it",
            re.match(VERSION_ID_PATTERN, published.version) is not None,
            f"id {published.version!r} would be refused by the endpoint's path check",
        )

        # --- 6. the published document carries nothing voter-derived ------
        document = json.dumps(published.model_dump(mode="json")).lower()
        leaked = [word for word in VOTER_WORDS if word in document]
        check(
            "the published document mentions no voter, profile or interest data",
            not leaked,
            f"mentions {leaked}",
        )

        # --- 7. the citation target resolves, live ------------------------
        by_id = service.version(published.version)
        check(
            "the version is fetchable by its own id and reports the same weights",
            by_id.version == published.version and by_id.weights == published.weights,
            f"by-id read gave {by_id.version!r} / {by_id.weights!r}",
        )
        check(
            "the version publishes its own canonical public path",
            by_id.path == f"/v1/methodology/{published.version}",
            f"path is {by_id.path!r}",
        )

        # --- 8. an unpublished id is a clean miss, live -------------------
        already = client.get_row(TABLE_NAME, UNPUBLISHED_VERSION) is not None
        check(
            "the version id used for the negative case really is absent live",
            not already,
            f"{UNPUBLISHED_VERSION} unexpectedly exists; the next check would be vacuous",
        )
        try:
            service.version(UNPUBLISHED_VERSION)
        except ZeroDBNotFoundError:
            check("an unpublished version id is refused live as not-found", True)
        else:
            check(
                "an unpublished version id is refused live as not-found",
                False,
                "a document was returned for a version that was never published",
            )

        # --- 9. the changelog, over an unfiltered live read ---------------
        listed = service.versions()
        logger.info("        live changelog holds %d version(s)", len(listed))
        for entry in listed:
            logger.info(
                "          %s  published_at=%s  current=%s",
                entry.version,
                entry.published_at,
                entry.is_current,
            )
        dates = [entry.published_at for entry in listed]
        check(
            "the live changelog is ordered newest first",
            dates == sorted(dates, reverse=True),
            f"published_at order is {dates}",
        )
        current_rows = [entry for entry in listed if entry.is_current]
        check(
            "exactly one live row is marked current",
            len(current_rows) == 1,
            f"{len(current_rows)} row(s) marked current: "
            f"{[entry.version for entry in current_rows]}",
        )
        uncitable = [
            entry.version
            for entry in listed
            if not entry.changelog_url or re.match(VERSION_ID_PATTERN, entry.version) is None
        ]
        check(
            "every live version carries a changelog_url and a servable id",
            not uncitable,
            f"{len(uncitable)} uncitable version(s): {uncitable}",
        )

        # --- 10. the HTTP surface, over a live client, with no creds ------
        # The app with ONLY the ZeroDB dependency pointed at the live project,
        # so this is the real router, the real response model and the real
        # bytes a journalist receives.
        app = create_app()
        app.dependency_overrides[zerodb_client_for_methodology] = lambda: client
        with TestClient(app) as http:
            current_response = http.get("/v1/methodology")
            check(
                "GET /v1/methodology answers 200 over live ZeroDB with no credentials",
                current_response.status_code == 200,
                f"status {current_response.status_code}: {current_response.text[:200]}",
            )
            sent_auth = {key.lower() for key in current_response.request.headers}
            check(
                "that request really carried no Authorization header",
                "authorization" not in sent_auth,
                "the request was authenticated, so this proves nothing about a public read",
            )
            body = current_response.json() if current_response.status_code == 200 else {}
            check(
                "the HTTP document names the live version and its weights",
                body.get("version") == published.version
                and body.get("weights", {}).get("tiers") == published.weights.get("tiers"),
                f"body named {body.get('version')!r}",
            )
            logger.info(
                "        weights_not_yet_published=%s", body.get("weights_not_yet_published")
            )
            # Updated by story 6.1, which is the event this check's own failure
            # message anticipated ("if epic 6 has since published one of these,
            # this check is the thing to update"). 6.1 published `2026.2` with
            # all three documented groups, so the live page now correctly
            # reports NONE as unpublished.
            #
            # Re-pointed at the real invariant rather than deleted: what
            # matters to 12.2 is that the page never claims a weight group it
            # does not carry, and never silently omits one it does. That holds
            # whether a version carries one group or three, so this check now
            # asserts the relationship between the two fields instead of a
            # snapshot of which groups happened to exist in October 2026.
            reported_unpublished = set(body.get("weights_not_yet_published") or [])
            carried = {group for group, weights in (body.get("weights") or {}).items() if weights}
            check(
                "the HTTP document's weights_not_yet_published is exactly the documented "
                "groups the live version does not carry",
                reported_unpublished == set(DOCUMENTED_WEIGHT_GROUPS) - carried,
                f"reported {sorted(reported_unpublished)}, carries {sorted(carried)}",
            )

            versions_response = http.get("/v1/methodology/versions")
            http_listed = (
                versions_response.json().get("versions", [])
                if versions_response.status_code == 200
                else None
            )
            check(
                "GET /v1/methodology/versions answers 200 and lists the live versions",
                http_listed is not None and len(http_listed) == len(listed),
                f"status {versions_response.status_code}, "
                f"{len(http_listed) if http_listed is not None else '?'} listed, "
                f"expected {len(listed)}",
            )

            by_id_response = http.get(f"/v1/methodology/{published.version}")
            check(
                f"GET /v1/methodology/{published.version} answers 200 (the citation target)",
                by_id_response.status_code == 200,
                f"status {by_id_response.status_code}: {by_id_response.text[:200]}",
            )

            missing_response = http.get(f"/v1/methodology/{UNPUBLISHED_VERSION}")
            check(
                "an unpublished version answers 404 over HTTP, not 500",
                missing_response.status_code == 404,
                f"status {missing_response.status_code}: {missing_response.text[:200]}",
            )

            # The one piece of input hardening worth checking live rather than
            # only against the fake: this is the API's only endpoint taking a
            # path parameter from an unauthenticated caller.
            for junk in ("../../etc/passwd", "2026", "abcd.1", "2026.1.1"):
                junk_response = http.get(f"/v1/methodology/{junk}")
                check(
                    f"a malformed version id {junk!r} is refused at the edge",
                    junk_response.status_code in (404, 422),
                    f"status {junk_response.status_code}",
                )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        if seeded:
            logger.info(
                "LEFT IN PLACE (intentionally): the methodology_versions row story 4.2 "
                "seeds. Permanent published reference data — the profile editor reads "
                "its tier weights and this page publishes it; deleting it would make "
                "both silently wrong."
            )
        if backfilled:
            logger.info(
                "LEFT IN PLACE (intentionally): the changelog_url on live row %s, updated "
                "because --set-changelog-url was passed. A deliberate edit to permanent "
                "published reference data, not test debris.",
                backfilled,
            )
        if not seeded and not backfilled:
            logger.info("wrote nothing: every check above was a read of story 4.2's live row")
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
