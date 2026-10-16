"""Pydantic v2 row models for Informed's NoSQL tables (story 1.4).

datamodel.md rule 5: "Integrity lives in code — a FastAPI repository layer
validates references with Pydantic before every write." These models are that
validation. Each one declares, in one place:

  * its **table** — so nothing above the repository spells a table name twice;
  * its **natural key** — the fields the deterministic id is built from, so the
    id can be derived rather than trusted, and a supplied id that contradicts
    the key is caught (datamodel.md rule 1);
  * its **references** — field name to referenced table, so the repository can
    check each one exists before writing (there are no foreign keys);
  * whether it is **append-only**, and if so what `is_current` is scoped by
    (datamodel.md rule 6).

Why `extra="forbid"`: ZeroDB's `schema_definition` is not a validator. A row
carrying `cannonical_name` is accepted by the API and then invisible to every
query reading `canonical_name` — a silent data-loss bug that no runtime error
would ever surface. Forbidding unknown fields here is the only place that typo
can be caught.

**Only five tables of the 22 are modelled here**, on purpose. Story 1.4's job is
the generic repository machinery, and these five exercise all of it:
`districts` (no references at all), `people` (an embedded `terms[]` list),
`roll_calls` (an optional reference), `member_votes` (two required references
and a compound natural key — the clearest reference-validation case in
datamodel.md) and `verdicts` plus `candidate_positions` (the append-only
`is_current` path, with a single and a compound version key respectively).
Later stories add their own tables' models as they start ingesting them; the
pattern to copy is `RowModel`'s four `model_config` extras below.

Field-level fidelity to datamodel.md is deliberate but not exhaustive: fields
these five tables carry are typed, and the controlled vocabularies
(`position`, `vote_type`, `verdict`, `qa_status`) are `Literal`s rather than
bare strings, because an out-of-vocabulary value would otherwise flow into
scoring and be counted as "some other position" instead of rejected.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --- controlled vocabularies (datamodel.md "Legislative record" and
# "Promises, verdicts and positions") --------------------------------------

#: datamodel.md: "`position` takes `yea`, `nay`, `present` or `not_voting`".
VotePosition = Literal["yea", "nay", "present", "not_voting"]

#: datamodel.md: "`vote_type` takes `final_passage`, `amendment`, `cloture`,
#: `motion_to_recommit`, `procedural` or `other`". Scoring weights these
#: differently (a cloture vote is not a final-passage vote), so an unknown
#: value must not reach it.
VoteType = Literal[
    "final_passage",
    "amendment",
    "cloture",
    "motion_to_recommit",
    "procedural",
    "other",
]

#: datamodel.md: "`verdict` takes `kept`, `partial`, `broken`,
#: `no_opportunity`, `absent` or `not_testable`".
VerdictValue = Literal["kept", "partial", "broken", "no_opportunity", "absent", "not_testable"]

#: datamodel.md: "`qa_status` takes `pending`, `approved` or `rejected`".
QaStatus = Literal["pending", "approved", "rejected"]


class RowModel(BaseModel):
    """Base class for every Informed NoSQL row.

    Subclasses set four class variables, which together are the whole contract
    the generic `Repository` reads:

        table_name: str
            The ZeroDB table. One model, one table.

        natural_key: tuple[str, ...]
            The fields the id is built from, in order. Empty means the id is
            itself the natural key and is supplied by the caller (`districts`
            uses an OCD-ID; `people` a bioguide id). When non-empty, the
            repository derives the id with `build_id` and rejects a supplied id
            that disagrees — which is how two ingest adapters are stopped from
            minting two ids for the same record.

        strip_id_prefix: frozenset[str]
            Natural-key fields whose entity prefix is dropped when composing
            the id. `member_votes` lists `person_id`, because datamodel.md
            documents `{roll_call_id}:{bioguide}`; `candidate_positions` does
            NOT, because datamodel.md documents `{person_id}:...` with the
            prefix intact. Opt-in per field so each table's id matches its
            documented pattern rather than one global guess.

        references: dict[str, str]
            Field name to the table whose id that field holds. Checked before
            every write. A field that is None is skipped: an optional reference
            that is absent is not a dangling one.

        version_key: tuple[str, ...] | None
            None for an ordinary table. Non-None marks the table append-only
            (datamodel.md rule 6) and names the fields `is_current` is scoped
            by: `("promise_id",)` for `verdicts`, `("person_id", "stance_id")`
            for `candidate_positions`.

    `id` is optional on the model but always populated before a write, so a
    stored row always carries it. The repository fills it from `build_id` when
    the caller left it out.
    """

    model_config = ConfigDict(extra="forbid")

    table_name: ClassVar[str]
    natural_key: ClassVar[tuple[str, ...]] = ()
    strip_id_prefix: ClassVar[frozenset[str]] = frozenset()
    references: ClassVar[dict[str, str]] = {}
    version_key: ClassVar[tuple[str, ...] | None] = None

    id: str | None = None

    @classmethod
    def build_id(cls, values: dict[str, Any]) -> str | None:
        """The deterministic id for this record, or None if it cannot be built.

        Joins the natural-key fields with ":", stripping an entity prefix only
        from the fields the model lists in `strip_id_prefix`. **Per table, not
        global**, because datamodel.md's two documented patterns disagree:

          * `member_votes` is `{roll_call_id}:{bioguide}` — the bare bioguide,
            so `us-house:119-2:452:B000944`;
          * `candidate_positions` is `{person_id}:{stance_id}:{method_ver}` —
            the prefixed person id, so `person:B000944:...`.

        Stripping everywhere produced `B000944:en-US:...` for the second, an id
        no consumer rebuilding it from the row's own `person_id` would ever
        find. Making it opt-in per field keeps each table's id exactly as
        datamodel.md documents it.

        Returns None when the model has no natural key, or when a key field is
        missing: a model-validation error naming the missing field is a better
        answer than an id with a hole in it, and that error is raised by the
        field being required.
        """
        if not cls.natural_key:
            return None
        parts: list[str] = []
        for field_name in cls.natural_key:
            value = values.get(field_name)
            if value is None:
                return None
            text = str(value)
            parts.append(_id_suffix(text) if field_name in cls.strip_id_prefix else text)
        return ":".join(parts)

    def row_data(self) -> dict[str, Any]:
        """The dict to hand ZeroDB: every field, `None`s included.

        `None`s are kept rather than dropped, because clearing a field is a
        real update — `roll_calls.bill_id` going from a bill to None when a
        correction removes the association has to actually clear in storage,
        and ZeroDB's PUT replaces the row wholesale so an omitted key would
        simply vanish rather than clear.
        """
        return self.model_dump(mode="json")


def _id_suffix(value: str) -> str:
    """Strip Informed's entity prefix from one id value.

    `person:B000944` becomes `B000944`, matching datamodel.md's
    `{roll_call_id}:{bioguide}` pattern for `member_votes`. Applied **only** to
    the fields a model names in `strip_id_prefix` — see `build_id` for why that
    is per table rather than global.

    Ids that are not entity-prefixed (an OCD-ID, a roll-call id, whose colons
    separate meaningful segments) are returned whole: only a leading
    `person:`/`prm:`-style prefix is removed, and only from the front, never
    from the middle of an id.
    """
    prefix, separator, suffix = value.partition(":")
    if separator and prefix in _ID_PREFIXES:
        return suffix
    return value


#: Entity-prefixed id schemes from datamodel.md's id-pattern column. Only these
#: are stripped when an id is embedded inside a compound id; a jurisdiction or
#: roll-call id, whose colons separate meaningful segments, is left whole.
_ID_PREFIXES = frozenset({"person", "prm", "src", "stm", "cor", "aud", "prov", "vp"})


# --- Reference and people -------------------------------------------------


class District(RowModel):
    """A legislative district, keyed by its OCD-ID.

    Modelled here as the simplest case: no references at all, so it proves the
    repository skips reference checking entirely rather than making a pointless
    round trip (datamodel.md's districts reference `bodies`, but `bodies` is not
    modelled in this story, so the reference is carried as a plain field and
    will be declared when `Body` lands).
    """

    table_name: ClassVar[str] = "districts"

    body_id: str
    name: str
    state: str
    valid_from: str
    valid_to: str | None = None


class Term(BaseModel):
    """One term a person served. Embedded in `people`, never its own table.

    datamodel.md rule 3: "embed what is always read together — terms inside
    `people`". A term is never queried on its own, so it is a nested model
    rather than a row, and ZeroDB stores the list as an opaque array.
    """

    model_config = ConfigDict(extra="forbid")

    body_id: str
    district_id: str | None = None
    party: str | None = None
    start: str | None = None
    end: str | None = None


class Person(RowModel):
    """A legislator or candidate, keyed `person:{bioguide}` or `person:{uuid}`.

    The id is supplied rather than derived: a bioguide id is the natural key,
    and there is nothing to compose it from.
    """

    table_name: ClassVar[str] = "people"

    canonical_name: str
    external_ids: dict[str, str] = Field(default_factory=dict)
    photo_url: str | None = None
    terms: list[Term] = Field(default_factory=list)


# --- Legislative record ---------------------------------------------------


class Tally(BaseModel):
    """A roll call's vote counts, embedded in the roll call row."""

    model_config = ConfigDict(extra="forbid")

    yea: int = 0
    nay: int = 0
    present: int = 0
    not_voting: int = 0


