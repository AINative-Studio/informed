"""The en-US issue taxonomy, version 1 (story 3.1).

What this is
============

Twenty top-level policy areas, each with three subtopics, seeded into
`issue_topics` as permanent reference data. It is the vocabulary the entire
product is measured against: a voter's interests (prd.md Feature 1) are
topics from this tree, story 3.2's stance cards hang off its subtopics, story
3.4 embeds both, and every alignment score is computed against it. Nothing
else in Informed invents a topic.

Why it lives in its own module, next to `app/seed.py`
=====================================================

`app/seed.py` seeds the *reference tree* — one jurisdiction and two chambers,
three rows that every ingest path writes against. This is ~80 rows of
*editorial content*: wording a cross-partisan advisory panel reviews before
each release (prd.md: "Stance cards and issue taxonomy reviewed by a
cross-partisan advisory panel before each release"). Those are different kinds
of data with different review paths, and folding them into one module would
mean a panel's wording change touching the module that story 2.3's districts
and 2.4's ballot depend on. Same mechanism (`Repository.upsert`), same
permanence, separate module.

It is deliberately **not** wired into `Bootstrapper.run()`, unlike
`app.seed.Seeder`. Story 2.1's three rows are a hard dependency of bootstrap's
own acceptance criterion ("rows exist after bootstrap") and cost three writes;
this is ~80 writes against a project observed to throttle at 60 requests per
minute (datamodel.md "Open questions"), each one a find-then-write costing more
than one request. Adding roughly three minutes of paced writes to every
bootstrap — including the many bootstraps that are no-op re-runs — to restate
rows that change only when a panel revises wording is the wrong trade. It gets
its own CLI instead: `python scripts/seed_taxonomy.py`.

Why these rows are PERMANENT
============================

Same rule as `app/seed.py`: this is production reference data, not test debris.
Deleting it would orphan every voter interest, stance card and score that cites
a topic id. A `en-US:healthcare:v1` row in the live project is this module's
output and is supposed to be there. The live verification script
(`scripts/verify_issue_taxonomy_live.py`) therefore does not clean up after
itself.

Ids, and why the version is in them
===================================

datamodel.md gives `issue_topics` the id pattern `{locale}:{slug}:v{n}`, so
`en-US:healthcare:v1`. Three consequences worth stating, because each one was a
decision:

  1. **The slug is flat, not parent-prefixed.** A subtopic is
     `en-US:prescription-drug-pricing:v1`, not
     `en-US:healthcare/prescription-drug-pricing:v1`. datamodel.md's pattern
     has exactly three segments and no room for a parent, and keeping the slug
     flat means re-parenting a subtopic in a later taxonomy version does not
     change its id — so a voter profile or stored promise citing it still
     resolves. The cost is that slugs must be unique across the whole tree,
     which `tests/test_issue_taxonomy_unit.py` asserts.
  2. **The version is part of the id**, so taxonomy v2 writes new rows rather
     than mutating these. That is what lets `issue_topics` stay an ordinary
     upsert table instead of an append-only one: history survives because v1's
     ids are never reused. A row citing `:v1` keeps meaning exactly what it
     meant when it was cited, which is prd.md's reproducibility promise applied
     to the taxonomy.
  3. **The locale is first**, matching datamodel.md's "tree by locale" query
     and prd.md's "adding a country is an adapter plus a taxonomy
     localization". A localized tree is a sibling set of rows under its own
     locale prefix, not a translation column on these.

Naming: nonpartisan by construction
===================================

prd.md's first product principle is "nonpartisan by construction", and for a
taxonomy that principle lands on the labels themselves. "Gun safety" and "Gun
rights" name the same policy area from opposite sides; a voter who rejects the
framing has been told what the app thinks before they have picked anything, and
no amount of downstream scoring neutrality repairs that.

So every label here names a **policy domain** or a **measurable quantity**,
never a desired direction:

  * domains: "Firearms policy", "Immigration and border policy",
    "Abortion and reproductive health policy";
  * quantities: "Prescription drug pricing", "Federal deficit and debt",
    "Housing supply and construction".

`STANCE_PRESUPPOSING_WORDS` holds the words a label may not contain, grouped
there by what each one smuggles in. The pattern they share: "Immigration
reform" presumes the status quo is broken, "Border security" presumes
enforcement is the frame, and "Voting rights" / "Election integrity" are the
two sides of one topic. `tests/test_issue_taxonomy_unit.py` fails the build if
any of them appears in a label, and `scripts/verify_issue_taxonomy_live.py`
re-checks the same rule against what is actually stored.

Two deliberate exceptions, both because the word is part of a proper name
rather than a framing:

  * **"Social Security"** — the name of a specific federal programme (Title II
    of the Social Security Act). It is checked as a whole phrase before the
    bare word "security" is looked for.
  * **"Civil liberties"** — the standard neutral name for the domain. The
    alternative, "Civil rights", collides with "rights"; "liberties" is used in
    both the constitutional-law and the political-science literature as the
    domain label, and there is no competing partisan label for it.

Where a topic has two commonly-used names from opposite sides, this module
names **both halves of the dispute** rather than picking one: "Abortion and
reproductive health policy", "Immigration and border policy", "Elections and
voting administration". Naming both is more neutral than finding a third word
that reads as a euphemism to everybody.

This list is explicitly reviewable, not final: prd.md routes taxonomy wording
through a cross-partisan advisory panel (story 3.3 builds that workflow), and a
panel edit is a new `taxonomy_version`, which under rule 2 above means new rows
rather than a rewrite of these.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from app.bootstrap import default_pacer
from app.repositories import Repository
from app.repositories.models import IssueTopic
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: The locale this module's tree is written in. One locale per module-level
#: tree: a localized taxonomy (prd.md's "adapter plus a taxonomy
#: localization") is a sibling set of rows under its own locale prefix, added
#: by the story that adds the country, not a translation of these rows.
TAXONOMY_LOCALE = "en-US"

#: Story 3.1's acceptance criterion: `taxonomy_version` = 1. A panel revision
#: bumps this and writes new rows under new `:v2` ids; see the module
#: docstring, rule 2.
TAXONOMY_VERSION = 1

#: "About 20 topics and 60 subtopics" (backlog.md 3.1), pinned exactly so that
#: a 21st topic is a deliberate edit with a test to update rather than drift.
EXPECTED_TOPIC_COUNT = 20
EXPECTED_SUBTOPIC_COUNT = 60


# ---------------------------------------------------------------------------
# The neutrality word list — see the module docstring for the reasoning.
# ---------------------------------------------------------------------------

#: Words a topic label must not contain, because each presupposes a stance on
#: the topic it labels rather than naming it. Checked as whole words, lowercase,
#: by `tests/test_issue_taxonomy_unit.py` and by the feature file, so a label
#: added by a later story cannot quietly reintroduce a framing.
#:
#: Grouped by what they smuggle in — the groups below are the comments in the
#: set itself, so the two cannot drift:
#:
#:   * **a side of a named dispute** — one half of a pair that names the same
#:     topic from two directions: gun-rights/gun-safety,
#:     voting-rights/election-integrity, pro-choice/pro-life,
#:     border-security/immigrant-rights;
#:   * **that the status quo is broken** — `reform`, `fix`, `crisis`,
#:     `modernize`, `restore`, `overhaul`, `broken`;
#:   * **that a direction is the good one** — `protection`, `relief`,
#:     `strengthen`, `defend`, `improve`, `save`;
#:   * **that the speaker is the reasonable party** — `sensible`,
#:     `commonsense`, `responsible`, `fairness`, and the pejoratives `burden`,
#:     `waste`, `handouts`, `amnesty`, `illegal`.
#:
#: Not an attempt at a complete lexicon of political framing — no word list
#: could be. It is a backstop that catches the specific framings this product's
#: topic area actually attracts, and it is the reviewable artefact a later
#: story (3.3's advisory panel) extends rather than a claim to be exhaustive.
#:
#: `security` and `liberty` have documented exceptions (`Social Security`,
#: `Civil liberties`); see `ALLOWED_PHRASES` and the module docstring.
STANCE_PRESUPPOSING_WORDS: frozenset[str] = frozenset(
    {
        # a side of a named dispute
        "rights",
        "safety",
        "security",
        "freedom",
        "liberty",
        "integrity",
        "choice",
        "pro-life",
        "pro-choice",
        # the status quo is broken
        "reform",
        "fix",
        "crisis",
        "modernize",
        "restore",
        "overhaul",
        "broken",
        # a direction is the good one
        "protection",
        "relief",
        "strengthen",
        "defend",
        "improve",
        "save",
        # the speaker is the reasonable party
        "sensible",
        "commonsense",
        "responsible",
        "fairness",
        "burden",
        "waste",
        "handouts",
        "amnesty",
        "illegal",
    }
)

#: Phrases containing a listed word that are nonetheless neutral, because the
#: word is part of a proper name or the standard domain label rather than a
#: framing. Both are argued in the module docstring. Kept as an explicit,
#: short, commented allowlist rather than loosening the word list, so each
#: exception is visible and has to be justified.
ALLOWED_PHRASES: tuple[str, ...] = (
    # Title II of the Social Security Act: a programme's name.
    "social security",
    # The standard neutral domain label; "Civil rights" would collide with
    # "rights" and has no more neutral competitor.
    "civil liberties",
)


def contains_stance_presupposing_word(name: str) -> str | None:
    """The first stance-presupposing word in `name`, or None if it is neutral.

    Allowlisted phrases (`ALLOWED_PHRASES`) are removed before the search, so
    "Social Security and retirement income" passes while "Border security"
    does not. Matching is on whole words, lowercased, so "Research" does not
    trip on "search" and "Defense" does not trip on "defend".

    Exposed as a function rather than inlined in the tests because the live
    verification script checks the same rule against what is actually stored —
    one implementation, three callers.
    """
    lowered = name.lower()
    for phrase in ALLOWED_PHRASES:
        lowered = lowered.replace(phrase, " ")
    for word in sorted(STANCE_PRESUPPOSING_WORDS):
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return word
    return None


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Topic:
    """One taxonomy node as authored, before it becomes a row.

    Authoring in a nested structure and flattening to rows in `taxonomy_rows()`
    keeps the parent/child relationship visible in the source — a reviewer sees
    the tree, not a list of 80 rows each repeating its parent's id — while the
    stored shape stays the flat `parent_id` table datamodel.md specifies.

    `slug` is the id's middle segment and must be unique across the whole tree
    (module docstring, rule 1). `name` is what a voter reads.
    """

    slug: str
    name: str
    subtopics: tuple[Topic, ...] = ()

    @property
    def row_id(self) -> str:
        """datamodel.md's `{locale}:{slug}:v{n}`.

        Composed here rather than by `RowModel.build_id`, which joins natural-key
        values verbatim and so would give `en-US:healthcare:1` without the
        literal `v`. See `IssueTopic`'s docstring.
        """
        return f"{TAXONOMY_LOCALE}:{self.slug}:v{TAXONOMY_VERSION}"

    def row(self, *, parent: Topic | None = None) -> IssueTopic:
        return IssueTopic(
            id=self.row_id,
            parent_id=None if parent is None else parent.row_id,
            name=self.name,
            locale=TAXONOMY_LOCALE,
            taxonomy_version=TAXONOMY_VERSION,
        )


def _topic(slug: str, name: str, *subtopics: tuple[str, str]) -> Topic:
    """A top-level topic and its subtopics, written as (slug, name) pairs."""
    return Topic(
        slug=slug,
        name=name,
        subtopics=tuple(Topic(slug=sub_slug, name=sub_name) for sub_slug, sub_name in subtopics),
    )


#: Words that appear in a slug but not in its label, with a reason each.
#:
#: Why this list exists rather than a rule that a slug must simply match its
#: label: **a slug is half of a stored id, and these ids are live and
#: permanent** (see "Why these rows are PERMANENT"). Renaming a slug does not
#: edit a row — it writes a *new* row under a new id and abandons the old one,
#: orphaning anything already citing it. So when a label is revised inside a
#: taxonomy version, the label changes and the slug stays exactly as it is.
#:
#: A slug being *shorter* than its label is normal and unremarkable
#: (`minimum-wage-and-overtime` for "Minimum wage and overtime rules"), so
#: that is not tracked. What is tracked is a slug containing a word its label
#: does not, because that is the signature of either a relabelling or a typo,
#: and the two need telling apart.
#:
#: Three entries are relabellings from story 3.1's own neutrality pass, made
#: after the slugs were fixed:
#:
#:   * `safety` — `food-safety-inspection` became "Food inspection and
#:     labeling". "Food safety" frames inspection as protection, and `safety`
#:     is on the stance list.
#:   * `choice`, `school`, `charters` — `school-choice-and-charters` became
#:     "Public, charter and private schooling". "School choice" is one side's
#:     term for the policy; the neutral label names the three sectors instead.
#:   * `forgiveness` — `student-loan-repayment-and-forgiveness` became
#:     "Student loan repayment and cancellation". "Forgiveness" implies a debt
#:     that ought to be excused; "cancellation" names the mechanism.
#:
#: Three are ordinary abbreviations, not relabellings:
#:
#:   * `speech` — `free-speech-and-assembly` for "Free expression and
#:     assembly"; "free speech" is the common short form of the domain.
#:   * `k12` — `education-k12` for "Education (K-12)"; the hyphen and
#:     parentheses are dropped from the slug.
#:   * `id` — `voter-registration-and-id` for "Voter registration and
#:     identification".
SLUG_WORDS_NOT_IN_LABEL: frozenset[str] = frozenset(
    {
        # relabelled during the neutrality pass
        "safety",
        "choice",
        "school",
        "charters",
        "forgiveness",
        # ordinary abbreviations
        "speech",
        "k12",
        "id",
    }
)

#: Words ignored when comparing a slug's words against its label's. Joining
#: words carry no meaning for this comparison and appear or drop out freely.
_SLUG_STOP_WORDS: frozenset[str] = frozenset({"and", "or", "of", "the", "a", "an", "to", "in"})


def slug_words_absent_from_label(slug: str, name: str) -> set[str]:
    """Words in `slug` that `name` does not contain, ignoring joining words.

    Used only by the drift test, never by the seeder: a stored slug is never
    recomputed from its label, because that would change an id. See
    `SLUG_WORDS_NOT_IN_LABEL`.
    """
    slug_words = _words(slug) - _SLUG_STOP_WORDS
    label_words = _words(name) - _SLUG_STOP_WORDS
    return slug_words - label_words


def _words(text: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


#: The en-US taxonomy, version 1: 20 top-level policy areas, 3 subtopics each.
#:
#: Ordering is alphabetical by `name`, deliberately. Any other order encodes an
#: editorial judgement about which issues matter most — exactly the judgement
#: prd.md forbids the app from making — and the UI is free to re-order per
#: voter (story 3.6's quick-pick path) without that choice being baked into the
#: stored data. "Healthcare" sorting between "Foreign policy" and "Housing" is
#: an accident of the alphabet, which is the point.
#:
#: Every label is a policy domain or a measurable quantity; see the module
#: docstring for the naming rule, and `STANCE_PRESUPPOSING_WORDS` for the words
#: that are rejected and why.
TOPICS: tuple[Topic, ...] = (
    _topic(
        "abortion-and-reproductive-health-policy",
        "Abortion and reproductive health policy",
        # Names both halves of the dispute rather than "reproductive rights"
        # (one side's label) or "life issues" (the other's).
        ("gestational-limits-and-exceptions", "Gestational limits and exceptions"),
        ("public-funding-of-abortion", "Public funding of abortion"),
        ("contraception-and-fertility-services", "Contraception and fertility services"),
    ),
    _topic(
        "agriculture-and-food-policy",
        "Agriculture and food policy",
        ("farm-subsidies-and-crop-insurance", "Farm subsidies and crop insurance"),
        ("food-assistance-programs", "Food assistance programs"),
        ("food-safety-inspection", "Food inspection and labeling"),
    ),
    _topic(
        "civil-liberties-and-discrimination-law",
        "Civil liberties and discrimination law",
        # "Civil liberties" is the standard domain label; see ALLOWED_PHRASES.
        ("free-speech-and-assembly", "Free expression and assembly"),
        ("religious-exemptions", "Religious exemptions"),
        ("anti-discrimination-enforcement", "Anti-discrimination enforcement"),
    ),
    _topic(
        "criminal-justice-and-policing",
        "Criminal justice and policing",
        # Not "police reform" or "law and order": both name a side.
        ("sentencing-and-incarceration", "Sentencing and incarceration"),
        ("police-funding-and-oversight", "Police funding and oversight"),
        ("drug-enforcement-and-treatment", "Drug enforcement and treatment"),
    ),
    _topic(
        "education-k12",
        "Education (K-12)",
        ("school-funding-formulas", "School funding formulas"),
        ("school-choice-and-charters", "Public, charter and private schooling"),
        ("curriculum-and-standards", "Curriculum and standards"),
    ),
    _topic(
        "elections-and-voting-administration",
        "Elections and voting administration",
        # Not "voting rights" and not "election integrity": the two labels for
        # this topic from opposite sides. "Administration" is what the policy
        # actually governs.
        ("voter-registration-and-id", "Voter registration and identification"),
        ("mail-and-early-voting", "Mail and early voting"),
        ("redistricting-and-apportionment", "Redistricting and apportionment"),
    ),
    _topic(
        "energy-policy",
        "Energy policy",
        ("oil-and-gas-production", "Oil and gas production"),
        ("renewable-and-nuclear-generation", "Renewable and nuclear generation"),
        ("electricity-prices-and-grid", "Electricity prices and grid capacity"),
    ),
    _topic(
        "environment-and-climate-policy",
        "Environment and climate policy",
        # "Climate policy", not "climate crisis" or "climate action".
        ("greenhouse-gas-emissions", "Greenhouse gas emissions"),
        ("air-and-water-quality-rules", "Air and water quality rules"),
        ("public-lands-and-conservation", "Public lands and conservation"),
    ),
    _topic(
        "federal-budget-and-taxes",
        "Federal budget and taxes",
        ("individual-income-tax-rates", "Individual income tax rates"),
        ("corporate-and-business-taxation", "Corporate and business taxation"),
        ("federal-deficit-and-debt", "Federal deficit and debt"),
    ),
    _topic(
        "firearms-policy",
        "Firearms policy",
        # Not "gun safety" and not "gun rights": the brief's own example of two
        # labels that each presuppose a stance.
        ("background-checks-and-purchase-rules", "Background checks and purchase rules"),
        ("firearm-categories-and-capacity-limits", "Firearm categories and capacity limits"),
        ("carry-permits-and-preemption", "Carry permits and state preemption"),
    ),
    _topic(
        "foreign-policy-and-defense",
        "Foreign policy and defense",
        ("defense-spending-and-force-posture", "Defense spending and force posture"),
        ("military-intervention-and-alliances", "Military intervention and alliances"),
        ("foreign-aid-and-sanctions", "Foreign aid and sanctions"),
    ),
    _topic(
        "healthcare",
        "Healthcare",
        # prd.md names "Healthcare -> Prescription drug pricing" itself; the
        # consistency anchor for the whole tree.
        ("prescription-drug-pricing", "Prescription drug pricing"),
        ("health-insurance-coverage", "Health insurance coverage"),
        ("medicare-and-medicaid-financing", "Medicare and Medicaid financing"),
    ),
    _topic(
        "higher-education-and-student-debt",
        "Higher education and student debt",
        ("tuition-and-institutional-aid", "Tuition and institutional aid"),
        ("student-loan-repayment-and-forgiveness", "Student loan repayment and cancellation"),
        ("workforce-and-vocational-training", "Workforce and vocational training"),
    ),
    _topic(
        "housing",
        "Housing",
        ("housing-supply-and-construction", "Housing supply and construction"),
        ("rent-levels-and-tenant-law", "Rent levels and tenant law"),
        ("homelessness-services", "Homelessness services"),
    ),
    _topic(
        "immigration-and-border-policy",
        "Immigration and border policy",
        # Names both halves rather than "immigration reform" (presumes the
        # status quo is broken) or "border security" (presumes enforcement is
        # the frame).
        ("legal-immigration-levels-and-visas", "Legal immigration levels and visas"),
        ("border-enforcement-and-asylum", "Border enforcement and asylum processing"),
        ("status-of-long-term-residents", "Status of long-term undocumented residents"),
    ),
    _topic(
        "infrastructure-and-transportation",
        "Infrastructure and transportation",
        ("roads-bridges-and-transit", "Roads, bridges and transit"),
        ("broadband-access", "Broadband access"),
        ("water-and-utility-systems", "Water and utility systems"),
    ),
    _topic(
        "jobs-wages-and-labor-law",
        "Jobs, wages and labor law",
        ("minimum-wage-and-overtime", "Minimum wage and overtime rules"),
        ("union-organizing-and-bargaining", "Union organizing and collective bargaining"),
        ("workplace-health-and-standards", "Workplace health and standards"),
    ),
    _topic(
        "social-security-and-retirement",
        "Social Security and retirement income",
        # "Social Security" is a programme name; see ALLOWED_PHRASES.
        ("social-security-solvency", "Social Security solvency and benefit levels"),
        ("retirement-age-and-eligibility", "Retirement age and eligibility"),
        ("private-retirement-savings", "Private retirement savings rules"),
    ),
    _topic(
        "technology-and-data-privacy",
        "Technology and data privacy",
        ("personal-data-collection-rules", "Personal data collection rules"),
        ("platform-liability-and-moderation", "Platform liability and content moderation"),
        ("artificial-intelligence-oversight", "Artificial intelligence oversight"),
    ),
    _topic(
        "veterans-and-military-families",
        "Veterans and military families",
        ("veterans-health-care-access", "Veterans health care access"),
        ("disability-and-benefit-claims", "Disability ratings and benefit claims"),
        ("transition-to-civilian-employment", "Transition to civilian employment"),
    ),
)


def taxonomy_rows() -> list[IssueTopic]:
    """Every row the seed writes, parents before their own children.

    The order is load-bearing, not cosmetic: `IssueTopic.parent_id` is a
    declared reference, so the repository resolves it before writing and a
    subtopic emitted before its parent is refused against a project that has
    never been seeded (datamodel.md rule 5). Asserted on this list directly in
    `tests/test_issue_taxonomy_unit.py`, so the guarantee belongs to the data
    rather than to the seeder's loop.
    """
    rows: list[IssueTopic] = []
    for parent in TOPICS:
        rows.append(parent.row())
        rows.extend(child.row(parent=parent) for child in parent.subtopics)
    return rows


def taxonomy_row_ids() -> set[str]:
    """Every id this module writes, for the live verifier to read back.

    Derived from `taxonomy_rows()` rather than listed separately, so the two
    cannot drift — a verifier checking a stale id list would report PASS on
    rows the seed no longer writes. Pinned by a test in the step-definition
    module.
    """
    return {str(row.id) for row in taxonomy_rows()}


# ---------------------------------------------------------------------------
# The seeder
# ---------------------------------------------------------------------------


@dataclass
class TaxonomySeedReport:
    """What one taxonomy seed run wrote, for the CLI's summary.

    Both lists hold every row the run wrote, whether or not its stored contents
    changed — like `app.seed.SeedReport`, this seed restates its rows rather
    than skipping existing ones, so "written" is the only count it can honestly
    report. The idempotency claim is about the stored row *count* and about the
    second run issuing PUTs rather than POSTs, both of which the feature file
    and the live verifier check directly.
    """

    topics_written: list[str] = field(default_factory=list)
    subtopics_written: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.topics_written) + len(self.subtopics_written)


class TaxonomySeeder:
    """Writes the en-US taxonomy, idempotently, parents before children.

    `Repository.upsert` is the whole idempotency mechanism (see
    `app/seed.py`'s docstring, which this module follows exactly): it finds the
    row by its natural-key id and replaces it, or inserts when there is none,
    so running the seed any number of times leaves exactly one row per id.
    "Replaces", not "skips" — this module is the single source of truth for the
    taxonomy, so a hand-edited label is restored on the next run rather than
    preserved.

    `pace` is the same injectable pacer `Bootstrapper` and `Seeder` use, and it
    matters more here than for either: ~80 rows, each an upsert costing a find
    and a write, against a project observed to throttle at 60 requests per
    minute (datamodel.md "Open questions").

    **It defaults to the real pacer, not a no-op** — unlike `app.seed.Seeder`,
    which takes its pacer from the `Bootstrapper` that owns it and so defaults
    to no pacing for the tests that construct it bare. This seeder's only
    production caller is a CLI of its own, with nothing above it to supply a
    pacer, and 80 unpaced writes is the one case here that would actually hit
    the ceiling. Tests pass `pace=lambda: None` explicitly.

    Not atomic, like every other write path here: two concurrent seeds could
    both find nothing for the same id and both insert. Acceptable for a seed
    that runs manually per environment, and the next run collapses the
    duplicate it would find.
    """

    def __init__(self, client: ZeroDBClient, *, pace: Callable[[], None] | None = None) -> None:
        self._client = client
        self._pace = pace if pace is not None else default_pacer()
        self._topics: Repository[IssueTopic] = Repository(client=client, model=IssueTopic)

    def run(self) -> TaxonomySeedReport:
        """Write every topic and subtopic and report them.

        Raises:
            MissingReferenceError: a subtopic's parent is not stored. In
                practice unreachable — `taxonomy_rows()` emits parents first —
                so it would mean a parent's write succeeded and the read back
                could not find it, which is the ZeroDB filter behaviour story
                1.4 pinned and is worth failing loudly on rather than seeding
                half a tree.
            ZeroDBError: a live call failed.
        """
        report = TaxonomySeedReport()
        for row in taxonomy_rows():
            self._pace()
            self._topics.upsert(row.row_data())
            row_id = str(row.id)
            if row.parent_id is None:
                report.topics_written.append(row_id)
            else:
                report.subtopics_written.append(row_id)
            logger.info("taxonomy seed: wrote %r (%s)", row_id, row.name)
        logger.info(
            "taxonomy seed: %d topic(s) and %d subtopic(s) written",
            len(report.topics_written),
            len(report.subtopics_written),
        )
        return report


# A guard on the editorial data itself, checked at import rather than only in
# the test suite: a seeder that writes the wrong number of rows into the live
# project is worse than one that refuses to start. The tests assert the same
# counts, and name the offending topic when they fail.
assert len(TOPICS) == EXPECTED_TOPIC_COUNT, (
    f"expected {EXPECTED_TOPIC_COUNT} top-level topics, got {len(TOPICS)}"
)
assert sum(len(parent.subtopics) for parent in TOPICS) == EXPECTED_SUBTOPIC_COUNT, (
    f"expected {EXPECTED_SUBTOPIC_COUNT} subtopics, "
    f"got {sum(len(parent.subtopics) for parent in TOPICS)}"
)


__all__ = [
    "ALLOWED_PHRASES",
    "EXPECTED_SUBTOPIC_COUNT",
    "EXPECTED_TOPIC_COUNT",
    "SLUG_WORDS_NOT_IN_LABEL",
    "STANCE_PRESUPPOSING_WORDS",
    "TAXONOMY_LOCALE",
    "TAXONOMY_VERSION",
    "TOPICS",
    "TaxonomySeedReport",
    "TaxonomySeeder",
    "Topic",
    "contains_stance_presupposing_word",
    "slug_words_absent_from_label",
    "taxonomy_row_ids",
    "taxonomy_rows",
]
