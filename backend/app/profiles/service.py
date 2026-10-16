"""The only way a voter profile is written or read (story 14.1).

===============================================================================
THE CONTRACT
===============================================================================

Callers above this module — the profile endpoints (story 3.3), the alignment
scorer (4.1–4.4), export and delete (14.4) — work with plaintext
`InterestProfile` objects and never touch a ciphertext, a key or a row id.
Everything below it sees only sealed bytes. That boundary is the story:

    save(user_id, payload)   validate → encrypt → upsert, version bumped
    load(user_id)            read → unwrap key → decrypt → validate
    export(user_id)          load, as a plain dict (prd.md's one-tap export)
    delete(user_id)          delete the row AND shred the key
    validate(payload)        the same model, for anonymous mode (14.3)

`user_id=None` means anonymous mode throughout, and datamodel.md is explicit
about what that means: "In anonymous mode no row is written". `save(None, …)`
raises rather than writing, because a silent no-op would let a bug store
nothing while the UI showed a saved profile.

===============================================================================
ORDER OF OPERATIONS, AND WHY
===============================================================================

`save` does this, in this order, every time:

  1. **Validate** the payload against `InterestProfile`. Before the key is
     touched, before anything is encrypted: an invalid profile must not create
     a data key, and must leave no row behind. (A rejected save on a voter's
     first attempt would otherwise mint a key for a profile that does not
     exist.)
  2. **Read the current row** for its version and history. One request, and the
     only way to know what version this save is.
  3. **Build the new history** from the previous version — the date it changed,
     never its contents (datamodel.md).
  4. **Ensure the data key**, creating it on a first save.
  5. **Encrypt**, with the row id as additional authenticated data.
  6. **Upsert** the row under its HMAC id.

Steps 2 and 6 are not atomic, and ZeroDB offers no conditional write
(datamodel.md's open questions). Two concurrent saves can read version 3 and
both write version 4; the loser's interests are lost and the winner's are
intact and consistent. For a single human editing their own profile that is an
acceptable race — and the alternative, a lock this process cannot enforce
across workers, would not actually fix it.

`delete` deletes the row **first**, then shreds the key. Of the two torn
states, "row gone, key still present" leaves a key that opens nothing, while
"key gone, row still present" leaves an undecryptable row that `load` would
raise on — and a voter who tapped delete should never see an error from their
own profile afterwards. Both calls are idempotent, so a retry completes
whichever half is outstanding.

===============================================================================
NOTHING HERE LOGS A PROFILE
===============================================================================

`logger` carries exactly three facts, and only ever these: the version number,
the fact that a save/delete happened, and the `key_ref`. **Never** the user id
(it would re-link the HMAC rows to accounts, undoing the id scheme), never a
topic or stance id, never a note, never a row id, never key material.

That is a rule a future edit can break silently, so it is tested rather than
documented: `tests/test_profiles_unit.py::test_no_plaintext_reaches_the_logs`
captures every record at DEBUG across save, load, export and delete, and fails
on any of them appearing. Validation error *messages* are held to the same rule
inside `models.py` — a message quoting the offending topic id would leak through
an HTTP 422 as readily as through a log.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from app.profiles.crypto import ProfileCipher, ProfileKeyring
from app.profiles.models import (
    MAX_HISTORY_ENTRIES,
    HistoryEntry,
    InterestProfile,
    VoterProfileRow,
)
from app.repositories.base import Repository
from app.repositories.models import Person
from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBNotFoundError

logger = logging.getLogger(__name__)


class AnonymousModeError(RuntimeError):
    """A write was attempted with no user id.

    datamodel.md: "In anonymous mode no row is written: the same payload lives
    in the browser, and `/v1/alignment` accepts it inline." Raised rather than
    quietly returning, so a caller that lost its user id fails loudly instead
    of dropping a voter's profile on the floor while the UI says "saved".
    """


class PersonNotFoundError(ValueError):
    """A follow was attempted for a `person_id` that is not a live `people` row.

    Story 15.1's reference-validation requirement, in the same spirit as story
    1.4's repository-layer rule: a field that *could* point at a record that
    does not exist is a field some future bug will leave dangling, and a
    voter's followed-person list is exactly that field. Checked at follow time
    rather than only at save/load time, so the rejection happens before
    anything is written — the same "validate before you touch storage" order
    `save()`'s own docstring describes for tier limits.
    """


class VoterProfileService:
    """Reads and writes voter profiles, encrypted, one voter at a time.

    Holds no per-request state; safe to share across requests like the client
    and keyring underneath it.
    """

    def __init__(self, *, client: ZeroDBClient, keyring: ProfileKeyring) -> None:
        self._client = client
        self._keyring = keyring
        # `people` is read-only from here: this service never writes a
        # `people` row, only checks one exists before letting a voter follow
        # it (story 15.1). A plain `Repository`, not a cached reference-check
        # dict like `app.repositories.base.Repository._check_references` —
        # one follow/unfollow call is one lookup, not the batch-ingest case
        # that cache exists for.
        self._people: Repository[Person] = Repository(client=client, model=Person)

    # --- validation (also the anonymous-mode path) ------------------------

    def validate(self, payload: dict[str, Any]) -> InterestProfile:
        """Validate a payload without storing it.

        The anonymous-mode entry point (story 14.3) and the pre-flight for
        `save`. One model validates both paths deliberately: anonymous mode
        must not be a route around the tier limits, or two voters' profiles
        would score under different rules.
        """
        return InterestProfile.model_validate(payload)

    # --- writes -----------------------------------------------------------

    def save(
        self,
        user_id: str | None,
        payload: dict[str, Any],
        *,
        locale: str = "en-US",
        jurisdiction_ids: list[str] | None = None,
    ) -> VoterProfileRow:
        """Encrypt and store one voter's profile, bumping the version.

        See the module docstring for the order of operations and why it is that
        order. The returned row is the ciphertext row as stored — callers get
        back the metadata (version, updated_at), never the payload they just
        handed in, so a caller cannot accidentally round-trip plaintext through
        a response model.

        Raises:
            AnonymousModeError: `user_id` is None — anonymous mode writes no row.
            ValidationError: the payload is not a valid `InterestProfile`.
                Nothing is written and no key is created.
        """
        self._require_identified(user_id, "save a profile")
        profile = self.validate(payload)

        row_id = self._keyring.row_id(user_id)
        previous = self._stored_row(row_id)
        version = 1 if previous is None else previous.version + 1

        profile = profile.model_copy(
            update={"history": self._next_history(user_id, row_id, previous)}
        )

        data_key = self._keyring.ensure_data_key(user_id)
        sealed = ProfileCipher(data_key).encrypt(profile.payload(), aad=row_id)

        row = VoterProfileRow(
            id=row_id,
            version=version,
            locale=locale,
            jurisdiction_ids=list(jurisdiction_ids or []),
            updated_at=_now(),
            key_ref=self._keyring.key_ref,
            payload=sealed,
        )
        self._client.upsert_row(VoterProfileRow.table_name, row_id, row.row_data())

        # Version and key_ref only. Not the row id: it is an HMAC, but it is
        # also a stable per-voter identifier, and a log of "vp:9f2c… saved at
        # 19:20" plus an access log is a re-identification path.
        logger.info("voter profile saved at version %d under %s", version, row.key_ref)
        return row

    def follow(self, user_id: str | None, person_id: str) -> InterestProfile:
        """Add `person_id` to the voter's followed persons, then save.

        Story 15.1: "Follows are stored inside the encrypted profile
        payload" — so this is deliberately a read-modify-write through the
        same `save()` path every other profile change takes, not a second
        storage mechanism. A voter who has never saved a profile gets one
        created here with the minimum this model requires: `save()` already
        refuses a profile with zero primary interests (`InterestProfile`'s
        own validator), so following an incumbent cannot be someone's very
        first action before they have stated any interest — matching the
        onboarding order every other path in this app already enforces.

        Idempotent: following someone already followed is a no-op write (same
        set, bumped version like any other save) rather than an error — a
        double-tap of a follow button must not surface a failure.

        Raises:
            PersonNotFoundError: `person_id` is not a live row in `people`.
                Checked BEFORE the profile is touched, so a rejected follow
                leaves the stored profile exactly as it was.
            AnonymousModeError: `user_id` is None.
        """
        self._require_identified(user_id, "follow a person")
        self._require_person_exists(person_id)

        profile = self.load(user_id) or _blank_profile_for_first_follow()
        if person_id in profile.followed_person_ids:
            updated = profile
        else:
            updated = profile.model_copy(
                update={"followed_person_ids": [*profile.followed_person_ids, person_id]}
            )
        self.save(user_id, updated.payload())
        return self.load(user_id)  # type: ignore[return-value]  # just saved; cannot be None

    def unfollow(self, user_id: str | None, person_id: str) -> InterestProfile:
        """Remove `person_id` from the voter's followed persons, then save.

        No existence check: unfollowing a person_id that no longer exists (or
        never did) must always succeed — a voter removing a stale follow is
        never the wrong action, unlike adding one. Idempotent for the same
        reason `follow` is: unfollowing someone not followed is a no-op save.

        Raises:
            AnonymousModeError: `user_id` is None.

        A voter with no saved profile has nothing to unfollow: this returns an
        unvalidated, unsaved blank profile (zero interests) rather than
        raising or writing anything — there is no row to touch, and a retried
        or out-of-order unfollow must stay harmless rather than surface the
        "needs a primary interest" error a bare save would, since nothing was
        ever asked to be saved here.
        """
        self._require_identified(user_id, "unfollow a person")
        profile = self.load(user_id)
        if profile is None:
            # Nothing stored means nothing followed. `model_construct` skips
            # validation deliberately: a blank profile would fail the "at
            # least one primary interest" rule, but nothing is being saved
            # here, so there is nothing for that rule to protect.
            return _blank_profile_for_first_follow()
        if person_id not in profile.followed_person_ids:
            updated = profile
        else:
            updated = profile.model_copy(
                update={
                    "followed_person_ids": [
                        pid for pid in profile.followed_person_ids if pid != person_id
                    ]
                }
            )
        self.save(user_id, updated.payload())
        return self.load(user_id)  # type: ignore[return-value]  # just saved; cannot be None

    def delete(self, user_id: str | None) -> None:
        """Delete a profile and shred its data key. Idempotent.

        prd.md: "One-tap export and permanent deletion." datamodel.md:
        "Deleting a profile deletes the row and shreds the data key."

        Both halves are attempted even if the first finds nothing, so a
        half-deleted profile (a crash between the two calls) is completed by a
        retry rather than left with an orphaned key. Anonymous mode is a no-op:
        there is nothing stored to delete, and raising would make the one-tap
        delete button fail for the users who need it least.

        `ProfileKeyring.shred` documents exactly what "shredded" means and what
        it does not claim.
        """
        if user_id is None:
            return
        row_id = self._keyring.row_id(user_id)
        with suppress(ZeroDBNotFoundError):
            self._client.delete_row(VoterProfileRow.table_name, row_id)
        # Attempted even when the row was already gone: a crash between the two
        # calls must be completed by a retry, not leave an orphaned key.
        self._keyring.shred(user_id)
        logger.info("voter profile deleted and its data key shredded")

    # --- reads ------------------------------------------------------------

    def load(self, user_id: str | None) -> InterestProfile | None:
        """One voter's decrypted profile, or None when there is no row.

        None covers both "never saved" and "deleted", which are the same answer
        to the caller. A row that exists but whose key has been shredded is
        **not** None: `ProfileKeyring.data_key` raises, because an
        undecryptable row is a broken state worth surfacing rather than
        presenting a voter with an empty profile.
        """
        if user_id is None:
            return None
        row_id = self._keyring.row_id(user_id)
        row = self._stored_row(row_id)
        if row is None:
            return None
        data_key = self._keyring.data_key(user_id)
        plaintext = ProfileCipher(data_key).decrypt(row.payload, aad=row_id)
        return InterestProfile.model_validate(plaintext)

    def export(self, user_id: str | None) -> dict[str, Any] | None:
        """The profile as a plain JSON-ready dict (prd.md's one-tap export).

        Story 14.4 builds the endpoint on this. A dict rather than the model so
        the export is a stable document rather than this release's model shape.
        """
        profile = self.load(user_id)
        return None if profile is None else profile.payload()

    def version_of(self, user_id: str | None) -> int:
        """The stored version, or 0 when nothing is stored.

        Readable without the data key — that is the point of `version` living
        on the outer row — so a client can check for staleness without a
        decryption.
        """
        if user_id is None:
            return 0
        row = self._stored_row(self._keyring.row_id(user_id))
        return 0 if row is None else row.version

    # --- internals --------------------------------------------------------

    def _stored_row(self, row_id: str) -> VoterProfileRow | None:
        row = self._client.get_row(VoterProfileRow.table_name, row_id)
        if row is None:
            return None
        return VoterProfileRow.model_validate(row.row_data)

    def _next_history(
        self, user_id: str, row_id: str, previous: VoterProfileRow | None
    ) -> list[HistoryEntry]:
        """History for the save in progress: dates, never contents.

        The previous version's own history is carried forward by decrypting
        it, which needs the data key — so on a first save, and on any row this
        process cannot open (a shredded key, a `key_ref` from a scheme this
        build does not implement), the history starts from just this one entry
        rather than failing the save. A voter must always be able to save a new
        profile, even over a row that can no longer be read.

        Trimmed here as well as in the model, so the trim happens before
        encryption rather than only on the way back out.
        """
        if previous is None:
            return []
        entry = HistoryEntry(version=previous.version, changed_at=previous.updated_at)
        try:
            data_key = self._keyring.data_key(user_id)
            earlier = InterestProfile.model_validate(
                ProfileCipher(data_key).decrypt(previous.payload, aad=row_id)
            ).history
        except Exception:  # noqa: BLE001 - see the docstring: never block a save
            # Deliberately broad and deliberately silent. The exception could
            # be raised from code holding partially-decrypted bytes, so logging
            # it risks logging plaintext — the one thing this story forbids.
            # The cost of swallowing it is a shorter history; the cost of
            # raising is a voter who cannot save a new profile.
            return [entry]
        return [*earlier, entry][-MAX_HISTORY_ENTRIES:]

    @staticmethod
    def _require_identified(user_id: str | None, action: str) -> None:
        if user_id is None:
            raise AnonymousModeError(
                f"cannot {action} in anonymous mode: no row is written, and the "
                "profile stays in the browser (datamodel.md, prd.md privacy rules)"
            )

    def _require_person_exists(self, person_id: str) -> None:
        """Story 15.1: reject a follow for a `person_id` not in `people`.

        The same reference-integrity philosophy as `Repository._check_references`
        (story 1.4) — "a field that *could* hold a dangling reference is a
        field some future code will leave dangling" — applied here because
        `followed_person_ids` cannot go through that repository check itself:
        it lives inside an encrypted payload the generic `Repository` layer
        never sees (see `VoterProfileRow`'s docstring on why that table is
        deliberately not a `RowModel`).
        """
        if self._people.get(person_id) is None:
            raise PersonNotFoundError(
                f"{person_id!r} is not a known person; only a real, currently "
                "loaded legislator or candidate can be followed"
            )

    def __repr__(self) -> str:
        return "VoterProfileService(<redacted>)"


def _now() -> str:
    """An ISO-8601 UTC timestamp, as every other Informed row uses."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _blank_profile_for_first_follow() -> InterestProfile:
    """A minimally-valid profile for a voter who follows before they save.

    `InterestProfile` requires at least one primary interest (prd.md: "no
    candidate score is shown until at least one primary interest exists"),
    so `follow()` cannot write an *empty* profile for a brand-new voter —
    it would be rejected the same way a save with no interests is. This is
    not a product decision this story makes silently: a voter reaching the
    follow button with no profile yet is expected to have gone through
    onboarding already in the normal flow, but `follow()` must not crash if
    they somehow have not. Rather than invent a fabricated "interest" with a
    real topic id (which would misrepresent what the voter actually said),
    this returns an InterestProfile with zero interests and lets the
    model's own validator raise `ValidationError` naming the real rule — the
    same error a bare `POST /v1/profiles` with no interests would produce —
    so the router can turn it into the same 422 a client already knows how
    to handle, rather than a bespoke "follow before onboarding" error shape.
    """
    return InterestProfile.model_construct(
        interests=[], evidence_mode="blend", followed_person_ids=[], history=[]
    )


__all__ = ["AnonymousModeError", "PersonNotFoundError", "VoterProfileService"]
