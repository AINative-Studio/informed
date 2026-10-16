"""The stored `corrections` row and the public wire shapes (story 12.4).

prd.md, "Trust, neutrality, privacy and safety": "Public correction form on
every claim, with an SLA (72 hours to triage)."

datamodel.md documents the table as:

    | `corrections` | `cor:{uuid}` | target_table, target_id, body,
      contact_email (optional), status, sla_due_at, resolved_at | Open by SLA |

and its retention table adds: `corrections.contact_email` is retained
"90 days after resolution", "Optional field."

Three things that table leaves open, decided here and argued below: what
`status` may hold, how an anonymous reporter checks it, and what "created_at"
is (the SLA is defined relative to it but it is not in the field list).

===============================================================================
THE STATUS VOCABULARY
===============================================================================

datamodel.md enumerates the vocabularies for `verdict` ("kept, partial,
broken, no_opportunity, absent, not_testable") and for `qa_status` ("pending,
approved, rejected") in prose beneath the governance tables. It gives
`corrections.status` no values at all. So this story defines them:

    received  — submitted, not yet looked at. The state every correction is
                born in, and the only one the 72-hour clock runs against.
    triaged   — a human has read it and decided what it is. This is the state
                prd.md's SLA actually promises: "72 hours to triage", not 72
                hours to fix. Reaching it stops the clock.
    resolved  — acted on and closed. Sets `resolved_at`, which starts the
                90-day retention clock on `contact_email`.
    rejected  — closed without a change: not an error, out of scope, or
                unreproducible. Also sets `resolved_at`, because the
                reporter's email has the same retention promise whichever way
                their report was closed.

**`received` rather than `pending`**, deliberately, even though `pending` is
the more obvious word: datamodel.md already spends `pending` on `qa_status`,
where it means "a verdict awaiting human QA". One token meaning "nobody has
looked at this yet" in one table and "awaiting approval" in another is the
kind of near-miss that survives review and then misleads whoever writes the
ops dashboard. `received` also says the true thing to a reporter reading their
status page: we have it.

**`duplicate` is deliberately NOT a status.** It was the obvious fifth value
and it is a *reason* for closing, not a state: a duplicate is `rejected` (or
`resolved`, if the original was fixed), and recording which original it
duplicates needs a field datamodel.md does not have. Adding a status for it
would mean the ops dashboard has to treat two values as closed-without-change,
and the retention sweep has to know about a third terminal state. When there
is a story that needs it, it belongs as a `resolution_reason`, not here.

===============================================================================
HOW AN ANONYMOUS REPORTER CHECKS STATUS
===============================================================================

backlog.md 12.4: "status is visible to the reporter". There are no accounts
for anonymous reporters — prd.md's "Anonymous mode: full use without an
account" is a product principle, not an exception — and `contact_email` is
optional, so the reporter may have given us no way to reach them at all. The
status therefore has to be reachable from something the reporter holds.

What a submission returns is a **tracking token**: 32 bytes from
`secrets.token_urlsafe`, handed to the reporter once and never stored in
clear. What the row stores is `sha256(token)`, and `GET /v1/corrections/
{token}` hashes what it is given and looks the row up by that digest.

Three properties that come from doing it this way rather than the obvious
alternatives:

  * **The row id does not open the lookup.** datamodel.md's id is
    `cor:{uuid}`, and a uuid4 is unguessable — so why not look up by id?
    Because ids travel: they appear in an ops dashboard, in a log line, in a
    screenshot in a ticket, in this story's own verification output. The
    moment one leaks, the body and status of a stranger's report leak with it.
    A separate secret means the id can be as public as every other id in this
    codebase.
  * **A database dump does not yield the tokens.** Storing the digest is the
    same posture `voter_profiles` takes with its HMAC'd id (story 14.1): the
    stored value cannot be replayed against the endpoint.
  * **Nothing has to be revoked when the email is erased 90 days after
    resolution.** The token is not derived from the email and does not expire
    with it, so a reporter who gave no email at all gets exactly the same
    status page as one who did — which is what makes "optional" true rather
    than nominal.

The honest limitations, both flagged in this story's PR:

  * **A lost token cannot be recovered.** There is nothing to authenticate the
    reporter with, so there is no reset path. Mitigation available today: the
    response tells the reporter to keep the link. A reporter who gave an email
    can be mailed their status by a human; one who gave none cannot, and that
    is the price of anonymity rather than a bug.
  * **The token is a bearer secret in a URL path**, so it reaches server logs
    and browser history like any bearer-in-URL scheme. It is scoped to exactly
    one read of one correction's status — never a write, never the body, never
    the email (see `CorrectionStatus`) — which is why that trade is acceptable
    here and would not be for anything else.

===============================================================================
THE TWO FIELDS THIS STORY ADDS TO THE DOCUMENTED ROW
===============================================================================

`created_at` and `tracking_token_hash` are not in datamodel.md's field list,
and both are load-bearing rather than convenient:

  * **`created_at`** is what `sla_due_at` is defined relative to. datamodel.md
    gives the table `sla_due_at` and the main query "Open by SLA", so the
    deadline is documented; without the submission time stored beside it, "was
    this triaged late, and by how much" is unanswerable after the fact, and
    the SLA becomes a number nobody can audit. Every other governance table in
    datamodel.md carries its own timestamp (`methodology_versions.published_at`,
    `audit_runs.run_at`, `provenance_log.fetched_at`), so this is consistent
    with the model rather than a departure from it.
  * **`tracking_token_hash`** is the anonymous status lookup, above. It holds a
    digest, never a token.

Both are in `bootstrap.py`'s ZeroDB schema for this table as of this story.
The scenario "A correction row carries no voter profile fields" pins the whole
set, so a later story adding a third field has to come through this docstring.

===============================================================================
WHAT THIS ROW DELIBERATELY DOES NOT HOLD
===============================================================================

**No reporter identity beyond the optional email.** No IP address, no user
agent, no account id, no `user_id`. A correction is political speech about a
candidate — "this claim about person X is wrong" — and a table that paired it
with an identifier would be exactly the kind of record prd.md's privacy
section exists to prevent. The cost is that abuse mitigation on this endpoint
cannot be per-reporter; prd.md's "rate limits and bot protection on public
endpoints" is the story that addresses it, and it can do so at the edge
without storing anything here.

**No reference check on `target_id`.** `target_table` is checked against the
real schema (a correction about `not_a_real_table` is a client bug, and
accepting it would make the ops queue unreadable), but the target row is NOT
required to exist. A reporter writing "there is no such stance on my ballot"
is reporting something real, and refusing it would discard the report that
matters most. This is the same `target_table`/`target_id` split
`ProvenanceLog` and `PanelReview` document for their own polymorphic
references — the generic reference check maps one field to one fixed table and
cannot express "whichever table this other field names".
"""

