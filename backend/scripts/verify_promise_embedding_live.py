#!/usr/bin/env python3
"""Verify promise embedding and promise.extracted against LIVE ZeroDB (9.4, 9.5).

!!! THIS SCRIPT WRITES SYNTHETIC TEST DATA AND DELETES IT AGAIN !!!

===============================================================================
WHY THE SUBJECT IS SYNTHETIC, AND WHAT WAS TRIED FIRST
===============================================================================

**The live `promises` table is empty, and this script did not get to choose a
real promise to embed.** Measured at the start of this story (2026-10-20):
`promises` total = 0, against 6 real `promise_sources` rows (all
`campaign_website`, for Ossoff, Pelosi and Collins — three story 9.1 captures
plus three re-captures). Story 9.2's debate and questionnaire sources are not
in the live project either: its verification script creates its captures and
removes them again with `--cleanup`, which is the correct posture for a
verifier and means its 4 source types leave no rows behind.

**The real extraction pipeline was then run for real against those live
sources, not skipped.** `scripts/extract_promises.py --dry-run`, 2026-10-20,
real HTTP fetches of all six rows' real recorded urls:

    6 sources, 264 candidate passages -> 8 "commitments", 0 testable

and **all 8 are visibly false positives**, which is worse than story 9.3's
original 0-for-7:

  * two are ActBlue donation-page legal boilerplate — "Americans living
    abroad may only contribute to political entities if they have a verified
    U.S. passport…" — because electjon.com now 302-redirects to
    `secure.actblue.com/donate/ossoff-tv-hit`, so the Ossoff capture no
    longer reaches a campaign page at all;
  * two are "That's why your support is", a fragment off the same donation
    page;
  * two are a Pelosi sentence CRITICISING opponents ("…after passing a sinful
    budget that rips away healthcare…"), read as a commitment by *Congress*;
  * two are a Collins biography sentence about "backing President Trump by
    passing America First".

Writing those would place eight fabricated promises attributed to three real,
named public figures — two of them sitting members of Congress — into the
system of record of an app whose entire claim is cited evidence. Story 9.3
refused to do that and the reasoning has not changed, so this script does not
do it either, and does not quietly pick the least-bad one to make the live run
look fuller.

**So the subject here is explicitly synthetic**, in the same shape story 6.2's
`verify_alignment_scoring_live.py` uses for the same reason: a
`person:TEST-EMBED-9-4` whose name says SYNTHETIC TEST CANDIDATE in the row
itself, a promise whose `quote_span` is a made-up sentence no real person
said, and every row deleted at the end with a live re-read confirming it is
gone.

**What that does and does not prove.** It proves the live API behaviours this
story's code is built on — which is what a live verifier is for, and all of
them were found by measurement rather than assumed. It does NOT prove anything
about extraction quality, and the live `promises` table is still empty after
this run, exactly as it was before. The one thing it cannot prove is that real
promise text embeds well, because no real promise text exists to embed; that
is blocked on the same missing LLM credential story 9.3 documented, and
becomes real with no change to `app.ingest.promise_embedding`.

===============================================================================
WHAT THIS PROVES AGAINST THE REAL API
===============================================================================

  1. **`embed_and_store` returns NO vector id**, so story 9.4's acceptance
     criterion cannot be satisfied from the write response. Measured, with
     the full live response body printed. This is the fact the whole module
     is shaped around.
  2. **`bge-m3` at 1024 dimensions**, read off the live response rather than
     assumed (datamodel.md was corrected from 768 earlier; this re-confirms
     the correction).
  3. **The vector id IS recoverable** by listing the namespace and matching
     the `promise_id` this module wrote into the metadata — the two-call
     workaround, verified end to end.
  4. **The five-field metadata round-trips verbatim**, `topic_id: None`
     included. The null-but-present key is the one story 10.1 depends on and
     the one a store could silently drop.
  5. **The row carries the `vector_id`** after the run — story 9.4's
     criterion, re-read from the live table rather than from the report.
  6. **`promise.extracted` really publishes**, with exactly datamodel.md's
     `promise_id` + `person_id` payload, and is readable back from the live
     event log.
  7. **A second run embeds nothing and emits nothing.** Idempotency against
     the real store, which is the check the fake cannot make meaningfully.
  8. **A changed `quote_span` rebuilds** — stale vector deleted, fresh one
     stored, still exactly one vector for that promise.
  9. **Cleanup removes every synthetic row AND its vector**, confirmed by a
     live re-read of each id.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_promise_embedding_live.py
    python backend/scripts/verify_promise_embedding_live.py --keep   # debug only

Exit codes:
    0  every check passed and cleanup confirmed
    1  a check failed (cleanup still ran unless --keep)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.promise_embedding import (  # noqa: E402
    PROMISE_EXTRACTED_TOPIC,
    PROMISES_NAMESPACE,
    PromiseEmbedder,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import (  # noqa: E402
    Candidacy,
    District,
    Election,
    Person,
    Promise,
    PromiseSource,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("verify_promise_embedding_live")

# --- the synthetic subject ----------------------------------------------------
#
# Every id says TEST-EMBED-9-4 and every human-readable field says SYNTHETIC
# TEST, so a row that somehow survives cleanup is unmistakable in the live
# table and cannot be read as a claim about anybody real.

PERSON_ID = "person:TEST-EMBED-9-4"
ELECTION_ID = "us-2026-11-03-test-embed-9-4"
DISTRICT_ID = "ocd-division/country:us/state:zz:test-embed-9-4"
CANDIDACY_ID = f"{ELECTION_ID}:{DISTRICT_ID}:{PERSON_ID}"
SOURCE_ID = "src:TEST-EMBED-9-4"
PROMISE_ID = "prm:TEST-EMBED-9-4-0001"

#: A made-up sentence, of the shape a real testable commitment takes, said by
#: nobody. Deliberately names a fictional state and a figure no real campaign
#: used, so a search hit on it cannot be mistaken for real promise text.
SYNTHETIC_QUOTE = (
    "SYNTHETIC TEST: I will vote to cap the Zzyzx state ferry toll at $7 per crossing."
)
SYNTHETIC_QUOTE_REVISED = (
    "SYNTHETIC TEST REVISED: I will vote to cap the Zzyzx state ferry toll at $9 per crossing."
)


class CheckFailed(Exception):
    """A live check failed. Raised rather than printed so cleanup still runs."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        logger.info("  PASS  %s", label)
        return
    raise CheckFailed(f"{label}{': ' + detail if detail else ''}")


