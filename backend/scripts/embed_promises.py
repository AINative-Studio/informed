#!/usr/bin/env python3
"""CLI entrypoint for stories 9.4 and 9.5: embed promises, emit the event.

    python backend/scripts/embed_promises.py                   # every source
    python backend/scripts/embed_promises.py --source-id src:… # one source

For every `promises` row extracted from a `promise_sources` row, stores a
vector of the promise's own verbatim quote in the `promises` namespace,
writes that vector's id back onto the row (story 9.4), and emits
`promise.extracted` so story 10.1's mapping agent starts automatically
(story 9.5).

**Run `scripts/extract_promises.py --write` first.** This script only reads
`promises`; there is nothing to embed from an empty table. As of 2026-10-20
the live project holds **zero** `promises` rows — story 9.3 deliberately
wrote none, because the only extractor available is an explicitly-labeled
stand-in whose output is not usable as promise data, and writing it would
have put fabricated promises attributed to real public figures into the
system of record. Re-measured during this story against all six live sources:
264 candidate passages produced 8 "commitments", 0 testable, all 8 false
positives (donation-page boilerplate, a sentence criticising opponents read
as the speaker's own commitment, a biography sentence) — so story 9.3's
0-for-7 is now 0-for-8. See `app.ingest.promise_extraction`'s module
docstring and `scripts/verify_promise_embedding_live.py`'s.

So on the live project today this script correctly finds nothing to do, and
says so.

Idempotent: a second run with nothing changed embeds nothing and emits
nothing. A promise whose `quote_span` has changed since it was embedded is
rebuilt (stale vector deleted, fresh one written) rather than duplicated.

The vectors this writes are production data, not fixtures: story 10.1's bill
mapping is a semantic search of this namespace. Do not delete them.

Reads ZeroDB credentials from the environment via `ZeroDBClient.from_env()`,
like every other script here. Never pass a credential on the command line.

All the logic lives in `app.ingest.promise_embedding`, covered by
`tests/features/promise_embedding.feature` and
`tests/test_promise_embedding_unit.py` against the in-memory fake; this file
is intentionally thin and has no logic of its own to assert on.

Exit codes:
    0  every source this run touched was embedded, or had nothing to embed
    1  no ZeroDB credentials, or at least one promise could not be embedded
       (the report names each one; a re-run mends what is mendable)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.promise_embedding import (  # noqa: E402
    DEFAULT_MIN_INTERVAL_SECONDS,
    PROMISE_EXTRACTED_TOPIC,
    PROMISES_NAMESPACE,
    PromiseEmbedder,
    PromiseEmbeddingError,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import PromiseSource  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("embed_promises")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source-id",
        action="append",
        default=None,
        help="embed promises from this promise_sources id only (repeatable); default: all",
    )
    parser.add_argument("--limit", type=int, default=100, help="how many sources to read")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    try:
        client = ZeroDBClient.from_env()
    except Exception as exc:  # noqa: BLE001 - any credential problem is the same exit
        logger.error("no ZeroDB credentials: %s", exc)
        return 1

    try:
        return _run(client, source_ids=args.source_id, limit=args.limit)
    finally:
        client.close()


def _run(client: ZeroDBClient, *, source_ids: list[str] | None, limit: int) -> int:
    sources_repo: Repository[PromiseSource] = Repository(client=client, model=PromiseSource)
    if source_ids:
        sources = [row for row in (sources_repo.get(sid) for sid in source_ids) if row]
        missing = len(source_ids) - len(sources)
        if missing:
            logger.error("%d of the named promise_sources ids do not exist", missing)
            return 1
    else:
        sources = sources_repo.query(limit=limit)

    if not sources:
        logger.error("no promise_sources rows (story 9.1/9.2 write those)")
        return 1

    logger.info(
        "Embedding promises into %r and emitting %r for %d source(s) in project %r. "
        "Each promise costs an embed, a listing read, a row write and an event, "
        "paced at %.1fs, so expect this to be slow for a large source.",
        PROMISES_NAMESPACE,
        PROMISE_EXTRACTED_TOPIC,
        len(sources),
        client.project_id,
        DEFAULT_MIN_INTERVAL_SECONDS,
    )

    embedder = PromiseEmbedder(client, pace=lambda: time.sleep(DEFAULT_MIN_INTERVAL_SECONDS))
    totals = {"promises": 0, "embedded": 0, "rebuilt": 0, "skipped": 0, "events": 0}
    failures: list[str] = []

    for source in sources:
        assert source.id is not None  # noqa: S101 - a stored row always carries one
        logger.info("--- %s (%s)", source.id, source.url)
        try:
            report = embedder.embed_promises_for_source(source.id)
        except (PromiseEmbeddingError, ZeroDBError) as exc:
            logger.error("could not embed %s: %s", source.id, exc)
            failures.append(f"{source.id}: {exc}")
            continue

        if report.promise_count == 0 and not report.failures:
            logger.info("  no promises extracted from this source yet; nothing to embed")
            continue

        logger.info(
            "  %d promise(s): %d embedded (%d rebuilt), %d already current, %d %s event(s) emitted",
            report.promise_count,
            len(report.embedded),
            len(report.rebuilt),
            len(report.skipped),
            len(report.events_emitted),
            PROMISE_EXTRACTED_TOPIC,
        )
        totals["promises"] += report.promise_count
        totals["embedded"] += len(report.embedded)
        totals["rebuilt"] += len(report.rebuilt)
        totals["skipped"] += len(report.skipped)
        totals["events"] += len(report.events_emitted)
        for failure in report.failures:
            logger.error("  FAILED %s", failure)
        failures.extend(report.failures)

    logger.info(
        "%d promise(s) across %d source(s): %d embedded (%d rebuilt), %d already "
        "current, %d event(s) emitted, %d failure(s)",
        totals["promises"],
        len(sources),
        totals["embedded"],
        totals["rebuilt"],
        totals["skipped"],
        totals["events"],
        len(failures),
    )
    if totals["promises"] == 0 and not failures:
        logger.info(
            "Nothing to embed. The live project holds zero promises rows today "
            "because story 9.3 wrote none on purpose -- see this script's docstring. "
            "Run scripts/extract_promises.py --write once a real extractor exists."
        )
        return 0
    if failures:
        logger.error(
            "%d promise(s) could not be embedded. Each is named above; a re-run "
            "mends whatever is mendable, because every step here is idempotent.",
            len(failures),
        )
        return 1
    logger.info(
        "These vectors are production data, not fixtures: story 10.1's bill mapping "
        "is a semantic search of the %r namespace. Do not delete them.",
        PROMISES_NAMESPACE,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
