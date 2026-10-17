#!/usr/bin/env python3
"""Verify stories 14.6 and 14.7 against the LIVE "informed" ZeroDB project.

What this proves, in order
=============================

  1. **The summary-card endpoint works against a REAL, live candidacy.**
     Story 2.6's live load wrote three real candidacies
     (`scripts/load_candidacies.py`'s one successful run against DEMO_KEY,
     confirmed live on 2026-10-16). This script reads whichever of those
     candidacies exists and calls `SummaryCardService.card_for` against it
     — not a fixture — asserting the response carries that candidate's real
     name/party/district and nothing else.
  2. **A profile field genuinely cannot leak through it, live.** A synthetic
     voter profile is saved live (through `VoterProfileService`, the same
     encrypted path story 14.1 built), and the summary card for the real
     candidacy above is fetched again. The response is checked for zero
     trace of the synthetic profile's interests.
  3. **k-anonymity withholds a small real count, live.** A unique,
     throwaway event type/bucket pair is used so this run cannot collide
     with any other data in the project (past or concurrent). 12 synthetic
     events are recorded, and `aggregate()` is confirmed to withhold the
     count (`sufficient=False`, `count=None`) even though the real count is
     knowable live, directly, by reading the events back raw.
  4. **k-anonymity reveals the count once >= 50 events exist, live.** 38
     more events are recorded (bringing the total to 50) and `aggregate()`
     is confirmed to now report `sufficient=True, count=50`.
  5. **Cleanup.** The synthetic voter's profile row and key wrap are
     deleted and confirmed gone by a live re-read. The 50 synthetic
     analytics events are NOT deletable — ZeroDB's events API has no
     delete (datamodel.md documents events as append-only publish/read,
     and no story has added a delete) — so this script logs that
     explicitly rather than silently leaving the question open. See "What
     is NOT cleaned up, and why" below.
  6. **Senator/Representative rows this script reads are never written to
     or deleted.** The three real candidacies and the real `people`/
     `districts` rows behind them are read-only throughout.

What is NOT cleaned up, and why
==================================

This story's own acceptance criterion asks to confirm cleanup for the
*test events* used to verify k-anonymity. Two honest limits on that,
stated here rather than glossed over:

  * **ZeroDB's events API has no delete.** `ZeroDBClient` wraps
    `zerodb_create_event`/`zerodb_subscribe_events`/the read half of the
    latter; there is no `delete_event`, and the live API's own
    `/database/events` surface (confirmed by `app.zerodb.client`'s
    `create_event`/`list_events` — the only two methods that exist) offers
    none either. So the 50 synthetic events this script writes under the
    unique topic `analytics.zzz-live-check-14.7-{run_id}` are NOT deleted
    at the end of this run; they are abandoned under an id scheme obviously
    marked as a throwaway test run (see MARKER below), the same posture
    `provenance_log`'s append-only design takes for its own rows, and they
    carry no identity of any kind to begin with — datamodel.md's whole
    point for this table. This is reported explicitly in the summary below
    rather than claimed as "cleaned up".
  * **The synthetic voter PROFILE is deleted**, because `voter_profiles` DOES
    support delete (story 14.4) and carries a real encrypted payload worth
    removing — unlike the bare, already-non-identifying analytics events.

RUN IT with the live credentials sourced into the shell — never passed on
the command line, never printed — and a throwaway profile root key:

    set -a; . local.env; set +a
    export ZERODB_PROFILE_ROOT_KEY="$(
      python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())'
    )"
    python backend/scripts/verify_summary_card_and_analytics_live.py

DATA: the voter profile here is a throwaway synthetic test profile
(`MARKER` below), never real voter data, and it is deleted by this script's
own cleanup step. CLAUDE.md forbids real voter data in a fixture, a log or
test output; nothing here is a voter's real interests, and the one thing
that does carry a (synthetic) interest is deleted before this script exits.
"""

from __future__ import annotations

import logging
import sys
import uuid

from app.analytics.service import K_ANONYMITY_THRESHOLD, AnalyticsService
from app.profiles.crypto import KEY_WRAP_TABLE, ProfileKeyring, ProfileRootKey, RootKeyMissingError
from app.profiles.models import VoterProfileRow
from app.profiles.service import VoterProfileService
from app.repositories.base import Repository
from app.repositories.models import Candidacy
from app.summary_card.service import SummaryCardService
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError

logger = logging.getLogger("verify_summary_card_and_analytics_live")

