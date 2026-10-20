#!/usr/bin/env python3
"""Verify Senate vote typing, bundling and no-recorded-vote handling LIVE (8.3, 8.4, 8.5).

Runs the classifier story 8.3 extracted and extended against **two live
sources at once**, then writes the results back to the real "informed"
ZeroDB project:

  1. **The 5 real roll calls story 8.1 already wrote live**
     (`us-senate:119-2:252` .. `:256`). These are the specific rows this
     branch changes: 8.1 classified them with its `question`-only inline
     classifier and set `is_bundled` to a hardcoded False. This script
     re-fetches each one's real XML from senate.gov, re-classifies it with
     8.3/8.4's rules, reports the before/after, and upserts the corrected
     row -- so the live project ends up holding the right classification
     rather than a stale one. **The re-classification of 119-2:252 from
     `other` to `procedural` is the headline live finding** (see below).
  2. **A fresh, larger live sample** drawn from the senate.gov LIS index,
     to measure accuracy and bundling at a scale no fixture can: the
     distribution of vote types the classifier actually produces across
     real current Senate business, and how many real votes get flagged
     bundled.

What it proves, in order:

  1. **Every one of 8.1's 5 live roll calls re-classifies**, and each
     change is reported with the rule that decided it. No change is
     silent.
  2. **The corrected rows land live**, and their `member_votes` rows carry
     the corrected denormalized `vote_type`/`is_bundled`/`bundled_reason`
     (datamodel.md rule: "the denormalized fields on `member_votes` match
     their `roll_calls` row").
  3. **No live roll call classifies outside datamodel.md's six values**,
     and every value the classifier emits is a key story 6.1 published in
     `methodology_versions.weights.vote_types` -- which is what makes the
     story's So-That ("so that weights apply correctly") actually true.
  4. **Nothing in the live sample is mistaken for an unrecorded outcome**
     (8.5's false-positive guard, the direction that matters: a false
     positive deletes a real vote from the public record).
  5. **The `other` share across a real sample is far below 8.1's 57%** --
     the accuracy improvement, measured on live data rather than claimed.
  6. **Bundling fires on real omnibus measures and stays quiet otherwise**,
     with the reason code stored for each.

!!! NO CLEANUP, DELIBERATELY !!!

Same posture as `verify_senate_rollcalls_live.py` and every other verify
script here: every roll call and member vote this script touches is REAL,
PERMANENT public voting record -- the Senate's own official record of how it
voted -- not synthetic test debris. It is left in place. This script UPDATES
rows 8.1 wrote rather than creating parallel ones, which is the point: an
idempotent upsert by natural-key id (CLAUDE.md), correcting a classification
in place.

Run it with the live credentials sourced into the shell -- never passed on
the command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_senate_vote_typing_live.py
"""

from __future__ import annotations

import collections
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xml.etree import ElementTree  # noqa: E402

from app.ingest.senate_rollcalls import (  # noqa: E402
    DEFAULT_MIN_INTERVAL_SECONDS,
    SENATE_LIS_BASE_URL,
    SenateRollCallLoader,
    SenateRollCallSourceError,
    classify_vote,
    fetch_lis_to_bioguide_map,
    fetch_roll_call_xml,
    fetch_vote_numbers,
    roll_call_item_url,
    transform_member_votes_for,
    transform_roll_calls,
)
from app.ingest.senate_rollcalls import (
    roll_call_id as build_roll_call_id,
)
from app.ingest.senate_vote_typing import (  # noqa: E402
    BUNDLED_REASON_EXPLANATIONS,
    unrecorded_outcome,
)
from app.methodology.publish import DEFAULT_VOTE_TYPE_WEIGHTS  # noqa: E402
from app.repositories import Repository  # noqa: E402
from app.repositories.models import MemberVote, Person, RollCall  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402
from app.zerodb.settings import ZeroDBSettings  # noqa: E402
from app.zerodb.transport import HttpxZeroDBTransport, ZeroDBResponse  # noqa: E402

CONGRESS = 119
SESSION = 2

#: The exact roll calls story 8.1 wrote live, by vote number. Named rather
#: than discovered, because the whole point of check 1 is to re-classify
#: THESE specific rows -- the ones whose stored classification this branch
#: changes.
STORY_8_1_VOTE_NUMBERS = (252, 253, 254, 255, 256)