class RollCall(RowModel):
    """A recorded vote, keyed `{body}:{session}:{roll}`.

    Carries the story's optional-reference case: `bill_id` is nullable because a
    roll call on a procedural motion need not attach to a bill, so the
    repository must check it only when it is present.
    """

    table_name: ClassVar[str] = "roll_calls"
    references: ClassVar[dict[str, str]] = {"bill_id": "bills"}

    voted_at: str
    question: str
    vote_type: VoteType
    result: str
    tally: Tally = Field(default_factory=Tally)
    bill_id: str | None = None
    amendment_ref: str | None = None
    is_bundled: bool = False
    topic_ids: list[str] = Field(default_factory=list)
    source_url: str | None = None
    content_hash: str | None = None


class MemberVote(RowModel):
    """How one person voted on one roll call, keyed `{roll_call_id}:{bioguide}`.

    The clearest reference-validation case in datamodel.md, and why it is
    modelled in this story: it references both `roll_calls` and `people`, and
    its id is composed from both. At roughly 600k rows per Congress it is also
    the table where the repository's per-call reference cache earns its keep —
    the several hundred votes on one roll call must not each re-read it.

    The italicised fields in datamodel.md (`voted_at`, `vote_type`, `bill_id`,
    `topic_ids`, `is_bundled`) are denormalised copies of the roll call's, so
    scoring reads one table. Keeping them in sync is an integrity check
    datamodel.md assigns to CI and the nightly audit, not to this write path:
    checking them here would mean re-reading the roll call's full row on every
    one of those 600k writes.
    """

    table_name: ClassVar[str] = "member_votes"
    natural_key: ClassVar[tuple[str, ...]] = ("roll_call_id", "person_id")
    #: datamodel.md documents `{roll_call_id}:{bioguide}` — the bare bioguide.
    strip_id_prefix: ClassVar[frozenset[str]] = frozenset({"person_id"})
    references: ClassVar[dict[str, str]] = {
        "roll_call_id": "roll_calls",
        "person_id": "people",
    }

    roll_call_id: str
    person_id: str
    position: VotePosition
    voted_at: str
    vote_type: VoteType
    bill_id: str | None = None
    topic_ids: list[str] = Field(default_factory=list)
    is_bundled: bool = False