# --- setup --------------------------------------------------------------------


def _seed_synthetic_graph(client: ZeroDBClient) -> None:
    """Write the reference rows a synthetic promise needs to validate."""
    Repository(client=client, model=Person).upsert(
        {
            "id": PERSON_ID,
            "canonical_name": "SYNTHETIC TEST CANDIDATE (not a real person)",
            "external_ids": {},
            "photo_url": None,
            "terms": [],
        }
    )
    Repository(client=client, model=Election).upsert(
        {
            "id": ELECTION_ID,
            "jurisdiction_id": "us-federal",
            "name": "SYNTHETIC TEST ELECTION (verify_promise_embedding_live)",
            "election_date": "2026-11-03",
            "kind": "general",
        }
    )
    Repository(client=client, model=District).upsert(
        {
            "id": DISTRICT_ID,
            "body_id": "us-house",
            "name": "SYNTHETIC TEST DISTRICT",
            "state": "ZZ",
            "valid_from": "2025-01-03",
        }
    )
    Repository(client=client, model=Candidacy).upsert(
        {
            "id": CANDIDACY_ID,
            "election_id": ELECTION_ID,
            "district_id": DISTRICT_ID,
            "person_id": PERSON_ID,
            "person_name": "SYNTHETIC TEST CANDIDATE (not a real person)",
            "party": None,
            "is_incumbent": False,
            "status": "filed",
        }
    )
    Repository(client=client, model=PromiseSource).upsert(
        {
            "id": SOURCE_ID,
            "candidacy_id": CANDIDACY_ID,
            "source_type": "campaign_website",
            "url": "https://example.invalid/synthetic-test-embed-9-4",
            "archived_file_id": "synthetic-test-embed-9-4",
            "captured_at": "2026-10-20T00:00:00+00:00",
        }
    )
    Repository(client=client, model=Promise).upsert(
        {
            "id": PROMISE_ID,
            "candidacy_id": CANDIDACY_ID,
            "person_id": PERSON_ID,
            "source_id": SOURCE_ID,
            "quote_span": SYNTHETIC_QUOTE,
            "actor": "the speaker",
            "action": "vote to cap",
            "object": "the Zzyzx state ferry toll at $7 per crossing",
            "direction": "support",
            "topic_id": None,
            "stance_id": None,
            "testable": True,
            # Unmistakable in the table: not a model name, and not story 9.3's
            # stub version either, because no extractor produced this row.
            "extractor_version": "synthetic-test-embed-9-4",
            "confidence": 0.5,
            "vector_id": None,
            "mappings": [],
            "qa_pending": False,
        }
    )
    logger.info("seeded the synthetic graph: %s -> %s", CANDIDACY_ID, PROMISE_ID)


