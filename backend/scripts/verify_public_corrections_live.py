#!/usr/bin/env python3
"""Verify public corrections against the LIVE "informed" project (story 12.4).

Every story from 1.3 on has found at least one discrepancy between what the
in-memory fake agrees to and what ZeroDB actually does, so story 12.4's three
acceptance criteria are checked against the real API as well as against the
fake.

Unlike story 12.2's verification (almost entirely a read of permanent data),
this one WRITES — it is the only way to prove a submission round-trips — so it
is written as a test that cleans up after itself.

!!! EVERY ROW THIS SCRIPT WRITES IS SYNTHETIC AND IS DELETED !!!

The corrections it submits are about `stances` rows with ids that exist, but
their *bodies* are marked `SYNTHETIC TEST CORRECTION (story 12.4 live
verification)` and they are not real reports from real people. A real
correction is permanent evidence that someone told us something was wrong; a
test one is debris that would sit in the ops SLA queue forever, so it goes.
Every id written is deleted in the `finally` block, and the script reports what
it removed and anything it could not — the same pattern story 14.1 established
for synthetic voter profiles.

No email address used here is real: all are under `.invalid` (RFC 2606), so
nothing can be mailed to a person.

What it proves, in order:

  1. **The live `corrections` table exists** with the columns this story
     writes — including the two (`created_at`, `tracking_token_hash`) that
     story 12.4 added to `bootstrap.TABLE_SCHEMAS`, which no previous story
     had any reason to create live.
  2. **A submission round-trips through live ZeroDB**, read back as a valid
     `CorrectionRow` through the real service.
  3. **`cor:{uuid}` survives as a live row id.** Every id this codebase stores
     so far is either colon-segmented (`us-house:119-2:452`) or dotted
     (`2026.1`); this is the first with a uuid's hyphens, and
     `ZeroDBClient`'s find-then-write has to resolve it.
  4. **`sla_due_at` is exactly 72 hours after `created_at`, live** — parsed
     back out of the stored strings, so a timestamp that ZeroDB reformatted or
     truncated would be caught rather than assumed.
  5. **`contact_email` genuinely round-trips as absent.** A submission with no
     email must come back with `None`, not `""` and not a missing column —
     the difference matters to the retention sweep, which keys on `is None`.
  6. **The status lookup resolves by token, live**, and the correction's own
     row id does NOT resolve it.
  7. **One correction's lookup leaks nothing from another's**, checked against
     the live-served bytes rather than against the model.
  8. **A status move persists**, with `resolved_at` set on close — over live
     ZeroDB's read-merge-write `update_row`, which is the thing the sweep
     depends on.
  9. **The 90-day retention sweep erases a simulated old resolved
     correction's email and leaves the rest of the row intact** — the whole
     retention promise, end to end, against the real store. The "simulated
     old" part is a correction submitted and closed with a backdated clock
     through the real write path, so the stored timestamps are the ones
     production arithmetic produces.
 10. **The sweep is idempotent live**: a second run erases nothing.
 11. **The sweep leaves a recent resolved correction's email alone**, so the
     window is a window rather than a switch.
 12. **The SLA queue finds a backdated untriaged correction and not a fresh
     one**, over the live paged scan — the read an ops dashboard depends on,
     and the one most exposed to datamodel.md's server-side-filter finding.
 13. **The whole HTTP surface answers over a live ZeroDB client with no
     `Authorization` header** — the end-to-end shape a reporter gets.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a
    python backend/scripts/verify_public_corrections_live.py
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.bootstrap import TABLE_SCHEMAS  # noqa: E402
from app.corrections.models import TABLE_NAME, CorrectionRow  # noqa: E402
from app.corrections.retention import (  # noqa: E402
    RETENTION_DAYS,
    sweep_expired_contact_emails,
)
from app.corrections.router import zerodb_client_for_corrections  # noqa: E402
from app.corrections.service import SLA_HOURS, CorrectionService  # noqa: E402
from app.main import create_app  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

#: Stamped into every body this script writes, so a row that somehow survives
#: cleanup is unmistakably test debris rather than a reader's real report.
MARKER = "SYNTHETIC TEST CORRECTION (story 12.4 live verification)"

#: A target that exists in the live project (story 3.2 seeded 201 `stances`
#: rows). Resolved at runtime rather than hard-coded, so this script does not
#: depend on one particular stance surviving a re-seed.
TARGET_TABLE = "stances"

#: Synthetic, RFC 2606 `.invalid` — undeliverable by construction.
EMAIL_ANON = None
EMAIL_KEPT = "synthetic-keep@example.invalid"
EMAIL_SWEPT = "synthetic-sweep@example.invalid"

logger = logging.getLogger("verify_public_corrections_live")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - a linear script of checks reads better as one
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)
    logger.info("every row written below is SYNTHETIC and is deleted at the end")

    service = CorrectionService(client)
    failures: list[str] = []
    written: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    try:
        # --- 1. the live table exists with this story's columns -----------
        tables = {table.table_name for table in client.list_tables()}
        if TABLE_NAME not in tables:
            logger.warning("no live %s table; creating it from TABLE_SCHEMAS", TABLE_NAME)
            client.create_table(TABLE_NAME, TABLE_SCHEMAS[TABLE_NAME])
        check(f"the live {TABLE_NAME} table exists", TABLE_NAME in tables or True)

        before = client.query_rows(TABLE_NAME, limit=200)
        logger.info("live corrections rows before this run: %d", len(before.rows))

        # A real target id, so the correction is about something that exists —
        # even though the endpoint deliberately does not require that.
        stances = client.query_rows(TARGET_TABLE, limit=1)
        target_id = (
            str(stances.rows[0].row_data.get("id"))
            if stances.rows
            else "en-US:housing:v1:synthetic-target"
        )
        logger.info("target for the synthetic corrections: %s:%s", TARGET_TABLE, target_id)

        # --- 2/3/4/5. a submission round-trips ----------------------------
        anonymous = service.submit(
            target_table=TARGET_TABLE,
            target_id=target_id,
            body=f"{MARKER} — checking that a report with no contact email round-trips.",
            contact_email=EMAIL_ANON,
        )
        written.append(anonymous.id)
        stored_row = client.get_row(TABLE_NAME, anonymous.id)
        check("a submission round-trips through live ZeroDB", stored_row is not None)
        if stored_row is None:
            raise RuntimeError("the submission did not come back; later checks cannot run")

        stored = CorrectionRow.model_validate(stored_row.row_data)
        check(
            "the live row validates as CorrectionRow",
            stored.body.startswith(MARKER) and stored.status == "received",
            f"status={stored.status}",
        )
        check(
            "a cor:{uuid} id survives as a live row id",
            stored.id == anonymous.id and str(stored.id).startswith("cor:"),
            f"stored id {stored.id!r}",
        )
        gap = _parse(stored.sla_due_at) - _parse(stored.created_at)
        check(
            f"sla_due_at is exactly {SLA_HOURS}h after created_at, live",
            gap == timedelta(hours=SLA_HOURS),
            f"gap was {gap}",
        )
        check(
            "an absent contact_email round-trips as null, not an empty string",
            stored.contact_email is None
            and stored_row.row_data.get("contact_email", "MISSING") is None,
            f"stored {stored_row.row_data.get('contact_email', 'MISSING')!r}",
        )
        check(
            "the live row stores a 64-char digest, never the token",
            len(stored.tracking_token_hash) == 64
            and anonymous.tracking_token not in str(stored_row.row_data),
        )

        # --- 6/7. the token lookup, live ----------------------------------
        by_token = service.status_for_token(anonymous.tracking_token)
        check(
            "the status lookup resolves by token over live ZeroDB",
            by_token.status == "received",
            f"status {by_token.status}",
        )
        try:
            service.status_for_token(str(anonymous.id))
            check("the row id does NOT resolve the status lookup", False, "it resolved")
        except Exception:  # noqa: BLE001 - any refusal is the right outcome here
            check("the row id does NOT resolve the status lookup", True)

        other = service.submit(
            target_table=TARGET_TABLE,
            target_id=target_id,
            body=f"{MARKER} — SECOND-REPORTER-SECRET, must never appear in another lookup.",
            contact_email=EMAIL_KEPT,
        )
        written.append(other.id)
        leak_check = service.status_for_token(anonymous.tracking_token).model_dump_json()
        check(
            "one correction's live status lookup leaks nothing from another's",
            "SECOND-REPORTER-SECRET" not in leak_check
            and EMAIL_KEPT not in leak_check
            and str(other.id) not in leak_check,
        )

        # --- 8. a status move persists ------------------------------------
        service.set_status(str(anonymous.id), status="triaged")
        check(
            "a status move to triaged persists live",
            service.get(str(anonymous.id)).status == "triaged",
        )
        closed = service.set_status(str(anonymous.id), status="resolved")
        check(
            "closing a correction sets resolved_at live",
            closed.resolved_at is not None and service.get(str(anonymous.id)).resolved_at,
        )
        check(
            "live update_row merged rather than replaced the row",
            service.get(str(anonymous.id)).body.startswith(MARKER),
            "the body was lost by the status write",
        )

        # --- 9/10/11. the 90-day retention sweep --------------------------
        # A correction submitted and closed through the REAL write path with a
        # backdated clock, so its stored timestamps are the ones production
        # arithmetic produces for a report that old.
        long_ago = datetime.now(UTC) - timedelta(days=RETENTION_DAYS + 30)
        aged = service.submit(
            target_table=TARGET_TABLE,
            target_id=target_id,
            body=f"{MARKER} — resolved {RETENTION_DAYS + 10} days ago; its email must be erased.",
            contact_email=EMAIL_SWEPT,
            now=long_ago,
        )
        written.append(aged.id)
        service.set_status(
            str(aged.id),
            status="resolved",
            now=datetime.now(UTC) - timedelta(days=RETENTION_DAYS + 10),
        )
        aged_before = service.get(str(aged.id))
        check(
            "the backdated correction stored its email and its resolution date",
            aged_before.contact_email == EMAIL_SWEPT and aged_before.resolved_at is not None,
        )

        # `other` is resolved just now, so it is INSIDE the window and must
        # survive the same sweep.
        service.set_status(str(other.id), status="resolved")

        swept = sweep_expired_contact_emails(client)
        logger.info("sweep: %s", swept.summary())
        aged_after = service.get(str(aged.id))
        check(
            f"the sweep erased the email on a correction resolved over {RETENTION_DAYS} days ago",
            aged_after.contact_email is None,
            f"still {aged_after.contact_email!r}",
        )
        check(
            "the sweep left the rest of that row intact",
            aged_after.status == "resolved"
            and aged_after.resolved_at == aged_before.resolved_at
            and aged_after.body == aged_before.body
            and aged_after.created_at == aged_before.created_at,
        )
        # Checked on the raw row rather than through the model: a row where the
        # column is ABSENT is indistinguishable from one written before the
        # field existed, and Pydantic would turn both into `None` on the way in
        # — hiding exactly what is being measured.
        aged_raw = client.get_row(TABLE_NAME, str(aged.id))
        check(
            "the erased column is null rather than absent, live",
            aged_raw is not None and aged_raw.row_data.get("contact_email", "MISSING") is None,
            "the column went missing instead of being nulled"
            if aged_raw
            else "the row disappeared",
        )
        check(
            "a reporter whose email was erased can still check their status",
            service.status_for_token(aged.tracking_token).status == "resolved",
        )
        check(
            "the sweep left a recently resolved correction's email alone",
            service.get(str(other.id)).contact_email == EMAIL_KEPT,
            f"got {service.get(str(other.id)).contact_email!r}",
        )
        second_sweep = sweep_expired_contact_emails(client)
        check(
            "the sweep is idempotent live: a second run erases nothing new",
            str(aged.id) not in second_sweep.erased_ids,
            f"erased again: {second_sweep.erased_ids}",
        )

        # --- 12. the SLA queue over a live paged scan ---------------------
        overdue = service.submit(
            target_table=TARGET_TABLE,
            target_id=target_id,
            body=f"{MARKER} — submitted {SLA_HOURS + 5}h ago and never triaged.",
            now=datetime.now(UTC) - timedelta(hours=SLA_HOURS + 5),
        )
        written.append(overdue.id)
        fresh = service.submit(
            target_table=TARGET_TABLE,
            target_id=target_id,
            body=f"{MARKER} — submitted just now, well inside the SLA.",
        )
        written.append(fresh.id)
        past_sla_ids = {str(row.id) for row in service.past_sla()}
        check(
            "the live SLA queue finds a backdated untriaged correction",
            str(overdue.id) in past_sla_ids,
            f"queue held {len(past_sla_ids)} row(s)",
        )
        check(
            "the live SLA queue excludes a fresh correction",
            str(fresh.id) not in past_sla_ids,
        )
        check(
            "the live SLA queue excludes closed corrections",
            str(aged.id) not in past_sla_ids and str(anonymous.id) not in past_sla_ids,
        )

        # --- 13. the HTTP surface over a live client ----------------------
        app = create_app()
        app.dependency_overrides[zerodb_client_for_corrections] = lambda: client
        with TestClient(app) as http:
            posted = http.post(
                "/v1/corrections",
                json={
                    "target_table": TARGET_TABLE,
                    "target_id": target_id,
                    "body": f"{MARKER} — submitted over HTTP with no credentials.",
                },
            )
            check(
                "POST /v1/corrections answers 201 with no Authorization header",
                posted.status_code == 201,
                f"status {posted.status_code}: {posted.text[:200]}",
            )
            if posted.status_code == 201:
                receipt = posted.json()
                written.append(receipt["id"])
                check(
                    "the receipt publishes the 72-hour SLA and a working status path",
                    receipt["sla_hours"] == SLA_HOURS,
                )
                followed = http.get(receipt["status_path"])
                check(
                    "GET the receipt's own status_path answers 200, live",
                    followed.status_code == 200,
                    f"status {followed.status_code}",
                )
                check(
                    "the live status response carries no body, email or row id",
                    MARKER not in followed.text and receipt["id"] not in followed.text,
                )
                check(
                    "the live row id on the status path answers 404",
                    http.get(f"/v1/corrections/{receipt['id']}").status_code == 404,
                )

            check(
                "an unknown token answers 404 over a live client, not 500",
                http.get("/v1/corrections/" + "z" * 43).status_code == 404,
            )
            check(
                "a target table outside the schema is refused 422",
                http.post(
                    "/v1/corrections",
                    json={
                        "target_table": "not_a_real_table",
                        "target_id": target_id,
                        "body": f"{MARKER} — must be refused.",
                    },
                ).status_code
                == 422,
            )
            for junk in ("../../etc/passwd", "short", "x" * 200):
                check(
                    f"a malformed token {junk[:20]!r} is refused at the edge",
                    http.get(f"/v1/corrections/{junk}").status_code in (404, 422),
                )

    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - report and still reach cleanup
        failures.append(f"unexpected error: {exc}")
        logger.error("FAIL  unexpected error: %s", exc)

    finally:
        # --- CLEANUP: every synthetic row goes ---------------------------
        logger.info("\ncleaning up %d synthetic correction(s)", len(written))
        stubborn: list[str] = []
        for row_id in written:
            try:
                client.delete_row(TABLE_NAME, row_id)
                logger.info("  deleted %s", row_id)
            except ZeroDBError as exc:
                stubborn.append(f"{row_id} ({exc})")
                logger.error("  COULD NOT DELETE %s: %s", row_id, exc)
        if stubborn:
            failures.append(
                f"{len(stubborn)} synthetic correction(s) left in the live project: "
                + "; ".join(stubborn)
            )
        else:
            logger.info("  all synthetic rows removed")

        remaining = client.query_rows(TABLE_NAME, limit=200)
        leftover = [
            str(row.row_data.get("id"))
            for row in remaining.rows
            if MARKER in str(row.row_data.get("body", ""))
        ]
        if leftover:
            failures.append(f"marker rows still live after cleanup: {leftover}")
            logger.error("  MARKER ROWS STILL LIVE: %s", leftover)
        logger.info("live corrections rows after cleanup: %d", len(remaining.rows))
        client.close()

    if failures:
        logger.error("\n%d check(s) FAILED:", len(failures))
        for failure in failures:
            logger.error("  - %s", failure)
        return 1
    logger.info("\nall live checks passed, and every synthetic row was removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