from __future__ import annotations

import re
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.repositories.models import RowModel

#: The ZeroDB table, per datamodel.md.
TABLE_NAME = "corrections"

#: datamodel.md's id pattern for this table: `cor:{uuid}`. A uuid4 in the
#: canonical hyphenated form, lower-case — which is what `uuid.uuid4()`
#: stringifies to, and what the live rows this story wrote carry.
CORRECTION_ID_PATTERN = r"^cor:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"

#: The status vocabulary this story defines; see the module docstring for why
#: these four and why not `pending` or `duplicate`.
CorrectionStatusValue = Literal["received", "triaged", "resolved", "rejected"]

#: The statuses that close a correction and therefore set `resolved_at` — which
#: is what starts the 90-day retention clock on `contact_email`. Both of them,
#: because the reporter's email carries the same promise whichever way their
#: report was closed.
CLOSED_STATUSES: frozenset[str] = frozenset({"resolved", "rejected"})

#: The status a correction is born in, and the only one the 72-hour triage
#: clock runs against (`CorrectionService.past_sla`).
INITIAL_STATUS = "received"

#: Longest accepted report. Long enough for a reporter to explain a
#: mis-attributed roll call with the detail that makes it actionable; short
#: enough that this uncredentialed, unauthenticated write cannot be used to
#: push megabytes into the table. Not a judgement about what is worth saying —
#: a reporter who needs more can give an email and be written back to.
MAX_BODY_LENGTH = 4_000

#: Longest accepted contact email. Comfortably above RFC 5321's 254-character
#: path limit for a real address.
MAX_EMAIL_LENGTH = 254

#: A conservative email shape check, NOT a validator of deliverability.
#:
#: Deliberately a regex rather than `pydantic.EmailStr`: that needs the
#: `email-validator` package, and taking on a dependency to spell-check an
#: OPTIONAL field on one endpoint is the wrong trade — especially since no
#: amount of syntax checking tells you an address is real, which only sending
#: to it does. What this is for is catching the reporter who typed their
#: address wrong and would otherwise never hear back: one `@`, something
#: either side of it, a dot in the domain, and no whitespace.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def is_email_shaped(value: str) -> bool:
    """Whether `value` could plausibly be an email address.

    See `_EMAIL_PATTERN`: a shape check so a typo is caught at submission,
    never a claim that the address exists.
    """
    return bool(_EMAIL_PATTERN.fullmatch(value))


# --- the two rules both the request and the row enforce -------------------
#
# Defined once and applied by both models. See `CorrectionRequest`'s docstring
# for why the duplication is deliberate: the request boundary exists so a
# reporter's typo is a 422 naming the field rather than a 500 from the
# repository, and the row keeps the rule so a direct service call cannot store
# a blank report either.


