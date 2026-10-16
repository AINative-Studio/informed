#!/usr/bin/env python3
"""Verify free-text topic suggestion against the LIVE "informed" project (3.5).

Every story from 1.3 on has found at least one discrepancy between the fake
and the real API, so story 3.5's acceptance criteria — "the top 3 suggestions
appear; nothing is saved until the voter confirms; the text itself is not
stored" — are checked against the real ZeroDB project and story 3.4's real
`topics` embeddings, not only against the in-memory fake's crude token-overlap
scorer.

What it proves, in order:

  1. **Real semantic relevance.** A handful of genuine free-text concerns,
     each phrased the way a voter who does not know the taxonomy's names
     actually would, are sent through `TopicService.suggest` against the
     real bge-m3 embeddings story 3.4 wrote. The reported top suggestion for
     each is logged for a human to read and judge — "sensible, not random"
     is not something a script can assert on its own, so this prints the
     query and the full ranked list rather than hard-asserting a specific
     topic_id, which could legitimately shift if the taxonomy's wording
     changes.
  2. **No row is written anywhere.** Every one of the 24 live tables'
     `total` row count (`ZeroDBClient.query_rows(..., limit=1)`) is read
     before and after the whole run and compared.
  3. **No vector is written anywhere.** The live `topics` namespace's vector
     count (`ZeroDBClient.get_vector_stats`) is read before and after.
  4. **No log line emitted by the APPLICATION — anything under the `app.`
     logger namespace — contains any of the free-text strings sent.**
     Checked by attaching a `logging.Handler` to the `app` logger (which
     `app.topics.service` and `app.zerodb.client` are both children of) that
     records every formatted message emitted during the run. This
     deliberately does NOT capture this script's own diagnostic printing —
     `logger.info("query: %r", query)` below prints each query to the
     operator's terminal on purpose, so a human can judge relevance; the
     privacy rule under test is that INFORMED's application code never logs
     the text, not that this verification script's own report is silent
     about what it tested.

!!! NO CLEANUP NEEDED !!!

This script writes nothing, so — unlike `verify_stance_cards_live.py` or
`verify_embed_taxonomy_live.py` — there is nothing to leave in place or to
avoid deleting. It depends on story 3.4's embeddings already being live
(`python scripts/embed_taxonomy.py`), which is checked explicitly rather than
assumed.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_free_text_interest_mapping_live.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap import TABLE_SCHEMAS  # noqa: E402
from app.embed_taxonomy import TOPICS_NAMESPACE  # noqa: E402
from app.topics.service import TopicService  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("verify_free_text_interest_mapping_live")

#: Genuine free-text concerns, phrased the way a voter who does not know the
#: taxonomy's own names actually would — never copied from a real voter
#: (CLAUDE.md: no real voter data in a fixture, log or test output). Each is
#: paired with the taxonomy wording a human reader can sanity-check the top
#: suggestion against.
SAMPLE_QUERIES: tuple[str, ...] = (
    "I'm worried about how much I pay for my medications every month",
    "I can't find a doctor who takes new patients where I live",
    "my internet at home is too slow and too expensive",
    "I'm worried about whether my kid's school has enough funding",
    "gas and groceries cost too much right now",
)


class _RecordingHandler(logging.Handler):
    """Captures every formatted log message emitted while it is attached.

    Attached to the `app` logger (see `main`), so it sees every logger under
    that namespace — `app.zerodb.client`'s retry/error logging included, not
    only whatever `app.topics.service` itself emits. That is the point: the
    privacy rule this script checks is "no log line from Informed's own code
    carries the free text", for every module on the call path, not only the
    one this story added.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


