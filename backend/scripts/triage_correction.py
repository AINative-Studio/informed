#!/usr/bin/env python3
"""The ops side of public corrections: see the queue, move a status (12.4).

backlog.md 12.4 is "so that mistakes get fixed", and prd.md commits to triage
within 72 hours. This is the path a human uses to meet that commitment until
there is an authenticated admin surface.

!!! WHY THIS IS A SCRIPT AND NOT AN ENDPOINT !!!

There is no authentication anywhere in this codebase (see
`app/panel_review/router.py` and `app/profiles/router.py` for the same gap
stated at the same volume). An unauthenticated `PATCH /v1/corrections/{id}`
would let anyone who can reach the API mark an inconvenient correction
`rejected` — which is precisely how a correction about a candidate gets buried,
on the one feature whose whole purpose is that mistakes cannot be buried.

So story 12.4 mounts the two PUBLIC operations only (submit, and the reporter's
own status read) and leaves triage here, where it requires code access rather
than an HTTP request. `app/corrections/service.py` carries the full argument.
Exposing it over HTTP is the follow-up, and it needs the auth story first.

Run it with the live credentials sourced into the shell — never passed on the
command line, never printed:

    set -a; . local.env; set +a

    # The 72-hour SLA queue: what the commitment has been missed on
    python backend/scripts/triage_correction.py --past-sla

    # Everything still open, oldest first
    python backend/scripts/triage_correction.py --queue

    # One report in full, including the reporter's email if they gave one
    python backend/scripts/triage_correction.py --show cor:3f1b…

    # Move a status
    python backend/scripts/triage_correction.py --id cor:3f1b… --status triaged
    python backend/scripts/triage_correction.py --id cor:3f1b… --status resolved
    python backend/scripts/triage_correction.py --id cor:3f1b… --status rejected

## What this prints, and the one place it prints an email

`--queue` and `--past-sla` print ids, targets, statuses and deadlines — never
a body and never an email, because those listings are the thing most likely to
be pasted into a ticket or a chat.

`--show` prints one correction in full, the `contact_email` included. That is
the point of it: a human triaging a report needs to read the report, and
needs the address if they are going to reply. It is the one deliberate
disclosure in this story's tooling, it is per-id and opt-in, and the output is
for the operator's screen — not for a ticket, a log aggregator or a commit.
Everything else here is built so that `--show` is the only way an address
reaches a terminal.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.corrections.models import CorrectionRow, CorrectionStatusValue  # noqa: E402
from app.corrections.service import (  # noqa: E402
    SLA_HOURS,
    CorrectionService,
    UnknownCorrectionError,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402

logger = logging.getLogger("triage_correction")

STATUSES = sorted(CorrectionStatusValue.__args__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--past-sla",
        action="store_true",
        help=(
            f"List corrections still untriaged past their {SLA_HOURS}-hour "
            "deadline, oldest deadline first. Ids and deadlines only."
        ),
    )
    parser.add_argument(
        "--queue",
        action="store_true",
        help="List every correction not yet closed, oldest submission first.",
    )
    parser.add_argument(
        "--show",
        metavar="CORRECTION_ID",
        default=None,
        help=(
            "Print one correction in full, INCLUDING the reporter's contact "
            "email. For a human reading a report on their own screen."
        ),
    )
    parser.add_argument(
        "--id",
        metavar="CORRECTION_ID",
        default=None,
        help="The correction to move, used with --status.",
    )
    parser.add_argument(
        "--status",
        choices=STATUSES,
        default=None,
        help=(
            "The status to move it to. `triaged` is what the 72-hour "
            "commitment asks for; `resolved` and `rejected` close it and start "
            "the 90-day clock on the reporter's email."
        ),
    )
    return parser.parse_args()


def _summarise(row: CorrectionRow) -> str:
    """One line per correction. No body, no email — see the module docstring."""
    return (
        f"  {row.id}  {row.status:<9} target={row.target_table}:{row.target_id}  "
        f"submitted={row.created_at}  triage_due={row.sla_due_at}"
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    if not any((args.past_sla, args.queue, args.show, args.status)):
        logger.error("nothing to do: pass --past-sla, --queue, --show or --id/--status")
        return 2
    if bool(args.status) != bool(args.id):
        logger.error("--status and --id go together: name the correction to move")
        return 2

    client = ZeroDBClient.from_env()
    logger.info("project: %s", client.project_id)
    service = CorrectionService(client)

    try:
        if args.past_sla:
            overdue = service.past_sla()
            logger.info("%d correction(s) past the %d-hour triage SLA:", len(overdue), SLA_HOURS)
            for row in overdue:
                logger.info("%s", _summarise(row))

        if args.queue:
            queue = service.open_corrections()
            logger.info("%d open correction(s), oldest first:", len(queue))
            for row in queue:
                logger.info("%s", _summarise(row))

        if args.show:
            row = service.get(args.show)
            logger.info("correction %s", row.id)
            logger.info("  status:       %s", row.status)
            logger.info("  target:       %s / %s", row.target_table, row.target_id)
            logger.info("  submitted:    %s", row.created_at)
            logger.info("  triage due:   %s", row.sla_due_at)
            logger.info("  resolved at:  %s", row.resolved_at or "—")
            # The one deliberate disclosure in this tooling; see the docstring.
            logger.info("  contact:      %s", row.contact_email or "(none given)")
            logger.info("  report:       %s", row.body)

        if args.status and args.id:
            updated = service.set_status(args.id, status=args.status)
            logger.info("correction %s is now %s", updated.id, updated.status)
            if updated.resolved_at:
                logger.info(
                    "  closed at %s; any contact email is erased %d days after that "
                    "(scripts/sweep_correction_emails.py)",
                    updated.resolved_at,
                    90,
                )
    except UnknownCorrectionError as exc:
        logger.error("FAILED: %s", exc)
        return 1
    except ZeroDBError as exc:
        logger.error("FAILED: ZeroDB error: %s", exc)
        return 1
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