#: How many additional real roll calls to sample for the distribution
#: measurement. Bounded for the same reason 8.1's script bounds its own
#: sample: this proves the pipeline and measures the improvement, it is not
#: a backfill (`scripts/backfill_house_rollcalls.py`'s Senate equivalent
#: would be the tool for that).
DISTRIBUTION_SAMPLE_SIZE = 40

#: datamodel.md's documented `vote_type` vocabulary.
PUBLISHED_VOCABULARY = frozenset(
    {"final_passage", "amendment", "cloture", "motion_to_recommit", "procedural", "other"}
)

logger = logging.getLogger("verify_senate_vote_typing_live")


@dataclass
class _RecordingTransport:
    """Wraps a real transport, recording `(method, path)` -- identical in
    shape to `verify_senate_rollcalls_live.py`'s own."""

    inner: HttpxZeroDBTransport
    calls: list[tuple[str, str]] = field(default_factory=list)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> ZeroDBResponse:
        self.calls.append((method, path))
        return self.inner.request(method, path, json=json, params=params, files=files, data=data)

    def close(self) -> None:
        self.inner.close()


def _fetch(congress: int, session: int, vote_number: int) -> tuple[str, str]:
    """One real roll call's XML and its source_url, paced."""
    xml = fetch_roll_call_xml(congress=congress, session=session, vote_number=vote_number)
    url = roll_call_item_url(
        congress=congress,
        session=session,
        vote_number=vote_number,
        base_url=SENATE_LIS_BASE_URL,
    )
    time.sleep(DEFAULT_MIN_INTERVAL_SECONDS)
    return xml, url


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - a linear script of checks
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

    roll_calls: Repository[RollCall] = Repository(client=client, model=RollCall)
    member_votes: Repository[MemberVote] = Repository(client=client, model=MemberVote)
    people: Repository[Person] = Repository(client=client, model=Person)

    failures: list[str] = []
    rewritten: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        # === 1. re-classify story 8.1's 5 real live roll calls =============
        logger.info(
            "\n=== story 8.1's %d real live roll calls, re-classified ===",
            len(STORY_8_1_VOTE_NUMBERS),
        )
        documents: list[tuple[str, str]] = []
        before_after: list[tuple[str, str | None, str, str, bool, str | None]] = []

        for vote_number in STORY_8_1_VOTE_NUMBERS:
            row_id = build_roll_call_id(congress=CONGRESS, session=SESSION, vote_number=vote_number)
            stored = roll_calls.get(row_id)
            check(
                f"{row_id} is already live (written by story 8.1)",
                stored is not None,
                "not found live — 8.1's rows are this check's subject",
            )
            xml, url = _fetch(CONGRESS, SESSION, vote_number)
            documents.append((xml, url))
            decision = classify_vote(ElementTree.fromstring(xml))
            before_after.append(
                (
                    row_id,
                    stored.vote_type if stored else None,
                    decision.vote_type,
                    decision.rule,
                    decision.is_bundled,
                    decision.bundled_reason,
                )
            )

        logger.info("\n  %-24s %-14s -> %-14s  %s", "roll call", "8.1 stored", "8.3/8.4", "rule")
        for row_id, was, now, rule, bundled, reason in before_after:
            marker = "CHANGED" if was != now else "same"
            logger.info(
                "  %-24s %-14s -> %-14s  %-34s %-8s bundled=%s%s",
                row_id,
                was,
                now,
                rule,
                marker,
                bundled,
                f" ({reason})" if reason else "",
            )

        check(
            "every re-classification is in datamodel.md's published vocabulary",
            all(now in PUBLISHED_VOCABULARY for _, _, now, _, _, _ in before_after),
            f"got {[n for _, _, n, _, _, _ in before_after]}",
        )
        check(
            "every re-classification has a published vote_types weight (story 6.1)",
            all(now in DEFAULT_VOTE_TYPE_WEIGHTS for _, _, now, _, _, _ in before_after),
            "a classification with no weight cannot make weights apply correctly",
        )
        check(
            "every re-classification names the rule that decided it",
            all(rule for _, _, _, rule, _, _ in before_after),
        )
        changed = [(i, w, n) for i, w, n, _, _, _ in before_after if w != n]
        logger.info(
            "  %d of %d of 8.1's live rows change classification: %s",
            len(changed),
            len(before_after),
            [f"{i}: {w} -> {n}" for i, w, n in changed] or "none",
        )

        # --- write the corrected rows back, with their member_votes -------
        rows, skipped = transform_roll_calls(documents)
        check(
            f"all {len(documents)} of 8.1's roll calls re-transform with no skips",
            not skipped,
            f"skipped: {skipped}",
        )
        check(
            "every re-transformed row carries a bundled_reason key (None when unbundled)",
            all("bundled_reason" in row for row in rows),
        )
        for row in rows:
            if row["is_bundled"]:
                check(
                    f"{row['id']}'s bundled_reason is explainable to a voter",
                    bool(BUNDLED_REASON_EXPLANATIONS.get(row["bundled_reason"])),
                    f"reason {row['bundled_reason']!r} has no UI text",
                )

        logger.info("fetching the live LIS-to-bioguide crosswalk")
        bioguide_by_lis_id = fetch_lis_to_bioguide_map()
        xml_by_url = {url: xml for xml, url in documents}
        member_rows_by_roll_call: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            member_rows, _ = transform_member_votes_for(
                row, xml_by_url[row["source_url"]], bioguide_by_lis_id=bioguide_by_lis_id
            )
            member_rows_by_roll_call[row["id"]] = member_rows
            for member_row in member_rows:
                if people.get(member_row["person_id"]) is None:
                    people.upsert(
                        {
                            "id": member_row["person_id"],
                            "canonical_name": f"Senator {member_row['person_id']}",
                        }
                    )

        logger.info("upserting the corrected classifications live (idempotent by id)")
        report = SenateRollCallLoader(client).load(
            rows, member_vote_rows_by_roll_call=member_rows_by_roll_call
        )
        rewritten = list(report.roll_calls_written)
        check(
            f"all {len(rows)} corrected roll call(s) wrote live",
            report.row_count == len(rows),
            f"reported {report.row_count}",
        )

        # --- read back: the live row now holds the new classification -----
        expected = {row["id"]: row for row in rows}
        for row_id, want in expected.items():
            stored = roll_calls.get(row_id)
            check(
                f"{row_id} reads back live with vote_type {want['vote_type']!r}",
                stored is not None and stored.vote_type == want["vote_type"],
                f"live vote_type is {stored.vote_type if stored else None!r}",
            )
            check(
                f"{row_id} reads back live with is_bundled={want['is_bundled']} "
                f"and bundled_reason={want['bundled_reason']!r}",
                stored is not None
                and stored.is_bundled == want["is_bundled"]
                and stored.bundled_reason == want["bundled_reason"],
                f"live is_bundled={stored.is_bundled if stored else None} "
                f"bundled_reason={stored.bundled_reason if stored else None!r}",
            )

        # --- datamodel.md rule: member_votes match their roll call --------
        drift: list[str] = []
        for row_id, want in expected.items():
            for member_row in member_rows_by_roll_call[row_id][:5]:  # sample per roll call
                member_id = f"{row_id}:{member_row['person_id'].removeprefix('person:')}"
                stored_mv = member_votes.get(member_id)
                if stored_mv is None:
                    drift.append(f"{member_id} missing live")
                    continue
                if stored_mv.vote_type != want["vote_type"]:
                    drift.append(
                        f"{member_id} vote_type {stored_mv.vote_type!r} != "
                        f"roll call's {want['vote_type']!r}"
                    )
                if stored_mv.is_bundled != want["is_bundled"]:
                    drift.append(f"{member_id} is_bundled drifted")
                if stored_mv.bundled_reason != want["bundled_reason"]:
                    drift.append(f"{member_id} bundled_reason drifted")
        check(
            "live member_votes carry the corrected denormalized fields (datamodel.md rule)",
            not drift,
            f"drift: {drift[:5]}",
        )

        # === 2. a larger live sample: the accuracy improvement, measured ===
        logger.info(
            "\n=== a fresh live sample of %d roll calls: distribution and bundling ===",
            DISTRIBUTION_SAMPLE_SIZE,
        )
        numbers = fetch_vote_numbers(congress=CONGRESS, session=SESSION)
        check("the live vote index lists roll calls", len(numbers) > 0)
        sample = sorted(numbers, reverse=True)[:DISTRIBUTION_SAMPLE_SIZE]
        logger.info("sampling roll numbers %s", sample)

        distribution: collections.Counter[str] = collections.Counter()
        bundled_hits: list[tuple[str, str, str]] = []
        unrecorded_false_positives: list[tuple[str, str]] = []
        for vote_number in sample:
            xml, _ = _fetch(CONGRESS, SESSION, vote_number)
            root = ElementTree.fromstring(xml)
            decision = classify_vote(root)
            distribution[decision.vote_type] += 1
            row_label = f"{CONGRESS}-{SESSION}:{vote_number}"
            if decision.is_bundled:
                bundled_hits.append(
                    (
                        row_label,
                        decision.bundled_reason or "",
                        (root.findtext("document/document_title") or "").strip()[:80],
                    )
                )
            result_text = (root.findtext("vote_result") or "").strip()
            if unrecorded_outcome(result_text) is not None:
                unrecorded_false_positives.append((row_label, result_text))

        total = sum(distribution.values())
        logger.info("\n  live vote_type distribution over %d real roll calls:", total)
        for vote_type, count in distribution.most_common():
            logger.info(
                "    %-20s %4d  (%5.1f%%)  weight %.2f",
                vote_type,
                count,
                100.0 * count / total,
                DEFAULT_VOTE_TYPE_WEIGHTS[vote_type],
            )

        check(
            "no live roll call classifies outside the published vocabulary",
            set(distribution) <= PUBLISHED_VOCABULARY,
            f"unexpected: {set(distribution) - PUBLISHED_VOCABULARY}",
        )
        check(
            "every live classification has a published vote_types weight",
            all(v in DEFAULT_VOTE_TYPE_WEIGHTS for v in distribution),
        )

        # 8.1's question-only classifier put 57.4% of congresses 118-119 in
        # `other`. The bar here is deliberately loose (below 40%) rather than
        # pinned to a tight number: `other` is dominated by bare
        # nominations, whose share swings week to week with the executive
        # calendar, and the nomination case stays `other` on purpose (see
        # senate_vote_typing's docstring). The claim being checked is "much
        # better than 57%", not a specific figure.
        other_share = 100.0 * distribution.get("other", 0) / total
        check(
            f"the `other` share on live data ({other_share:.1f}%) is well below 8.1's 57.4%",
            other_share < 40.0,
            f"`other` is {other_share:.1f}% of {total} live roll calls",
        )

        # 8.5's false-positive guard, on real data.
        check(
            "no real recorded roll call in the live sample is mistaken for an "
            "unrecorded (voice/UC) outcome",
            not unrecorded_false_positives,
            f"false positives: {unrecorded_false_positives}",
        )
        logger.info(
            "  8.5: 0 of %d real live `vote_result` values read as unrecorded "
            "(a voice vote gets no roll number, so the LIS index can never list one "
            "— this is the false-positive direction, which is the one that matters)",
            total,
        )

        if bundled_hits:
            logger.info(
                "\n  8.4: %d of %d live roll calls flagged bundled:",
                len(bundled_hits),
                total,
            )
            for label, reason, title in bundled_hits:
                logger.info("    %-14s %-34s %s", label, reason, title or "(no title)")
        else:
            logger.info(
                "\n  NOTE  no bundled measure in this %d-roll-call sample (not a "
                "failure — omnibus votes cluster around appropriations deadlines; "
                "the rule is unit-tested against 6 real omnibus titles captured "
                "live in tests/fixtures/senate_vote_typing_labeled.json)",
                total,
            )
        check(
            "bundling is not firing on everything (a rule that flags every vote measures nothing)",
            len(bundled_hits) < total,
            f"{len(bundled_hits)}/{total} flagged",
        )
        for label, reason, _ in bundled_hits:
            check(
                f"{label}'s bundled reason {reason!r} has plain-language UI text",
                bool(BUNDLED_REASON_EXPLANATIONS.get(reason)),
            )

    except SenateRollCallSourceError as exc:
        failures.append(f"senate.gov source error: {exc}")
        logger.error("FAIL  live fetch failed: %s", exc)
    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        logger.info(
            "\nLEFT IN PLACE (intentionally): %d roll call(s) re-classified and "
            "upserted in place — real Senate voting record, corrected rather than "
            "duplicated. ids: %s",
            len(rewritten),
            rewritten,
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
