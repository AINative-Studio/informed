"""Drift repair for denormalized `member_votes` fields (story 16.4).

datamodel.md documents the italicised fields on `member_votes` — `voted_at`,
`vote_type`, `bill_id`, `topic_ids`, `is_bundled` — as copies of the roll
call's own fields, kept that way so scoring reads one table instead of
joining `member_votes` to `roll_calls` on every read (there are no joins in
ZeroDB anyway — see `MemberVote`'s own docstring in `app.repositories.models`
for why the write path does not re-read the roll call on every one of ~600k
writes per Congress). datamodel.md's own integrity check names the consequence
and the fix in one sentence: "any drift triggers a re-copy." This module is
that re-copy.

===============================================================================
WHY A SEPARATE MODULE FROM app.integrity.checks
===============================================================================

`app.integrity.checks.check_member_votes_drift` is check 4's *detection* half
and is read-only, by design — a nightly integrity check's job is to report,
not to mutate data while an operator is trying to understand what happened.
This module is the *repair*: given the same drift, it writes the corrected
values back. The two share `app.integrity.checks.drifted_fields` so "what
counts as drift" cannot disagree between them, and this module's own report
(`RepairResult`) is deliberately shaped like `SweepResult` from
`app.corrections.retention` — scanned/repaired counts and a list of ids, never
sensitive content — because this is the same genre of operation: a periodic,
idempotent, read-then-fix pass over one table, run by the same scheduler
(`app.scheduling.jobs`) as the email retention sweep.

===============================================================================
WHY THE WRITE IS A PATCH, NOT A REWRITE
===============================================================================

Like `app.corrections.retention._erase_contact_email`, the repair writes
through `ZeroDBClient.update_row`, which read-merge-writes, rather than
`Repository.upsert`, which would replace the row wholesale. The reason is the
same: a `member_votes` row's own fields (`position`, `roll_call_id`,
`person_id`) are not this repair's business, and a wholesale replacement built
from only the fields this module knows about would silently drop them. Patching
only the fields that actually drifted is also what makes the result's claim
("1 row repaired, fields: vote_type") precisely true — nothing else about the
row was touched, which the feature file's "leaves undrifted fields ... and the
vote's own position untouched" scenario pins.

===============================================================================
WHY RUNNING TWICE IS A NO-OP
===============================================================================

The repair is a pure function of what is currently stored: it reads every
`member_votes` row, compares it to its referenced `roll_calls` row, and
rewrites only the fields that disagree. Once rewritten, a row's denormalized
fields equal the roll call's by construction, so a second run finds no drift
and writes nothing. No cursor, no watermark, nothing outside ZeroDB that a
missed run could desynchronize — exactly the property
`app.corrections.retention`'s module docstring names as what makes a sweep
"safe to schedule later", and the same property this module is tested for
directly (`drift_repair.feature`'s "idempotent" scenario).

===============================================================================
WHAT THIS DOES NOT GUARANTEE
===============================================================================

Not atomic: each drifted row is one `update_row` call, and a crash mid-run
leaves some rows repaired and others not — which is simply "not yet repaired",
not a corrupted state, and the next scheduled run (or check 4's own nightly
report) finds exactly those remaining rows. Nothing here is a transaction,
consistent with every other write path in this codebase (`app.repositories.base`'s
module docstring is explicit that nothing is atomic for the same reason: ZeroDB
offers no compare-and-set).

Also not addressed here: a `member_votes` row whose `roll_call_id` does not
resolve to any stored `roll_calls` row at all. That is check 1's territory
(`app.integrity.checks.check_references`) — there is no "correct" denormalized
value to copy from a roll call that does not exist, so this repair skips such
a row rather than guessing, exactly as `check_member_votes_drift` does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.integrity.checks import drifted_fields, paged_rows
from app.repositories.models import MemberVote, RollCall
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DriftRecord:
    """One `member_votes` row's drift, and what was (or would be) re-copied.

    `fields` names only what actually disagreed — the same precision
    `SweepResult.erased_ids` holds to ids only, never swept content.
    """

    member_vote_id: str
    roll_call_id: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class RepairResult:
    """What one drift-repair run did. Shaped like `SweepResult` deliberately —
    same genre of periodic idempotent repair, same report shape.
    """

    scanned: int = 0
    repaired: int = 0
    #: Drift found and fixed by THIS run. Empty on a second run over the same
    #: data — see the module docstring for why that is the idempotence proof.
    records: tuple[DriftRecord, ...] = field(default_factory=tuple)
    #: member_votes rows whose roll_call_id does not resolve to a stored
    #: roll_calls row — check 1's territory, skipped here rather than guessed.
    dangling: int = 0

    def summary(self) -> str:
        return (
            f"scanned {self.scanned} member_votes row(s): repaired {self.repaired}, "
            f"{self.dangling} with a dangling roll_call_id (skipped)"
        )


def repair_member_votes_drift(
    client: ZeroDBClient, *, dry_run: bool = False
) -> RepairResult:
    """Re-copy every drifted denormalized field on `member_votes`.

    Idempotent: a second run over the same data repairs nothing and reports
    `repaired == 0`. See the module docstring for why.

    Args:
        client: the ZeroDB client to read and write through.
        dry_run: report what would be repaired and write nothing, mirroring
            `sweep_expired_contact_emails`'s own `dry_run` — useful for a
            first run against a live project where "how many rows is this
            about to rewrite" is worth answering before rewriting them.

    Returns:
        A `RepairResult`. `records` holds row and roll-call ids and field
        NAMES only, never the values themselves — this is vote data, not
        voter data, so there is no privacy concern, but the convention of
        reporting ids-and-shape rather than content is kept consistent with
        every other periodic-repair report in this codebase.
    """
    member_votes = paged_rows(client, MemberVote.table_name)
    roll_calls_by_id: dict[str, dict[str, Any]] = {}

    records: list[DriftRecord] = []
    dangling = 0

    for vote_row in member_votes:
        roll_call_id = vote_row.get("roll_call_id")
        if not roll_call_id:
            continue
        roll_call_id = str(roll_call_id)

        if roll_call_id not in roll_calls_by_id:
            roll_call = client.get_row(RollCall.table_name, roll_call_id)
            if roll_call is None:
                dangling += 1
                continue
            roll_calls_by_id[roll_call_id] = roll_call.row_data

        drifted = drifted_fields(vote_row, roll_calls_by_id[roll_call_id])
        if not drifted:
            continue

        vote_id = str(vote_row.get("id"))
        patch = {field_name: roll_call_value for field_name, (_, roll_call_value) in drifted.items()}
        if not dry_run:
            client.update_row(MemberVote.table_name, vote_id, patch)
            logger.info(
                "repaired drift on member_votes %s: %s", vote_id, sorted(drifted)
            )
        records.append(
            DriftRecord(
                member_vote_id=vote_id,
                roll_call_id=roll_call_id,
                fields=tuple(sorted(drifted)),
            )
        )

    result = RepairResult(
        scanned=len(member_votes),
        repaired=len(records),
        records=tuple(records),
        dangling=dangling,
    )
    logger.info(
        "member_votes drift repair (%s): %s",
        "dry run" if dry_run else "live",
        result.summary(),
    )
    return result


__all__ = ["DriftRecord", "RepairResult", "repair_member_votes_drift"]
