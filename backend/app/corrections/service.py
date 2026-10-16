"""Submitting, tracking and triaging public corrections (story 12.4).

Three jobs, one per acceptance criterion in backlog.md 12.4:

    submit()        anyone reports an error; the 72-hour clock starts
    status_for()    the reporter reads their own status, with no account
    set_status()    someone triages, resolves or rejects it

plus `past_sla()`, which answers datamodel.md's documented main query for this
table ("Open by SLA") and is what an ops dashboard — or a nightly alert — reads
to find the reports the 72-hour commitment has been missed on.

Writes go through `Repository` (story 1.4) rather than the raw client, so the
row is Pydantic-validated before it reaches ZeroDB and the id/natural-key rules
hold. Reads go through the client directly where a filtered page is wanted,
which is the same split `PanelReviewService` uses.

## Why the clock is injectable

Every method takes `now`. Not for convenience: two of this story's acceptance
criteria are *about* time (72 hours to triage, 90 days to erase an email), and
the only alternatives are a test that sleeps for three days or one that reaches
in and rewrites `sla_due_at` after the fact. The second is the trap — it proves
the sweep works against a row the production write path would never produce.
Backdating through the real `submit()` with an injected `now` means the stored
`sla_due_at` is the one production arithmetic computes for that moment.

## What is not here

**No authentication on `set_status`.** Triage is an ops action and there is no
auth layer anywhere in this codebase (see `app.panel_review.router` and
`app.profiles.router` for the same gap stated at the same volume). The
consequence is specific and worth naming: nothing in code stops a caller who
can reach the API from marking a report `rejected`, which is how an
inconvenient correction gets buried. So `set_status` is deliberately NOT
exposed over HTTP by this story — it is a service method, reached by a script
or a future authenticated admin surface. The public router mounts the two
public operations only. That is a real mitigation rather than a deferral: the
abuse path requires code access today, not an HTTP request.

**No notification.** A reporter who gave an email is not written to when their
status changes, because nothing in this repo can send mail (no provider, no
template, no queue) and the retention promise on `contact_email` means the
sending path needs its own thought about what it quotes back. Follow-up,
flagged in the PR. The status link is what makes the criterion "status is
visible to the reporter" true today, and it works for the reporter who gave no
email at all — which an email notification never could.

**No per-reporter rate limit.** Nothing identifying is stored (see
`models.py`), so there is nothing to limit on. prd.md's "rate limits and bot
protection on public endpoints" is the story that handles it, at the edge.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from app.corrections.models import (
    CLOSED_STATUSES,
    INITIAL_STATUS,
    TABLE_NAME,
    CorrectionReceipt,
    CorrectionRow,
    CorrectionStatus,
    CorrectionStatusValue,
)
from app.repositories import Repository
from app.zerodb.client import ZeroDBClient

#: prd.md's triage commitment: "Public correction form on every claim, with an
#: SLA (72 hours to triage)." One definition, used to compute every
#: `sla_due_at`, to answer `past_sla()`, and published to the reporter on both
#: endpoints so the promise they are shown is the one the code enforces.
SLA_HOURS = 72

#: Bytes of entropy in a tracking token. 32 bytes is 256 bits, which
#: `secrets.token_urlsafe` renders as 43 URL-safe characters. The lookup is
#: unauthenticated and un-rate-limited in code, so the token has to be
#: unguessable on its own merits rather than behind a throttle.
TOKEN_BYTES = 32

#: How many rows one page of an SLA or retention scan reads. `corrections` is
#: reporter-driven and should stay small relative to every ingest table, but it
#: is the one table in this codebase whose size is set by the public, so the
#: scans page rather than assuming one read sees everything.
PAGE_SIZE = 200

#: A ceiling on how many pages one scan will walk, so a table that has grown
#: far past expectations cannot turn an ops read into an unbounded loop
#: against a rate-limited API (story 1.2 measured 60 req/min).
MAX_PAGES = 50

#: Where the reporter's status page lives. One definition, used by the router
#: to declare the route and by the receipt to publish the link, so the two
#: cannot drift.
STATUS_PATH_TEMPLATE = "/v1/corrections/{token}"

#: Carries row counts and ids only — never a correction's body, and never a
#: reporter's email or token. A correction is political speech (see
#: `models.py`), so this logger is held to the same rule as `app.profiles`'.
logger = logging.getLogger(__name__)


class UnknownCorrectionError(LookupError):
    """No correction matches that id or that tracking token."""


def token_digest(token: str) -> str:
    """`sha256(token)` as hex — what the row stores in place of the token.

    sha256 with no salt and no stretching, deliberately. The usual argument for
    bcrypt/argon2 is that a password is low-entropy and guessable; a 256-bit
    random token is not, so a slow KDF would buy nothing against an offline
    attacker and would cost a KDF round on every status read. This is the same
    reasoning that makes a plain digest right for an API key and wrong for a
    password.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_correction_id() -> str:
    """datamodel.md's `cor:{uuid}` — a fresh uuid4 per report.

    Random rather than content-addressed: two reporters who independently spot
    the same error must get two rows. See `CorrectionRow`'s docstring.
    """
    return f"cor:{uuid.uuid4()}"


