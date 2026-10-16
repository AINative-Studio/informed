#!/usr/bin/env python3
"""Verify encrypted profile storage against the LIVE ZeroDB project (14.1).

Stories 1.2, 1.3 and 1.4 each learned the same lesson: the in-memory fake
agreed with the code, the code was wrong, and only a live call found out. For
this story the stakes are different in kind — a bug here is not a duplicate row
but a voter's political opinions readable in a database the operator can dump —
so the one claim that cannot be taken on trust is **"the row as actually stored
in ZeroDB is ciphertext"**. Only a live read answers that.

What it proves, in order:

  1. **The stored row is genuinely ciphertext.** The row is read back through
     the raw client (not through the service, which would decrypt it) and every
     synthetic topic id, stance id and note is asserted absent from the full
     JSON of the row — and from a base64-decode of the payload, and from a
     naive XOR/rot13 pass, so "not trivially reversible" is tested rather than
     asserted.
  2. **The row shape is datamodel.md's seven fields**, with nothing extra that
     could carry an opinion.
  3. **The row id is an HMAC**, not the user id, and is deterministic across
     two independent keyring instances.
  4. **Decryption round-trips** — the same interests come back.
  5. **Each save bumps the version**, read live.
  6. **Delete + key shred makes the data permanently unrecoverable.** The
     ciphertext is retained in memory first, then the profile is deleted. The
     script then tries to decrypt the retained ciphertext *with its own
     decrypt function and the real root key* and asserts it fails — which is
     the strongest form of this check available: the author of the code,
     holding the key and the ciphertext, cannot read it.
  7. **Cleanup is confirmed by a live re-read**, not assumed.

RUN IT with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    export ZERODB_PROFILE_ROOT_KEY="$(
      python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())'
    )"
    python backend/scripts/verify_profiles_live.py

The root key is generated fresh for the run and never persisted, so the
throwaway profile this script writes is unreadable the moment the shell exits —
belt and braces alongside the delete.

DATA: every interest is obviously synthetic (`test:zzz-live-check-14.1-*`) and
political in no way whatsoever. CLAUDE.md forbids real voter data in a fixture,
a log or test output, and a *live* run is the one place where carelessness would
write it to a real database. The synthetic user id carries the same marker so a
row left behind by a crashed run is greppable.

CLEANUP: the profile row and its key wrap are deleted before the script exits,
including on failure, and each is then re-read to confirm it is gone. Unlike
the taxonomy and legislator loads, nothing here is reference data meant to
persist.
"""

from __future__ import annotations

import base64
import codecs
import json
import logging
import sys
from typing import Any

from app.profiles import PROFILE_TABLES
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
MARKER = "zzz-live-check-14.1"

#: The synthetic voter. Not a person, and carries the marker so the HMAC it
#: produces is reproducible from this file alone during a cleanup hunt.
USER_ID = f"synthetic-{MARKER}"

#: Strings that must never appear in the stored row. Checked against the row
#: JSON, the decoded payload bytes, and two trivial "encodings" — the
#: acceptance criterion is "not trivially reversible", not merely "not equal".
SECRETS = (
    f"test:{MARKER}-alpha:v1",
    f"test:{MARKER}-alpha:v1:test-stance",
    f"test:{MARKER}-beta:v1",
    "synthetic test note",
    USER_ID,
)

logger = logging.getLogger("verify_profiles_live")


