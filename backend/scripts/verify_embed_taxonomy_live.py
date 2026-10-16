#!/usr/bin/env python3
"""Verify topic/stance embedding against the LIVE "informed" project (3.4).

Every story from 1.3 on has found at least one discrepancy between the fake
and the real API, so story 3.4's acceptance criteria ("both namespaces are
populated; embeddings are rebuilt when a version changes") are checked against
the real ZeroDB project, not only against the in-memory fake.

What it proves, in order:

  1. **Both namespaces end up fully populated.** After running the pipeline,
     `topics` holds one real embedding (metadata carrying `topic_id`) per live
     `issue_topics` row, and `stances` holds one per live `stances` row — on
     top of whichever bootstrap-marker vector story 1.3 already left there.
  2. **Running it a second time changes nothing.** The second run's report
     shows zero newly-written and zero rebuilt rows, and the live vector count
     in each namespace is unchanged — the idempotency half of the acceptance
     criterion, proven against live listing rather than assumed from the
     report alone.
  3. **A version bump rebuilds, live.** A small, clearly-marked subset of
     topics/stances has its version bumped in a scratch copy of the pipeline's
     bookkeeping — simulated against the real stored vectors' metadata rather
     than against the live `issue_topics`/`stances` rows (bumping the real
     taxonomy/wording version would be a panel action this script has no
     business taking) — by hand-deleting the live vector for one real topic
     and one real stance and re-embedding it at a synthetic higher version,
     then checking that a third pipeline run leaves exactly one current
     vector for that id, at the bumped version, rather than two.

!!! NO CLEANUP, DELIBERATELY !!!

Like `verify_stance_cards_live.py` and `verify_issue_taxonomy_live.py`, this
script does **not** delete the vectors it writes. They are permanent
production reference data: story 3.5's free-text interest matching and the
promise/vote classifier both depend on every topic and stance being embedded.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_embed_taxonomy_live.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.embed_taxonomy import (  # noqa: E402
    STANCES_NAMESPACE,
    TOPICS_NAMESPACE,
    TaxonomyEmbedder,
)
from app.seed_stances import stance_row_ids  # noqa: E402
from app.seed_taxonomy import taxonomy_row_ids  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("verify_embed_taxonomy_live")


def _real_vectors(client: ZeroDBClient, namespace: str, id_key: str) -> list[dict]:
    """Every vector in `namespace` that carries `id_key` in its metadata.

    Excludes story 1.3's bootstrap-marker vector, which carries neither
    `topic_id` nor `stance_id` — see `app.embed_taxonomy`'s module docstring.
    Pages through the whole namespace rather than trusting a single page,
    exactly like `TaxonomyEmbedder._list_all_vectors`.
    """
    records: list[dict] = []
    offset = 0
    while True:
        page = client.list_vectors(namespace=namespace, limit=200, offset=offset)
        records.extend(page.vectors)
        if not page.vectors or len(page.vectors) < 200:
            break
        offset += 200
    return [v.model_dump() for v in records if v.vector_metadata.get(id_key)]


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - a linear script of checks reads better as one
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

    try:
        expected_topic_ids = taxonomy_row_ids()
        expected_stance_ids = stance_row_ids()

        # --- 1. run once, confirm both namespaces are fully populated -----
        logger.info(
            "running the embedding pipeline (run 1 of 3) — paced, so this takes "
            "several minutes on a first run"
        )
        topics_report, stances_report = TaxonomyEmbedder(client).run()
        check(
            f"the topics report covers all {len(expected_topic_ids)} live topics",
            topics_report.row_count == len(expected_topic_ids),
            f"reported {topics_report.row_count}",
        )
        check(
            f"the stances report covers all {len(expected_stance_ids)} live stances",
            stances_report.row_count == len(expected_stance_ids),
            f"reported {stances_report.row_count}",
        )

        live_topic_vectors = _real_vectors(client, TOPICS_NAMESPACE, "topic_id")
        live_stance_vectors = _real_vectors(client, STANCES_NAMESPACE, "stance_id")
        topic_ids_embedded = {v["vector_metadata"]["topic_id"] for v in live_topic_vectors}
        stance_ids_embedded = {v["vector_metadata"]["stance_id"] for v in live_stance_vectors}

        check(
            f"the live {TOPICS_NAMESPACE} namespace has exactly one embedding "
            f"per topic ({len(expected_topic_ids)} total)",
            len(live_topic_vectors) == len(expected_topic_ids)
            and topic_ids_embedded == expected_topic_ids,
            f"found {len(live_topic_vectors)} vector(s) covering "
            f"{len(topic_ids_embedded)} distinct topic(s)",
        )
        check(
            f"the live {STANCES_NAMESPACE} namespace has exactly one embedding "
            f"per stance ({len(expected_stance_ids)} total)",
            len(live_stance_vectors) == len(expected_stance_ids)
            and stance_ids_embedded == expected_stance_ids,
            f"found {len(live_stance_vectors)} vector(s) covering "
            f"{len(stance_ids_embedded)} distinct stance(s)",
        )

        sample = live_topic_vectors[0] if live_topic_vectors else None
        check(
            "a sampled live topic vector's metadata has topic_id, locale, taxonomy_version",
            sample is not None
            and {"topic_id", "locale", "taxonomy_version"} <= set(sample["vector_metadata"]),
            f"sample metadata: {sample['vector_metadata'] if sample else None}",
        )
        stance_sample = live_stance_vectors[0] if live_stance_vectors else None
        check(
            "a sampled live stance vector's metadata has stance_id, topic_id, wording_version",
            stance_sample is not None
            and {"stance_id", "topic_id", "wording_version"}
            <= set(stance_sample["vector_metadata"]),
            f"sample metadata: {stance_sample['vector_metadata'] if stance_sample else None}",
        )

        # --- 2. run again: idempotency ------------------------------------
        logger.info("running the embedding pipeline again (run 2 of 3) — the idempotency check")
        topics_report_2, stances_report_2 = TaxonomyEmbedder(client).run()
        check(
            "the second run wrote no new topic embeddings",
            topics_report_2.written == [],
            f"written: {topics_report_2.written[:5]}",
        )
        check(
            "the second run wrote no new stance embeddings",
            stances_report_2.written == [],
            f"written: {stances_report_2.written[:5]}",
        )
        check(
            "the second run reports every topic as already current",
            len(topics_report_2.skipped) == len(expected_topic_ids),
            f"{len(topics_report_2.skipped)} of {len(expected_topic_ids)} skipped",
        )
        check(
            "the second run reports every stance as already current",
            len(stances_report_2.skipped) == len(expected_stance_ids),
            f"{len(stances_report_2.skipped)} of {len(expected_stance_ids)} skipped",
        )

        live_topic_vectors_after = _real_vectors(client, TOPICS_NAMESPACE, "topic_id")
        live_stance_vectors_after = _real_vectors(client, STANCES_NAMESPACE, "stance_id")
        check(
            "the live topic vector count is unchanged after re-running",
            len(live_topic_vectors_after) == len(live_topic_vectors),
            f"{len(live_topic_vectors_after)} now vs {len(live_topic_vectors)} before",
        )
        check(
            "the live stance vector count is unchanged after re-running",
            len(live_stance_vectors_after) == len(live_stance_vectors),
            f"{len(live_stance_vectors_after)} now vs {len(live_stance_vectors)} before",
        )

        # --- 3. simulate a version bump and confirm the rebuild -----------
        # Bumping the real `issue_topics`/`stances` row's version would be a
        # panel action this script has no business taking, so the bump is
        # simulated on the LIVE VECTOR directly: delete the one real topic's
        # and one real stance's current vector and replace it with a vector
        # carrying a synthetic higher version, then confirm the next pipeline
        # run treats that as stale and rebuilds it back to the real version —
        # i.e. the rebuild path really does delete-then-insert on a version
        # mismatch, proven in both directions (down-bump counts too).
        sample_topic_id = next(iter(sorted(expected_topic_ids)))
        sample_stance_id = next(iter(sorted(expected_stance_ids)))
        logger.info(
            "simulating a version change on %r and %r, then running a third time",
            sample_topic_id,
            sample_stance_id,
        )

        topic_vector = next(
            v
            for v in live_topic_vectors_after
            if v["vector_metadata"]["topic_id"] == sample_topic_id
        )
        stance_vector = next(
            v
            for v in live_stance_vectors_after
            if v["vector_metadata"]["stance_id"] == sample_stance_id
        )
        client.delete_vector(vector_id=topic_vector["vector_id"], namespace=TOPICS_NAMESPACE)
        client.embed_and_store(
            texts=[str(topic_vector["document"])],
            namespace=TOPICS_NAMESPACE,
            metadata_list=[{**topic_vector["vector_metadata"], "taxonomy_version": 999}],
        )
        client.delete_vector(vector_id=stance_vector["vector_id"], namespace=STANCES_NAMESPACE)
        client.embed_and_store(
            texts=[str(stance_vector["document"])],
            namespace=STANCES_NAMESPACE,
            metadata_list=[{**stance_vector["vector_metadata"], "wording_version": 999}],
        )

        topics_report_3, stances_report_3 = TaxonomyEmbedder(client).run()
        check(
            "the third run rebuilt exactly the simulated topic",
            topics_report_3.rebuilt == [sample_topic_id],
            f"rebuilt: {topics_report_3.rebuilt}",
        )
        check(
            "the third run rebuilt exactly the simulated stance",
            stances_report_3.rebuilt == [sample_stance_id],
            f"rebuilt: {stances_report_3.rebuilt}",
        )

        after_rebuild_topic_vectors = [
            v
            for v in _real_vectors(client, TOPICS_NAMESPACE, "topic_id")
            if v["vector_metadata"]["topic_id"] == sample_topic_id
        ]
        after_rebuild_stance_vectors = [
            v
            for v in _real_vectors(client, STANCES_NAMESPACE, "stance_id")
            if v["vector_metadata"]["stance_id"] == sample_stance_id
        ]
        topic_versions_found = [
            v["vector_metadata"].get("taxonomy_version") for v in after_rebuild_topic_vectors
        ]
        check(
            "exactly one live topic vector remains for the bumped id, at the real version",
            len(after_rebuild_topic_vectors) == 1
            and after_rebuild_topic_vectors[0]["vector_metadata"]["taxonomy_version"] == 1,
            f"found {len(after_rebuild_topic_vectors)}: {topic_versions_found}",
        )
        stance_versions_found = [
            v["vector_metadata"].get("wording_version") for v in after_rebuild_stance_vectors
        ]
        check(
            "exactly one live stance vector remains for the bumped id, at the real version",
            len(after_rebuild_stance_vectors) == 1
            and after_rebuild_stance_vectors[0]["vector_metadata"]["wording_version"] == 1,
            f"found {len(after_rebuild_stance_vectors)}: {stance_versions_found}",
        )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        logger.info(
            "LEFT IN PLACE (intentionally): %d topic embedding(s) in %r and %d stance "
            "embedding(s) in %r — permanent reference data, not test debris.",
            len(expected_topic_ids) if "expected_topic_ids" in locals() else 0,
            TOPICS_NAMESPACE,
            len(expected_stance_ids) if "expected_stance_ids" in locals() else 0,
            STANCES_NAMESPACE,
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
