#!/usr/bin/env python3
"""Verify the stance cards against the LIVE "informed" project (story 3.2).

Every story from 1.3 on has found at least one discrepancy between what the
in-memory fake agrees to and what ZeroDB actually does, so story 3.2's
acceptance criteria ("each subtopic has at least 3 stances; display order is
randomized per session; no stance is preselected") are checked against the real
API as well as the fake.

What it proves, in order:

  1. **Every row exists with the right fields**, read back through the
     repository — so each stored row is also proven to still validate against
     `Stance` rather than merely being present.
  2. **Every one of story 3.1's 60 subtopics has at least 3 live stance rows**,
     counted over an unfiltered live read rather than over the ids the seed
     thinks it wrote, and each row's `topic_id` is resolved back to a stored
     `issue_topics` row.
  3. **Direction codes within each subtopic are not all identical** and span
     both signs with no repeat — the acceptance criterion's real content,
     computed from what is actually stored.
  4. **`wording_version` is 1 throughout**, over the same unfiltered read.
  5. **No stored row carries a default/preselected/order field.** Read from raw
     `row_data`, not through the model: through `Stance` the answer is trivially
     yes because `extra="forbid"` would have refused the write, and the question
     worth asking live is whether the bytes in storage carry one.
  6. **Seeding twice leaves exactly one row per id.** Counted from an
     unfiltered live read, not through `Repository.get` — `get` stops at the
     first match and so cannot tell one row from two, which is the whole
     question idempotency asks. This is the check that catches ZeroDB's
     always-appending `POST /rows` leaking through the upsert's find step.
  7. **The second run issued PUTs, not POSTs** — the mechanism rather than the
     symptom, recorded off the client's own transport.
  8. **No stored label or description names a political side**, checked against
     what is in the database rather than against the module constant, so a row
     written by some other path is caught too.
  9. **A live read through `StanceService` comes back in a different order each
     time**, and never with a selection flag — the backend's half of
     "randomized per session", proven against real rows.
 10. **A stance whose subtopic does not exist is refused live.** The reference
     check (datamodel.md rule 5) is what makes `scripts/seed_taxonomy.py` a
     real prerequisite, so it is proven rather than assumed.

!!! NO CLEANUP, DELIBERATELY !!!

Like `verify_seed_live.py` and `verify_issue_taxonomy_live.py`, and unlike
`verify_repository_live.py`, this script does **not** delete what it wrote. The
stance corpus is permanent production reference data: a voter's encrypted
profile stores `stance_id`s from it (datamodel.md's voter-profile document), as
do `candidate_positions` and every promise classified under a stance, so
deleting these rows would orphan all of it — and the next
`scripts/seed_stances.py` would simply write them back. An
`en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs` row in the live
project is this seed's output, not forgotten test debris.

The ONE row this script writes that is not permanent is the deliberately
orphaned stance of check 10 — and only if the reference check failed to refuse
it, in which case the row is real debris and the script reports a failure.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_stance_cards_live.py
"""

from __future__ import annotations

import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.repositories import MissingReferenceError, Repository  # noqa: E402
from app.repositories.models import IssueTopic, Stance  # noqa: E402
from app.seed_stances import (  # noqa: E402
    MINIMUM_STANCES_PER_SUBTOPIC,
    STANCES,
    WORDING_VERSION,
    StanceSeeder,
    balance_complaints,
    contains_partisan_identity_word,
    stance_row_ids,
    stance_rows,
    stances_by_topic,
)
from app.seed_taxonomy import (  # noqa: E402
    EXPECTED_SUBTOPIC_COUNT,
    TAXONOMY_LOCALE,
    TAXONOMY_VERSION,
    taxonomy_rows,
)
from app.stances.service import StanceService  # noqa: E402
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

    Check 7 needs the *mechanism* behind "exactly one row per id": the second
    seed run must resolve each natural-key id to ZeroDB's internal UUID and
    `PUT` it, never `POST` a second row (story 1.2: `POST /rows` always
    appends). The in-memory fake records its calls for exactly this reason; the
    live `HttpxZeroDBTransport` does not, and giving it a call log for one
    verification script's benefit would put test instrumentation into the
    production path.

    So this decorator lives here instead, copied from
    `verify_issue_taxonomy_live.py` rather than shared with it: a verification
    script is a record of one story's measurement, and a shared helper edited
    by a later story would silently change what an earlier story's recorded run
    proved. Only the method and path are kept — a stance label is public
    reference data, but logging request bodies by default is a habit worth not
    forming in a repo whose other tables hold political-opinion data.
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


