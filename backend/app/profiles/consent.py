"""The GDPR special-category consent record (story 14.2).

===============================================================================
WHAT THIS STORY ADDS THAT STORY 4.1 DID NOT
===============================================================================

Story 4.1 built the consent *gate*: `POST /v1/profiles` requires
`consent: true` and refuses anything else with a 422. That satisfies "no save
without it" and nothing else. It is a boolean in a request body that is
discarded the moment the request ends, so after the fact there is no way to
answer the only questions a consent regime actually asks:

    *Did this person consent?  To what wording?  When?  Is it still current?*

Article 9(2)(a) GDPR permits processing political opinions on *explicit
consent*, and Article 7(1) puts the burden of **demonstrating** that consent on
the controller. A boolean that was true once during a request demonstrates
nothing. So this module adds the record, and the profile save writes one every
time.

===============================================================================
THE DESIGN, AND WHY IT IS SHAPED THIS WAY
===============================================================================

datamodel.md has no consent table — it is not among the 22 — so this is a new
**infrastructure table**, in the same category and for the same reason as
`voter_profile_keys` (14.1), `panel_reviews` (3.3) and `scheduled_jobs` (16.x):
a documented rule ("GDPR special-category consent recorded") cannot be
implemented without it, and datamodel.md fixes `voter_profiles` at exactly
seven fields so the record cannot live there.

    voter_consents    vc:{sha256}
      profile_ref     vp:{hmac(user_id)}  -- the SAME HMAC the profile row uses
      consent_version "2026.1"            -- which wording was shown
      action          granted | withdrawn
      lawful_basis    gdpr-9-2-a-explicit-consent
      recorded_at     ISO-8601 UTC

Four decisions worth the words:

**1. Append-only, like `provenance_log` and `panel_reviews`.** datamodel.md:
"Verdicts, positions and the provenance log are append-only... never update
history in place." A consent ledger is the same kind of object: the question
"what did they agree to in March" must stay answerable in June, and an
`UPDATE consent SET version='2026.2'` would forge an agreement to wording the
voter never saw. So there is no update path here at all — `record()` only ever
inserts, and withdrawal is a *new row* with `action="withdrawn"`, never a
mutation or a delete of the grant.

**2. The id is content-addressed — `vc:{sha256}` over the row's own content —
exactly like `provenance_log`'s `prov:{sha256}` and `panel_reviews`'
`pr:{sha256}`.** The same (voter, version, action, timestamp) tuple always
lands on the same id, so a retried save is idempotent rather than duplicating
the ledger, and no sequence counter has to be read before a write. Two genuine
re-consents differ in `recorded_at`, so they are two rows — which is what
"append" has to mean for a ledger that records *events*. Timestamps are to the
second (like every other Informed row), so two saves inside the same second
collapse to one record; that is the right trade — the ledger's purpose is to
show what wording was agreed and when, not to count button presses.

**3. The row names no voter.** `profile_ref` is the `vp:{hmac(user_id)}` id,
not the user id — the same reasoning 14.1 applies to `voter_profiles` and
`voter_profile_keys`: a dump of this table must not be joinable back to
accounts. A consent ledger listing real user ids beside "political opinions"
would be a worse leak than the encrypted profile it documents, because it needs
no key to read.

**4. The row holds no interests and is not encrypted.** It records *that* a
voter agreed to a named piece of wording, never what they said afterwards.
There is nothing in it to encrypt, and encrypting it would be actively wrong:
the record has to survive the deletion of the profile's data key, or the
withdrawal record would become unreadable at exactly the moment it matters.
That is also why withdrawal is recorded here rather than inside the (deleted)
profile payload.

===============================================================================
WHY THE CONSENT TEXT IS VERSIONED IN CODE
===============================================================================

`CONSENT_VERSIONS` below holds the wording, keyed by version. It is a constant
rather than a table because the text is a *release artifact* — it ships with the
code that shows it, it is reviewed like code, and the one thing that must never
happen is the stored record pointing at a version whose wording nobody can
reproduce. A row in ZeroDB could be edited after the fact, silently rewriting
what a voter is recorded as having agreed to; a constant in a tagged commit
cannot. (`methodology_versions` is a table for the opposite reason: its weights
are *operational* and change between releases by design.)

A save therefore may only consent to a version this build actually knows:
`record()` rejects an unknown version rather than storing a dangling pointer.
Superseded versions stay in the dict forever so old records stay resolvable.

===============================================================================
ANONYMOUS MODE (STORY 14.3)
===============================================================================

An anonymous voter never reaches this module. `VoterProfileService.save`
refuses `user_id=None` before any consent work happens, so no consent row is
written either — which is the right answer twice over: there is nothing stored
to consent *to*, and a consent row keyed to an anonymous session would itself
be the only server-side trace of a voter who asked for none. The acceptance
criterion "anonymous mode is offered as the alternative" is served by
`consent_offer()`, which returns the wording *and* the anonymous alternative
together, so a client cannot render the consent ask without the alternative
being in the same payload.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: The consent ledger's table. Infrastructure, not one of datamodel.md's 22 —
#: see the module docstring, and `app.bootstrap.INFRASTRUCTURE_TABLES`.
CONSENT_TABLE = "voter_consents"

#: GDPR Article 9(2)(a): processing special-category data on the data
#: subject's explicit consent. Recorded on the row rather than assumed, so an
#: audit reads the claimed basis from the data instead of from a code comment.
LAWFUL_BASIS = "gdpr-9-2-a-explicit-consent"

#: What a consent record can say happened. `granted` on a save, `withdrawn` on
#: a delete. Deliberately not a boolean: "withdrawn" is an event with its own
#: timestamp, and a boolean would have to be flipped in place — the one thing
#: an append-only ledger must never do.
ConsentAction = Literal["granted", "withdrawn"]

#: How many consent events one profile's history reads back at most. A ceiling
#: rather than unbounded paging: one voter's ledger is a handful of rows (one
#: per genuine re-consent, one per withdrawal), and a profile somehow holding
#: thousands is a bug worth bounding rather than faithfully paginating into an
#: export response. Well above any plausible real count.
MAX_CONSENT_HISTORY = 500

#: How two events in the SAME second are ordered. See `ConsentLedger.history`
#: for the bug that made this necessary: a withdrawal presupposes a grant, so
#: of the two possible orderings of a tied pair, grant-then-withdrawal is the
#: only one that can have actually happened.
_ACTION_RANK: dict[str, int] = {"granted": 0, "withdrawn": 1}


class ConsentText(BaseModel):
    """One version of the consent wording, as shown to a voter.

    Prose, in code, under version control — see the module docstring on why
    this is not a table. Every field is text a voter reads before deciding;
    nothing here is derived or operational.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    #: The ask itself. Names political opinions explicitly: GDPR explicit
    #: consent has to be specific about the special category involved, and
    #: "your preferences" would not be.
    summary: str
    #: What is stored, and what is not. Bullet points rather than a paragraph
    #: because ui-prd.md 2.2 requires this to be "an actual choice", and a
    #: choice a voter cannot skim is not one.
    what_is_stored: tuple[str, ...]
    #: The alternative, carried in the same object so no client can render the
    #: ask without it (story 14.2: "anonymous mode is offered as the
    #: alternative").
    anonymous_alternative: str
    #: The rights that make the consent withdrawable in practice — story 14.4's
    #: export and delete, named here so the promise and the endpoints ship
    #: together.
    your_rights: tuple[str, ...]