# --- Promises, verdicts and positions ------------------------------------


class Verdict(RowModel):
    """One promise's verdict at one point in time, keyed `{promise_id}:v{n}`.

    Append-only (datamodel.md rule 6): a changed verdict is a new row, and
    `is_current` moves. The version number is assigned by the repository from
    what is already stored, not by the caller, so two ingest runs cannot both
    decide they are writing v3.

    `is_current` has no default on purpose. It is set by `append_version` and by
    nothing else: a caller able to pass `is_current=True` could create a second
    current row, which is exactly the integrity check datamodel.md lists
    ("exactly one `is_current = true` row exists per promise").
    """

    table_name: ClassVar[str] = "verdicts"
    references: ClassVar[dict[str, str]] = {
        "promise_id": "promises",
        "person_id": "people",
    }
    version_key: ClassVar[tuple[str, ...] | None] = ("promise_id",)

    promise_id: str
    person_id: str
    verdict: VerdictValue
    confidence: float = Field(ge=0.0, le=1.0)
    methodology_version: str
    qa_status: QaStatus = "pending"
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    is_current: bool | None = None
    created_at: str | None = None


class CandidatePosition(RowModel):
    """A person's computed position on one stance, under one methodology.

    Keyed `{person_id}:{stance_id}:{method_ver}` and append-only like
    `verdicts`, but with a **compound** version key: datamodel.md's integrity
    check is "exactly one `is_current = true` row per (person, stance)", not per
    person. Modelled in this story to prove the repository's `is_current`
    handling is not quietly single-field.

    `s_value` is bounded to [-1, 1] by datamodel.md. The bound is enforced here
    because a value outside it would not error anywhere downstream — it would
    just silently distort every alignment score computed from it.
    """

    table_name: ClassVar[str] = "candidate_positions"
    natural_key: ClassVar[tuple[str, ...]] = ("person_id", "stance_id", "methodology_version")
    #: No `strip_id_prefix`: datamodel.md documents `{person_id}:...` with the
    #: `person:` prefix intact here, unlike `member_votes`' `{bioguide}`.
    strip_id_prefix: ClassVar[frozenset[str]] = frozenset()
    references: ClassVar[dict[str, str]] = {
        "person_id": "people",
        "stance_id": "stances",
    }
    version_key: ClassVar[tuple[str, ...] | None] = ("person_id", "stance_id")

    person_id: str
    stance_id: str
    s_value: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_count: int = Field(ge=0)
    basis: str
    methodology_version: str
    is_current: bool | None = None
    computed_at: str | None = None

    @model_validator(mode="after")
    def _evidence_supports_a_confident_position(self) -> CandidatePosition:
        """No evidence means no confidence.

        prd.md's "evidence over opinion": a position computed from zero records
        must not be presented as a confident one, and the cheapest place to stop
        that is before it is stored.
        """
        if self.evidence_count == 0 and self.confidence > 0:
            raise ValueError("confidence must be 0 when evidence_count is 0")
        return self


__all__ = [
    "CandidatePosition",
    "District",
    "MemberVote",
    "Person",
    "QaStatus",
    "RollCall",
    "RowModel",
    "Tally",
    "Term",
    "Verdict",
    "VerdictValue",
    "VotePosition",
    "VoteType",
]
