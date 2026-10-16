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

**Only some of the 22 tables are modelled here**, on purpose: a table gets its
model in the story that first reads or writes it. Story 1.4 modelled the five
that exercise all of the generic repository machinery, story 2.1 added
`jurisdictions` and `bodies` because it seeds them, and story 3.1 added
`issue_topics` — the first table whose declared reference points back at
itself — and story 3.2 added `stances`, whose id is required to extend the id
of the row it references.

Story 1.4's five and what each one proves:
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

#: What a cross-partisan advisory panel decided about one row's wording
#: (story 3.3; prd.md: "stance cards and issue taxonomy reviewed by a
#: cross-partisan advisory panel before each release").
#:
#: Five values. Two of them are decisions in the ordinary sense, and the other
#: three each exist because a real state in this workflow has no honest
#: spelling among the first two:
#:
#:   * `approved` — a named panel member read this wording and passed it. With
#:     `grandfathered`, the only value that lets wording through
#:     `PanelReviewService`'s gated read path.
#:   * `rejected` — a named panel member read it and refused it, with a reason
#:     (required; see `PanelReview._a_rejection_says_why`).
#:   * `proposed` — an author has offered new wording and nobody has judged it
#:     yet. It is in the ledger because a proposal must be *findable* by a
#:     reviewer and *visible* in the public history prd.md promises, and it is
#:     a `decision` value rather than a separate table because "what was
#:     proposed" and "what was decided about it" belong in one ordered record
#:     that cannot disagree with itself. It never passes the gate.
#:   * `withdrawn` — a proposal its own author pulled before anyone reviewed
#:     it. Recorded rather than deleted, because the ledger is append-only and
#:     "this wording was floated and dropped" is part of that same public
#:     record.
#:   * `grandfathered` — **not** approval. Stories 3.1 and 3.2 seeded the live
#:     taxonomy and stance corpus before this workflow existed, so those rows
#:     have never been through a panel. Marking them `approved` would be a lie
#:     about a review that did not happen; leaving them unmarked would make
#:     every gated read empty and take the product down. This value says
#:     exactly what is true: serving, and still owed a review. See
#:     `app.panel_review.service.PanelReviewService.grandfather_existing_corpus`.
#:
#: There is deliberately **no `pending`**, which `proposed` is not a synonym
#: for. `proposed` is a thing someone did; "pending" would be the absence of a
#: decision, and the absence of a decision is already represented by the
#: absence of a row. A ledger storing both would have to distinguish "no row"
#: from "a row saying nothing", and they mean the same thing to the gate. What
#: a reviewer queue needs — rows with no approval yet — is computed by
#: `PanelReviewService.queue` from what is and is not in the ledger.
ReviewDecision = Literal["approved", "rejected", "proposed", "withdrawn", "grandfathered"]

#: The tables story 3.3's review ledger passes judgement on: the two carrying
#: panel-reviewed editorial wording. A `Literal` rather than a bare string
#: because `PanelReview.subject_table` is resolved to a real table by
#: `PanelReviewService`, and a typo'd table name would store a decision that
#: no gate ever reads — the row would then serve unreviewed wording while the
#: ledger showed it approved, which is the one failure this whole story exists
#: to prevent.
ReviewSubjectTable = Literal["issue_topics", "stances"]

#: The scale a jurisdiction legislates at. datamodel.md's `jurisdictions` row
#: carries a `level` field and gives `us-federal`, `eu` and `uk` as its id
#: examples, without enumerating the vocabulary; these are the levels those
#: three examples need (`eu` is supranational, `uk` national, `us-federal`
#: federal) plus the two sub-national levels epic 17's non-US jurisdictions
#: will reach for. A `Literal` rather than a bare string for the same reason
#: every other vocabulary here is one: a jurisdiction stored at level
#: "fedral" raises nowhere — it just stops appearing in the query that asks
#: for federal ones.
JurisdictionLevel = Literal["supranational", "national", "federal", "state", "local"]

#: Which house of a bicameral legislature a body is, or `unicameral` for one
#: that has no counterpart. Normalised rather than named per country ("House"
#: vs "Commons" vs "Bundestag") so that a query for "the lower chamber" is one
#: query across every jurisdiction — prd.md's `JurisdictionAdapter` promise
#: that adding a country is an adapter, not a rewrite. The country's own name
#: for the chamber lives in `Body.name`.
Chamber = Literal["lower", "upper", "unicameral"]


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