#: Every consent version this build can resolve, newest last. Superseded
#: versions are never removed: a record written under 2026.1 must stay
#: resolvable after 2026.2 ships, or the ledger points at wording nobody can
#: reproduce.
CONSENT_VERSIONS: dict[str, ConsentText] = {
    "2026.1": ConsentText(
        version="2026.1",
        summary=(
            "Informed is asking you to save your political priorities to your "
            "account. Your political opinions are special-category personal "
            "data, and we only store them if you explicitly agree here."
        ),
        what_is_stored=(
            "The issues you choose, the outcome you want on each, how much "
            "each matters to you, and any note you write — encrypted with a "
            "key held for you alone.",
            "Nothing else about your politics. We do not store your address "
            "with your interests, and we never store a score.",
            "We never sell your interests, share them, or use them to target you with anything.",
            "You can export everything we hold, or delete it permanently, at "
            "any time, in one action.",
        ),
        anonymous_alternative=(
            "You do not have to agree. Anonymous mode gives you the same full "
            "app with your priorities kept in this browser only — nothing "
            "about you is sent to or stored on our servers."
        ),
        your_rights=(
            "Export: download everything stored for you as JSON.",
            "Delete: permanently erase your profile and destroy its "
            "encryption key, so even a backup cannot be read.",
            "Withdraw: deleting your profile withdraws this consent, and the "
            "withdrawal is recorded.",
        ),
    ),
}

