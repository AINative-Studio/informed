"""The voter-profile shapes: the stored row and the decrypted payload (14.1).

Two models, mirroring datamodel.md's "Voter profile document" section exactly:

  * `VoterProfileRow` — what ZeroDB holds. "A profile row stores only what is
    needed to find and decrypt it": id, version, locale, jurisdiction_ids,
    updated_at, key_ref, payload. There is deliberately no field for an
    interest, a stance or a user id, because a field that *could* hold a
    political opinion in clear text is a field some future code will.
  * `InterestProfile` — the decrypted payload, "validated by the Pydantic
    `InterestProfile` model" in datamodel.md's own words, carrying the limits
    that section states.

**Why the limits live here and not in a service.** datamodel.md says "Limits are
enforced in the model", and the reason is the encryption: once a payload is
sealed, nothing downstream can inspect it cheaply, so a profile with 40 primary
interests would be stored, re-read and scored before anyone noticed. The model
is the last point at which the data is both readable and rejectable.

**No weights anywhere.** datamodel.md: "The tier sets the weight: `primary` 3.0,
`secondary` 1.5, `nice_to_have` 0.5. The live values come from
`methodology_versions` and are never hard-coded." A `weight` field on an
interest would freeze one methodology version into every stored profile, and
re-publishing the methodology (epic 11) would then silently fail to re-score
anyone. `Interest` therefore carries the tier and nothing derived from it; the
scoring stories read the live weights by tier.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: datamodel.md's three tiers, spelled as they appear in the stored payload
#: (`nice_to_have`, underscored — prd.md's prose writes "nice-to-have").
InterestTier = Literal["primary", "secondary", "nice_to_have"]

#: Which evidence a voter wants their alignment computed from: votes only,
#: promises only, or both (prd.md, Feature 3's evidence blend). A `Literal`
#: because an unrecognised mode would silently fall through to a default and
#: score the voter against evidence they excluded.
EvidenceMode = Literal["votes", "promises", "blend"]

#: prd.md's tier table: "1–5 interests" for primary. At least one, because
#: prd.md's own acceptance criterion is "no candidate score is shown until at
#: least one primary interest exists" — a stored profile with none is a profile
#: that cannot be scored, and is better refused at the door.
MIN_PRIMARY_INTERESTS = 1
MAX_PRIMARY_INTERESTS = 5

#: prd.md's tier table: "Up to 8" for secondary. Nice-to-have is "Unlimited"
#: and so has no constant here.
MAX_SECONDARY_INTERESTS = 8

#: prd.md, Feature 1: the note is a short free-text aside an agent maps to the
#: taxonomy, not an essay. Bounded because it is encrypted into a payload that
#: is read whole, and because an unbounded field is an unbounded row.
MAX_NOTE_LENGTH = 2_000

#: Change dates kept in the payload. datamodel.md: "History keeps change dates,
#: not prior contents". Bounded so a voter who re-tiers constantly (the UI
#: offers drag-and-drop) cannot grow their own payload without limit.
MAX_HISTORY_ENTRIES = 50

#: `vp:` plus a SHA-256 HMAC in hex. Enforced on the row so a caller cannot
#: store a raw user id in the id field — the exact leak the HMAC exists to
#: prevent, and one that no runtime error would otherwise surface.
_ROW_ID_PATTERN = re.compile(r"^vp:[0-9a-f]{64}$")


class Interest(BaseModel):
    """One topic, the stance a voter wants on it, and how much it matters.

    Fields are datamodel.md's payload example exactly: topic_id, stance_id,
    tier, intensity, dealbreaker, note.
    """

    model_config = ConfigDict(extra="forbid")

    #: A `topics` row id, e.g. `en-US:rx-drug-pricing:v1`. Not validated against
    #: the taxonomy here: that table is epic 3's, and a repository-style
    #: reference check cannot run inside an encrypted payload anyway. Story 3.x
    #: checks the topic exists when the profile is submitted.
    topic_id: str = Field(min_length=1)

    #: A `stances` row id — "the outcome the voter wants" (prd.md, Feature 1).
    stance_id: str = Field(min_length=1)

    tier: InterestTier

    #: prd.md: "Intensity inside its tier (1–3) for tie-breaking." Bounded
    #: because a 7 would not error anywhere downstream, it would just quietly
    #: outweigh every other interest in the same tier.
    intensity: int = Field(default=2, ge=1, le=3)

    #: prd.md's tier table allows a dealbreaker only on a primary interest.
    #: Checked on the profile rather than here, because an `Interest` alone
    #: cannot see its own tier's rule in context — see
    #: `InterestProfile._dealbreakers_are_primary_only`.
    dealbreaker: bool = False

    #: prd.md: "Optional free-text note, which an agent maps to taxonomy
    #: topics". A voter's own words about their politics — the most sensitive
    #: field in the payload, and one more reason the payload is encrypted
    #: whole rather than per field.
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)


class HistoryEntry(BaseModel):
    """One past version of a profile: when it changed, not what it said.

    datamodel.md: "History keeps change dates, not prior contents, unless the
    user turns on 'compare my past priorities'." Two fields, and no room for a
    third: a model that could hold prior interests is a model that would end up
    holding them by default, which is the opposite of data minimisation.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    changed_at: str