def main() -> int:  # noqa: PLR0915 - a linear script of checks reads better as one
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

    recorder = _RecordingHandler()
    #: Informed's own application code, not this script's own diagnostic
    #: printing (which lives at module level, outside the `app` namespace,
    #: and deliberately DOES print each query for a human to read).
    app_logger = logging.getLogger("app")

    try:
        # --- 0. precondition: story 3.4's embeddings must already be live -
        stats_before_all = client.get_vector_stats()
        topics_vector_count_before = stats_before_all.namespaces.get(TOPICS_NAMESPACE, 0)
        check(
            f"the live {TOPICS_NAMESPACE} namespace already has embeddings "
            "(run scripts/embed_taxonomy.py first if this fails)",
            topics_vector_count_before > 0,
            f"found {topics_vector_count_before} vector(s)",
        )

        # --- 1. snapshot every table's row count, before anything else ----
        row_counts_before: dict[str, int] = {}
        for table_name in TABLE_SCHEMAS:
            page = client.query_rows(table_name, limit=1)
            row_counts_before[table_name] = page.total
        logger.info("snapshotted row counts for %d tables before the run", len(row_counts_before))

        # --- 2. real semantic relevance, against the real embeddings ------
        app_logger.addHandler(recorder)
        service = TopicService(client)
        suggestions_by_query: dict[str, list] = {}
        try:
            for index, query in enumerate(SAMPLE_QUERIES, start=1):
                suggestions = service.suggest(text=query, locale="en-US")
                suggestions_by_query[query] = suggestions
                # Printed deliberately, by THIS script, for the human running
                # it to judge relevance — see the module docstring's item 1.
                # This is not the privacy check; item 5 below is.
                logger.info("query %d: %r", index, query)
                if not suggestions:
                    logger.info("  (no suggestions)")
                for rank, suggestion in enumerate(suggestions, start=1):
                    # `suggestion.score` is None against the live API (see
                    # the module docstring's item 1 and
                    # `TopicSuggestion.score`'s own docstring) — formatted as
                    # a string here rather than with `%.4f`, which raises on
                    # `None` instead of printing it.
                    score_display = (
                        f"{suggestion.score:.4f}" if suggestion.score is not None else "none"
                    )
                    logger.info(
                        "  #%d  %-45s score=%-6s (%s)",
                        rank,
                        suggestion.name,
                        score_display,
                        suggestion.topic_id,
                    )
                check(
                    f"query {index} returned at most 3 suggestions",
                    len(suggestions) <= 3,
                    f"returned {len(suggestions)}",
                )
                check(
                    f"query {index} returned at least 1 suggestion",
                    len(suggestions) >= 1,
                    "returned none — a human should judge whether that is right",
                )
        finally:
            app_logger.removeHandler(recorder)

        # --- 3. no row written anywhere ------------------------------------
        row_counts_after = {
            table_name: client.query_rows(table_name, limit=1).total for table_name in TABLE_SCHEMAS
        }
        changed_tables = {
            table_name: (row_counts_before[table_name], row_counts_after[table_name])
            for table_name in TABLE_SCHEMAS
            if row_counts_before[table_name] != row_counts_after[table_name]
        }
        check(
            f"no row was written to any of the {len(TABLE_SCHEMAS)} live tables",
            not changed_tables,
            f"changed: {changed_tables}",
        )

        # --- 4. no vector written anywhere ---------------------------------
        topics_vector_count_after = client.get_vector_stats(
            namespace=TOPICS_NAMESPACE
        ).namespaces.get(TOPICS_NAMESPACE, 0)
        check(
            f"the live {TOPICS_NAMESPACE} namespace's vector count is unchanged",
            topics_vector_count_after == topics_vector_count_before,
            f"{topics_vector_count_before} before, {topics_vector_count_after} after",
        )

        # --- 5. no log line from the APPLICATION carried the free text -----
        # `recorder` only ever saw messages from the `app` logger namespace
        # (attached/detached around the `service.suggest(...)` calls above),
        # so this is checking Informed's own code — `app.topics.service` and
        # whatever `app.zerodb.client` logged on the way — never this
        # script's own `logger.info("query: %r", query)` diagnostic printing.
        leaked: list[str] = []
        for query in SAMPLE_QUERIES:
            for message in recorder.messages:
                if query in message:
                    leaked.append(f"a query string was found in an app log line: {message!r}")
        check(
            f"none of the {len(SAMPLE_QUERIES)} sample free-text strings appear in "
            f"any of the {len(recorder.messages)} log line(s) the application itself emitted",
            not leaked,
            "; ".join(leaked[:5]),
        )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
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