#: The version a client is shown today — the newest in `CONSENT_VERSIONS`.
#: Derived rather than a second constant, so adding a version cannot leave the
#: "current" pointer behind.
CURRENT_CONSENT_VERSION = next(reversed(CONSENT_VERSIONS))


class UnknownConsentVersionError(ValueError):
    """A save consented to a version of the wording this build does not have.

    Refused rather than stored. A record naming `2025.0` when no such wording
    exists in the deployed code is strictly worse than no record: it reads like
    proof of consent while pointing at text nobody can produce. The usual cause
    is a stale client (a PWA cached across a release), so the message tells the
    caller to re-read `GET /v1/profiles/consent` and ask again.
    """


class ConsentRecord(BaseModel):
    """One consent event, as stored. Append-only; never updated in place.

    Carries no user id and no interests — see the module docstring's decisions
    3 and 4. `profile_ref` is the same `vp:{hmac(user_id)}` the profile row
    uses, so this table names no accounts and still points unambiguously at
    exactly one voter's profile.
    """

    model_config = ConfigDict(extra="forbid")

    table_name: ClassVar[str] = CONSENT_TABLE

    #: `vc:{sha256}` over this row's own content. Content-addressed like
    #: `provenance_log`'s `prov:{sha256}`, so a retried write is idempotent.
    id: str
    #: `vp:{hmac(user_id)}`. Not the user id. Never the user id.
    profile_ref: str
    #: Which `CONSENT_VERSIONS` entry the voter was shown.
    consent_version: str
    action: ConsentAction
    lawful_basis: str = LAWFUL_BASIS
    recorded_at: str

    def row_data(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def consent_record_id(
    *, profile_ref: str, consent_version: str, action: str, recorded_at: str
) -> str:
    """`vc:{sha256}` over the content that makes this event what it is.

    The same four facts always give the same id, which is what makes a retried
    save idempotent against the ledger instead of duplicating it. Separator
    bytes between the fields so that no two different tuples can concatenate to
    the same string — `("a", "bc")` and `("ab", "c")` must not collide.
    """
    material = "\x1f".join((profile_ref, consent_version, action, recorded_at))
    return f"vc:{sha256(material.encode('utf-8')).hexdigest()}"


class ConsentOffer(BaseModel):
    """What `GET /v1/profiles/consent` returns: the ask and the alternative.

    One payload holding both, deliberately. ui-prd.md 2.2: the consent
    language and the anonymous-mode alternative go "side by side, as an actual
    choice — not a pre-checked box". A client that fetched only the consent text
    could render a consent-or-nothing screen; a client that fetches this one
    cannot, because the alternative arrives in the same object.
    """

    model_config = ConfigDict(extra="forbid")

    consent_version: str = Field(
        description="Pass this back as `consent_version` on POST /v1/profiles"
    )
    summary: str
    what_is_stored: list[str]
    anonymous_alternative: str
    your_rights: list[str]
    lawful_basis: str = LAWFUL_BASIS


def consent_offer(version: str | None = None) -> ConsentOffer:
    """The consent wording to show, defaulting to the current version.

    Raises:
        UnknownConsentVersionError: no such version in this build.
    """
    text = _text_for(version or CURRENT_CONSENT_VERSION)
    return ConsentOffer(
        consent_version=text.version,
        summary=text.summary,
        what_is_stored=list(text.what_is_stored),
        anonymous_alternative=text.anonymous_alternative,
        your_rights=list(text.your_rights),
    )


class ConsentLedger:
    """Append-only consent records over one ZeroDB project.

    Holds no per-request state; shareable like `ZeroDBClient` and
    `ProfileKeyring`. Takes a `profile_ref` (the HMAC row id) rather than a
    user id on every method, so this class never sees a user id at all — the
    HMAC is computed by `ProfileKeyring` one layer up, and a bug here cannot
    leak an identity it was never handed.
    """

    def __init__(self, client: ZeroDBClient) -> None:
        self._client = client

    def record(
        self,
        *,
        profile_ref: str,
        consent_version: str,
        action: ConsentAction = "granted",
        recorded_at: str | None = None,
    ) -> ConsentRecord:
        """Append one consent event. Idempotent for an identical event.

        Written with a check-then-insert against the content-addressed id —
        the same pattern `app.ingest.house_rollcalls.write_provenance` uses for
        `provenance_log`, and for the same reason: there is no version number
        to compute and no `is_current` flag to move, so `upsert` would be the
        wrong verb even though the id makes a duplicate impossible.

        Raises:
            UnknownConsentVersionError: `consent_version` is not in this build.
        """
        text = _text_for(consent_version)
        when = recorded_at or _now()
        record = ConsentRecord(
            id=consent_record_id(
                profile_ref=profile_ref,
                consent_version=text.version,
                action=action,
                recorded_at=when,
            ),
            profile_ref=profile_ref,
            consent_version=text.version,
            action=action,
            recorded_at=when,
        )
        existing = self._client.get_row(CONSENT_TABLE, record.id)
        if existing is None:
            self._client.insert_row(CONSENT_TABLE, record.row_data())
        # The version and the action, never the profile_ref: it is an HMAC, but
        # it is also a stable per-voter handle, and "vp:9f2c… consented at
        # 19:20" beside an access log is a re-identification path — the same
        # rule `VoterProfileService.save` follows for its own log line.
        logger.info("consent %s recorded under version %s", action, text.version)
        return record

    def history(self, profile_ref: str) -> list[ConsentRecord]:
        """Every consent event for one profile, oldest first.

        Sorted here rather than trusted from storage: ZeroDB documents no row
        order, and a ledger whose order depends on storage internals cannot
        answer "what did they most recently agree to".

        The sort key is `(recorded_at, action_rank, id)`, and the middle term
        exists because of a real bug this ordering had first. Timestamps are
        second-resolution (like every other Informed row), and a voter who
        saves a profile and immediately deletes it produces a grant and a
        withdrawal **in the same second**. With only `(recorded_at, id)` the
        tie broke on a sha256 digest — i.e. arbitrarily — so the ledger could
        read "withdrawn, then granted" for a voter who did the opposite, and
        `current_grant` would then report live consent for someone who had
        just erased everything. Ranking `granted` before `withdrawn` within
        one second resolves it in the only direction that can be real: a
        withdrawal presupposes a grant, so of the two orderings of a tied
        pair, grant-then-withdrawal is the one that can have happened.

        The id stays as the final tiebreak so the order is still total and
        deterministic for two events that tie on both.
        """
        page = self._client.query_rows(
            CONSENT_TABLE, filter={"profile_ref": profile_ref}, limit=MAX_CONSENT_HISTORY
        )
        records = [ConsentRecord.model_validate(row.row_data) for row in page.rows]
        # Defensive: `query_rows`' server-side filter is not trusted blindly —
        # `app.zerodb.client` documents that it can be ignored by the live API
        # (which is why the fake can simulate that), and returning another
        # voter's consent row here would be a cross-voter leak.
        records = [record for record in records if record.profile_ref == profile_ref]
        return sorted(
            records,
            key=lambda record: (record.recorded_at, _ACTION_RANK[record.action], record.id),
        )

    def current_grant(self, profile_ref: str) -> ConsentRecord | None:
        """The latest event, if it is a grant rather than a withdrawal.

        None when consent was never given, or was given and then withdrawn.
        "Latest event" rather than "latest grant" on purpose: a grant followed
        by a withdrawal is not current consent, and a reader that scanned for
        the newest `granted` row would say it was.
        """
        events = self.history(profile_ref)
        if not events:
            return None
        latest = events[-1]
        return latest if latest.action == "granted" else None

    def __repr__(self) -> str:
        return "ConsentLedger(<redacted>)"


def _text_for(version: str) -> ConsentText:
    text = CONSENT_VERSIONS.get(version)
    if text is None:
        raise UnknownConsentVersionError(
            f"consent version {version!r} is not one this release can show. "
            f"The current consent version is {CURRENT_CONSENT_VERSION!r} — read "
            "GET /v1/profiles/consent and ask again with the version it returns."
        )
    return text


def _now() -> str:
    """An ISO-8601 UTC timestamp, as every other Informed row uses."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "CONSENT_TABLE",
    "CONSENT_VERSIONS",
    "CURRENT_CONSENT_VERSION",
    "LAWFUL_BASIS",
    "ConsentAction",
    "ConsentLedger",
    "ConsentOffer",
    "ConsentRecord",
    "ConsentText",
    "UnknownConsentVersionError",
    "consent_offer",
    "consent_record_id",
]