# --- the checks ---------------------------------------------------------------


def _check_embed_returns_no_vector_id(client: ZeroDBClient) -> str | None:
    """Check 1 and 2: the live write response, printed in full.

    This is the fact `app.ingest.promise_embedding` is shaped around, so it is
    measured here on every run rather than cited from a prior one. The probe
    vector is deleted immediately.
    """
    logger.info("--- 1/2: what does the live embed-and-store actually return?")
    response = client._request(  # noqa: SLF001 - deliberately below the typed client
        "POST",
        f"/projects/{client.project_id}/embeddings/embed-and-store",
        json={
            "texts": ["SYNTHETIC TEST probe: does embed-and-store return a vector id?"],
            "namespace": PROMISES_NAMESPACE,
            "metadata_list": [{"probe_9_4_verify": True}],
        },
    )
    body = response.json_body if isinstance(response.json_body, dict) else {}
    logger.info("  live response: %s", json.dumps(body, sort_keys=True))

    id_like = [key for key in body if "vector_id" in key or key in {"ids", "vector_ids"}]
    _check(
        "embed_and_store returns NO vector id, so 9.4's criterion needs a second read",
        not id_like,
        f"found id-like keys {id_like}",
    )
    _check("the live embedding model is bge-m3", body.get("model") == "bge-m3", str(body))
    _check("the live embedding is 1024-dimensional", body.get("dimensions") == 1024, str(body))

    # Clean the probe up: it carries no promise_id, so the embedder would
    # ignore it, but leaving litter in a production namespace is not on.
    for record in client.list_vectors(namespace=PROMISES_NAMESPACE, limit=200).vectors:
        if record.vector_metadata.get("probe_9_4_verify"):
            client.delete_vector(vector_id=record.vector_id, namespace=PROMISES_NAMESPACE)
            logger.info("  deleted the probe vector %s", record.vector_id)
            return record.vector_id
    return None