def synthetic_profile(*, with_note: bool = True) -> dict[str, Any]:
    """An obviously-synthetic profile. Two interests, no politics."""
    return {
        "interests": [
            {
                "topic_id": f"test:{MARKER}-alpha:v1",
                "stance_id": f"test:{MARKER}-alpha:v1:test-stance",
                "tier": "primary",
                "intensity": 3,
                "dealbreaker": True,
                "note": "synthetic test note" if with_note else None,
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


def trivially_reversed(raw: bytes) -> list[str]:
    """Cheap "is it actually encrypted" probes over the raw payload.

    A ciphertext that fails any of these is not encrypted, it is obfuscated.
    Not a cryptanalysis — the point is that the three transformations an
    operator would try in five minutes (decode, single-byte XOR, rot13) yield
    nothing recognisable.
    """
    candidates = [raw.decode("latin-1")]
    for key in range(1, 256):
        candidates.append(bytes(byte ^ key for byte in raw).decode("latin-1"))
    candidates.append(codecs.encode(raw.decode("latin-1"), "rot13"))
    return candidates


def ensure_tables(client: ZeroDBClient) -> None:
    """Create the two profile tables if the live project lacks them.

    The live project may predate story 14.1's bootstrap change, and a 404 on a
    missing table would read as an encryption failure rather than as missing
    infrastructure. Creating them is idempotent in the client (story 1.3).
    """
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

    keyring = ProfileKeyring(client=client, root_key=root)
    service = VoterProfileService(client=client, keyring=keyring)
    row_id = keyring.row_id(USER_ID)

    failures: list[str] = []
    retained_ciphertext: str | None = None

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        ensure_tables(client)

        # --- 1. the row as stored is ciphertext --------------------------
        service.save(USER_ID, synthetic_profile(), jurisdiction_ids=["us-federal"])

        # Read through the RAW client, not the service: the service would
        # decrypt, which is exactly the thing not being tested here.
        raw_row = client.get_row(VoterProfileRow.table_name, row_id)
        check("the profile row exists live", raw_row is not None)
        if raw_row is None:
            raise RuntimeError("no row to inspect")

        row_json = json.dumps(raw_row.row_data)
        leaked = [secret for secret in SECRETS if secret in row_json]
        check(
            "no interest, stance, note or user id appears in the stored row",
            not leaked,
            f"LEAKED IN CLEAR TEXT: {leaked}" if leaked else "",
        )

        payload = str(raw_row.row_data.get("payload", ""))
        retained_ciphertext = payload
        decoded = base64.b64decode(payload)
        reversible = [
            secret
            for secret in SECRETS
            for candidate in trivially_reversed(decoded)
            if secret in candidate
        ]
        check(
            "the payload is not trivially reversible (decode / XOR / rot13)",
            not reversible,
            f"recovered by a trivial transform: {sorted(set(reversible))}" if reversible else "",
        )
        check(
            "the payload does not parse as JSON (it is not a plaintext document)",
            not _is_json(decoded),
            "the stored payload decoded straight to JSON",
        )

        # --- 2. the row shape is datamodel.md's seven fields -------------
        check(
            "the stored row carries exactly datamodel.md's seven fields",
            set(raw_row.row_data)
            == {
                "id",
                "version",
                "locale",
                "jurisdiction_ids",
                "updated_at",
                "key_ref",
                "payload",
            },
            f"fields are {sorted(raw_row.row_data)}",
        )
        check(
            "the row records which key scheme sealed it",
            str(raw_row.row_data.get("key_ref", "")).startswith("local-envelope:"),
            f"key_ref is {raw_row.row_data.get('key_ref')!r}",
        )

        # --- 3. the row id is a deterministic HMAC -----------------------
        check(
            "the row id is vp:{64 hex} and contains no user id",
            row_id.startswith("vp:") and len(row_id) == 67 and USER_ID not in row_id,
            f"row id length {len(row_id)}",
        )
        check(
            "a second keyring over the same root key derives the same row id",
            ProfileKeyring(client=client, root_key=root).row_id(USER_ID) == row_id,
        )
        check(
            "a different root key derives a different row id",
            ProfileKeyring(client=client, root_key=ProfileRootKey(bytes(32))).row_id(USER_ID)
            != row_id,
        )

        # --- 4. decryption round-trips -----------------------------------
        loaded = service.load(USER_ID)
        expected = synthetic_profile()
        check(
            "the stored ciphertext decrypts back to the same interests",
            loaded is not None
            and [item.topic_id for item in loaded.interests]
            == [item["topic_id"] for item in expected["interests"]],
            "round trip did not return the same topics",
        )
        check(
            "the dealbreaker and intensity survive the round trip",
            loaded is not None
            and loaded.interests[0].dealbreaker is True
            and loaded.interests[0].intensity == 3,
        )
        check(
            "the free-text note survives the round trip",
            loaded is not None and loaded.interests[0].note == "synthetic test note",
        )

        # --- 5. each save bumps the version, read live -------------------
        second = service.save(USER_ID, synthetic_profile(with_note=False))
        live_version = client.get_row(VoterProfileRow.table_name, row_id)
        check(
            "a second save bumps the version to 2, live",
            second.version == 2
            and live_version is not None
            and live_version.row_data.get("version") == 2,
            f"live version is {live_version.row_data.get('version') if live_version else None}",
        )
        reloaded = service.load(USER_ID)
        check(
            "history records the previous version and not its contents",
            reloaded is not None
            and [entry.version for entry in reloaded.history] == [1]
            and "synthetic test note"
            not in json.dumps([entry.model_dump() for entry in reloaded.history]),
        )

        rows = client.query_rows(VoterProfileRow.table_name, filter={"id": row_id}, limit=100)
        duplicates = sum(1 for row in rows.rows if row.row_data.get("id") == row_id)
        check(
            "re-saving updated the one row rather than appending a second",
            duplicates == 1,
            f"found {duplicates} rows for this profile, expected 1",
        )

        # --- 6. delete + shred makes the data unrecoverable --------------
        # Retain the ciphertext BEFORE deleting, so the check below is the real
        # one: can the author of this code, holding the live root key and the
        # exact bytes, read the profile after a delete?
        before_delete = client.get_row(VoterProfileRow.table_name, row_id)
        assert before_delete is not None
        retained_ciphertext = str(before_delete.row_data["payload"])

        wrap_before = client.get_row(KEY_WRAP_TABLE, row_id)
        check("the wrapped data key existed before the delete", wrap_before is not None)

        service.delete(USER_ID)

        check(
            "the profile row is gone from live",
            client.get_row(VoterProfileRow.table_name, row_id) is None,
        )
        check(
            "the wrapped data key is gone from live (the key is shredded)",
            client.get_row(KEY_WRAP_TABLE, row_id) is None,
        )
        check("load returns nothing after the delete", service.load(USER_ID) is None)

        # The decisive check. Holding the root key and the ciphertext, try to
        # decrypt with this code's own decrypt function.
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

        # A fresh key for the same voter must not open the old ciphertext
        # either: the new DEK is random, not derived from the user id.
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
        # --- 7. cleanup, then confirm it took by re-reading live --------
        logger.info("cleaning up the live test profile and its key")
        for table in (VoterProfileRow.table_name, KEY_WRAP_TABLE):
            try:
                client.delete_row(table, row_id)
            except ZeroDBNotFoundError:
                logger.info("  already gone: %s", table)
            except ZeroDBError as exc:
                failures.append(f"could not delete from {table}: {exc}")
                logger.error("  FAILED to delete from %s: %s", table, exc)

        # Re-read to confirm the delete took. A 404 on the TABLE is also
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
        if leftover:
            failures.append(f"live rows left behind in: {', '.join(leftover)}")
            logger.error("FAIL  cleanup left rows in %s", ", ".join(leftover))
        else:
            logger.info("PASS  the profile row and its key wrap are both confirmed gone")

        client.close()

    if failures:
        logger.error("\n%d check(s) FAILED:", len(failures))
        for failure in failures:
            logger.error("  - %s", failure)
        return 1
    logger.info("\nall live checks passed")
    return 0


def _is_json(raw: bytes) -> bool:
    try:
        json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