class Jurisdiction(RowModel):
    """A polity whose legislature Informed tracks, keyed by a slug.

    datamodel.md: id pattern `us-federal`, `eu`, `uk`; fields name,
    country_code, level, adapter_name, active.

    The id is supplied rather than derived. There is no composable natural key
    — `us-federal` is not a function of "United States" and "federal" (the UK
    is `uk`, not `uk-national`) — so the slug IS the natural key, and the
    repository requires the caller to pass it, exactly as it does for `people`
    and `districts`.

    `adapter_name` names the `JurisdictionAdapter` implementation that ingests
    this jurisdiction (prd.md: "each jurisdiction plugs in through a
    `JurisdictionAdapter` interface — adding a country is an adapter plus a
    taxonomy localization, not a rewrite"). That interface is **story 17.1**;
    until it exists this is a recorded intent, deliberately a plain string
    rather than an import or a registry lookup, so nothing here pretends the
    adapter is implemented. The resolution from this string to a class belongs
    in 17.1, which is where a name with no adapter behind it should start
    failing loudly.

    `active` gates datamodel.md's documented main query ("All active"): a
    jurisdiction that has been retired stays in the table — rows referencing
    it keep resolving — but drops out of ingest and out of the ballot.
    """

    table_name: ClassVar[str] = "jurisdictions"

    name: str
    country_code: str = Field(min_length=2, max_length=2)
    level: JurisdictionLevel
    adapter_name: str
    active: bool = True


class Body(RowModel):
    """A legislative chamber, keyed by a slug: `us-house`, `us-senate`.

    datamodel.md: id pattern `us-house`, `us-senate`; fields jurisdiction_id,
    name, chamber, seats. Like `Jurisdiction`, the slug is the natural key and
    is supplied by the caller.

    `jurisdiction_id` is a declared reference, so the repository refuses a
    chamber whose jurisdiction is not stored (datamodel.md rule 5). That is
    what forces the seed to write `us-federal` before either chamber, and the
    ordering is asserted rather than assumed — see
    `tests/features/seed_reference_data.feature`.

    `seats` is bounded below because 0 seats is not a chamber, and a negative
    count would silently distort any later "share of the chamber" arithmetic
    rather than erroring.
    """

    table_name: ClassVar[str] = "bodies"
    references: ClassVar[dict[str, str]] = {"jurisdiction_id": "jurisdictions"}

    jurisdiction_id: str
    name: str
    chamber: Chamber
    seats: int = Field(ge=1)


class IssueTopic(RowModel):
    """One node of the issue taxonomy, keyed `{locale}:{slug}:v{n}` (story 3.1).

    datamodel.md: id pattern `{locale}:{slug}:v{n}`; fields parent_id, name,
    locale, taxonomy_version; main query "tree by locale".

    The id is supplied rather than derived, even though it looks composable.
    `build_id` joins natural-key values with ":" verbatim, which would give
    `en-US:healthcare:1` — the version segment needs the literal `v` prefix
    datamodel.md documents, and a natural key cannot express that. Composing it
    in the one place that knows the pattern (`app.seed_taxonomy.Topic.row_id`)
    and letting the repository require it, exactly as `Jurisdiction` and `Body`
    do for their slugs, keeps the stored id matching the doc rather than
    matching whatever `build_id` happens to produce.

    `parent_id` is a **self-reference**: a subtopic points at the top-level
    topic above it, in the same table. Declared as a reference so the
    repository refuses a subtopic whose parent is not stored (datamodel.md rule
    5), which is what forces the seed to write parents first. It is None for a
    top-level topic — an absent optional reference is not a dangling one, which
    is the case `Repository._check_references` already skips.

    What a self-reference does NOT buy, and why the validator below exists: the
    reference check asks only "does a row with this id exist", so a row naming
    *itself* as its parent resolves happily — the row is there, it is this one.
    That is an infinite tree, and a tree walk over it would not terminate. It
    is cheap to reject here and impossible to detect downstream, so it is
    rejected here.

    Deeper cycles (a -> b -> a) are NOT caught by this model: detecting one
    needs the whole tree, which a single row's validation cannot see. Story
    3.1's seed emits a fixed two-level tree and its tests assert that depth, so
    no cycle can arise from this story's data; a later story that lets an
    editor re-parent a topic is where a whole-tree acyclicity check belongs
    (and it is in this story's PR as an open question).

    `taxonomy_version` is the taxonomy's own revision, not a row version — the
    whole tree is published at a version, and all of story 3.1's rows are at 1.
    It is bounded below because a version 0 or negative is not a published
    taxonomy, and `issue_topics` is deliberately NOT append-only: a new
    taxonomy version writes new rows under new ids (`:v2`), so the previous
    version's rows survive untouched without `is_current` bookkeeping.
    """

    table_name: ClassVar[str] = "issue_topics"
    references: ClassVar[dict[str, str]] = {"parent_id": "issue_topics"}

    parent_id: str | None = None
    name: str = Field(min_length=1)
    locale: str
    taxonomy_version: int = Field(ge=1)

    @model_validator(mode="after")
    def _a_topic_is_not_its_own_parent(self) -> IssueTopic:
        """Reject a self-parented row; see the class docstring for why here."""
        if self.parent_id is not None and self.parent_id == self.id:
            raise ValueError(f"issue topic {self.id!r} is its own parent")
        return self