#: The one row this script writes that is NOT permanent stance data: a card
#: pointing at a subtopic that does not exist, used to prove the reference
#: check refuses it. If the check works the row is never written at all; the
#: `zzz-live-check` marker is here so that a row left behind by a *failing*
#: check is obvious in a table listing.
ORPHAN_STANCE_ID = "en-US:zzz-live-check-3-2-nowhere:v1:orphan-stance"
MISSING_TOPIC_ID = "en-US:zzz-live-check-3-2-nowhere:v1"

#: Subtopics whose shuffled read is exercised in check 9. The most contested
#: ones in the corpus, because those are where a persistent first option would
#: do the most damage, plus prd.md's own anchor.
SHUFFLE_CHECKED_SUBTOPICS: tuple[str, ...] = (
    "en-US:prescription-drug-pricing:v1",
    "en-US:gestational-limits-and-exceptions:v1",
    "en-US:background-checks-and-purchase-rules:v1",
    "en-US:border-enforcement-and-asylum:v1",
)

#: Field names that would mean a stance is preselected or pre-ranked. Checked
#: on raw `row_data` in check 5; the model forbids all of them, so any one
#: appearing live means a row was written by something other than this seed.
FORBIDDEN_FIELDS = frozenset(
    {
        "default",
        "is_default",
        "preselected",
        "selected",
        "recommended",
        "default_choice",
        "suggested",
        "display_order",
        "rank",
        "sort_order",
        "position",
    }
)

#: How many rows one unfiltered live read asks for. The corpus is ~200 rows, so
#: this is generous headroom; a page that comes back full would mean the counts
#: below are reading a subset, which `read_all_rows` reports rather than
#: silently under-counting (story 1.4: live ZeroDB's server-side filter matches
#: nothing, so all filtering here is client-side over what the page returned).
PAGE_SIZE = 1000

#: How many service reads check 9 samples per subtopic. Enough that a shuffle
#: which is really a rotation, or one that pins the first element, shows up;
#: small enough that 4 subtopics cost 4 page reads rather than 4 x 40.
SHUFFLE_SAMPLES = 40

logger = logging.getLogger("verify_stance_cards_live")


