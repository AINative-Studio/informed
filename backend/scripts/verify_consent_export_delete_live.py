#!/usr/bin/env python3
"""Verify consent, export and delete against the LIVE ZeroDB project (14.2, 14.4).

Story 14.1's own live script proved the thing that could only be proved live:
"the row as actually stored in ZeroDB is ciphertext". This script proves the
three claims THIS work makes that a fake cannot settle:

  1. **The consent record is really there, in the real table.** `voter_consents`
     is a table this story adds, so the first question is whether the live
     project accepts its schema at all, whether a content-addressed `vc:` id
     round-trips, and whether `insert_row` genuinely appends rather than
     replacing (which would silently turn an append-only ledger into a state
     field). Read back through the RAW client, not the ledger, so a bug in the
     ledger's own reader cannot hide a bad write.
  2. **The export round-trips through real ciphertext.** A synthetic profile is
     saved, read back through `GET`-equivalent service calls, and every field
     is compared to what went in — through actual AES-GCM against a real row,
     not the fake's dict store.
  3. **Delete + key shred is permanent, and fast enough.** The acceptance
     criterion is "completes in under 1 minute", and a single-row delete plus a
     key shred plus a ledger append is three live calls — so the number is
     measured here rather than assumed, against the real API's real latency and
     the 60 req/min throttle the project was observed to have. Then the
     strongest available form of the shred claim, borrowed from 14.1: holding
     the live root key AND the retained ciphertext, this code's own decrypt
     function must fail.

It also checks the two things about the ledger that matter most and are easiest
to get wrong at a distance: that a consent row contains no interest and no user
id when read raw from the live table, and that the withdrawal record survives
the deletion of the profile and its key (which is the whole reason the ledger
is a separate, unencrypted table — see app/profiles/consent.py).

RUN IT with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    export ZERODB_PROFILE_ROOT_KEY="$(
      python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())'
    )"
    python backend/scripts/verify_consent_export_delete_live.py

The root key is generated fresh for the run and never persisted, so the
throwaway profile this script writes is unreadable the moment the shell exits —
belt and braces alongside the delete.

DATA: every interest is obviously synthetic (`test:zzz-live-check-14.2-*`) and
political in no way whatsoever. CLAUDE.md forbids real voter data in a fixture,
a log or test output, and a *live* run is the one place where carelessness would
write it to a real database. The synthetic user id carries the same marker so a
row left behind by a crashed run is greppable.

CLEANUP: the profile row, its key wrap AND every consent row this run wrote are
deleted before the script exits, including on failure, and each is then re-read
to confirm it is gone. The consent rows are cleaned up too even though a real
voter's would be retained — these are a test run's rows, not a person's record,
and the reasoning that keeps a real ledger through a delete (it is the proof the
processing was lawful) has no force for a synthetic voter who does not exist.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import suppress
from typing import Any

from app.profiles import PROFILE_TABLES
from app.profiles.consent import (
    CONSENT_TABLE,
    CURRENT_CONSENT_VERSION,
    LAWFUL_BASIS,
    ConsentLedger,
)
from app.profiles.crypto import (
    KEY_WRAP_TABLE,
    ProfileCipher,
    ProfileKeyring,
    ProfileRootKey,
    RootKeyMissingError,
)
from app.profiles.models import VoterProfileRow
from app.profiles.service import VoterProfileService
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError

#: Marker in every id this script writes, so a row left by a crashed run is
#: obvious and greppable in the live project.
MARKER = "zzz-live-check-14.2"

#: The synthetic voter. Not a person, and carries the marker so the HMAC it
#: produces is reproducible from this file alone during a cleanup hunt.
USER_ID = f"synthetic-{MARKER}"

#: Strings that must never appear in a consent row read raw from the live
#: table. The acceptance criterion for the ledger's privacy is "no interest
#: and no identity", and these are what would prove it violated.
MUST_NOT_APPEAR_IN_CONSENT = (
    f"test:{MARKER}-alpha:v1",
    f"test:{MARKER}-alpha:v1:test-stance",
    f"test:{MARKER}-beta:v1",
    "synthetic consent test note",
    USER_ID,
)

#: Story 14.4's budget. Checked against a real measurement, not assumed.
DELETE_BUDGET_SECONDS = 60.0

logger = logging.getLogger("verify_consent_export_delete_live")


def synthetic_profile() -> dict[str, Any]:
    """An obviously-synthetic profile. Two interests, no politics."""
    return {
        "interests": [
            {
                "topic_id": f"test:{MARKER}-alpha:v1",
                "stance_id": f"test:{MARKER}-alpha:v1:test-stance",
                "tier": "primary",
                "intensity": 3,
                "dealbreaker": True,
                "note": "synthetic consent test note",
            },
            {
                "topic_id": f"test:{MARKER}-beta:v1",
                "stance_id": f"test:{MARKER}-beta:v1:test-stance",
                "tier": "secondary",
                "intensity": 1,
                "dealbreaker": False,
            },
        ],
        "evidence_mode": "blend",
    }


def ensure_tables(client: ZeroDBClient) -> None:
    """Create the three profile tables if the live project lacks them.

    The live project predates story 14.2's `voter_consents`, and a 404 on a
    missing table would read as a consent-recording failure rather than as
    missing infrastructure. `PROFILE_TABLES` is the single list this script,
    every fixture and `app.bootstrap` read, so the table cannot be created
    here under a name the app does not use.
    """
    from app.bootstrap import TABLE_SCHEMAS

    existing = {table.table_name for table in client.list_tables(limit=200)}
    for name in PROFILE_TABLES:
        if name not in existing:
            client.create_table(name, TABLE_SCHEMAS[name])
            logger.info("created missing table %s", name)


def raw_consent_rows(client: ZeroDBClient, profile_ref: str) -> list[dict[str, Any]]:
    """Consent rows for one profile, read through the RAW client.

    Not through `ConsentLedger.history`: a bug in the ledger's own reader (its
    client-side re-filter, its sort) must not be able to hide a bad write, and
    this script's job is to check what is actually in the live table.
    """
    page = client.query_rows(CONSENT_TABLE, filter={"profile_ref": profile_ref}, limit=100)
    return [row.row_data for row in page.rows if row.row_data.get("profile_ref") == profile_ref]


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - a linear script of checks reads best as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        root = ProfileRootKey.from_env()
    except RootKeyMissingError as exc:
        logger.error("FAIL  %s", exc)
        return 1

    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)

    keyring = ProfileKeyring(client=client, root_key=root)
    service = VoterProfileService(client=client, keyring=keyring)
    ledger = ConsentLedger(client)
    row_id = keyring.row_id(USER_ID)

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        ensure_tables(client)

        # --- 0. a clean slate, so a crashed previous run cannot pass this -
        for table in PROFILE_TABLES:
            with suppress(ZeroDBNotFoundError):
                client.delete_row(table, row_id)
        for stale in raw_consent_rows(client, row_id):
            with suppress(ZeroDBNotFoundError):
                client.delete_row(CONSENT_TABLE, str(stale["id"]))
        check(
            "no consent row for this synthetic voter exists before the run",
            raw_consent_rows(client, row_id) == [],
        )

        # --- 1. the consent record lands in the real table ----------------
        service.save(USER_ID, synthetic_profile(), jurisdiction_ids=["us-federal"])

        rows = raw_consent_rows(client, row_id)
        check("a consent row exists live after the save", len(rows) == 1, f"found {len(rows)}")
        if not rows:
            raise RuntimeError("no consent row to inspect")

        record = rows[0]
        check(
            "the live consent row carries exactly the six documented fields",
            set(record)
            == {"id", "profile_ref", "consent_version", "action", "lawful_basis", "recorded_at"},
            f"fields are {sorted(record)}",
        )
        check(
            "the live consent row's id is content-addressed (vc:{64 hex})",
            str(record["id"]).startswith("vc:") and len(str(record["id"])) == 67,
            f"id is {record['id']!r}",
        )
        check(
            "the live consent row names the version the voter was shown",
            record["consent_version"] == CURRENT_CONSENT_VERSION,
            f"version is {record['consent_version']!r}",
        )
        check(
            "the live consent row claims GDPR Article 9(2)(a) explicit consent",
            record["lawful_basis"] == LAWFUL_BASIS,
            f"basis is {record['lawful_basis']!r}",
        )
        check(
            "the live consent row records a UTC timestamp",
            str(record["recorded_at"]).endswith("Z"),
            f"recorded_at is {record['recorded_at']!r}",
        )
        check(
            "the live consent row is keyed by the same HMAC the profile row uses",
            record["profile_ref"] == row_id,
        )

        # The privacy claim, checked against the live bytes rather than the model.
        record_json = json.dumps(record)
        leaked = [secret for secret in MUST_NOT_APPEAR_IN_CONSENT if secret in record_json]
        check(
            "no interest, stance, note or user id appears in the live consent row",
            not leaked,
            f"LEAKED IN CLEAR TEXT: {leaked}" if leaked else "",
        )

        # --- 2. the ledger really appends, live ---------------------------
        # A second save in the same second deliberately collapses to one record
        # (the id is content-addressed over a second-resolution timestamp), so
        # this appends with an explicit later time instead — the behaviour under
        # test is "a later consent appends", and what makes it later is the
        # timestamp, not a sleep.
        ledger.record(
            profile_ref=row_id,
            consent_version=CURRENT_CONSENT_VERSION,
            recorded_at="2026-10-17T12:00:00Z",
        )
        appended = raw_consent_rows(client, row_id)
        check(
            "a later consent appends a second live row rather than replacing the first",
            len(appended) == 2,
            f"found {len(appended)} rows, expected 2",
        )
        check(
            "the earlier consent row still carries its own original timestamp",
            len({str(row["recorded_at"]) for row in appended}) == 2,
            f"timestamps are {sorted(str(row['recorded_at']) for row in appended)}",
        )
        check(
            "re-recording an identical event is idempotent live (no third row)",
            (
                ledger.record(
                    profile_ref=row_id,
                    consent_version=CURRENT_CONSENT_VERSION,
                    recorded_at="2026-10-17T12:00:00Z",
                )
                is not None
                and len(raw_consent_rows(client, row_id)) == 2
            ),
            f"found {len(raw_consent_rows(client, row_id))} rows after a repeat",
        )

        # --- 3. the export round-trips through real ciphertext ------------
        document = service.export_document(USER_ID)
        check("the export document is produced live", document is not None)
        if document is None:
            raise RuntimeError("no export document to inspect")

        sent = synthetic_profile()
        check(
            "the exported interests match what was saved, topic for topic",
            [item["topic_id"] for item in document["profile"]["interests"]]
            == [item["topic_id"] for item in sent["interests"]],
            "the export did not round-trip the same topics",
        )
        check(
            "the exported stances, tiers and intensities match what was saved",
            [
                (item["stance_id"], item["tier"], item["intensity"])
                for item in document["profile"]["interests"]
            ]
            == [(item["stance_id"], item["tier"], item["intensity"]) for item in sent["interests"]],
        )
        check(
            "the exported dealbreaker survives the round trip",
            document["profile"]["interests"][0]["dealbreaker"] is True,
        )
        check(
            "the exported free-text note survives the round trip",
            document["profile"]["interests"][0]["note"] == "synthetic consent test note",
        )
        check(
            "the export is self-describing (version, updated_at, locale, exported_at)",
            document["version"] == 1
            and str(document["updated_at"]).endswith("Z")
            and document["locale"] == "en-US"
            and str(document["exported_at"]).endswith("Z"),
            f"version={document['version']!r} locale={document['locale']!r} "
            f"updated_at={document['updated_at']!r} exported_at={document['exported_at']!r}",
        )
        check(
            "the export carries the jurisdictions the profile was saved under",
            document["jurisdiction_ids"] == ["us-federal"],
            f"jurisdiction_ids are {document['jurisdiction_ids']!r}",
        )
        check(
            "the export carries the consent history that was actually recorded",
            len(document["consent"]) == 2
            and all(item["action"] == "granted" for item in document["consent"])
            and all(
                item["consent_version"] == CURRENT_CONSENT_VERSION for item in document["consent"]
            ),
            f"consent history is {document['consent']!r}",
        )
        check(
            "the export withholds the HMAC handle and the key_ref",
            "key_ref" not in document and row_id not in json.dumps(document),
        )

        # --- 4. delete + shred, measured ----------------------------------
        # Retain the ciphertext BEFORE deleting, so the decisive check below is
        # the real one: can the author of this code, holding the live root key
        # and the exact bytes, read the profile after a delete?
        before_delete = client.get_row(VoterProfileRow.table_name, row_id)
        check("the profile row existed before the delete", before_delete is not None)
        if before_delete is None:
            raise RuntimeError("nothing to delete")
        retained_ciphertext = str(before_delete.row_data["payload"])

        check(
            "the wrapped data key existed before the delete",
            client.get_row(KEY_WRAP_TABLE, row_id) is not None,
        )

        started = time.monotonic()
        service.delete(USER_ID)
        elapsed = time.monotonic() - started
        logger.info("      delete took %.3f seconds against the live API", elapsed)
        check(
            f"the delete completed in under {DELETE_BUDGET_SECONDS:.0f} seconds, live",
            elapsed < DELETE_BUDGET_SECONDS,
            f"took {elapsed:.3f}s",
        )

        check(
            "the profile row is gone from live",
            client.get_row(VoterProfileRow.table_name, row_id) is None,
        )
        check(
            "the wrapped data key is gone from live (the key is shredded)",
            client.get_row(KEY_WRAP_TABLE, row_id) is None,
        )
        check("load returns nothing after the delete", service.load(USER_ID) is None)
        check("export returns nothing after the delete", service.export_document(USER_ID) is None)

        # --- 5. the withdrawal record survives the deletion ---------------
        after_delete = raw_consent_rows(client, row_id)
        withdrawals = [row for row in after_delete if row.get("action") == "withdrawn"]
        check(
            "a withdrawal record exists live after the delete",
            len(withdrawals) == 1,
            f"found {len(withdrawals)} withdrawal rows",
        )
        check(
            "the grant records survive the delete alongside the withdrawal",
            len([row for row in after_delete if row.get("action") == "granted"]) == 2,
            f"found {len(after_delete)} consent rows in total",
        )
        check(
            "the withdrawal names the version that was granted",
            bool(withdrawals) and withdrawals[0]["consent_version"] == CURRENT_CONSENT_VERSION,
        )
        surviving_json = json.dumps(after_delete)
        still_leaked = [secret for secret in MUST_NOT_APPEAR_IN_CONSENT if secret in surviving_json]
        check(
            "the surviving consent rows hold no interest and no user id",
            not still_leaked,
            f"LEAKED IN CLEAR TEXT: {still_leaked}" if still_leaked else "",
        )

        # --- 6. the decisive shred check ----------------------------------
        recovered: str | None = None
        try:
            data_key = keyring.data_key(USER_ID)
        except LookupError:
            recovered = None
        else:
            try:
                recovered = json.dumps(
                    ProfileCipher(data_key).decrypt(retained_ciphertext, aad=row_id)
                )
            except Exception:  # noqa: BLE001 - any failure here is the pass case
                recovered = None
        check(
            "the retained ciphertext cannot be decrypted after the key is shredded, "
            "even with the live root key",
            recovered is None,
            "THE DATA WAS RECOVERED AFTER DELETE — the shred does not work",
        )

        fresh_key = keyring.ensure_data_key(USER_ID)
        try:
            ProfileCipher(fresh_key).decrypt(retained_ciphertext, aad=row_id)
            reopened = True
        except Exception:  # noqa: BLE001 - failure is the pass case
            reopened = False
        check(
            "a newly generated data key for the same voter does not open the old ciphertext",
            not reopened,
            "a replacement key decrypted the old payload",
        )
        keyring.shred(USER_ID)

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - report and still clean up
        failures.append(f"{type(exc).__name__}: {exc}")
        logger.error("FAIL  %s: %s", type(exc).__name__, exc)

    finally:
        # --- 7. cleanup, then confirm it took by re-reading live ----------
        logger.info("cleaning up the live test profile, its key and its consent rows")
        for table in (VoterProfileRow.table_name, KEY_WRAP_TABLE):
            try:
                client.delete_row(table, row_id)
            except ZeroDBNotFoundError:
                logger.info("  already gone: %s", table)
            except ZeroDBError as exc:
                failures.append(f"could not delete from {table}: {exc}")
                logger.error("  FAILED to delete from %s: %s", table, exc)

        try:
            for row in raw_consent_rows(client, row_id):
                with suppress(ZeroDBNotFoundError):
                    client.delete_row(CONSENT_TABLE, str(row["id"]))
        except ZeroDBNotFoundError:
            logger.info("  table %s does not exist; nothing to leave behind", CONSENT_TABLE)
        except ZeroDBError as exc:
            failures.append(f"could not delete from {CONSENT_TABLE}: {exc}")
            logger.error("  FAILED to delete from %s: %s", CONSENT_TABLE, exc)

        # Re-read to confirm the deletes took. A 404 on the TABLE is also
        # "gone" — it means the run failed before `ensure_tables` created it,
        # so there is nothing to leave behind — and must not be reported as a
        # cleanup failure, which would mask the real first failure.
        leftover: list[str] = []
        for table in (VoterProfileRow.table_name, KEY_WRAP_TABLE):
            try:
                if client.get_row(table, row_id) is not None:
                    leftover.append(table)
            except ZeroDBNotFoundError:
                logger.info("  table %s does not exist; nothing to leave behind", table)
        try:
            remaining = raw_consent_rows(client, row_id)
            if remaining:
                leftover.append(f"{CONSENT_TABLE} ({len(remaining)} row(s))")
        except ZeroDBNotFoundError:
            logger.info("  table %s does not exist; nothing to leave behind", CONSENT_TABLE)

        if leftover:
            failures.append(f"live rows left behind in: {', '.join(leftover)}")
            logger.error("FAIL  cleanup left rows in %s", ", ".join(leftover))
        else:
            logger.info(
                "PASS  the profile row, its key wrap and every consent row are confirmed gone"
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