def clean_body(value: str) -> str:
    """Trim a report and refuse a blank one.

    A body of spaces passes `min_length=1` and is useless to whoever triages
    it: the 72-hour clock would be running on a report with no content.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("a correction with an empty body reports nothing")
    return stripped


def clean_contact_email(value: str | None) -> str | None:
    """Normalise an optional address, refusing one nobody could use.

    An unusable address is worse than none: it carries the reporter's 90-day
    retention obligation and a privacy risk while never reaching them. Blank
    is treated as absent — an empty form field is "I would rather not", not
    "my address is the empty string".
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if not is_email_shaped(stripped):
        raise ValueError(f"{stripped!r} is not shaped like an email address")
    return stripped


class CorrectionRow(RowModel):
    """One public correction report, keyed `cor:{uuid}` (story 12.4).

    See the module docstring for the status vocabulary, the tracking-token
    scheme, and why `created_at` and `tracking_token_hash` join datamodel.md's
    documented field list.

    `natural_key` is EMPTY and the id is supplied by the caller, for the same
    reason `PromiseSource`'s and `ProvenanceLog`'s are: there is no combination
    of fields to derive `cor:{uuid}` from. Nor should there be — a
    content-addressed id (the scheme `panel_reviews` uses) would make two
    reporters who independently spot the same error collide onto one row,
    silently discarding the second report. Two people reporting the same
    mistake is corroboration, which is information, and the thing this table
    exists to collect.

    `references` is empty by design: `target_id` names a row in whichever table
    `target_table` says, and accepting a target that does not exist is a
    requirement rather than an oversight. See the module docstring.

    Not append-only. datamodel.md rule 6 names `verdicts`,
    `candidate_positions` and `provenance_log`; this table is not among them,
    and its `status`/`resolved_at`/`contact_email` fields are all defined by
    being updated in place (a status that moves, a resolution time that is set,
    an email that is erased at 90 days). A `version_key` here would make the
    retention sweep append a new row carrying the email it was erasing.
    """

    table_name: ClassVar[str] = TABLE_NAME
    references: ClassVar[dict[str, str]] = {}

    #: Which table the reported claim lives in. Checked against the real
    #: schema by `CorrectionService.submit` rather than by a `Literal` here:
    #: the set of tables is `bootstrap.TABLE_SCHEMAS`, which grows every epic,
    #: and a hard-coded list in this model would silently start refusing
    #: corrections about new tables.
    target_table: str = Field(min_length=1)
    #: The reported row's id, exactly as the reader saw it. NOT resolved — see
    #: the module docstring on why a correction about a nonexistent row is
    #: accepted.
    target_id: str = Field(min_length=1)
    #: What the reporter says is wrong.
    body: str = Field(min_length=1, max_length=MAX_BODY_LENGTH)
    #: Optional, per datamodel.md. Erased 90 days after `resolved_at` by
    #: `app.corrections.retention`. Never returned by any endpoint.
    contact_email: str | None = Field(default=None, max_length=MAX_EMAIL_LENGTH)
    status: CorrectionStatusValue = INITIAL_STATUS
    #: When this was submitted. The field `sla_due_at` is relative to, and what
    #: makes "was this triaged late" auditable after the fact.
    created_at: str
    #: created_at + 72 hours (prd.md's triage commitment), computed once at
    #: submission and stored rather than derived on read — so the deadline a
    #: correction was actually held to survives a later change to the SLA.
    sla_due_at: str
    #: Set when the status becomes `resolved` or `rejected`, cleared if it ever
    #: moves back to an open state. Starts the 90-day retention clock.
    resolved_at: str | None = None
    #: sha256 of the reporter's tracking token. The token itself is never
    #: stored; see the module docstring.
    tracking_token_hash: str = Field(min_length=64, max_length=64)

    @field_validator("body")
    @classmethod
    def _body_says_something(cls, value: str) -> str:
        return clean_body(value)

    @field_validator("contact_email")
    @classmethod
    def _email_is_usable(cls, value: str | None) -> str | None:
        return clean_contact_email(value)


# --- wire shapes ----------------------------------------------------------