class Stance(RowModel):
    """One balanced stance card, keyed `{topic_id}:{slug}` (story 3.2).

    datamodel.md: id pattern `{topic_id}:{slug}`; fields topic_id, label,
    description, direction_code, wording_version; main query "by topic".

    A stance card is the outcome a voter wants, in plain words (prd.md Feature
    1: "lower out-of-pocket drug costs"). Three or more hang off every one of
    story 3.1's 60 subtopics, written to span the real disagreement rather than
    to avoid taking a position — see `app.seed_stances` for the corpus and the
    editorial rules it is held to.

    The id is supplied rather than derived, for the same reason `IssueTopic`'s
    is: `build_id` joins natural-key values verbatim, and the natural key here
    is (topic_id, slug) — which would in fact produce the right string. The
    problem is the other direction: `slug` is not a stored field, so declaring
    it in a natural key would mean either storing a redundant column or
    teaching `build_id` about a field the model does not have. Composing the id
    where the slug lives (`app.seed_stances.StanceCard.row_id`) and validating
    the relationship here is the smaller of the two.

    `topic_id` is a declared reference, so the repository refuses a card whose
    subtopic is not stored (datamodel.md rule 5). That is what makes story
    3.1's seed a hard prerequisite of this one, and it is checked live rather
    than assumed.

    **There is deliberately no `default`, `preselected`, `display_order` or
    `rank` field.** Story 3.2's third acceptance criterion is "no stance is
    preselected", and the way to satisfy it is for the concept to have nowhere
    to live: with `extra="forbid"`, a write carrying `default: true` is
    refused rather than stored and ignored, and a stored-but-ignored flag is
    the dangerous case — a later UI story would find it and honour it. The
    absence is pinned by a test (`test_stance_has_no_default_selection_field`)
    so that adding one has to be argued for.

    Ordering is absent for the same reason. datamodel.md gives this table no
    order column, and any stored order is a ranking: whichever card sits first
    gets picked more. Display order is randomized — per request by
    `app.stances.service.StanceService`, per session by the client. See that
    module's docstring for the split.

    `wording_version` is the panel-reviewed wording's revision (story 3.3 is
    the review workflow; prd.md: "wording changes are versioned and public").
    All of story 3.2's rows are at 1. Bounded below because version 0 is not
    published wording. Unlike `issue_topics`, the version is NOT part of the
    id: a wording fix must keep the id stable, because a voter's encrypted
    profile stores `stance_id` and re-wording a card must not orphan their
    saved choice. That is the opposite trade from the taxonomy's, and it is
    deliberate — a taxonomy version re-parents and renames topics, where a
    wording version only rewrites the same position in better English.
    """

    table_name: ClassVar[str] = "stances"
    references: ClassVar[dict[str, str]] = {"topic_id": "issue_topics"}

    topic_id: str
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    #: A signed position on the subtopic's own axis, as a string because
    #: ZeroDB's `stances` schema declares this column `"string"` (bootstrap's
    #: `TABLE_SCHEMAS`). See `app.seed_stances.DIRECTION_CODES` for the scheme
    #: and why it is signed rather than a set of named categories.
    direction_code: str = Field(pattern=r"^(?:0|[-+][12])$")
    wording_version: int = Field(ge=1)

    @model_validator(mode="after")
    def _the_id_extends_the_topic_id(self) -> Stance:
        """datamodel.md's `{topic_id}:{slug}`, checked rather than trusted.

        The one query datamodel.md documents for this table is "by topic", and
        it filters on the `topic_id` field — so a row whose id sat under a
        different topic's prefix would store fine and simply never appear in
        the menu for either topic. A silent disappearance is worse than a
        refused write, and the two values are written in different places
        (`StanceCard.row_id` composes the id, the card declares the slug), so
        a mismatch is a live possibility rather than a theoretical one.

        Skipped when `id` is None: the repository fills it in before writing,
        and a model constructed for validation alone need not carry one.
        """
        if self.id is not None and not self.id.startswith(f"{self.topic_id}:"):
            raise ValueError(
                f"stance id {self.id!r} does not extend its topic_id {self.topic_id!r}"
            )
        return self


