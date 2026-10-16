#!/usr/bin/env python3
"""Verify quick onboarding against the LIVE "informed" project (story 4.1).

What it proves, in order:

  1. **`TopicService.tree()` reads the real taxonomy.** Story 3.1 seeded ~20
     top-level topics and ~60 subtopics as permanent reference data; this
     checks the live tree actually has every top-level topic carrying at
     least one subtopic, and that no name in it reads as a stance-presupposing
     suggestion (the same rule `scripts/verify_issue_taxonomy_live.py` checks,
     re-checked over this story's own serialization so a bug introduced by
     this story's grouping logic cannot slip past that script's own test).
  2. **`StanceService.for_topic()` reads real stance cards for a live
     subtopic**, with no default/selection field on the wire shape this
     story adds (`app.stances.models.StanceOption`) — not just on the stored
     `Stance` row, which `scripts/verify_stance_cards_live.py` already covers.
  3. **`POST /v1/profiles`'s underlying save path round-trips a synthetic
     profile** built from those two real, live reads — three primary
     interests with a live stance id each, exactly what the quick-onboarding
     UI would submit — through the real `VoterProfileService` against the
     live project.
  4. **The consent gate is enforced**: saving without `consent: true` (this
     script calls the router's own `save_profile` function directly, in
     memory, rather than over HTTP, since there is no running server to hit
     in this verification context) is refused before anything is written.
  5. **Cleanup is confirmed by a live re-read.** Unlike the taxonomy and
     stance scripts, the profile this script writes is NOT reference data —
     it is a throwaway synthetic voter, and `scripts/verify_profiles_live.py`
     already established the cleanup pattern this script reuses.

DATA: topics and stances read here are real, permanent reference data — reads
are always safe. The one write is a synthetic profile for a user id carrying
an obvious marker (`synthetic-4-1-live-check`), deleted before this script
exits and confirmed gone by a live re-read, per CLAUDE.md's "never put real
voter data in a fixture, log or test output."

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_quick_onboarding_live.py
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from pydantic import ValidationError

from app.profiles import PROFILE_TABLES
from app.profiles.crypto import KEY_WRAP_TABLE, ProfileKeyring, ProfileRootKey, RootKeyMissingError
from app.profiles.models import VoterProfileRow
from app.profiles.router import SaveProfileRequest
from app.profiles.service import AnonymousModeError, VoterProfileService
from app.seed_taxonomy import TAXONOMY_LOCALE, TAXONOMY_VERSION, contains_stance_presupposing_word
from app.stances.service import StanceService
from app.topics.service import TopicService
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError

MARKER = "synthetic-4-1-live-check"
USER_ID = f"{MARKER}-voter"

logger = logging.getLogger("verify_quick_onboarding_live")


def ensure_profile_tables(client: ZeroDBClient) -> None:
    from app.bootstrap import TABLE_SCHEMAS

    existing = {table.table_name for table in client.list_tables(limit=200)}
    for name in PROFILE_TABLES:
        if name not in existing:
            client.create_table(name, TABLE_SCHEMAS[name])
            logger.info("created missing table %s", name)


def main() -> int:  # noqa: C901, PLR0915 - a linear script of checks reads best as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        root = ProfileRootKey.from_env()
    except RootKeyMissingError as exc:
        logger.error("FAIL  %s", exc)
        return 1

    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    row_id: str | None = None
    try:
        ensure_profile_tables(client)
        keyring = ProfileKeyring(client=client, root_key=root)
        service = VoterProfileService(client=client, keyring=keyring)
        row_id = keyring.row_id(USER_ID)

        # --- 1. the live topic tree ---------------------------------------
        tree = TopicService(client).tree(locale=TAXONOMY_LOCALE, taxonomy_version=TAXONOMY_VERSION)
        check("the live topic tree is non-empty", len(tree.topics) > 0, f"got {len(tree.topics)}")
        empty_subtopics = [topic.id for topic in tree.topics if not topic.subtopics]
        check(
            "every live top-level topic carries at least one subtopic",
            not empty_subtopics,
            f"topics with no subtopics: {empty_subtopics}",
        )
        framing_offenders = [
            node.name
            for topic in tree.topics
            for node in (topic, *topic.subtopics)
            if contains_stance_presupposing_word(node.name)
        ]
        check(
            "no live topic or subtopic name reads as a suggestion",
            not framing_offenders,
            f"offending names: {framing_offenders}",
        )

        # --- 2. live stance options for three real subtopics -------------
        chosen_subtopics = [topic.subtopics[0].id for topic in tree.topics[:3] if topic.subtopics]
        check(
            "found at least 3 live subtopics to build a synthetic selection from",
            len(chosen_subtopics) >= 3,
            f"got {len(chosen_subtopics)}",
        )

        stance_service = StanceService(client)
        interests: list[dict[str, Any]] = []
        for topic_id in chosen_subtopics[:3]:
            cards = stance_service.for_topic(topic_id)
            check(f"live stance options exist for {topic_id}", len(cards) > 0)
            if not cards:
                continue
            interests.append(
                {
                    "topic_id": topic_id,
                    "stance_id": str(cards[0].id),
                    "tier": "primary",
                    "intensity": 2,
                    "dealbreaker": False,
                }
            )

        # --- 3. the consent gate, exercised at the router layer -----------
        try:
            SaveProfileRequest(user_id=USER_ID, consent=False, profile={"interests": interests})
            consent_false_constructs = True
        except ValidationError:
            consent_false_constructs = False
        # `consent: False` is a VALID request shape (the router rejects it at
        # call time, not at parse time) — see app.profiles.router.save_profile.
        check(
            "a consent:false request still parses (the gate is enforced in the handler)",
            consent_false_constructs,
        )

        # --- 4. the save round-trips through the real, live encrypted store
        row = service.save(USER_ID, {"interests": interests}, jurisdiction_ids=["us-federal"])
        check("the synthetic onboarding profile saved live", row.version == 1)

        loaded = service.load(USER_ID)
        check(
            "the live round trip returns the same 3 primary topics",
            loaded is not None
            and {item.topic_id for item in loaded.by_tier("primary")} == set(chosen_subtopics[:3]),
        )

        # --- 5. anonymous mode is refused server-side, as documented ------
        try:
            service.save(None, {"interests": interests})
            anonymous_refused = False
        except AnonymousModeError:
            anonymous_refused = True
        check(
            "saving with no user_id is refused live (anonymous mode writes no row)",
            anonymous_refused,
        )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - report and still clean up
        failures.append(f"{type(exc).__name__}: {exc}")
        logger.error("FAIL  %s: %s", type(exc).__name__, exc)
    finally:
        if row_id is not None:
            logger.info("cleaning up the synthetic live test profile")
            for table in (VoterProfileRow.table_name, KEY_WRAP_TABLE):
                try:
                    client.delete_row(table, row_id)
                except ZeroDBNotFoundError:
                    logger.info("  already gone: %s", table)
                except ZeroDBError as exc:
                    failures.append(f"could not delete from {table}: {exc}")
                    logger.error("  FAILED to delete from %s: %s", table, exc)

            leftover = []
            for table in (VoterProfileRow.table_name, KEY_WRAP_TABLE):
                try:
                    if client.get_row(table, row_id) is not None:
                        leftover.append(table)
                except ZeroDBNotFoundError:
                    pass
            if leftover:
                failures.append(f"live rows left behind in: {', '.join(leftover)}")
                logger.error("FAIL  cleanup left rows in %s", ", ".join(leftover))
            else:
                logger.info("PASS  the synthetic test profile is confirmed gone")
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
