#!/usr/bin/env python3
"""Verify the issue taxonomy against the LIVE "informed" project (story 3.1).

Every story from 1.3 on has found at least one discrepancy between what the
in-memory fake agrees to and what ZeroDB actually does, so story 3.1's
acceptance criteria ("about 20 topics and 60 subtopics in en-US; parent and
child tree; `taxonomy_version` = 1") are checked against the real API as well
as the fake.

What it proves, in order:

  1. **Every row exists with the right fields**, read back through the
     repository — so each stored row is also proven to still validate against
     `IssueTopic` rather than merely being present.
  2. **The parent/child tree resolves live.** For each of several subtopics,
     the stored `parent_id` is read back and the parent row is fetched: a
     dangling parent is the failure mode the two-request seed could produce and
     the fake could not.
  3. **Every row is en-US at `taxonomy_version` 1**, counted over an unfiltered
     live read rather than over the ids the seed thinks it wrote.
  4. **Seeding twice leaves exactly one row per id.** Counted from an
     unfiltered live read, not through `Repository.get` — `get` stops at the
     first match and so cannot tell one row from two, which is the whole
     question idempotency asks. This is the check that catches ZeroDB's
     always-appending `POST /rows` leaking through the upsert's find step.
  5. **The second run issued PUTs, not POSTs** — the mechanism rather than the
     symptom, recorded off the client's own transport.
  6. **No stored label presupposes a stance**, checked against what is actually
     in the database rather than against the module constant, so a row written
     by some other path is caught too.
  7. **A subtopic whose parent does not exist is refused live.** The reference
     check (datamodel.md rule 5) is what makes the seed's parents-first
     ordering load-bearing, so it is proven live rather than assumed.

!!! NO CLEANUP, DELIBERATELY !!!

Like `verify_seed_live.py` and unlike `verify_repository_live.py`, this script
does **not** delete what it wrote. The taxonomy is permanent production
reference data: every voter interest (prd.md Feature 1), stance card (story
3.2), topic embedding (story 3.4) and alignment score cites a topic id, so
deleting these rows would orphan all of it — and the next
`scripts/seed_taxonomy.py` would simply write them back. An
`en-US:healthcare:v1` row in the live project is this seed's output, not
forgotten test debris.

The ONE row this script writes that is not permanent is the deliberately
orphaned subtopic of check 7 — and only if the reference check failed to refuse
it, in which case the row is real debris and the script reports a failure.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_issue_taxonomy_live.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.repositories import MissingReferenceError, Repository  # noqa: E402
from app.repositories.models import IssueTopic  # noqa: E402
from app.seed_taxonomy import (  # noqa: E402
    EXPECTED_SUBTOPIC_COUNT,
    EXPECTED_TOPIC_COUNT,
    TAXONOMY_LOCALE,
    TAXONOMY_VERSION,
    TaxonomySeeder,
    contains_stance_presupposing_word,
    taxonomy_row_ids,
    taxonomy_rows,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError  # noqa: E402
from app.zerodb.settings import ZeroDBSettings  # noqa: E402
from app.zerodb.transport import (  # noqa: E402
    HttpxZeroDBTransport,
    ZeroDBResponse,
    ZeroDBTransport,
)


class RecordingTransport:
    """A real transport that also keeps a log of `(method, path)`.

    Check 5 needs the *mechanism* behind "exactly one row per id": the second
    seed run must resolve each natural-key id to ZeroDB's internal UUID and
    `PUT` it, never `POST` a second row (story 1.2: `POST /rows` always
    appends). The in-memory fake records its calls for exactly this reason; the
    live `HttpxZeroDBTransport` does not, and giving it a call log for one
    verification script's benefit would put test instrumentation into the
    production path.

    So this decorator lives here instead, in the script that needs it. It
    satisfies the `ZeroDBTransport` protocol by delegating, which is the whole
    point of that seam existing — see `app/zerodb/transport.py`. Only the
    method and path are kept: the payloads contain nothing sensitive here (a
    taxonomy label is public reference data), but logging request bodies by
    default is a habit worth not forming in a repo whose other tables hold
    political-opinion data.
    """

    def __init__(self, inner: ZeroDBTransport) -> None:
        self._inner = inner
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> ZeroDBResponse:
        self.calls.append((method, path))
        return self._inner.request(method, path, json=json, params=params)

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()


def build_recording_client() -> tuple[ZeroDBClient, RecordingTransport]:
    """A live client whose calls are logged, built from the same env vars.

    Mirrors `ZeroDBClient.from_env` rather than calling it, because `from_env`
    builds its own transport and the client exposes no seam to wrap it
    afterwards. Credentials come from the environment exactly as everywhere
    else and are never printed.
    """
    settings = ZeroDBSettings()
    recording = RecordingTransport(
        HttpxZeroDBTransport(
            base_url=settings.base_url,
            api_key=settings.api_key.get_secret_value(),
            timeout_seconds=settings.timeout_seconds,
        )
    )
    client = ZeroDBClient(
        transport=recording,
        project_id=settings.project_id,
        max_attempts=settings.max_attempts,
    )
    return client, recording


#: The one row this script writes that is NOT permanent taxonomy data: a
#: subtopic pointing at a parent that does not exist, used to prove the
#: reference check refuses it. If the check works the row is never written at
#: all; the `zzz-live-check` marker is here so that a row left behind by a
#: *failing* check is obvious in a table listing.
ORPHAN_TOPIC_ID = "en-US:zzz-live-check-3-1-orphan:v1"
MISSING_PARENT_ID = "en-US:zzz-live-check-3-1-nowhere:v1"

#: Subtopics whose parent link is resolved individually in check 2. One per
#: structural case worth proving rather than all 60: the prd.md anchor
#: ("Healthcare -> Prescription drug pricing"), the first and last topics in
#: the tree's alphabetical order (so an off-by-one in the write loop shows),
#: and one whose parent name contains an allowlisted phrase.
SPOT_CHECKED_SUBTOPICS: tuple[str, ...] = (
    "en-US:prescription-drug-pricing:v1",
    "en-US:gestational-limits-and-exceptions:v1",
    "en-US:transition-to-civilian-employment:v1",
    "en-US:social-security-solvency:v1",
    "en-US:broadband-access:v1",
)

#: How many rows one unfiltered live read asks for. The taxonomy is ~80 rows,
#: so this is generous headroom; a page that comes back full would mean the
#: counts below are reading a subset, which `read_all_rows` reports rather than
#: silently under-counting (story 1.4: live ZeroDB's server-side filter matches
#: nothing, so all filtering here is client-side over what the page returned).
PAGE_SIZE = 500

logger = logging.getLogger("verify_issue_taxonomy_live")


def read_all_rows(client: ZeroDBClient) -> list[dict]:
    """Every `issue_topics` row, read unfiltered and live.

    Deliberately not `Repository.query`, which validates each row into the
    model: this read is also the input to the duplicate count, and a *second*
    row carrying an id is exactly what must be counted rather than rejected.
    Validation is proven separately in check 1.
    """
    page = client.query_rows("issue_topics", limit=PAGE_SIZE)
    if page.has_more or len(page.rows) >= PAGE_SIZE:
        logger.warning(
            "the unfiltered read returned a full page of %d rows; the counts below "
            "may be reading a subset of the table",
            len(page.rows),
        )
    return [row.row_data for row in page.rows]


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client, recording = build_recording_client()
    logger.info("project: %s", client.project_id)

    topics: Repository[IssueTopic] = Repository(client=client, model=IssueTopic)

    failures: list[str] = []
    orphan_written = False

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        expected_rows = taxonomy_rows()
        expected_total = EXPECTED_TOPIC_COUNT + EXPECTED_SUBTOPIC_COUNT

        # --- 1. seed once, then read every row back -----------------------
        logger.info(
            "seeding the %s taxonomy v%d (run 1 of 2) — %d rows, paced; this takes a few minutes",
            TAXONOMY_LOCALE,
            TAXONOMY_VERSION,
            expected_total,
        )
        first = TaxonomySeeder(client).run()
        check(
            f"the seed reports {EXPECTED_TOPIC_COUNT} topics and "
            f"{EXPECTED_SUBTOPIC_COUNT} subtopics written",
            len(first.topics_written) == EXPECTED_TOPIC_COUNT
            and len(first.subtopics_written) == EXPECTED_SUBTOPIC_COUNT,
            f"reported {len(first.topics_written)} topics "
            f"and {len(first.subtopics_written)} subtopics",
        )

        mismatched = []
        for expected in expected_rows:
            stored = topics.get(str(expected.id))
            if stored != expected:
                mismatched.append((str(expected.id), stored))
        check(
            f"all {expected_total} rows exist live and still validate as IssueTopic",
            not mismatched,
            f"{len(mismatched)} mismatch(es), first: {mismatched[0] if mismatched else None}",
        )

        # --- 2. the parent/child tree resolves live -----------------------
        for child_id in SPOT_CHECKED_SUBTOPICS:
            child = topics.get(child_id)
            if child is None:
                check(f"{child_id} resolves to a stored parent", False, "the subtopic is missing")
                continue
            if child.parent_id is None:
                check(
                    f"{child_id} resolves to a stored parent",
                    False,
                    "the stored row has no parent_id at all",
                )
                continue
            parent = topics.get(child.parent_id)
            check(
                f"{child_id} resolves to a stored parent",
                parent is not None and parent.parent_id is None,
                f"parent_id {child.parent_id!r} "
                + (
                    "is not stored"
                    if parent is None
                    else f"is itself a child of {parent.parent_id!r}"
                ),
            )
            if parent is not None:
                logger.info("        %s -> %s", child.name, parent.name)

        # --- 3. locale and taxonomy_version over an unfiltered read -------
        live_rows = read_all_rows(client)
        taxonomy_live = [row for row in live_rows if str(row.get("id")) in taxonomy_row_ids()]
        wrong_locale = [
            row.get("id") for row in taxonomy_live if row.get("locale") != TAXONOMY_LOCALE
        ]
        check(
            f"every live taxonomy row carries locale {TAXONOMY_LOCALE!r}",
            not wrong_locale,
            f"{len(wrong_locale)} row(s) with another locale: {wrong_locale[:5]}",
        )
        wrong_version = [
            row.get("id")
            for row in taxonomy_live
            if int(row.get("taxonomy_version", -1)) != TAXONOMY_VERSION
        ]
        check(
            f"every live taxonomy row carries taxonomy_version {TAXONOMY_VERSION}",
            not wrong_version,
            f"{len(wrong_version)} row(s) at another version: {wrong_version[:5]}",
        )

        live_parents = [row for row in taxonomy_live if row.get("parent_id") in (None, "")]
        live_children = [row for row in taxonomy_live if row.get("parent_id") not in (None, "")]
        check(
            f"the live tree has {EXPECTED_TOPIC_COUNT} top-level topics "
            f"and {EXPECTED_SUBTOPIC_COUNT} subtopics",
            len(live_parents) == EXPECTED_TOPIC_COUNT
            and len(live_children) == EXPECTED_SUBTOPIC_COUNT,
            f"live counts are {len(live_parents)} parents and {len(live_children)} children",
        )

        live_parent_ids = {str(row.get("id")) for row in live_parents}
        dangling = [
            (row.get("id"), row.get("parent_id"))
            for row in live_children
            if str(row.get("parent_id")) not in live_parent_ids
        ]
        check(
            "every live subtopic's parent_id names a stored top-level topic",
            not dangling,
            f"{len(dangling)} dangling: {dangling[:5]}",
        )

        # --- 6. neutrality, checked on what is actually stored ------------
        # Run here, off the same unfiltered read, rather than after the second
        # seed: the rows are identical either way and this saves a page read.
        framed = [
            (row.get("id"), row.get("name"), hit)
            for row in taxonomy_live
            if (hit := contains_stance_presupposing_word(str(row.get("name", "")))) is not None
        ]
        check(
            "no live topic name presupposes a stance",
            not framed,
            f"{len(framed)} framed label(s): {framed[:5]}",
        )

        # --- 4 and 5. seed a SECOND time ----------------------------------
        logger.info("seeding (run 2 of 2) — this is the idempotency check")
        calls_before = len(recording.calls)
        second = TaxonomySeeder(client).run()
        check(
            "the second run reports the same row count",
            second.row_count == expected_total,
            f"reported {second.row_count}, expected {expected_total}",
        )

        after_rows = read_all_rows(client)
        counts: dict[str, int] = {}
        for row in after_rows:
            row_id = str(row.get("id"))
            if row_id in taxonomy_row_ids():
                counts[row_id] = counts.get(row_id, 0) + 1
        duplicated = {row_id: n for row_id, n in counts.items() if n != 1}
        missing = sorted(taxonomy_row_ids() - set(counts))
        check(
            f"exactly one live row carries each of the {expected_total} topic ids after two runs",
            not duplicated and not missing,
            f"duplicated: {dict(list(duplicated.items())[:5])}; missing: {missing[:5]}",
        )

        second_run_calls = recording.calls[calls_before:]
        inserts = [
            (method, path)
            for method, path in second_run_calls
            if method == "POST" and path.endswith("/rows")
        ]
        updates = [
            (method, path)
            for method, path in second_run_calls
            if method == "PUT" and "/rows/" in path
        ]
        check(
            "the second run issued PUTs and no POSTs (the idempotency mechanism)",
            not inserts and len(updates) == expected_total,
            f"{len(inserts)} insert(s) and {len(updates)} update(s), "
            f"expected 0 and {expected_total}",
        )

        # The fields must also survive the second write, not just the count: an
        # upsert that found the row and replaced it with something else would
        # pass the count check and still be wrong.
        anchor = topics.get("en-US:prescription-drug-pricing:v1")
        check(
            "the prd.md anchor subtopic still carries its seeded fields after the second run",
            anchor is not None
            and anchor.name == "Prescription drug pricing"
            and anchor.parent_id == "en-US:healthcare:v1"
            and anchor.taxonomy_version == TAXONOMY_VERSION,
            f"stored row is {anchor!r}",
        )

        # --- 7. a subtopic with a missing parent is refused ---------------
        already_there = client.get_row("issue_topics", MISSING_PARENT_ID) is not None
        check(
            "the parent used for the negative case really is absent live",
            not already_there,
            f"{MISSING_PARENT_ID} unexpectedly exists; the next check would be vacuous",
        )

        try:
            topics.upsert(
                {
                    "id": ORPHAN_TOPIC_ID,
                    "parent_id": MISSING_PARENT_ID,
                    "name": "Orphan subtopic (live check)",
                    "locale": TAXONOMY_LOCALE,
                    "taxonomy_version": TAXONOMY_VERSION,
                }
            )
        except MissingReferenceError as exc:
            check(
                "a subtopic referencing a missing parent is refused live",
                exc.referenced_table == "issue_topics" and exc.referenced_id == MISSING_PARENT_ID,
                f"refused, but naming {exc.referenced_table}/{exc.referenced_id}",
            )
        else:
            orphan_written = True
            check(
                "a subtopic referencing a missing parent is refused live",
                False,
                "the write was ACCEPTED — the self-reference check does not work live",
            )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        # The taxonomy rows are PERMANENT and are deliberately left in place —
        # see this module's docstring. The only thing cleaned up is the orphan
        # row, which exists only if check 7 failed.
        if orphan_written:
            logger.info("cleaning up the orphan row left by the failed reference check")
            try:
                client.delete_row("issue_topics", ORPHAN_TOPIC_ID)
            except ZeroDBNotFoundError:
                logger.info("  already gone: issue_topics/%s", ORPHAN_TOPIC_ID)
            except ZeroDBError as exc:
                failures.append(f"could not delete issue_topics/{ORPHAN_TOPIC_ID}: {exc}")
                logger.error("  FAILED to delete issue_topics/%s: %s", ORPHAN_TOPIC_ID, exc)

        logger.info(
            "LEFT IN PLACE (intentionally): %d issue_topics rows (%s taxonomy v%d) — "
            "permanent reference data, not test debris. Every voter interest, stance "
            "card and alignment score cites these ids.",
            EXPECTED_TOPIC_COUNT + EXPECTED_SUBTOPIC_COUNT,
            TAXONOMY_LOCALE,
            TAXONOMY_VERSION,
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