class District(RowModel):
    """A legislative district, keyed by its OCD-ID.

    `body_id` holds a `bodies` id and **is** a declared reference, as of story
    2.3. Story 1.4 modelled this table with no references and explicitly left
    the call to 2.3, on the grounds that 2.3 is the story that writes real
    district rows and so is the one that should pay for turning the check on (a
    `bodies` row must exist before every `districts` row). 2.3 writes those rows
    lazily, from the geocoder's own answer, which makes the check load-bearing
    rather than ceremonial: it is the one thing standing between a resolved
    district and a `districts` row pointing at a chamber Informed has never
    heard of. datamodel.md rule 5 — integrity lives in code.

    It is also what keeps the ballot endpoint's documented 404 reachable. Once
    districts are created on demand, "Informed has no row for your district" is
    no longer a thing a voter can hit; "Informed cannot record your district,
    because its chamber was never seeded" is, and it is the same kind of failure
    — a data gap on Informed's side, not an address the voter can retype.
    """

    table_name: ClassVar[str] = "districts"
    references: ClassVar[dict[str, str]] = {"body_id": "bodies"}

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


# --- Governance and provenance --------------------------------------------


class PanelReview(RowModel):
    """One panel decision about one row's wording, keyed `pr:{sha256}` (3.3).

    prd.md: "Stance cards and issue taxonomy reviewed by a cross-partisan
    advisory panel before each release; wording changes are versioned and
    public." This is the ledger that makes "reviewed" a fact the code can check
    instead of a claim in a release note.

    Why a separate table rather than a status field on the reviewed row
    ===================================================================

    The obvious alternative — add `review_status`, `reviewed_by`,
    `reviewed_at` to `issue_topics` and `stances` — was rejected on four
    counts, each of which is on its own close to decisive:

      1. **The seeders would wipe it.** `app.seed_taxonomy.TaxonomySeeder` and
         `app.seed_stances.StanceSeeder` are documented as the single source of
         truth for their corpora and deliberately *restate* every row on every
         run ("a hand-edited label is restored on the next seed"). A status
         field on a stance row would be silently reset to the seeder's literal
         on the next `scripts/seed_stances.py`, which is the precise failure
         mode — unreviewed wording presenting as reviewed — this story exists
         to prevent.
      2. **A decision is history, not state.** prd.md promises wording changes
         are "versioned and public"; a single `reviewed_by` column holds the
         last reviewer and loses every earlier one, so "who passed the wording
         this card shipped under" becomes unanswerable the moment a second
         reviewer touches it. The third acceptance criterion is literally "the
         review history is visible", and a field cannot hold a history.
      3. **datamodel.md fixes those tables' fields.** `issue_topics` is
         documented with exactly `parent_id, name, locale, taxonomy_version`
         and `stances` with exactly `topic_id, label, description,
         direction_code, wording_version`. Both models set `extra="forbid"`
         and both have tests pinning the absence of extra fields (`stances`
         pins it hard, because that is how "no stance is preselected" is
         enforced structurally). Adding review columns means contradicting the
         data model and loosening the guard that keeps a `default: true` out of
         a stance row.
      4. **It would mean writing to 281 already-live rows.** The live project
         holds 80 `issue_topics` and 201 `stances` rows that four endpoints
         (stories 3.5, 4.1, 4.2, 4.4) read on every request. A separate ledger
         annotates them by id without touching a byte of them.

    The cost of the separate table, stated plainly: reading a row's status is a
    second read, and nothing in ZeroDB enforces that `subject_id` names a real
    row. The first is cheap at these sizes (both corpora fit in one page, and
    `PanelReviewService` reads the ledger once per request). The second is
    datamodel.md rule 5's standing answer — "integrity lives in code" — and is
    handled by `references` below, so the repository refuses a decision about a
    row that does not exist.

    Why the id is content-addressed
    ===============================

    `pr:{sha256}` of (subject_table, subject_id, version, reviewer_id,
    decision, reason, proposed-wording) — the same scheme `provenance_log`
    uses, and for the same reason: it makes the write idempotent without any
    bookkeeping. Re-running the grandfathering backfill, or a reviewer
    double-clicking approve, lands on a row that already exists rather than
    appending a second identical decision. Two *different* decisions about the
    same row hash differently and both persist, which is exactly right for a
    ledger — see `app.panel_review.service.review_id`.

    A sequential `{subject_id}:v{n}` id with `Repository.append_version` was
    the alternative and is wrong here twice over: it would make a repeated
    backfill append 281 duplicate rows per run, and `is_current` is the wrong
    model for a ledger whose whole value is that every entry stays readable.
    Which decision is in force is derived by `PanelReviewService` from
    `decided_at` plus the subject's current version, not from a flag.

    Fields
    ======

    `subject_version` is the `wording_version` (for a stance) or
    `taxonomy_version` (for a topic) that the reviewer actually read. It is the
    load-bearing field of the whole gate: an approval of version 1 says nothing
    about version 2, so bumping a version re-opens the question automatically
    rather than carrying an old approval forward. `PanelReviewService` refuses
    a decision naming a version the row is not at, so a reviewer cannot approve
    wording that has already moved under them.

    `reviewer_id` is a plain string and an explicit authentication stand-in,
    exactly as `user_id` is in `app.profiles.router` (see that module's
    docstring): no auth layer exists anywhere in this codebase yet, and
    inventing one here would be a different story. It is recorded rather than
    verified, and the honest consequence is that this ledger proves *what was
    claimed*, not *who claimed it*, until real authentication arrives. That is
    still strictly more than nothing — it is the difference between a release
    note saying "reviewed" and a row naming a reviewer, a version and a time —
    and it is flagged in this story's PR as the open question it is.

    **Panel composition is deliberately not modelled.** prd.md lists "who sits
    on the cross-partisan advisory panel, and how members are chosen" as an
    open question, and it is a governance decision rather than an engineering
    one. There is no `panel_members` table, no roster check and no quorum rule:
    this story builds the *workflow*, and a later story adds the roster once
    the business answers who is on it. What is enforced is the one rule that
    holds whatever the roster turns out to be — a reviewer may not approve
    their own proposal (`PanelReviewService` checks it, not this model, because
    it needs the proposal's author).

    `proposed_label`/`proposed_description`/`proposed_name` carry the *new*
    wording a proposal asks for, so the live row keeps its approved wording
    until the change is approved. `previous_label`/`previous_description`/
    `previous_name` carry what it replaced, which is what makes "the history
    shows what changed" answerable from the ledger alone rather than by
    diffing against a seeder constant that may itself have moved.
    """

    table_name: ClassVar[str] = "panel_reviews"
    #: `subject_id` is NOT declared here even though it names a real row: the
    #: generic reference check maps one field to one fixed table, and this
    #: field's table is whichever `subject_table` names. `PanelReviewService`
    #: resolves it explicitly before every write — the same split
    #: `ProvenanceLog` documents for its own `entity_table`/`entity_id` pair.
    references: ClassVar[dict[str, str]] = {}

    subject_table: ReviewSubjectTable
    subject_id: str = Field(min_length=1)
    subject_version: int = Field(ge=1)
    decision: ReviewDecision
    reviewer_id: str = Field(min_length=1)
    decided_at: str
    #: Why a reviewer refused the wording. Required for `rejected` (see the
    #: validator): a rejection without a reason is unactionable — the author
    #: cannot write a better version from it — and prd.md's "public" promise
    #: about wording changes is empty if the record does not say what was wrong.
    reason: str | None = None
    #: The proposal this decision judges, when it judges one rather than the
    #: live row. None for a plain approval of wording already stored.
    proposal_id: str | None = None
    proposed_label: str | None = None
    proposed_description: str | None = None
    proposed_name: str | None = None
    previous_label: str | None = None
    previous_description: str | None = None
    previous_name: str | None = None

    @model_validator(mode="after")
    def _a_rejection_says_why(self) -> PanelReview:
        """A rejected wording must carry its reason; see `reason`."""
        if self.decision == "rejected" and not (self.reason or "").strip():
            raise ValueError(
                f"panel review of {self.subject_id!r} rejects the wording but gives no "
                "reason; a rejection the author cannot act on is not a review"
            )
        return self

    @model_validator(mode="after")
    def _a_topic_is_not_judged_on_stance_wording(self) -> PanelReview:
        """Keep each subject table's wording fields to its own.

        A decision about an `issue_topics` row carrying `proposed_label` (a
        `stances` field) means the caller resolved the wrong table or the wrong
        row, and the stored decision would then be read as being about wording
        it never judged. Cheap to reject, invisible downstream.
        """
        if self.subject_table == "issue_topics" and (
            self.proposed_label is not None or self.proposed_description is not None
        ):
            raise ValueError(
                f"panel review of issue topic {self.subject_id!r} carries stance wording; "
                "a topic has a name, not a label and description"
            )
        if self.subject_table == "stances" and self.proposed_name is not None:
            raise ValueError(
                f"panel review of stance {self.subject_id!r} carries a topic name; "
                "a stance has a label and description, not a name"
            )
        return self