class InterestProfile(BaseModel):
    """A voter's priorities: the decrypted payload, validated.

    This object exists only in memory and only inside a request. It is never
    stored, never logged and never serialized anywhere but into the AES-GCM
    payload of a `VoterProfileRow`.
    """

    model_config = ConfigDict(extra="forbid")

    interests: list[Interest]

    #: prd.md, Feature 3: which evidence to score from. Defaults to the blend,
    #: which is the only mode that uses both the promise record and the voting
    #: record — the product's whole claim.
    evidence_mode: EvidenceMode = "blend"

    #: `people` ids a voter follows for alerts (epic 12). Ids, not opinions,
    #: but inside the encrypted payload regardless: who a voter follows is
    #: itself a political signal.
    followed_person_ids: list[str] = Field(default_factory=list)

    history: list[HistoryEntry] = Field(default_factory=list)

    # --- the rules datamodel.md states ----------------------------------

    @model_validator(mode="after")
    def _tier_counts_are_within_limits(self) -> InterestProfile:
        """1–5 primary, up to 8 secondary, nice-to-have unlimited.

        prd.md's tier table and datamodel.md's "Limits are enforced in the
        model". The messages name the tier and the limit, never a topic id: a
        validation error's text reaches logs and HTTP responses, and a message
        quoting the voter's interests would leak exactly what this story
        encrypts.
        """
        counts = {"primary": 0, "secondary": 0, "nice_to_have": 0}
        for interest in self.interests:
            counts[interest.tier] += 1

        if counts["primary"] < MIN_PRIMARY_INTERESTS:
            raise ValueError(
                f"a profile needs at least {MIN_PRIMARY_INTERESTS} primary interest; "
                "nothing can be scored without one"
            )
        if counts["primary"] > MAX_PRIMARY_INTERESTS:
            raise ValueError(
                f"at most {MAX_PRIMARY_INTERESTS} primary interests are allowed; "
                f"got {counts['primary']}"
            )
        if counts["secondary"] > MAX_SECONDARY_INTERESTS:
            raise ValueError(
                f"at most {MAX_SECONDARY_INTERESTS} secondary interests are allowed; "
                f"got {counts['secondary']}"
            )
        return self

    @model_validator(mode="after")
    def _dealbreakers_are_primary_only(self) -> InterestProfile:
        """datamodel.md: "`dealbreaker` allowed only on primary interests".

        A dealbreaker overrides the whole score (prd.md: "candidate A shows a
        dealbreaker warning above the score"), so allowing one on a tradeable
        interest would let a secondary preference veto a candidate — which
        contradicts what the voter said "secondary" meant.

        The count, not the topic, in the message: see the sibling validator.
        """
        offenders = sum(
            1 for interest in self.interests if interest.dealbreaker and interest.tier != "primary"
        )
        if offenders:
            raise ValueError(
                f"dealbreaker is allowed only on a primary interest; {offenders} "
                "non-primary interest(s) set it"
            )
        return self

    @model_validator(mode="after")
    def _one_stance_per_topic(self) -> InterestProfile:
        """No topic twice.

        Not in datamodel.md explicitly, enforced because of what scoring does
        with it: the same topic in two tiers would be counted twice at two
        weights, so a voter could inflate one issue past the tier limits the
        validator above enforces. The count, not the topic id, in the message.
        """
        topics = [interest.topic_id for interest in self.interests]
        duplicates = len(topics) - len(set(topics))
        if duplicates:
            raise ValueError(
                f"each topic may appear once; {duplicates} duplicate topic entr(ies) found"
            )
        return self

    @model_validator(mode="after")
    def _history_is_bounded(self) -> InterestProfile:
        """Keep the most recent `MAX_HISTORY_ENTRIES` change dates.

        Trimmed rather than rejected: a voter who has re-tiered 200 times has
        done nothing wrong, and failing their save to protect a payload size
        would be the wrong trade. The oldest dates go, which is also the
        privacy-preferring direction.
        """
        if len(self.history) > MAX_HISTORY_ENTRIES:
            self.history = self.history[-MAX_HISTORY_ENTRIES:]
        return self

    # --- helpers callers need -------------------------------------------

    def by_tier(self, tier: InterestTier) -> list[Interest]:
        """Interests in one tier, in the order the voter gave them."""
        return [interest for interest in self.interests if interest.tier == tier]

    @property
    def dealbreakers(self) -> list[Interest]:
        """The interests a candidate cannot contradict (prd.md, Feature 3)."""
        return [interest for interest in self.interests if interest.dealbreaker]

    def payload(self) -> dict[str, Any]:
        """The dict that gets encrypted. Never stored or logged as-is."""
        return self.model_dump(mode="json")