def _check_first_run(client: ZeroDBClient) -> None:
    """Checks 3-6: the vector id is recoverable, the row carries it, the event sent."""
    logger.info("--- 3/6: the first embedding run")
    report = PromiseEmbedder(client, pace=lambda: None).embed_promises_for_source(SOURCE_ID)
    logger.info(
        "  report: %d embedded, %d skipped, %d rebuilt, %d event(s), %d failure(s)",
        len(report.embedded),
        len(report.skipped),
        len(report.rebuilt),
        len(report.events_emitted),
        len(report.failures),
    )
    for failure in report.failures:
        logger.error("  FAILURE %s", failure)
    _check("the run reports no failures", not report.failures, "; ".join(report.failures))
    _check(
        "the synthetic promise is reported as embedded",
        report.embedded == [PROMISE_ID],
        str(report.embedded),
    )

    # Check 5: 9.4's criterion is a property of the ROW. Re-read it live.
    stored = Repository(client=client, model=Promise).get(PROMISE_ID)
    _check("the promise row reads back", stored is not None)
    assert stored is not None  # noqa: S101 - just checked
    _check(
        "the promise row carries a vector_id (story 9.4's acceptance criterion)",
        bool(stored.vector_id),
        f"vector_id={stored.vector_id!r}",
    )

    # Check 3 and 4: that id names a live vector, and its metadata is intact.
    vectors = _promise_vectors(client)
    _check("exactly one vector exists for the synthetic promise", len(vectors) == 1, str(vectors))
    vector = vectors[0]
    _check(
        "the row's vector_id names that live vector",
        stored.vector_id == vector.vector_id,
        f"row={stored.vector_id!r} live={vector.vector_id!r}",
    )
    _check(
        "the stored document is the promise's own verbatim quote",
        vector.document == SYNTHETIC_QUOTE,
        f"document={vector.document!r}",
    )

    metadata = vector.vector_metadata
    logger.info("  live metadata: %s", json.dumps(metadata, sort_keys=True, default=str))
    _check("metadata carries promise_id", metadata.get("promise_id") == PROMISE_ID, str(metadata))
    _check("metadata carries person_id", metadata.get("person_id") == PERSON_ID, str(metadata))
    _check(
        "metadata carries the topic_id key even though it is null",
        "topic_id" in metadata and metadata["topic_id"] is None,
        str(metadata),
    )
    _check("metadata carries the derived cycle", metadata.get("cycle") == 2026, str(metadata))
    _check("metadata carries testable", metadata.get("testable") is True, str(metadata))

    # Check 6: the event really published, with datamodel.md's payload.
    _check(
        "the run reports the promise.extracted event as emitted",
        report.events_emitted == [PROMISE_ID],
        str(report.events_emitted),
    )
    events = _promise_extracted_events(client)
    _check(
        f"exactly one live {PROMISE_EXTRACTED_TOPIC} event names the synthetic promise",
        len(events) == 1,
        f"{len(events)} event(s)",
    )
    payload = events[0].event_payload
    logger.info("  live event payload: %s", json.dumps(payload, sort_keys=True, default=str))
    _check(
        "the event payload is exactly datamodel.md's promise_id + person_id",
        set(payload) == {"promise_id", "person_id"}
        and payload["promise_id"] == PROMISE_ID
        and payload["person_id"] == PERSON_ID,
        str(payload),
    )


def _check_second_run_is_a_no_op(client: ZeroDBClient) -> None:
    """Check 7: idempotency against the real store."""
    logger.info("--- 7: a second run with nothing changed")
    before = len(_promise_extracted_events(client))
    report = PromiseEmbedder(client, pace=lambda: None).embed_promises_for_source(SOURCE_ID)
    logger.info(
        "  report: %d embedded, %d skipped, %d event(s)",
        len(report.embedded),
        len(report.skipped),
        len(report.events_emitted),
    )
    _check("the re-run embeds nothing", report.embedded == [], str(report.embedded))
    _check("the re-run skips the promise as current", report.skipped == [PROMISE_ID])
    _check("the re-run emits no second event", report.events_emitted == [])
    _check(
        "still exactly one vector for the synthetic promise",
        len(_promise_vectors(client)) == 1,
    )
    after = len(_promise_extracted_events(client))
    _check(
        "the live event log gained no event (9.5: emitted once per promise)",
        after == before,
        f"{before} -> {after}",
    )