class ProvenanceLog(RowModel):
    """One fetch of one external record, keyed `prov:{sha256}` (story 5.1).

    datamodel.md: id pattern `prov:{sha256}`; fields entity_table, entity_id,
    source_url, fetched_at, adapter_version; retained indefinitely,
    append-only ("never updated in place").

    Modelled here, not as a `version_key` append-only table like `Verdict`,
    because its id scheme is a different kind of append-only. `Verdict`'s id
    is `{promise_id}:v{n}` — sequential, numbered by `Repository.append_version`
    from what is already stored. `provenance_log`'s id is content-addressed:
    the sha256 of what was fetched (see `app.ingest.house_rollcalls.provenance_id`),
    so two fetches of byte-identical content deterministically produce the
    SAME id rather than two sequential versions of it. That gives idempotency
    for free — re-ingesting unchanged data naturally lands on a row that
    already exists — but it means `Repository.append_version`'s version-number
    bookkeeping does not apply and must not be used here: there is no
    `{prefix}:v{n}` to number, and forcing one would fight the id this table
    is documented to have.

    What "append-only" means operationally for THIS table, then, is simpler
    than `Verdict`'s: never call `Repository.upsert` on it (that would silently
    replace one fetch's provenance with another's under a shared id, which
    cannot happen anyway since the id is content-derived, but `upsert` is still
    the wrong verb). Write with `Repository.append_version`'s sibling —
    `app.ingest.house_rollcalls.write_provenance`, which does a plain
    check-then-insert (`get` by the content-addressed id; `insert_row` only if
    absent) through `ZeroDBClient` directly, since there is no version number
    to compute and no `is_current` flag to move. A later story that writes
    `provenance_log` for a different source should reuse that helper rather
    than inventing a second append-only pattern for the same table.

    `references` is intentionally empty even though datamodel.md's integrity
    rule ties every provenance row to the row it documents ("Every NoSQL row
    with a source_url has a matching provenance_log entry"): `entity_table` is
    a table NAME, not a row id, and the generic reference check (`field ->
    fixed table`) has no way to express "look up `entity_id` in whichever
    table `entity_table` names". Checking that association is this story's
    adapter's job (`house_rollcalls.py` writes the `roll_calls` row first, in
    the same call that writes its provenance entry), and the nightly
    integrity audit datamodel.md specifies is the backstop, not a declared
    reference here.
    """

    table_name: ClassVar[str] = "provenance_log"

    entity_table: str
    entity_id: str
    source_url: str
    fetched_at: str
    adapter_version: str


__all__ = [
    "Body",
    "CandidatePosition",
    "Chamber",
    "District",
    "IssueTopic",
    "Jurisdiction",
    "JurisdictionLevel",
    "MemberVote",
    "Person",
    "ProvenanceLog",
    "QaStatus",
    "RollCall",
    "RowModel",
    "Stance",
    "Tally",
    "Term",
    "Verdict",
    "VerdictValue",
    "VotePosition",
    "VoteType",
]