class CorrectionRequest(BaseModel):
    """What a reporter POSTs to `/v1/corrections`.

    `extra="forbid"` matters more than usual on this one: it is the API's first
    uncredentialed WRITE, and a field this model does not know about must be
    refused rather than ignored — an ignored `status` or `resolved_at` in a
    submission body is a caller who believes they set it.

    The blank-body and email-shape rules are enforced HERE as well as on
    `CorrectionRow`, deliberately duplicated rather than left to the row. They
    are the two things a reporter can plausibly get wrong, and the difference
    is the status code they see: caught at this boundary, FastAPI answers 422
    naming the field; left to the row, the same input reaches the repository
    and surfaces as a `RowValidationError` — a 500 for what is an ordinary
    correctable mistake. The row keeps its copies because a direct service
    call (a script, a future admin path) must not be able to store a blank
    report either. `_shared_validators` below is what keeps the two in step.
    """

    model_config = ConfigDict(extra="forbid")

    target_table: str = Field(
        min_length=1,
        description=(
            "Which table the claim being corrected lives in, e.g. `stances`, "
            "`verdicts`, `member_votes`. Must be a table in the schema."
        ),
        examples=["stances"],
    )
    target_id: str = Field(
        min_length=1,
        description="The id of the row the claim is on, exactly as shown to the reader.",
        examples=["en-US:housing:v1:build-more-homes"],
    )
    body: str = Field(
        min_length=1,
        max_length=MAX_BODY_LENGTH,
        description="What is wrong, in the reporter's own words.",
        examples=["The cited roll call is from the wrong session of Congress."],
    )
    contact_email: str | None = Field(
        default=None,
        max_length=MAX_EMAIL_LENGTH,
        description=(
            "Optional. Given only so we can follow up; never shown to anyone "
            "else, never used for anything else, and erased 90 days after the "
            "correction is closed. Leave it out to report anonymously — the "
            "status link works either way."
        ),
        examples=["reporter@example.org"],
    )

    @field_validator("body")
    @classmethod
    def _body_says_something(cls, value: str) -> str:
        return clean_body(value)

    @field_validator("contact_email")
    @classmethod
    def _email_is_usable(cls, value: str | None) -> str | None:
        return clean_contact_email(value)


class CorrectionReceipt(BaseModel):
    """What a reporter gets back from a submission.

    Carries the tracking token — the ONLY time it is ever disclosed, since the
    row stores only its digest. The body and the email the reporter just sent
    are deliberately not echoed: the reporter has them, and this response ends
    up in logs and screenshots.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description=(
            "The correction's public id. Quotable in a ticket; it does NOT "
            "open the status lookup — the tracking token does."
        ),
        examples=["cor:3f1b9c42-9a1e-4a7d-9e20-7c5b8d2f4a11"],
    )
    status: CorrectionStatusValue = Field(description="Always `received` for a new report.")
    tracking_token: str = Field(
        description=(
            "Keep this. It is shown once and is the only way to check this "
            "report's status without an account — we store only a hash of it, "
            "so it cannot be looked up or re-sent."
        )
    )
    status_path: str = Field(
        description="Where to check the status, token included.",
        examples=["/v1/corrections/0jKx…"],
    )
    sla_due_at: str = Field(
        description=(
            "When we have committed to having triaged this report, ISO-8601 "
            "UTC: 72 hours after submission (prd.md)."
        )
    )
    sla_hours: int = Field(
        description="The triage commitment in hours, so the deadline is legible."
    )


class CorrectionStatus(BaseModel):
    """What a reporter sees when they check their report with their token.

    **Status only.** No `body`, no `contact_email`, no `target_id`, no row id.
    The lookup is unauthenticated by necessity, so the blast radius of a leaked
    or brute-forced token is held to "someone learns that a report they already
    hold the token for is still open". Pinned by the feature file's three
    leakage scenarios.

    `target_table` is absent too, which looks over-cautious and is not: paired
    with a timestamp it narrows down which claim a report is about, and the
    reporter already knows what they reported.
    """

    model_config = ConfigDict(extra="forbid")

    status: CorrectionStatusValue = Field(
        description=(
            "`received` (we have it, not yet looked at), `triaged` (read and "
            "classified — this is the 72-hour commitment), `resolved` (acted "
            "on) or `rejected` (closed without a change)."
        )
    )
    submitted_at: str = Field(description="When this report was received, ISO-8601 UTC.")
    sla_due_at: str = Field(description="When we committed to having triaged it.")
    sla_hours: int = Field(description="The triage commitment in hours (72).")
    past_sla: bool = Field(
        description=(
            "True when this report is still untriaged and its 72-hour deadline "
            "has passed. Shown to the reporter rather than only to us: an SLA "
            "the person waiting cannot check is a promise to ourselves."
        )
    )
    resolved_at: str | None = Field(default=None, description="When it was closed, if it has been.")


__all__ = [
    "CLOSED_STATUSES",
    "CORRECTION_ID_PATTERN",
    "INITIAL_STATUS",
    "MAX_BODY_LENGTH",
    "MAX_EMAIL_LENGTH",
    "TABLE_NAME",
    "CorrectionReceipt",
    "CorrectionRequest",
    "CorrectionRow",
    "CorrectionStatus",
    "CorrectionStatusValue",
    "is_email_shaped",
]