def _check_a_changed_quote_rebuilds(client: ZeroDBClient) -> None:
    """Check 8: a revised quote_span deletes the stale vector and re-embeds."""
    logger.info("--- 8: the promise's quote_span changes")
    repo: Repository[Promise] = Repository(client=client, model=Promise)
    stored = repo.get(PROMISE_ID)
    assert stored is not None  # noqa: S101 - earlier checks established it
    stale_vector_id = stored.vector_id
    repo.upsert(
        {
            **stored.model_dump(),
            "id": PROMISE_ID,
            "quote_span": SYNTHETIC_QUOTE_REVISED,
            "object": "the Zzyzx state ferry toll at $9 per crossing",
        }
    )

    report = PromiseEmbedder(client, pace=lambda: None).embed_promises_for_source(SOURCE_ID)
    logger.info(
        "  report: %d embedded, %d rebuilt, %d event(s), %d failure(s)",
        len(report.embedded),
        len(report.rebuilt),
        len(report.events_emitted),
        len(report.failures),
    )
    _check("the rebuild reports no failures", not report.failures, "; ".join(report.failures))
    _check("the promise is reported as rebuilt", report.rebuilt == [PROMISE_ID])

    vectors = _promise_vectors(client)
    _check(
        "still exactly one vector for the promise after the rebuild",
        len(vectors) == 1,
        f"{len(vectors)} vector(s): {[v.vector_id for v in vectors]}",
    )
    _check(
        "the stale vector is really gone from the live namespace",
        vectors[0].vector_id != stale_vector_id,
        f"stale={stale_vector_id!r} current={vectors[0].vector_id!r}",
    )
    _check(
        "the new vector holds the revised quote",
        vectors[0].document == SYNTHETIC_QUOTE_REVISED,
        f"document={vectors[0].document!r}",
    )
    refreshed = repo.get(PROMISE_ID)
    assert refreshed is not None  # noqa: S101 - just written
    _check(
        "the row now points at the new vector",
        refreshed.vector_id == vectors[0].vector_id,
        f"row={refreshed.vector_id!r} live={vectors[0].vector_id!r}",
    )


# --- live reads ---------------------------------------------------------------


def _promise_vectors(client: ZeroDBClient) -> list:
    """Every live vector in the `promises` namespace naming the synthetic promise."""
    found = []
    offset = 0
    while True:
        page = client.list_vectors(namespace=PROMISES_NAMESPACE, limit=200, offset=offset)
        found.extend(
            record
            for record in page.vectors
            if record.vector_metadata.get("promise_id") == PROMISE_ID
        )
        if not page.vectors or len(page.vectors) < 200:
            break
        offset += 200
    return found


def _promise_extracted_events(client: ZeroDBClient) -> list:
    """Every live `promise.extracted` event naming the synthetic promise."""
    page = client.list_events(topic=PROMISE_EXTRACTED_TOPIC, limit=200)
    return [event for event in page.events if event.event_payload.get("promise_id") == PROMISE_ID]


# --- cleanup ------------------------------------------------------------------


