#!/usr/bin/env python3
"""Verify follow/unfollow against the LIVE "informed" ZeroDB project (15.1).

Follows a REAL sitting legislator, Senator Maria Cantwell
(`person:C000127`, a real bioguide id), already present in the live `people`
table — stories 2.2's `verify_legislators_live.py` and `load_legislators.py`
both wrote this row as real, permanent reference data, and the live
`people` table is exactly what `VoterProfileService.follow`'s existence
check reads (see `app.profiles.service`'s `PersonNotFoundError`). The voter
doing the following is a throwaway SYNTHETIC test profile, not Senator
Cantwell's own data — her row is read-only here, never written to or
deleted.

What it proves, in order:

  1. **Following a real, live legislator succeeds** — `person:C000127`
     resolves against the real `people` table, so the existence check
     passes and the follow is saved.
  2. **Following a synthetic, nonexistent person_id is rejected** —
     `PersonNotFoundError`, and nothing about the stored profile changes.
  3. **The follow round-trips through save and load**: a fresh `load()`
     (not the in-memory object `follow()` returned) shows the same
     followed-person list.
  4. **The follow genuinely lives inside the encrypted payload, not a
     separate table.** The row is read back through the raw client — the
     same "read what ZeroDB actually stored" discipline story 14.1's and
     4.2's live scripts use — and Senator Cantwell's person_id is asserted
     absent from the row's raw JSON, from a base64-decode of `payload`, and
     from the row's only other fields (id/version/locale/jurisdiction_ids/
     updated_at/key_ref) — there is no sixth field a follow list could be
     hiding in.
  5. **Unfollowing removes it, confirmed by a fresh load.**
  6. **Cleanup is confirmed by a live re-read.** The synthetic test voter's
     profile row and key wrap are deleted and confirmed gone. Senator
     Cantwell's `people` row is real, permanent reference data and is
     neither written to nor deleted by this script.

DATA: the only real-world fact this script writes is "this synthetic test
voter follows a real senator", inside a throwaway profile that is deleted
before the script exits. CLAUDE.md forbids real voter data in a fixture, a
log or test output -- there is no voter here, synthetic or real, whose
political opinions are logged; only a follow list containing one already
-public fact (who a senator is) is ever written, and even that is deleted.

Run it with the live credentials sourced into the shell -- never passed on
the command line, never printed -- and a throwaway profile root key if one
is not already set:

    set -a; . local.env; set +a
    export ZERODB_PROFILE_ROOT_KEY="$(
      python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())'
    )"
    python backend/scripts/verify_follow_incumbent_live.py
"""

from __future__ import annotations

import base64
import json
import logging
import sys

from app.profiles.crypto import KEY_WRAP_TABLE, ProfileKeyring, ProfileRootKey, RootKeyMissingError
from app.profiles.models import VoterProfileRow
from app.profiles.service import PersonNotFoundError, VoterProfileService
from app.repositories.base import Repository
from app.repositories.models import Person
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError

MARKER = "zzz-live-check-15.1"
USER_ID = f"synthetic-{MARKER}"

#: Senator Maria Cantwell — a real, permanent `people` row written by story
#: 2.2's live verification and full load. Read-only here.
REAL_LEGISLATOR_ID = "person:C000127"

#: A deliberately-synthetic id that must never exist in `people`.
NONEXISTENT_PERSON_ID = f"person:{MARKER}-does-not-exist"