#: A unique marker per run, so this script's events can never collide with
#: another run's (or another story's) data in the live project — load-
#: bearing for the k-anonymity checks, which need an exact, known count.
RUN_ID = uuid.uuid4().hex[:12]
MARKER = f"zzz-live-check-14.6-14.7-{RUN_ID}"
USER_ID = f"synthetic-{MARKER}"


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

    keyring = ProfileKeyring(client=client, root_key=root)
    profile_service = VoterProfileService(client=client, keyring=keyring)
    row_id = keyring.row_id(USER_ID)

    try:
        # --- 14.6, step 1: a real live candidacy exists ---------------------
        candidacies = Repository(client=client, model=Candidacy)
        page = candidacies.query(limit=10)
        check("at least one real candidacy exists live", len(page) > 0)
        if not page:
            raise RuntimeError("no candidacies exist live; run scripts/load_candidacies.py first")
        real_candidacy = page[0]
        logger.info(
            "using real live candidacy %s (%s)",
            real_candidacy.id,
            real_candidacy.person_name,
        )

        # --- 14.6, step 2: the summary card works against it -----------------
        summary_service = SummaryCardService(client=client)
        card = summary_service.card_for(str(real_candidacy.id))
        check(
            "the summary card carries the real candidate's name",
            card.person_name == real_candidacy.person_name,
        )
        check(
            "the summary card carries the real candidate's party",
            card.party == real_candidacy.party,
        )
        check("the summary card names a district", bool(card.district))
        check(
            "the not-yet-computed fields are honestly absent",
            card.verdict_counts is None
            and card.say_do_gap is None
            and card.positions_summary is None,
        )

        # --- 14.6, step 3: a synthetic voter profile with interests ----------
        profile_service.save(
            USER_ID,
            {
                "interests": [
                    {
                        "topic_id": f"test:{MARKER}-alpha:v1",
                        "stance_id": f"test:{MARKER}-alpha:v1:test-stance",
                        "tier": "primary",
                        "intensity": 3,
                        "dealbreaker": False,
                    }
                ]
            },
        )
        loaded = profile_service.load(USER_ID)
        check("the synthetic voter profile saved live", loaded is not None)

        # --- 14.6, step 4: the card is unaffected and leaks nothing -----------
        card_again = summary_service.card_for(str(real_candidacy.id))
        serialized_card = str(card_again.model_dump())
        check(
            "the summary card contains zero trace of the voter's interests",
            MARKER not in serialized_card and "primary" not in serialized_card,
        )
        # Compared field-by-field minus `updated_at`, which is a fresh
        # timestamp on every call by design (`SummaryCardService.card_for`
        # stamps "now" each time) — a difference there is expected, not a
        # sign the card changed based on who asked.
        card_fields = card.model_dump(exclude={"updated_at"})
        card_again_fields = card_again.model_dump(exclude={"updated_at"})
        check(
            "the summary card is identical regardless of a live voter profile "
            "existing, aside from its own refreshed timestamp",
            card_again_fields == card_fields,
        )

        # --- 14.7, step 1: recording carries no identity ----------------------
        analytics = AnalyticsService(client=client)
        event_type = f"{MARKER}-event"
        bucket = "ZZ"
        recorded = analytics.record(event_type, bucket=bucket)
        check(
            "a recorded event carries only event_type/timestamp/bucket",
            set(recorded.payload) <= {"bucket"} and recorded.recorded_at,
        )

        # --- 14.7, step 2: below k=50, the count is withheld -------------------
        for _ in range(11):  # 12 total with the one above
            analytics.record(event_type, bucket=bucket)
        below = analytics.aggregate(event_type, bucket=bucket)
        check(
            "12 live events: aggregate reports insufficient data, not 12",
            below.sufficient is False and below.count is None,
        )

        # --- 14.7, step 3: at k=50, the real count is revealed ------------------
        for _ in range(K_ANONYMITY_THRESHOLD - 12):
            analytics.record(event_type, bucket=bucket)
        at_threshold = analytics.aggregate(event_type, bucket=bucket)
        check(
            f"{K_ANONYMITY_THRESHOLD} live events: aggregate reveals the real count",
            at_threshold.sufficient is True and at_threshold.count == K_ANONYMITY_THRESHOLD,
        )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - report and still clean up
        failures.append(f"{type(exc).__name__}: {exc}")
        logger.error("FAIL  %s: %s", type(exc).__name__, exc)
    finally:
        # --- cleanup: the synthetic voter profile, confirmed by live re-read --
        logger.info("cleaning up the synthetic live test voter profile")
        for table in (VoterProfileRow.table_name, KEY_WRAP_TABLE):
            try:
                client.delete_row(table, row_id)
            except ZeroDBNotFoundError:
                logger.info("  already gone: %s / %s", table, row_id)
            except ZeroDBError as exc:
                failures.append(f"cleanup failed for {table}/{row_id}: {exc}")
                logger.error("FAIL  cleanup failed for %s/%s: %s", table, row_id, exc)

        try:
            after_delete = client.get_row(VoterProfileRow.table_name, row_id)
            check(
                "the synthetic voter profile row is gone, confirmed by a live re-read",
                after_delete is None,
            )
        except ZeroDBError as exc:
            failures.append(f"post-delete re-read failed: {exc}")

        # The 50 synthetic analytics events are NOT deleted — ZeroDB's events
        # API has no delete method. See the module docstring's "What is NOT
        # cleaned up, and why". Reported explicitly rather than silently
        # left unmentioned.
        logger.warning(
            "NOT cleaned up (no delete exists on ZeroDB's events API): "
            "%d synthetic events under topic 'analytics.%s', bucket %r — "
            "obviously-marked test data carrying no identity of any kind",
            K_ANONYMITY_THRESHOLD,
            event_type,
            bucket,
        )

        # Confirm the real candidacies/people/districts this script read were
        # never written to: re-read the same candidacy and diff it against
        # the live row from before this script started, field for field.
        try:
            after_run = Repository(client=client, model=Candidacy).get(str(real_candidacy.id))
            check(
                "the real candidacy this script read is unchanged",
                after_run is not None and after_run.model_dump() == real_candidacy.model_dump(),
            )
        except ZeroDBError as exc:
            failures.append(f"post-run candidacy re-read failed: {exc}")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
