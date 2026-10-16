#!/usr/bin/env python3
"""Verify tiers/limits, dealbreaker and profile load against the LIVE
"informed" project (stories 4.2, 4.3, 4.4).

What it proves, in order:

  1. **Tier weights are read from `methodology_versions` live**, not a
     constant — `MethodologyVersionService.current_version()` against the
     real project, seeding the one default row if the project has none yet
     (the same idempotent seed `scripts/verify_quick_onboarding_live.py`'s
     sibling scripts rely on for their own reference rows).
  2. **A profile with the full tier spread (5 primary / 8 secondary / a
     nice-to-have) saves live** through the real, encrypted `voter_profiles`
     store — the upper boundary prd.md's tier table allows, exercised
     against real stance ids from the live taxonomy rather than synthetic
     ones, so a live-only validation quirk cannot hide behind a fixture.
  3. **A dealbreaker on a primary interest round-trips live** and is visible
     on load — story 4.3's "shown in the profile summary", checked against
     the actual decrypted row this script reads back.
  4. **`GET /v1/profiles/{user_id}`'s underlying `load()` returns an
     identical profile** to what was saved — story 4.4's own acceptance
     criterion, phrased exactly that way.
  5. **A different voter's load sees nothing** — the per-user HMAC row id
     (story 14.1) already guarantees this structurally, re-checked here at
     the service layer against the live store rather than only in the
     in-memory fake.
  6. **Cleanup is confirmed by a live re-read.** The synthetic profile this
     script writes is deleted and the delete is confirmed, exactly as
     `scripts/verify_profiles_live.py` established. The seeded
     `methodology_versions` row is NOT cleaned up — like `app.seed.Seeder`'s
     three rows, it is permanent reference data a fresh environment needs,
     not run-specific debris (see `app.profiles.methodology`'s module
     docstring).

DATA: topics and stances read here are real, permanent reference data; the
one write is a synthetic profile for a user id carrying an obvious marker
(`synthetic-4-2-live-check`), deleted before this script exits and confirmed
gone by a live re-read. CLAUDE.md forbids real voter data in a fixture, a
log, or test output.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed — and a throwaway profile root key if one is not
already set:

    set -a; . local.env; set +a
    export ZERODB_PROFILE_ROOT_KEY="$(
      python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())'
    )"
    python backend/scripts/verify_profile_tiers_dealbreaker_and_load_live.py
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from app.profiles import PROFILE_TABLES
from app.profiles.crypto import KEY_WRAP_TABLE, ProfileKeyring, ProfileRootKey, RootKeyMissingError
from app.profiles.methodology import DEFAULT_TIER_WEIGHTS, MethodologyVersionService
from app.profiles.methodology import TABLE_NAME as METHODOLOGY_TABLE
from app.profiles.models import VoterProfileRow
from app.profiles.service import VoterProfileService
from app.seed_taxonomy import TAXONOMY_LOCALE, TAXONOMY_VERSION
from app.stances.service import StanceService
from app.topics.service import TopicService
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError

MARKER = "synthetic-4-2-live-check"
USER_ID = f"{MARKER}-voter"
OTHER_USER_ID = f"{MARKER}-other-voter"

logger = logging.getLogger("verify_profile_tiers_dealbreaker_and_load_live")


def ensure_tables(client: ZeroDBClient) -> None:
    from app.bootstrap import TABLE_SCHEMAS

    existing = {table.table_name for table in client.list_tables(limit=200)}
    for name in (*PROFILE_TABLES, METHODOLOGY_TABLE):
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

    row_ids: list[str] = []
    try:
        ensure_tables(client)
        keyring = ProfileKeyring(client=client, root_key=root)
        service = VoterProfileService(client=client, keyring=keyring)
        row_ids = [keyring.row_id(USER_ID), keyring.row_id(OTHER_USER_ID)]

        # --- 1. tier weights read live from methodology_versions ----------
        methodology = MethodologyVersionService(client)
        version = methodology.current_version()
        check(
            "the live methodology's tier weights match prd.md's published table",
            version.tier_weights == DEFAULT_TIER_WEIGHTS,
            f"got {version.tier_weights}",
        )
        check("a methodology version id is present live", bool(version.id))

        # --- live taxonomy + stances, enough subtopics for the full spread
        tree = TopicService(client).tree(locale=TAXONOMY_LOCALE, taxonomy_version=TAXONOMY_VERSION)
        all_subtopics = [sub.id for topic in tree.topics for sub in topic.subtopics]
        check(
            "the live taxonomy has at least 14 subtopics (5 primary + 8 secondary + 1)",
            len(all_subtopics) >= 14,
            f"got {len(all_subtopics)}",
        )

        stance_service = StanceService(client)

        def interest_for(topic_id: str, tier: str, *, dealbreaker: bool = False) -> dict[str, Any]:
            cards = stance_service.for_topic(topic_id)
            if not cards:
                raise RuntimeError(f"no live stance options for {topic_id}")
            return {
                "topic_id": topic_id,
                "stance_id": str(cards[0].id),
                "tier": tier,
                "intensity": 2,
                "dealbreaker": dealbreaker,
            }

        primary_topics = all_subtopics[0:5]
        secondary_topics = all_subtopics[5:13]
        nice_to_have_topic = all_subtopics[13]

        interests = (
            [
                interest_for(topic_id, "primary", dealbreaker=(topic_id == primary_topics[0]))
                for topic_id in primary_topics
            ]
            + [interest_for(topic_id, "secondary") for topic_id in secondary_topics]
            + [interest_for(nice_to_have_topic, "nice_to_have")]
        )

        # --- 2. the full tier spread saves live ----------------------------
        row = service.save(USER_ID, {"interests": interests}, jurisdiction_ids=["us-federal"])
        check(
            "a profile with 5 primary / 8 secondary / 1 nice-to-have saves live",
            row.version == 1,
        )

        # --- 3 & 4. dealbreaker and the load round trip --------------------
        loaded = service.load(USER_ID)
        check("the saved profile loads back live", loaded is not None)
        if loaded is not None:
            check(
                "load returns the same interest count saved",
                len(loaded.interests) == len(interests),
                f"got {len(loaded.interests)}, expected {len(interests)}",
            )
            check(
                "load returns the same topic/stance/tier for every interest",
                {(i.topic_id, i.stance_id, i.tier) for i in loaded.interests}
                == {(i["topic_id"], i["stance_id"], i["tier"]) for i in interests},
            )
            dealbreakers = [i for i in loaded.interests if i.dealbreaker]
            check(
                "exactly one dealbreaker loads back, on a primary interest",
                len(dealbreakers) == 1 and dealbreakers[0].tier == "primary",
                f"got {len(dealbreakers)} dealbreaker(s)",
            )

        # --- 5. a different voter's load sees nothing ----------------------
        other_loaded = service.load(OTHER_USER_ID)
        check("a different voter's live load returns no profile", other_loaded is None)

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - report and still clean up
        failures.append(f"{type(exc).__name__}: {exc}")
        logger.error("FAIL  %s: %s", type(exc).__name__, exc)
    finally:
        # --- 6. cleanup, confirmed by live re-read -------------------------
        # The seeded methodology_versions row is deliberately NOT deleted —
        # see the module docstring: it is permanent reference data, like
        # app.seed.Seeder's rows, not run-specific debris.
        logger.info("cleaning up the synthetic live test profiles")
        for row_id in row_ids:
            for table in (VoterProfileRow.table_name, KEY_WRAP_TABLE):
                try:
                    client.delete_row(table, row_id)
                except ZeroDBNotFoundError:
                    logger.info("  already gone: %s / %s", table, row_id)
                except ZeroDBError as exc:
                    failures.append(f"could not delete from {table}: {exc}")
                    logger.error("  FAILED to delete from %s: %s", table, exc)

        leftover: list[str] = []
        for row_id in row_ids:
            for table in (VoterProfileRow.table_name, KEY_WRAP_TABLE):
                try:
                    if client.get_row(table, row_id) is not None:
                        leftover.append(f"{table}:{row_id}")
                except ZeroDBNotFoundError:
                    pass
        if leftover:
            failures.append(f"live rows left behind: {', '.join(leftover)}")
            logger.error("FAIL  cleanup left rows: %s", ", ".join(leftover))
        else:
            logger.info("PASS  both synthetic test profiles are confirmed gone")
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