logger = logging.getLogger("verify_follow_incumbent_live")


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
    service = VoterProfileService(client=client, keyring=keyring)
    row_id = keyring.row_id(USER_ID)

    try:
        # --- 0. the real legislator actually exists live -------------------
        people = Repository(client=client, model=Person)
        cantwell = people.get(REAL_LEGISLATOR_ID)
        check(
            f"{REAL_LEGISLATOR_ID} (Sen. Maria Cantwell) exists live in people",
            cantwell is not None,
        )
        if cantwell is None:
            raise RuntimeError(
                f"{REAL_LEGISLATOR_ID} is not in the live people table; run "
                "scripts/load_legislators.py or verify_legislators_live.py first"
            )

        # --- seed a synthetic voter with a primary interest ------------------
        service.save(
            USER_ID,
            {
                "interests": [
                    {
                        "topic_id": f"test:{MARKER}-alpha:v1",
                        "stance_id": f"test:{MARKER}-alpha:v1:test-stance",
                        "tier": "primary",
                        "intensity": 2,
                        "dealbreaker": False,
                    }
                ]
            },
        )

        # --- 1. following a real legislator succeeds ------------------------
        followed = service.follow(USER_ID, REAL_LEGISLATOR_ID)
        check(
            "following a real legislator succeeds",
            REAL_LEGISLATOR_ID in followed.followed_person_ids,
        )

        # --- 2. following a nonexistent person_id is rejected ---------------
        rejected = False
        try:
            service.follow(USER_ID, NONEXISTENT_PERSON_ID)
        except PersonNotFoundError:
            rejected = True
        check("following a nonexistent person_id is rejected live", rejected)

        after_rejection = service.load(USER_ID)
        check(
            "the rejected follow changed nothing live",
            after_rejection is not None
            and after_rejection.followed_person_ids == [REAL_LEGISLATOR_ID],
        )

        # --- 3. the follow round-trips through a FRESH load -----------------
        reloaded = service.load(USER_ID)
        check(
            "the follow round-trips through save and load",
            reloaded is not None and REAL_LEGISLATOR_ID in reloaded.followed_person_ids,
        )

        # --- 4. the follow lives inside the encrypted payload ----------------
        raw_row = client.get_row(VoterProfileRow.table_name, row_id)
        check("the raw stored row is readable live", raw_row is not None)
        if raw_row is not None:
            stored = VoterProfileRow.model_validate(raw_row.row_data)
            expected_fields = {
                "id",
                "version",
                "locale",
                "jurisdiction_ids",
                "updated_at",
                "key_ref",
                "payload",
            }
            check(
                "the stored row's fields are exactly datamodel.md's seven",
                set(stored.row_data()) == expected_fields,
            )
            serialized_row = json.dumps(stored.row_data())
            check(
                "the followed person's id is absent from the raw row JSON",
                REAL_LEGISLATOR_ID not in serialized_row,
            )
            decoded_payload = base64.b64decode(stored.payload)
            check(
                "the followed person's id is absent from the base64-decoded payload bytes",
                REAL_LEGISLATOR_ID.encode() not in decoded_payload,
            )
            try:
                as_text = decoded_payload.decode("utf-8")
                json.loads(as_text)
                is_plaintext_json = True
            except (UnicodeDecodeError, json.JSONDecodeError):
                is_plaintext_json = False
            check("the stored payload is not readable as plaintext JSON", not is_plaintext_json)

        # --- 5. unfollowing removes it, confirmed by a fresh load ------------
        service.unfollow(USER_ID, REAL_LEGISLATOR_ID)
        after_unfollow = service.load(USER_ID)
        check(
            "unfollowing removes the person, confirmed by a fresh load",
            after_unfollow is not None
            and REAL_LEGISLATOR_ID not in after_unfollow.followed_person_ids,
        )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - report and still clean up
        failures.append(f"{type(exc).__name__}: {exc}")
        logger.error("FAIL  %s: %s", type(exc).__name__, exc)
    finally:
        # --- 6. cleanup, confirmed by live re-read ---------------------------
        # Senator Cantwell's `people` row is real, permanent reference data
        # and is NOT touched here — only the synthetic test voter's rows are.
        logger.info("cleaning up the synthetic live test voter profile")
        for table in (VoterProfileRow.table_name, KEY_WRAP_TABLE):
            try:
                client.delete_row(table, row_id)
            except ZeroDBNotFoundError:
                logger.info("  already gone: %s / %s", table, row_id)
            except ZeroDBError as exc:
                failures.append(f"could not delete from {table}: {exc}")
                logger.error("  FAILED to delete from %s: %s", table, exc)

        leftover: list[str] = []
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
            logger.info("PASS  the synthetic test voter's profile is confirmed gone")
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