def read_all_rows(client: ZeroDBClient) -> list[dict]:
    """Every `stances` row, read unfiltered and live.

    Deliberately not `Repository.query`, which validates each row into the
    model: this read is also the input to the duplicate count, and a *second*
    row carrying an id is exactly what must be counted rather than rejected.
    Validation is proven separately in check 1.
    """
    page = client.query_rows("stances", limit=PAGE_SIZE)
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

    stances: Repository[Stance] = Repository(client=client, model=Stance)
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
        expected_rows = stance_rows()
        expected_total = len(expected_rows)
        subtopic_ids = {str(row.id) for row in taxonomy_rows() if row.parent_id is not None}

        # --- 0. the prerequisite ------------------------------------------
        # Checked first and explicitly, because every other check below would
        # fail confusingly if the taxonomy were absent: 201 refused writes
        # rather than one legible "run the other seed first".
        missing_topics = sorted(
            topic_id for topic_id in sorted(subtopic_ids) if topics.get(topic_id) is None
        )
        check(
            f"all {EXPECTED_SUBTOPIC_COUNT} story-3.1 subtopics are stored live "
            "(the stance seed's prerequisite)",
            not missing_topics,
            f"{len(missing_topics)} missing, first: {missing_topics[:3]} — "
            "run scripts/seed_taxonomy.py first",
        )
        if missing_topics:
            logger.error("stopping: the taxonomy prerequisite is not met")
            return 1

        # --- 1. seed once, then read every row back -----------------------
        logger.info(
            "seeding %d stance card(s) at wording_version %d (run 1 of 2) — paced, "
            "so this takes several minutes",
            expected_total,
            WORDING_VERSION,
        )
        first = StanceSeeder(client).run()
        check(
            f"the seed reports {expected_total} stance(s) "
            f"across {EXPECTED_SUBTOPIC_COUNT} subtopics",
            first.row_count == expected_total and first.topics_covered == EXPECTED_SUBTOPIC_COUNT,
            f"reported {first.row_count} row(s) across {first.topics_covered} subtopic(s)",
        )

        mismatched = []
        for expected in expected_rows:
            stored = stances.get(str(expected.id))
            if stored != expected:
                mismatched.append((str(expected.id), stored))
        check(
            f"all {expected_total} rows exist live and still validate as Stance",
            not mismatched,
            f"{len(mismatched)} mismatch(es), first: {mismatched[0] if mismatched else None}",
        )

        # --- 2-5. one unfiltered read, four questions ---------------------
        live_rows = read_all_rows(client)
        corpus_live = [row for row in live_rows if str(row.get("id")) in stance_row_ids()]

        live_by_topic: dict[str, list[dict]] = {}
        for row in corpus_live:
            live_by_topic.setdefault(str(row.get("topic_id")), []).append(row)

        thin = {
            topic_id: len(live_by_topic.get(topic_id, []))
            for topic_id in sorted(subtopic_ids)
            if len(live_by_topic.get(topic_id, [])) < MINIMUM_STANCES_PER_SUBTOPIC
        }
        check(
            f"every one of the {EXPECTED_SUBTOPIC_COUNT} subtopics has at least "
            f"{MINIMUM_STANCES_PER_SUBTOPIC} live stance rows",
            not thin,
            f"{len(thin)} thin subtopic(s): {dict(list(thin.items())[:5])}",
        )

        dangling = sorted(
            {
                topic_id
                for topic_id in live_by_topic
                if topic_id not in subtopic_ids or topics.get(topic_id) is None
            }
        )
        check(
            "every live stance's topic_id resolves to a stored subtopic",
            not dangling,
            f"{len(dangling)} topic_id(s) that are not stored subtopics: {dangling[:5]}",
        )

        # The balance rule, computed over the LIVE codes by the same function
        # the import-time guard, the unit tests and the feature file use — so a
        # live corpus that drifted from the declaration is judged by exactly
        # the rule that shipped, not by a fourth hand-rolled copy of it.
        complaints = balance_complaints(
            {
                topic_id: [str(row.get("direction_code", "")) for row in rows]
                for topic_id, rows in live_by_topic.items()
            }
        )

        def _matching(fragment: str) -> dict[str, list[str]]:
            return {
                topic_id: reasons
                for topic_id, reasons in complaints.items()
                if any(fragment in reason for reason in reasons)
            }

        uniform = _matching("every stance is coded")
        one_sided = _matching("no option on the")
        repeated = _matching("repeated direction code")
        check(
            "no live subtopic's stances all share one direction code",
            not uniform,
            f"{len(uniform)} uniform subtopic(s): {dict(list(uniform.items())[:5])}",
        )
        check(
            "every live subtopic offers a negative and a positive direction code",
            not one_sided,
            f"{len(one_sided)} one-sided subtopic(s): {dict(list(one_sided.items())[:5])}",
        )
        check(
            "no live subtopic repeats a direction code",
            not repeated,
            f"{len(repeated)} subtopic(s) with a repeat: {dict(list(repeated.items())[:5])}",
        )

        wrong_version = [
            row.get("id")
            for row in corpus_live
            if int(row.get("wording_version", -1)) != WORDING_VERSION
        ]
        check(
            f"every live stance row carries wording_version {WORDING_VERSION}",
            not wrong_version,
            f"{len(wrong_version)} row(s) at another version: {wrong_version[:5]}",
        )

        flagged = [
            (row.get("id"), sorted(set(row) & FORBIDDEN_FIELDS))
            for row in corpus_live
            if set(row) & FORBIDDEN_FIELDS
        ]
        check(
            "no live stance row carries a default, preselected or order field",
            not flagged,
            f"{len(flagged)} flagged row(s): {flagged[:5]}",
        )

        # --- 8. wording neutrality, on what is actually stored ------------
        framed = [
            (row.get("id"), field, hit)
            for row in corpus_live
            for field in ("label", "description")
            if (hit := contains_partisan_identity_word(str(row.get(field, "")))) is not None
        ]
        check(
            "no live stance label or description names a political side",
            not framed,
            f"{len(framed)} framed field(s): {framed[:5]}",
        )

        # --- 9. the shuffled read, against real rows ----------------------
        # A seeded RNG so the result is a fact about the data and the service
        # rather than about today's entropy; the fairness bound is the same one
        # tests/test_stance_service_unit.py asserts against the fake.
        service = StanceService(client, rng=random.Random(20260316))
        for topic_id in SHUFFLE_CHECKED_SUBTOPICS:
            expected_set = {card.row_id for card in stances_by_topic().get(topic_id, [])}
            orders: set[tuple[str, ...]] = set()
            firsts: set[str] = set()
            complete = True
            for _ in range(SHUFFLE_SAMPLES):
                got = service.for_topic(topic_id)
                ids = tuple(str(row.id) for row in got)
                orders.add(ids)
                if ids:
                    firsts.add(ids[0])
                if set(ids) != expected_set:
                    complete = False
            check(
                f"{topic_id} reads back complete and in varying order",
                complete and len(orders) > 1 and len(firsts) == len(expected_set),
                f"complete={complete}, {len(orders)} distinct order(s), "
                f"{len(firsts)} of {len(expected_set)} card(s) appeared first",
            )
            logger.info("        %d card(s), %d distinct orders", len(expected_set), len(orders))

        # --- 6 and 7. seed a SECOND time ----------------------------------
        logger.info("seeding (run 2 of 2) — this is the idempotency check")
        calls_before = len(recording.calls)
        second = StanceSeeder(client).run()
        check(
            "the second run reports the same row count",
            second.row_count == expected_total,
            f"reported {second.row_count}, expected {expected_total}",
        )

        after_rows = read_all_rows(client)
        counts: dict[str, int] = {}
        for row in after_rows:
            row_id = str(row.get("id"))
            if row_id in stance_row_ids():
                counts[row_id] = counts.get(row_id, 0) + 1
        duplicated_ids = {row_id: n for row_id, n in counts.items() if n != 1}
        missing_ids = sorted(stance_row_ids() - set(counts))
        check(
            f"exactly one live row carries each of the {expected_total} stance ids after two runs",
            not duplicated_ids and not missing_ids,
            f"duplicated: {dict(list(duplicated_ids.items())[:5])}; missing: {missing_ids[:5]}",
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
        anchor_id = "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
        anchor = stances.get(anchor_id)
        declared = {str(row.id): row for row in expected_rows}[anchor_id]
        check(
            "the prd.md anchor stance still carries its seeded fields after the second run",
            anchor == declared,
            f"stored row is {anchor!r}",
        )

        # --- 10. a stance with a missing subtopic is refused --------------
        already_there = client.get_row("issue_topics", MISSING_TOPIC_ID) is not None
        check(
            "the subtopic used for the negative case really is absent live",
            not already_there,
            f"{MISSING_TOPIC_ID} unexpectedly exists; the next check would be vacuous",
        )

        try:
            stances.upsert(
                {
                    "id": ORPHAN_STANCE_ID,
                    "topic_id": MISSING_TOPIC_ID,
                    "label": "An orphan stance card (live check)",
                    "description": "Points at a subtopic that was never seeded, to prove the "
                    "repository refuses the write.",
                    "direction_code": "+1",
                    "wording_version": WORDING_VERSION,
                }
            )
        except MissingReferenceError as exc:
            check(
                "a stance referencing a missing subtopic is refused live",
                exc.referenced_table == "issue_topics" and exc.referenced_id == MISSING_TOPIC_ID,
                f"refused, but naming {exc.referenced_table}/{exc.referenced_id}",
            )
        else:
            orphan_written = True
            check(
                "a stance referencing a missing subtopic is refused live",
                False,
                "the write was ACCEPTED — the reference check does not work live",
            )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        # The stance rows are PERMANENT and are deliberately left in place —
        # see this module's docstring. The only thing cleaned up is the orphan
        # row, which exists only if check 10 failed.
        if orphan_written:
            logger.info("cleaning up the orphan row left by the failed reference check")
            try:
                client.delete_row("stances", ORPHAN_STANCE_ID)
            except ZeroDBNotFoundError:
                logger.info("  already gone: stances/%s", ORPHAN_STANCE_ID)
            except ZeroDBError as exc:
                failures.append(f"could not delete stances/{ORPHAN_STANCE_ID}: {exc}")
                logger.error("  FAILED to delete stances/%s: %s", ORPHAN_STANCE_ID, exc)

        logger.info(
            "LEFT IN PLACE (intentionally): %d stances rows (%s taxonomy v%d, "
            "wording_version %d) — permanent reference data, not test debris. A "
            "voter's saved profile, every candidate position and every classified "
            "promise cites these stance ids.",
            len(STANCES),
            TAXONOMY_LOCALE,
            TAXONOMY_VERSION,
            WORDING_VERSION,
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