def _cleanup(client: ZeroDBClient) -> bool:
    """Delete every synthetic row and vector, then confirm by a live re-read.

    Events are NOT deleted: ZeroDB's event log has no delete endpoint (the
    client exposes `create_event`/`list_events`/`subscribe_events` and nothing
    else), so the synthetic `promise.extracted` events this run published stay
    in the live log. They name `prm:TEST-EMBED-9-4-0001`, which resolves to
    nothing after cleanup — a dangling reference in an append-only log, which
    is the honest state rather than a hidden one. Flagged in this story's PR
    rather than worked around: a consumer that cannot resolve a promise_id
    must already tolerate it (a promise can be corrected away), and inventing
    a tombstone row to satisfy the lookup would be worse.
    """
    logger.info("=== cleanup ===")
    ok = True

    for record in _promise_vectors(client):
        try:
            client.delete_vector(vector_id=record.vector_id, namespace=PROMISES_NAMESPACE)
            logger.info("deleted vector %s", record.vector_id)
        except ZeroDBError as exc:
            logger.error("could not delete vector %s: %s", record.vector_id, exc)
            ok = False

    # Children before parents, so no delete is refused for a live reference.
    for table, row_id in (
        (Promise.table_name, PROMISE_ID),
        (PromiseSource.table_name, SOURCE_ID),
        (Candidacy.table_name, CANDIDACY_ID),
        (District.table_name, DISTRICT_ID),
        (Election.table_name, ELECTION_ID),
        (Person.table_name, PERSON_ID),
    ):
        try:
            client.delete_row(table, row_id)
            logger.info("deleted %s %s", table, row_id)
        except ZeroDBError as exc:
            logger.error("could not delete %s %s: %s", table, row_id, exc)
            ok = False

    # Check 9: confirm by re-reading, not by trusting the delete responses.
    for table, row_id in (
        (Promise.table_name, PROMISE_ID),
        (PromiseSource.table_name, SOURCE_ID),
        (Candidacy.table_name, CANDIDACY_ID),
        (District.table_name, DISTRICT_ID),
        (Election.table_name, ELECTION_ID),
        (Person.table_name, PERSON_ID),
    ):
        if client.get_row(table, row_id) is not None:
            logger.error("STILL PRESENT after cleanup: %s %s", table, row_id)
            ok = False
    remaining = _promise_vectors(client)
    if remaining:
        logger.error(
            "STILL PRESENT after cleanup: %d vector(s) %s",
            len(remaining),
            [record.vector_id for record in remaining],
        )
        ok = False

    if ok:
        logger.info("  PASS  cleanup removed every synthetic row and vector")
    return ok


# --- main ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--keep",
        action="store_true",
        help="skip cleanup. Debug only: the synthetic rows assert things about a "
        "candidate who does not exist, and must not be left in the live project.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        client = ZeroDBClient.from_env()
    except Exception as exc:  # noqa: BLE001 - any credential problem is the same exit
        logger.error("no ZeroDB credentials: %s", exc)
        return 1

    logger.info("live project: %s", client.project_id)
    _report_live_promise_state(client)

    failed: BaseException | None = None
    try:
        _seed_synthetic_graph(client)
        _check_embed_returns_no_vector_id(client)
        _check_first_run(client)
        _check_second_run_is_a_no_op(client)
        _check_a_changed_quote_rebuilds(client)
    except BaseException as exc:  # noqa: BLE001 - cleanup must run regardless
        failed = exc
        logger.error("CHECK FAILED: %s", exc)

    cleaned = True
    if args.keep:
        logger.warning(
            "--keep: the synthetic rows and vectors are STILL IN THE LIVE PROJECT. "
            "Delete them by hand: they assert things about a candidate who does not exist."
        )
    else:
        try:
            cleaned = _cleanup(client)
        except BaseException as exc:  # noqa: BLE001 - report, do not mask the real failure
            logger.error("cleanup itself failed: %s", exc)
            cleaned = False
        finally:
            client.close()

    if failed is not None:
        return 1
    if not cleaned:
        return 1
    logger.info("=== every live check passed ===")
    logger.info(
        "The live promises table is still empty: this run embedded a synthetic "
        "subject and removed it. Nothing real was embedded, because no real "
        "promise exists to embed -- see this script's docstring."
    )
    return 0


def _report_live_promise_state(client: ZeroDBClient) -> None:
    """Print the real state of the live promise tables before touching anything.

    Reported on every run rather than asserted, because the whole point of
    the synthetic subject is that this count is zero — and if a later story
    changes that, this line is where a reader sees it first.
    """
    try:
        sources = client.query_rows(PromiseSource.table_name, limit=1).total
        promises = client.query_rows(Promise.table_name, limit=1).total
    except ZeroDBError as exc:
        logger.warning("could not read the live promise tables: %s", exc)
        return
    logger.info(
        "live state before this run: %d promise_sources row(s), %d promises row(s)",
        sources,
        promises,
    )
    if promises == 0:
        logger.info(
            "  zero real promises, as expected: story 9.3 wrote none on purpose "
            "(its only extractor is a stand-in that scored 0-for-8 against these "
            "very sources on 2026-10-20). Hence the synthetic subject below."
        )


if __name__ == "__main__":
    raise SystemExit(main())