class VoterProfileRow(BaseModel):
    """The stored `voter_profiles` row: a locator plus ciphertext.

    datamodel.md's documented shape, field for field. Deliberately **not** a
    `RowModel` subclass: the repository layer (story 1.4) derives ids from
    natural keys and resolves declared references, and this table has neither —
    its id is an HMAC that only the keyring can compute, and datamodel.md's own
    diagram labels it "encrypted; no references in or out". Routing it through
    a layer whose job is reference integrity would add a reference-check pass
    over a table that must stay unjoinable. `VoterProfileService` writes it
    through the typed `ZeroDBClient` directly, which is the same boundary
    CLAUDE.md requires (no MCP calls from a repository) without borrowing
    machinery that does not apply.

    `references` and `table_name` are still declared, because the no-references
    rule is worth asserting in a test rather than describing in a comment.
    """

    model_config = ConfigDict(extra="forbid")

    table_name: ClassVar[str] = "voter_profiles"
    #: datamodel.md: "encrypted; no references in or out".
    references: ClassVar[dict[str, str]] = {}

    #: `vp:{hmac(user_id)}`. Pattern-checked so a raw user id cannot be stored
    #: here — that would undo the whole point of the HMAC, and nothing else
    #: would notice.
    id: str = Field(pattern=_ROW_ID_PATTERN.pattern)

    #: Bumped on every save (datamodel.md). Starts at 1, so 0 is a bug rather
    #: than an empty profile.
    version: int = Field(ge=1)

    #: On the outer row, not in the payload: it picks the taxonomy and the
    #: stance wording *before* anything is decrypted, and a locale is not a
    #: political opinion. datamodel.md puts it here for the same reason.
    locale: str = Field(default="en-US", min_length=2)

    #: Which jurisdictions this voter's ballot spans. Also outer-row, also not
    #: an opinion — and needed to choose adapters before decryption.
    jurisdiction_ids: list[str] = Field(default_factory=list)

    updated_at: str

    #: Which key scheme sealed `payload`, e.g. `local-envelope:…` today and
    #: `kms:informed/users/v1` once a real KMS is wired in. Per row rather than
    #: global config, so a table mid-migration is readable: each row says how
    #: to open it instead of the code guessing.
    key_ref: str = Field(min_length=1)

    #: base64(nonce || AES-256-GCM ciphertext || tag). Opaque here on purpose:
    #: this model must not be able to describe its own contents.
    payload: str = Field(min_length=1)

    def row_data(self) -> dict[str, Any]:
        """The dict handed to ZeroDB — exactly datamodel.md's seven fields."""
        return self.model_dump(mode="json")


__all__ = [
    "MAX_HISTORY_ENTRIES",
    "MAX_NOTE_LENGTH",
    "MAX_PRIMARY_INTERESTS",
    "MAX_SECONDARY_INTERESTS",
    "MIN_PRIMARY_INTERESTS",
    "EvidenceMode",
    "HistoryEntry",
    "Interest",
    "InterestProfile",
    "InterestTier",
    "VoterProfileRow",
]