class CorrectionService:
    """Reads and writes `corrections`.

    Holds no per-request state; safe to share across requests like every other
    `*Service` here.
    """

    def __init__(self, client: ZeroDBClient) -> None:
        self._client = client
        self._rows: Repository[CorrectionRow] = Repository(client=client, model=CorrectionRow)

    # --- public: submit ---------------------------------------------------

    def submit(
        self,
        *,
        target_table: str,
        target_id: str,
        body: str,
        contact_email: str | None = None,
        now: datetime | None = None,
    ) -> CorrectionReceipt:
        """Store one public correction report and start its 72-hour clock.

        `target_table` is checked against the real schema; `target_id` is NOT
        resolved. A report about a row that does not exist is accepted on
        purpose — see `app.corrections.models`' module docstring.

        Returns the receipt, which is the only time the tracking token is ever
        disclosed.

        Raises:
            ValueError: `target_table` is not a table in the schema, the body
                is blank, or the contact email is malformed.
        """
        self._require_known_table(target_table)
        submitted = now or datetime.now(UTC)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        row_id = new_correction_id()
        self._rows.upsert(
            {
                "id": row_id,
                "target_table": target_table,
                "target_id": target_id,
                "body": body,
                "contact_email": contact_email,
                "status": INITIAL_STATUS,
                "created_at": _iso(submitted),
                "sla_due_at": _iso(submitted + timedelta(hours=SLA_HOURS)),
                "resolved_at": None,
                "tracking_token_hash": token_digest(token),
            }
        )
        # Id only. Not the body (the report), not the target, not the email,
        # and above all not the token — a logged token is a logged credential.
        logger.info("correction %s received", row_id)
        return CorrectionReceipt(
            id=row_id,
            status=INITIAL_STATUS,
            tracking_token=token,
            status_path=STATUS_PATH_TEMPLATE.format(token=token),
            sla_due_at=_iso(submitted + timedelta(hours=SLA_HOURS)),
            sla_hours=SLA_HOURS,
        )

    # --- public: the reporter's own status --------------------------------

    def status_for_token(self, token: str, *, now: datetime | None = None) -> CorrectionStatus:
        """The status of the correction this token was issued for.

        Looks up by `sha256(token)`, so a stored row cannot be replayed against
        this endpoint and the correction's own id does not open it.

        Returns status only — see `CorrectionStatus` for what is withheld and
        why.

        Raises:
            UnknownCorrectionError: no correction carries that token's digest.
                Deliberately the same error whether the token is malformed,
                expired-looking or simply wrong, so the endpoint's 404 cannot
                be used to distinguish "no such token" from anything else.
        """
        row = self._row_by_token(token)
        return self._as_status(row, now=now or datetime.now(UTC))

    # --- ops: triage ------------------------------------------------------

    def set_status(
        self,
        correction_id: str,
        *,
        status: str,
        now: datetime | None = None,
    ) -> CorrectionRow:
        """Move one correction to a new status, keeping `resolved_at` honest.

        The whole triage/resolution path, and deliberately not exposed over
        HTTP by this story — see the module docstring for why.

        `resolved_at` is managed here rather than by the caller:

          * moving INTO `resolved` or `rejected` sets it, which starts the
            90-day retention clock on `contact_email`;
          * moving back OUT of either (a reopened report) clears it, because a
            correction that is open again has not been resolved, and leaving
            the timestamp set would have the retention sweep erase the email of
            a reporter whose report is still live;
          * re-stating a status a correction already has leaves the original
            timestamp alone, so a double-click does not reset the clock.

        Raises:
            UnknownCorrectionError: no correction with that id.
            ValueError: `status` is not in the vocabulary.
        """
        if status not in _STATUS_VALUES:
            raise ValueError(
                f"{status!r} is not a correction status; expected one of {sorted(_STATUS_VALUES)}"
            )
        current = self.get(correction_id)
        moment = now or datetime.now(UTC)
        resolved_at = current.resolved_at
        if status in CLOSED_STATUSES:
            # Set it only on the transition, so re-stating `resolved` does not
            # move the retention clock forward.
            if current.status not in CLOSED_STATUSES or resolved_at is None:
                resolved_at = _iso(moment)
        else:
            resolved_at = None
        updated = current.model_copy(update={"status": status, "resolved_at": resolved_at})
        self._rows.upsert(updated.model_dump())
        logger.info("correction %s is now %s", correction_id, status)
        return updated

    def get(self, correction_id: str) -> CorrectionRow:
        """One correction by id.

        Raises:
            UnknownCorrectionError: no correction with that id.
        """
        row = self._client.get_row(TABLE_NAME, correction_id)
        if row is None:
            raise UnknownCorrectionError(f"no correction {correction_id!r}")
        return CorrectionRow.model_validate(row.row_data)

    # --- ops: the SLA queue ----------------------------------------------

    def past_sla(self, *, now: datetime | None = None) -> list[CorrectionRow]:
        """Every correction still untriaged past its 72-hour deadline.

        datamodel.md's documented main query for this table ("Open by SLA"),
        and the read an ops dashboard or a nightly alert needs. Oldest deadline
        first, because that is the order to work them in.

        "Untriaged" means `status == "received"` exactly. A correction that has
        been triaged has met the commitment prd.md actually makes (72 hours to
        *triage*, not to fix), and one that is resolved or rejected is closed —
        so neither belongs in a queue of missed commitments, however long it
        has been open. Pinned by three scenarios in the feature file.

        Filtered client-side over a paged scan rather than by a server-side
        filter: datamodel.md's open question records that live ZeroDB's
        `/query` filter silently dropped matching rows, and the comparison here
        (`sla_due_at < now`) is a range anyway, which the filter does not
        express.
        """
        moment = _iso(now or datetime.now(UTC))
        overdue = [
            row
            for row in self.all_corrections()
            if row.status == INITIAL_STATUS and row.sla_due_at < moment
        ]
        overdue.sort(key=lambda row: row.sla_due_at)
        return overdue

    def open_corrections(self) -> list[CorrectionRow]:
        """Every correction not yet closed, oldest first — the triage queue."""
        queue = [row for row in self.all_corrections() if row.status not in CLOSED_STATUSES]
        queue.sort(key=lambda row: row.created_at)
        return queue

    def all_corrections(self) -> list[CorrectionRow]:
        """Every stored correction, read as pages.

        Public because the retention sweep (`app.corrections.retention`) needs
        the same scan and reaching into a private method across modules is how
        two paging policies end up diverging.

        Unfiltered, for the reason `past_sla` documents. Paged with a hard
        ceiling so an unexpectedly large table degrades into a logged partial
        answer rather than an unbounded request loop against a rate-limited
        API.
        """
        rows: list[CorrectionRow] = []
        for page_number in range(MAX_PAGES):
            page = self._client.query_rows(
                TABLE_NAME, limit=PAGE_SIZE, skip=page_number * PAGE_SIZE
            )
            rows.extend(CorrectionRow.model_validate(row.row_data) for row in page.rows)
            if len(page.rows) < PAGE_SIZE:
                return rows
        logger.error(
            "corrections scan stopped at %d rows (%d pages); the SLA queue and the "
            "retention sweep may be reading an incomplete table",
            len(rows),
            MAX_PAGES,
        )
        return rows

    # --- internals --------------------------------------------------------

    def _row_by_token(self, token: str) -> CorrectionRow:
        digest = token_digest(token)
        for row in self.all_corrections():
            # Constant-time compare on the digest. The digests are not secret
            # (they are stored), but comparing them this way costs nothing and
            # removes a timing signal about how many leading characters of a
            # guessed token were right.
            if secrets.compare_digest(row.tracking_token_hash, digest):
                return row
        raise UnknownCorrectionError("no correction matches that tracking token")

    def _as_status(self, row: CorrectionRow, *, now: datetime) -> CorrectionStatus:
        return CorrectionStatus(
            status=row.status,
            submitted_at=row.created_at,
            sla_due_at=row.sla_due_at,
            sla_hours=SLA_HOURS,
            past_sla=row.status == INITIAL_STATUS and row.sla_due_at < _iso(now),
            resolved_at=row.resolved_at,
        )

    def _require_known_table(self, target_table: str) -> None:
        """Refuse a correction about a table that is not in the schema.

        The one check this endpoint does make about its target, and the reason
        it makes it: `target_table` is what an ops queue groups by, so a typo'd
        or invented table name makes the queue unreadable and the report
        unroutable. Checked against `bootstrap.TABLE_SCHEMAS` rather than a
        list in this module, so a table added by a later epic is immediately
        correctable without touching this story.

        `target_id` is deliberately NOT resolved — see `models.py`.
        """
        from app.bootstrap import TABLE_SCHEMAS

        if target_table not in TABLE_SCHEMAS:
            raise ValueError(
                f"{target_table!r} is not a table in the Informed schema; "
                "a correction has to be about something we publish"
            )


#: The vocabulary as a runtime set, for `set_status`'s check. Derived from the
#: `Literal` so the two cannot disagree.
_STATUS_VALUES: frozenset[str] = frozenset(CorrectionStatusValue.__args__)


def _iso(moment: datetime) -> str:
    """An ISO-8601 UTC timestamp, `Z`-suffixed, to microsecond resolution.

    The spelling every other timestamp in this codebase uses
    (`app.profiles.service`, `app.panel_review.service`), which is what makes
    lexicographic string comparison a valid chronological comparison — the
    property `past_sla` and the retention sweep both rest on.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "MAX_PAGES",
    "PAGE_SIZE",
    "SLA_HOURS",
    "STATUS_PATH_TEMPLATE",
    "TOKEN_BYTES",
    "CorrectionService",
    "UnknownCorrectionError",
    "new_correction_id",
    "token_digest",
]
