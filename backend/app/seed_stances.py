"""Balanced stance cards for the en-US taxonomy, wording version 1 (story 3.2).

What this is
============

Three stance cards for every one of story 3.1's 60 subtopics — 180 rows of
editorial content seeded into `stances` as permanent reference data. A card is
"the outcome the voter wants, in plain words" (prd.md Feature 1), and these are
the only positions a voter can attach to a topic. A voter's encrypted profile
stores a `stance_id` from this file; every alignment score is computed against
one; every promise and roll call is classified as advancing or opposing one.

**This is the single highest-stakes neutrality surface in the product.** Story
3.1's topic labels could at worst annoy a voter with a framing; these cards
*are* the menu of available opinions. A set that offers two real positions and
one strawman does not merely read badly — it moves the scores, because a voter
steered off their actual view is then scored against the wrong stance, and
prd.md's "Dealbreaker overrides the average" scenario fires on the stance they
picked rather than the one they hold. So the editorial rules below are enforced
by tests, not left to good intentions.

How it relates to story 3.1's neutrality check
==============================================

The rule is inverted, and that inversion is the whole design.

Story 3.1 forbids a topic *name* from presupposing a stance: "Gun safety" and
"Gun rights" name the same area from opposite sides, so `issue_topics` labels
may not contain `safety`, `rights`, `reform`, `protection` and the rest of
`app.seed_taxonomy.STANCE_PRESUPPOSING_WORDS`.

A stance card's entire job is to state a position, so that list would reject
every row here — "Require a background check for every firearm sale" is
*supposed* to presuppose something. Neutrality for a stance card is a property
of the **set**, not of the sentence:

  1. **The set spans the real disagreement.** Three or more cards per subtopic,
     with `direction_code`s on both sides of the axis and no code repeated, so
     the menu is not one position in three outfits. Enforced per subtopic in
     `tests/test_stance_cards_unit.py` and over the stored rows in
     `tests/features/stance_cards.feature`.
  2. **Every side is written with equal seriousness.** The classic rig is to
     write your own position with specifics and the other as a caricature
     ("ban them all"). Tests compare per-side description length across the
     corpus and within the most contested subtopics, and put a floor under
     every description, because a one-liner next to a paragraph is a thumb on
     the scale whatever the words say.
  3. **No card names a side.** `PARTISAN_IDENTITY_WORDS` is the stance-card
     analogue of 3.1's list and holds party, movement and pejorative labels
     from *both* directions. A voter who agrees with a position but rejects its
     badge has been pushed off their own view, which is the same harm 3.1
     guards against, one level down.
  4. **Nothing is preselected and nothing is ranked.** See `Stance`'s docstring
     for why the model has no field for either, and `app.stances.service` for
     where display order actually comes from.

These are checkable proxies, not a proof of fairness. The real review is
prd.md's: "stance cards and issue taxonomy reviewed by a cross-partisan
advisory panel before each release", which story 3.3 builds. The tests are what
stops the corpus drifting between panels; `wording_version` is what lets a
panel revise it.

`direction_code`: why a signed axis
===================================

datamodel.md gives `stances` a `direction_code` and does not define its
vocabulary. `DIRECTION_CODES` below makes it a **signed position on the
subtopic's own axis**: `-2`, `-1`, `0`, `+1`, `+2`, stored as strings because
ZeroDB's `stances` schema declares the column `"string"`.

Why signed and ordered rather than a set of named categories
(`"expand"`/`"restrict"`/`"status-quo"`): epic 4's scoring needs to project a
candidate's measured position onto whichever stance the voter picked.
datamodel.md's `candidate_positions.s_value` runs −1 to +1 *against a stance*,
and prd.md's formula multiplies it straight into the alignment sum. Given a
candidate's position `a` on a subtopic's axis and a stance at `d`, a signed
scheme makes the projection arithmetic — the sign of `d` says which way the
voter wants to go and its magnitude says how far — where named categories
would need a hand-written table per subtopic mapping category pairs to a
number. The sign convention is per subtopic and recorded in the comment above
each group; it has no global political meaning, and deliberately so: there is
no axis along which "the federal government does more" is `+1` for every topic
in this file, and inventing one would be a political claim.

Magnitudes are used sparingly and symmetrically. `±1` is the common case: a
clear position in one direction. `±2` is for a subtopic where the real debate
has a maintained far position that a moderate one does not stand in for — a
total ban, say, versus a partial restriction. A corpus-level test holds the
counts of `-2` and `+2` within a few cards of each other, because offering a
strong option on one side only is a tilt that every per-subtopic check would
miss.

`0` means "this position does not move along the axis" — keep current federal
policy, or route the decision elsewhere (to states, to courts, to a study). It
is a real position that real voters hold, not a filler option, and it is never
the only alternative to a signed one: a subtopic whose three cards were `-1`,
`0`, `+1` with the two signed ones written thinly would pass every structural
check, so the length rules apply to it too.

Why it lives beside `app/seed_taxonomy.py`
==========================================

Same reasoning as 3.1's own module docstring: this is panel-reviewed editorial
content with its own review path, not the structural reference tree
`app/seed.py` writes. It is deliberately **not** wired into
`Bootstrapper.run()` — 180 upserts, each a find-then-write, against a project
observed to throttle at 60 requests per minute (datamodel.md "Open questions")
is several minutes added to every bootstrap including the no-op re-runs. It
gets its own CLI: `python scripts/seed_stances.py`, run after
`scripts/seed_taxonomy.py`, which is a hard prerequisite because `topic_id` is
a declared reference.

Why these rows are PERMANENT
============================

Same rule as `app/seed.py` and `app/seed_taxonomy.py`: production reference
data, not test debris. A voter's encrypted profile stores `stance_id`s from
this file, so deleting a row orphans a saved profile, every
`candidate_positions` row computed against it and every promise classified
under it. A `en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs` row in
the live project is this module's output and is supposed to be there.
`scripts/verify_stance_cards_live.py` therefore does not clean up after itself.

Ids, and why the wording version is NOT in them
===============================================

datamodel.md gives `stances` the id pattern `{topic_id}:{slug}`, so
`en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs`. The topic id
already carries the taxonomy version, which is what keeps a card pinned to the
tree it was written against.

The *wording* version is deliberately absent from the id, the opposite of
3.1's choice. A taxonomy version re-parents and renames topics, so new rows
under new ids are right: a profile citing `:v1` keeps meaning what it meant. A
wording version only rewrites the same position in better English, and a
voter's saved choice must survive that — re-wording a card under a new id would
silently orphan every profile that had picked it. So `wording_version` is a
field, the id is stable, and the panel's edit history is story 3.3's problem
rather than the id scheme's.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from app.bootstrap import default_pacer
from app.repositories import Repository
from app.repositories.models import Stance
from app.seed_taxonomy import EXPECTED_SUBTOPIC_COUNT, TAXONOMY_LOCALE, TAXONOMY_VERSION
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: Story 3.2's acceptance criterion names no version, but prd.md requires
#: stance wording to be versioned ("wording changes are versioned and public")
#: and story 3.3's panel workflow bumps this. All of story 3.2's rows are at 1.
WORDING_VERSION = 1

#: Story 3.2's acceptance criterion: "each subtopic has at least 3 stances".
#: Pinned as a constant so the per-subtopic test and the live verifier read the
#: same number, and so lowering it is a visible edit.
MINIMUM_STANCES_PER_SUBTOPIC = 3

#: The `direction_code` vocabulary: a signed position on the subtopic's own
#: axis. See the module docstring for why signed and ordered rather than named
#: categories, and for what each magnitude means. Stored as strings because
#: ZeroDB's `stances` schema declares the column `"string"`; `Stance` pins the
#: same shape as a field pattern so a row written by any other path is refused.
DIRECTION_CODES: tuple[str, ...] = ("-2", "-1", "0", "+1", "+2")


# ---------------------------------------------------------------------------
# Wording neutrality — the stance-card analogue of story 3.1's word list
# ---------------------------------------------------------------------------

#: Words a stance card's label or description must not contain, because each
#: names a political tribe rather than an outcome. This is NOT
#: `app.seed_taxonomy.STANCE_PRESUPPOSING_WORDS` — that list forbids taking a
#: position, which is the opposite of what a card is for. See the module
#: docstring, rule 3.
#:
#: Both sides of every pair are listed. A list that caught only one direction's
#: badges would itself be partisan, and the test
#: `test_the_identity_word_list_is_not_empty_or_trivially_satisfied` spot-checks
#: pairs for exactly that reason.
#:
#: Grouped by what each kind of word does:
#:
#:   * **party and institution names** — attaching a position to a party tells
#:     a voter which team the view belongs to before they have read it;
#:   * **ideology labels** — `liberal`, `conservative`, `progressive`,
#:     `socialist`, `libertarian`: a voter who holds the position but rejects
#:     the label has been pushed off their own view;
#:   * **movement badges** — `maga`, `woke`, `antifa`, `globalist`: used almost
#:     exclusively by opponents, so they read as a sneer whichever position
#:     they are attached to;
#:   * **pejoratives for the other side's position** — `radical`, `extreme`,
#:     `far-right`, `far-left`, `fringe`, `agenda`: a card using these is
#:     arguing against a neighbouring card rather than stating its own view.
#:
#: Matched as whole words, lowercased, with hyphenated entries matched as
#: written. `contains_partisan_identity_word` is the one implementation; the
#: unit tests, the feature file and the live verifier all call it.
PARTISAN_IDENTITY_WORDS: frozenset[str] = frozenset(
    {
        # party and institution names
        "democrat",
        "democrats",
        "democratic party",
        "republican",
        "republicans",
        "gop",
        "biden",
        "trump",
        # ideology labels
        "liberal",
        "liberals",
        "conservative",
        "conservatives",
        "progressive",
        "progressives",
        "socialist",
        "socialism",
        "communist",
        "libertarian",
        "leftist",
        "right-wing",
        "left-wing",
        "rightwing",
        "leftwing",
        # movement badges
        "maga",
        "woke",
        "antifa",
        "globalist",
        "globalists",
        "elites",
        "deep state",
        # pejoratives for a neighbouring position
        "radical",
        "extremist",
        "extreme",
        "far-right",
        "far-left",
        "fringe",
        "agenda",
        "so-called",
    }
)


def contains_partisan_identity_word(text: str) -> str | None:
    """The first partisan-identity word in `text`, or None if it names none.

    Matching is on whole words, lowercased, so "conservation" does not trip on
    `conservative` and "independent" does not trip on any `-dent` entry.
    Multi-word entries ("deep state") are matched as phrases.

    Exposed as a function rather than inlined in the tests because the live
    verification script checks the same rule against what is actually stored —
    one implementation, three callers, exactly as story 3.1 does for topic
    names.
    """
    lowered = text.lower()
    for word in sorted(PARTISAN_IDENTITY_WORDS):
        if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", lowered):
            return word
    return None


# ---------------------------------------------------------------------------
# The cards
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StanceCard:
    """One stance card as authored, before it becomes a row.

    `topic_slug` is a story 3.1 **subtopic** slug — the flat slug, not the id,
    so the author writes `prescription-drug-pricing` and the locale and
    taxonomy version are composed here. A typo is caught by
    `test_every_stance_topic_slug_is_a_real_subtopic` before any ZeroDB call,
    and by the repository's reference check if it somehow gets past that.

    `slug` is the id's last segment and must be unique within its subtopic (the
    topic id disambiguates across subtopics, so `cap-costs` may appear under
    two of them). It must not encode a position in the list — see
    `test_no_slug_leaks_the_direction_code`: the id reaches a voter's saved
    profile, and an id reading "option 1" reintroduces the ordering the
    shuffle exists to remove.
    """

    topic_slug: str
    slug: str
    label: str
    description: str
    direction_code: str

    @property
    def topic_id(self) -> str:
        """Story 3.1's `{locale}:{slug}:v{n}` for this card's subtopic."""
        return f"{TAXONOMY_LOCALE}:{self.topic_slug}:v{TAXONOMY_VERSION}"

    @property
    def row_id(self) -> str:
        """datamodel.md's `{topic_id}:{slug}`."""
        return f"{self.topic_id}:{self.slug}"

    def row(self) -> Stance:
        return Stance(
            id=self.row_id,
            topic_id=self.topic_id,
            label=self.label,
            description=self.description,
            direction_code=self.direction_code,
            wording_version=WORDING_VERSION,
        )


def _cards(topic_slug: str, *cards: tuple[str, str, str, str]) -> tuple[StanceCard, ...]:
    """One subtopic's cards, written as (slug, direction_code, label, description).

    The direction code sits second rather than last so a reader scanning a
    group sees the axis coverage down the left-hand side without reading the
    wording — which is how the balance of a group is actually reviewed.
    """
    return tuple(
        StanceCard(
            topic_slug=topic_slug,
            slug=slug,
            direction_code=direction_code,
            label=label,
            description=description,
        )
        for slug, direction_code, label, description in cards
    )


#: Every stance card, grouped by top-level topic in the same alphabetical order
#: story 3.1's `TOPICS` uses — for the same reason: any other order encodes a
#: judgement about which issues matter most, which prd.md forbids the app from
#: making.
#:
#: **How to read a group.** The comment above each subtopic records that
#: subtopic's axis and its sign convention, because the convention is local:
#: there is no direction along which `+1` means the same thing for firearms and
#: for farm subsidies, and inventing one would be a political claim. The
#: direction code is the second value on each line so the axis coverage reads
#: down the left of a group without reading the wording, which is how a group's
#: balance is actually reviewed.
#:
#: **Within a group the cards are NOT written in axis order.** A read that comes
#: back in insertion order is what a client with a broken shuffle would show,
#: and an axis-ordered menu makes one end of the spectrum the first thing every
#: voter reads. The ordering test fails the build if any group drifts back into
#: sorted order — defence in depth behind `StanceService`'s per-request shuffle.
STANCES: tuple[StanceCard, ...] = (
    # === Abortion and reproductive health policy =========================
    # Axis: legal access to abortion. Negative = narrower legal access,
    # positive = broader. Both magnitudes are used here because the maintained
    # far positions (no abortion at all; no gestational limit in statute) are
    # genuinely held and are not stood in for by the partial ones.
    *_cards(
        "gestational-limits-and-exceptions",
        (
            "viability-standard",
            "+1",
            "Allow abortion until fetal viability",
            "Keep abortion legal through roughly 24 weeks, with later procedures allowed when a "
            "physician finds a serious risk to the patient's health or a fatal fetal diagnosis.",
        ),
        (
            "early-pregnancy-limit",
            "-1",
            "Limit abortion to early pregnancy",
            "Set a federal limit at around six to fifteen weeks, with exceptions for the life of "
            "the mother, serious physical health risk, rape and incest.",
        ),
        (
            "no-statutory-limit",
            "+2",
            "Leave the timing to the patient and her doctor",
            "Write no gestational limit into federal law at all, treating the decision and its "
            "timing as a medical judgement between a patient and her physician.",
        ),
        (
            "prohibit-with-life-exception",
            "-2",
            "Prohibit abortion except to save the mother's life",
            "Ban abortion at every stage of pregnancy, with a single exception for procedures "
            "needed to prevent the death of the pregnant woman.",
        ),
    ),
    # Axis: public money for abortion. Negative = tighter restrictions on
    # public funds, positive = broader public coverage.
    *_cards(
        "public-funding-of-abortion",
        (
            "keep-hyde-restrictions",
            "-1",
            "Keep federal funds out of elective abortion",
            "Maintain the long-standing rule barring federal money from paying for abortion "
            "except in cases of rape, incest or danger to the mother's life.",
        ),
        (
            "cover-under-federal-health-programs",
            "+1",
            "Cover abortion like other medical care",
            "Let Medicaid, military health plans and federal employee insurance pay for abortion "
            "on the same terms as any other covered procedure.",
        ),
        (
            "bar-all-indirect-funding",
            "-2",
            "Bar any federal money reaching abortion providers",
            "Extend the funding ban to grants, tax credits and reimbursements for clinics and "
            "organizations that provide abortions, including for their unrelated services.",
        ),
    ),
    # Axis: public support for contraception and fertility services.
    # Negative = narrower mandates and more exemptions, positive = broader
    # guaranteed coverage.
    *_cards(
        "contraception-and-fertility-services",
        (
            "guarantee-no-cost-contraception",
            "+1",
            "Guarantee contraception at no out-of-pocket cost",
            "Require every health plan to cover the full range of prescription contraception, "
            "including long-acting methods, with no copay or deductible.",
        ),
        (
            "leave-coverage-to-plans-and-employers",
            "-1",
            "Let plans and employers decide what to cover",
            "Drop the federal contraception coverage mandate and let insurers, employers and "
            "state regulators set what a plan includes and at what cost.",
        ),
        (
            "add-fertility-treatment-coverage",
            "+2",
            "Cover fertility treatment, including IVF, as standard care",
            "Require insurers to cover diagnosis and treatment of infertility, in vitro "
            "fertilization included, and protect access to the procedure in federal law.",
        ),
        (
            "end-federal-family-planning-funding",
            "-2",
            "End federal funding for family planning clinics",
            "Stop the federal family planning grant program altogether and leave contraception and "
            "fertility services to be paid for privately or by the states.",
        ),
    ),
    # === Agriculture and food policy ======================================
    # Axis: federal support for farm income. Negative = less federal support
    # and more market exposure, positive = more support.
    *_cards(
        "farm-subsidies-and-crop-insurance",
        (
            "cap-payments-to-large-operations",
            "0",
            "Keep the programs but cap payments per farm",
            "Hold total spending roughly where it is while tightening per-farm payment limits so "
            "support flows to small and mid-sized operations rather than the largest ones.",
        ),
        (
            "expand-crop-insurance-support",
            "+1",
            "Strengthen the safety net for farm income",
            "Raise the federal share of crop insurance premiums and widen revenue guarantees so "
            "a bad harvest or a price collapse does not end a family farm.",
        ),
        (
            "phase-out-commodity-subsidies",
            "-1",
            "Let market prices set what farmers plant",
            "Phase out commodity price supports and subsidized premiums, leaving growers to "
            "manage price and weather risk through private insurance and futures markets.",
        ),
    ),
    # Axis: federal food assistance. Negative = narrower eligibility and more
    # conditions, positive = broader benefits.
    *_cards(
        "food-assistance-programs",
        (
            "tighten-work-requirements",
            "-1",
            "Tie food aid to work or training",
            "Require able-bodied adults without dependents to work, train or volunteer a set "
            "number of hours to keep receiving benefits beyond a few months.",
        ),
        (
            "raise-benefit-levels",
            "+1",
            "Raise food benefits to cover a full month",
            "Increase SNAP allotments to reflect the real cost of a healthy diet and index them "
            "so benefits do not lose value between farm bills.",
        ),
        (
            "block-grant-to-states",
            "-2",
            "Hand food assistance to the states as a block grant",
            "Convert SNAP into a fixed annual grant each state designs and administers itself, "
            "ending the open-ended federal entitlement to benefits.",
        ),
    ),
    # Axis: federal food regulation. Negative = lighter regulatory load,
    # positive = more inspection and disclosure.
    *_cards(
        "food-safety-inspection",
        (
            "more-frequent-inspections",
            "+1",
            "Inspect food facilities more often",
            "Fund more frequent unannounced inspections of processing plants and imported food, "
            "and give regulators authority to order a recall rather than request one.",
        ),
        (
            "streamline-overlapping-rules",
            "-1",
            "Cut duplicate inspection and paperwork requirements",
            "Consolidate overlapping federal food agencies and let third-party audits satisfy "
            "routine inspection requirements, reserving federal staff for high-risk facilities.",
        ),
        (
            "require-detailed-labeling",
            "+2",
            "Require labels to disclose origin, additives and processing",
            "Mandate on-package disclosure of country of origin, added sugar, ultra-processing "
            "and bioengineered ingredients so shoppers can compare products directly.",
        ),
        (
            "replace-inspection-with-private-certification",
            "-2",
            "Replace federal inspection with private certification",
            "Withdraw federal inspectors from routine work and let accredited private certifiers "
            "and retailer standards police food production, with liability law as the backstop.",
        ),
    ),
    # === Civil liberties and discrimination law ===========================
    # Axis: limits on expression. Negative = fewer legal limits on speech,
    # positive = more.
    *_cards(
        "free-speech-and-assembly",
        (
            "no-new-speech-restrictions",
            "-1",
            "Keep speech limits to what courts already allow",
            "Add no new federal categories of unlawful expression, leaving the narrow existing "
            "exceptions for threats, incitement and fraud as the only limits.",
        ),
        (
            "restrict-targeted-harassment",
            "+1",
            "Set federal limits on targeted harassment and threats",
            "Define and penalize sustained harassment, doxxing and credible threats against "
            "individuals, while leaving the underlying political argument untouched.",
        ),
        (
            "protect-protest-and-press-access",
            "0",
            "Keep current limits but secure protest and press access",
            "Leave the legal line where it is while barring permit fees, curfews and arrest "
            "practices that keep lawful demonstrations and reporters off public ground.",
        ),
    ),
    # Axis: religious exemptions from general law. Negative = fewer
    # exemptions, positive = broader exemptions.
    *_cards(
        "religious-exemptions",
        (
            "no-exemption-from-public-accommodation",
            "-1",
            "Apply service and employment law to everyone alike",
            "Keep anti-discrimination and licensing rules binding on all businesses open to the "
            "public, with no religious exemption from serving or employing anyone.",
        ),
        (
            "broaden-conscience-protections",
            "+1",
            "Let people and businesses opt out on religious grounds",
            "Give individuals, employers and service providers a clear statutory right to decline "
            "participation in practices that conflict with their sincerely held beliefs.",
        ),
        (
            "narrow-exemptions-to-houses-of-worship",
            "-2",
            "Confine exemptions to houses of worship and clergy",
            "Limit religious exemptions to congregations, clergy and the hiring of ministers, "
            "with no exemption for commercial businesses, hospitals or schools.",
        ),
    ),
    # Axis: federal anti-discrimination enforcement. Negative = narrower
    # federal role, positive = stronger enforcement and broader coverage.
    *_cards(
        "anti-discrimination-enforcement",
        (
            "expand-covered-categories",
            "+1",
            "Extend federal coverage to more settings and categories",
            "Write sexual orientation and gender identity into federal civil rights law and cover "
            "housing, credit, education and public accommodation explicitly.",
        ),
        (
            "focus-on-individual-intentional-cases",
            "-1",
            "Limit enforcement to proven intentional discrimination",
            "Require evidence of deliberate discrimination against a specific person, ending "
            "statistical disparate-impact cases and agency-initiated pattern suits.",
        ),
        (
            "fund-testing-and-audits",
            "+2",
            "Fund proactive testing and audits of large employers",
            "Pay for paired testing in housing and hiring, require pay-gap reporting from large "
            "firms, and let agencies open cases on the results without a complaint.",
        ),
        (
            "repeal-federal-coverage-of-private-firms",
            "-2",
            "Leave private hiring and service decisions to the states",
            "Repeal federal anti-discrimination requirements for private employers and businesses, "
            "leaving any such rules to state legislatures to write or not write.",
        ),
    ),
    # === Criminal justice and policing =====================================
    # Axis: severity of federal sentencing. Negative = longer and more
    # mandatory sentences, positive = shorter sentences and more diversion.
    *_cards(
        "sentencing-and-incarceration",
        (
            "end-mandatory-minimums",
            "+1",
            "Give judges discretion instead of mandatory minimums",
            "Repeal mandatory minimum sentences for nonviolent offenses and let judges weigh the "
            "facts of each case, with earned-time credits for program completion.",
        ),
        (
            "keep-long-sentences-for-violent-crime",
            "-1",
            "Keep long mandatory sentences for violent and repeat offenses",
            "Preserve mandatory minimums for violent crime, firearm offenses and repeat felonies, "
            "and restrict early release for anyone convicted of harming a person.",
        ),
        (
            "truth-in-sentencing-no-early-release",
            "-2",
            "Require every sentence to be served in full",
            "Abolish parole and earned-time credits for federal prisoners, add mandatory minimums "
            "for more offenses, and require the full term handed down to be served.",
        ),
        (
            "shift-to-treatment-and-diversion",
            "+2",
            "Divert most nonviolent cases out of prison entirely",
            "Route drug, property and mental-health cases into supervised treatment and "
            "restitution programs instead of custody, and cut the federal prison population.",
        ),
    ),
    # Axis: federal posture toward police departments. Negative = more funding
    # and legal latitude for officers, positive = more oversight and
    # alternatives to policing.
    *_cards(
        "police-funding-and-oversight",
        (
            "fund-hiring-and-equipment",
            "-1",
            "Fund more officers, training and equipment",
            "Expand federal grants for hiring, pay and equipment so departments can fill "
            "vacancies and keep patrol staffing up in high-crime areas.",
        ),
        (
            "national-standards-and-accountability",
            "+1",
            "Set national standards and make misconduct easier to sue over",
            "Condition federal grants on use-of-force and body-camera standards, build a national "
            "misconduct registry, and narrow qualified immunity in civil rights suits.",
        ),
        (
            "shift-calls-to-civilian-responders",
            "+2",
            "Move mental-health and welfare calls to civilian responders",
            "Fund unarmed crisis teams, social workers and medics to handle mental-health, "
            "homelessness and welfare calls, shrinking what armed officers are dispatched to.",
        ),
        (
            "bar-federal-conditions-on-police-grants",
            "-2",
            "Bar federal conditions on how departments police",
            "Send federal grants with no strings on use-of-force, oversight or data reporting, and "
            "keep qualified immunity intact so officers are not personally liable.",
        ),
    ),
    # Axis: drug policy. Negative = enforcement-first, positive = treatment
    # and legalization-first.
    *_cards(
        "drug-enforcement-and-treatment",
        (
            "legalize-and-regulate-cannabis",
            "+1",
            "Legalize and regulate cannabis federally",
            "Remove cannabis from the federal controlled substances schedule, tax and regulate "
            "legal sale, and expunge past federal possession convictions.",
        ),
        (
            "prosecute-trafficking-harder",
            "-1",
            "Treat trafficking as the main target of federal effort",
            "Concentrate federal resources on interdiction and long sentences for fentanyl and "
            "methamphetamine traffickers, including stiffer penalties for distribution deaths.",
        ),
        (
            "fund-treatment-and-harm-reduction",
            "0",
            "Keep the drug laws but pay for treatment capacity",
            "Leave the federal schedules where they are and fund residential treatment, "
            "medication-assisted therapy, naloxone distribution and overdose prevention instead.",
        ),
    ),
    # === Education (K-12) ==================================================
    # Axis: federal role in equalizing school funding. Negative = less federal
    # involvement, positive = more federal money and equalization.
    *_cards(
        "school-funding-formulas",
        (
            "increase-title-i-funding",
            "+1",
            "Send more federal money to high-poverty schools",
            "Raise Title I funding substantially and weight the formula so districts with the "
            "highest concentrations of poverty receive the largest per-student increases.",
        ),
        (
            "return-funding-decisions-to-states",
            "-1",
            "Let states and districts decide how school money is spent",
            "Consolidate federal education programs into flexible grants with few strings, "
            "leaving spending priorities to state legislatures and local school boards.",
        ),
        (
            "fund-facilities-and-staff-pay",
            "+2",
            "Pay for school buildings and a federal teacher pay floor",
            "Add federal capital funding for school construction and repair, plus a national "
            "minimum teacher salary supported by federal dollars.",
        ),
        (
            "end-the-federal-funding-role",
            "-2",
            "End the federal role in school funding entirely",
            "Close the federal education department and return its funding and authority to the "
            "states, letting each one raise and allocate school money as it chooses.",
        ),
    ),
    # Axis: public money following students to non-public schools. Negative =
    # money stays with public districts, positive = money follows the student.
    *_cards(
        "school-choice-and-charters",
        (
            "fund-vouchers-and-savings-accounts",
            "+1",
            "Let families use public funds at the school they pick",
            "Create federal scholarships or education savings accounts families can spend at "
            "private, charter, religious or home schooling of their choice.",
        ),
        (
            "keep-public-funds-in-public-schools",
            "-1",
            "Keep public education money in public schools",
            "Bar federal funds from private school tuition and require charter schools to meet "
            "the same admission, staffing and reporting rules as district schools.",
        ),
        (
            "expand-charters-with-accountability",
            "0",
            "Grow charters only where they outperform, and close the rest",
            "Keep the current public-private line but let strong charter operators expand while "
            "requiring authorizers to close schools that underperform their district.",
        ),
    ),
    # Axis: who decides what is taught. Negative = local and parental control,
    # positive = national standards and professional discretion.
    *_cards(
        "curriculum-and-standards",
        (
            "give-parents-review-and-opt-out",
            "-1",
            "Give parents the right to review and opt out of material",
            "Require schools to publish curricula and reading lists and let parents withdraw "
            "their child from specific lessons on sex education, religion or contested history.",
        ),
        (
            "leave-content-to-educators",
            "+1",
            "Leave what is taught to teachers and subject experts",
            "Bar legislatures from banning books or topics and leave curriculum design to "
            "licensed educators working from professional standards.",
        ),
        (
            "common-skills-benchmarks",
            "0",
            "Keep local control but publish comparable results",
            "Leave curriculum where it is set today while requiring every state to report "
            "reading and math results on a common national test so parents can compare.",
        ),
    ),
    # === Elections and voting administration ===============================
    # Axis: identification and registration requirements. Negative = stricter
    # verification, positive = easier registration.
    *_cards(
        "voter-registration-and-id",
        (
            "require-photo-id-to-vote",
            "-1",
            "Require government photo identification to vote",
            "Make a current government-issued photo ID a condition of casting a ballot in federal "
            "elections, with free IDs available to anyone who lacks one.",
        ),
        (
            "automatic-and-same-day-registration",
            "+1",
            "Register voters automatically and allow same-day signup",
            "Register every eligible citizen through motor vehicle and benefits agencies unless "
            "they decline, and let people register and vote on the same day.",
        ),
        (
            "citizenship-documents-and-list-audits",
            "-2",
            "Require proof of citizenship and regular roll audits",
            "Require documentary proof of citizenship to register and mandate routine list "
            "maintenance removing voters who have not voted or confirmed their address.",
        ),
    ),
    # Axis: ballot access methods. Negative = narrower absentee and early
    # options, positive = broader.
    *_cards(
        "mail-and-early-voting",
        (
            "mail-ballots-for-every-voter",
            "+1",
            "Send every registered voter a mail ballot",
            "Mail a ballot to each registered voter automatically, with prepaid return postage, "
            "drop boxes and at least two weeks of in-person early voting.",
        ),
        (
            "excuse-required-absentee-only",
            "-1",
            "Limit mail voting to voters with a stated reason",
            "Return to requiring an excuse such as illness, travel or military service for an "
            "absentee ballot, and concentrate voting on election day itself.",
        ),
        (
            "keep-options-with-tighter-deadlines",
            "0",
            "Keep today's options but fix deadlines and signature rules",
            "Leave mail and early voting broadly as they are while standardizing receipt "
            "deadlines, signature verification and the process for curing a rejected ballot.",
        ),
    ),
    # Axis: who draws districts. Negative = legislatures keep the pen,
    # positive = independent or standard-bound redistricting.
    *_cards(
        "redistricting-and-apportionment",
        (
            "independent-commissions-everywhere",
            "+1",
            "Require independent commissions to draw districts",
            "Mandate that every state hand congressional map drawing to a commission insulated "
            "from the legislature, with public hearings and published criteria.",
        ),
        (
            "legislatures-keep-map-drawing",
            "-1",
            "Leave map drawing to elected state legislatures",
            "Keep redistricting with the state legislatures the Constitution assigns it to, and "
            "bar federal rules dictating how a state draws its own districts.",
        ),
        (
            "ban-partisan-gerrymanders-in-statute",
            "+2",
            "Ban partisan map drawing and allow federal court review",
            "Write a measurable partisan-fairness standard into federal law and let federal "
            "courts strike down maps that exceed it, whichever party drew them.",
        ),
        (
            "bar-courts-from-reviewing-maps",
            "-2",
            "Keep courts out of map drawing altogether",
            "Treat redistricting as a political question for legislatures and voters, barring both "
            "federal and state courts from striking a map down on partisan grounds.",
        ),
    ),
    # === Energy policy =====================================================
    # Axis: domestic oil and gas development. Negative = restrict production,
    # positive = expand it.
    *_cards(
        "oil-and-gas-production",
        (
            "expand-leasing-and-permits",
            "+1",
            "Open more federal land and water to drilling",
            "Hold regular lease sales on federal acreage and offshore, shorten permit timelines, "
            "and approve pipeline and export terminal applications on a fixed schedule.",
        ),
        (
            "stop-new-federal-leasing",
            "-1",
            "Stop issuing new federal oil and gas leases",
            "End new leasing on federal land and waters, letting existing wells run out their "
            "life while no new federal acreage is opened to development.",
        ),
        (
            "approve-with-methane-and-cleanup-rules",
            "0",
            "Keep production steady but tighten methane and cleanup rules",
            "Continue leasing at roughly current levels while requiring methane capture, higher "
            "bonding for well cleanup and royalty rates that cover restoration costs.",
        ),
    ),
    # Axis: federal support for non-fossil generation. Negative = let the
    # market choose unsubsidized, positive = active federal buildout.
    *_cards(
        "renewable-and-nuclear-generation",
        (
            "extend-clean-energy-tax-credits",
            "+1",
            "Keep the tax credits building wind, solar and storage",
            "Extend and expand production and investment tax credits for wind, solar, storage and "
            "transmission so new clean capacity keeps coming online.",
        ),
        (
            "end-energy-subsidies-across-the-board",
            "-1",
            "End subsidies and let generators compete unaided",
            "Repeal federal tax credits and loan guarantees for every generation source alike, "
            "fossil and renewable, and let wholesale markets pick the winners.",
        ),
        (
            "build-new-nuclear-at-federal-scale",
            "+2",
            "Finance new nuclear construction directly",
            "Use federal loan guarantees, power purchase commitments and a streamlined licensing "
            "path to get a new generation of reactors built this decade.",
        ),
        (
            "bar-federal-money-from-generation",
            "-2",
            "Bar federal money from building generation at all",
            "Prohibit federal subsidies, loan guarantees and purchase commitments for any power "
            "plant, leaving every generation decision to private capital and state regulators.",
        ),
    ),
    # Axis: priority between bills and buildout. Negative = hold rates down
    # first, positive = invest in capacity first.
    *_cards(
        "electricity-prices-and-grid",
        (
            "build-interstate-transmission",
            "+1",
            "Build the interstate lines the grid is short of",
            "Give federal regulators authority to approve and site long-distance transmission so "
            "new generation can reach demand centers and reserve margins recover.",
        ),
        (
            "protect-ratepayers-from-increases",
            "-1",
            "Put household bills first when approving costs",
            "Require regulators to reject transmission and generation costs that raise "
            "residential bills, and fund bill assistance for low-income households.",
        ),
        (
            "pay-for-reliability-and-storage",
            "+2",
            "Pay directly for firm capacity, storage and hardening",
            "Fund grid-scale storage, wildfire and storm hardening and firm backup capacity "
            "through federal programs rather than waiting for markets to deliver them.",
        ),
    ),
    # === Environment and climate policy ====================================
    # Axis: federal action to cut emissions. Negative = less binding federal
    # action, positive = more.
    *_cards(
        "greenhouse-gas-emissions",
        (
            "binding-sector-targets",
            "+1",
            "Set binding emission limits sector by sector",
            "Give regulators authority to cap emissions from power, transport and industry on a "
            "declining schedule, with enforceable penalties for exceeding a limit.",
        ),
        (
            "no-federal-emission-mandates",
            "-1",
            "Leave emissions to states and technology, not federal mandates",
            "Repeal federal greenhouse gas rules and rely on state policy, private investment and "
            "improving technology rather than national limits.",
        ),
        (
            "carbon-price-with-rebate",
            "+2",
            "Price carbon economy-wide and rebate the revenue",
            "Charge a rising fee on fossil fuels at the point of extraction or import and return "
            "the money to households as an equal per-person dividend.",
        ),
        (
            "bar-federal-climate-regulation",
            "-2",
            "Bar federal agencies from regulating emissions at all",
            "Strip agencies of authority to treat greenhouse gases as pollutants, and withdraw "
            "from international emission commitments.",
        ),
    ),
    # Axis: pollution standards. Negative = lighter standards and faster
    # permitting, positive = stricter standards.
    *_cards(
        "air-and-water-quality-rules",
        (
            "tighten-pollutant-standards",
            "+1",
            "Tighten limits on soot, smog and water contaminants",
            "Set standards at the levels current health evidence supports for fine particulates, "
            "ozone and drinking-water contaminants including PFAS, and enforce them.",
        ),
        (
            "weigh-compliance-costs-first",
            "-1",
            "Require a cost-benefit test before any new standard",
            "Bar regulators from issuing a standard whose compliance costs exceed its measured "
            "benefits, and let existing plants keep operating under the rules they were built to.",
        ),
        (
            "keep-standards-fix-enforcement",
            "0",
            "Keep the current standards and actually enforce them",
            "Leave the numeric limits alone while funding monitoring, inspections and penalties so "
            "the standards already on the books are met in practice.",
        ),
    ),
    # Axis: use of federal land. Negative = more extraction and local control,
    # positive = more conservation.
    *_cards(
        "public-lands-and-conservation",
        (
            "open-lands-to-multiple-use",
            "-1",
            "Manage federal land for grazing, timber and recreation",
            "Keep federal land open to grazing, logging, mining and motorized recreation under "
            "multiple-use rules, and defer to state and county plans.",
        ),
        (
            "designate-more-protected-land",
            "+1",
            "Set aside more federal land and water from development",
            "Designate new national monuments, wilderness and marine conservation areas, and set "
            "a national target for conserving a share of land and coastal waters.",
        ),
        (
            "transfer-lands-to-states",
            "-2",
            "Transfer most federal land to the states",
            "Hand the majority of federal acreage outside parks and military ranges to the states "
            "it sits in, to manage, lease or sell as their own legislatures decide.",
        ),
    ),
    # === Federal budget and taxes ==========================================
    # Axis: individual income tax levels. Negative = lower rates, positive =
    # higher rates on upper incomes.
    *_cards(
        "individual-income-tax-rates",
        (
            "cut-rates-across-brackets",
            "-1",
            "Cut income tax rates across the brackets",
            "Lower marginal rates at every income level, index brackets fully for inflation, and "
            "make the expiring individual rate cuts permanent.",
        ),
        (
            "raise-top-rates-and-investment-taxes",
            "+1",
            "Raise rates on the highest incomes and investment gains",
            "Restore a higher top marginal rate, tax capital gains and dividends closer to wage "
            "rates above a threshold, and close the stepped-up basis at death.",
        ),
        (
            "flatten-to-one-low-rate",
            "-2",
            "Replace the brackets with a single low flat rate",
            "Collapse the income tax to one rate with a large standard exemption, eliminating "
            "most deductions and credits along with the graduated brackets.",
        ),
    ),
    # Axis: business tax levels. Negative = lower business taxes, positive =
    # higher.
    *_cards(
        "corporate-and-business-taxation",
        (
            "raise-the-corporate-rate",
            "+1",
            "Raise the corporate rate and tax overseas profits",
            "Lift the statutory corporate rate, apply a minimum tax to book income, and tax "
            "foreign earnings as they are made rather than when repatriated.",
        ),
        (
            "keep-rates-low-for-investment",
            "-1",
            "Keep business taxes low to attract investment",
            "Hold the corporate rate at or below its current level, make full expensing of "
            "equipment and research permanent, and avoid a minimum book tax.",
        ),
        (
            "close-breaks-without-raising-the-rate",
            "0",
            "Leave the rate alone and close the special breaks",
            "Keep the headline rate where it is while repealing industry-specific credits, "
            "carried interest treatment and deductions that favor one sector over another.",
        ),
    ),
    # Axis: approach to deficits. Negative = close the gap by cutting
    # spending, positive = tolerate borrowing or close it with revenue.
    *_cards(
        "federal-deficit-and-debt",
        (
            "cut-spending-to-balance",
            "-1",
            "Balance the budget by reducing federal spending",
            "Set statutory caps that bring outlays down to revenue over a decade, including "
            "slower growth in the largest benefit programs, without raising taxes.",
        ),
        (
            "raise-revenue-to-close-the-gap",
            "+1",
            "Close the deficit mainly with new revenue",
            "Bring the budget toward balance by raising taxes on high incomes, corporations and "
            "estates while holding current benefit levels where they are.",
        ),
        (
            "borrow-for-productive-investment",
            "+2",
            "Borrow when the money buys lasting assets",
            "Treat the deficit as a tool rather than a target, borrowing freely for "
            "infrastructure, research and childcare while interest costs stay manageable.",
        ),
        (
            "constitutional-balanced-budget-rule",
            "-2",
            "Write a balanced budget requirement into the Constitution",
            "Amend the Constitution to forbid outlays exceeding revenue except by a supermajority "
            "vote, binding every future Congress whatever its spending plans.",
        ),
    ),
    # === Firearms policy ===================================================
    # Axis: restrictions on acquiring a firearm. Negative = fewer
    # restrictions, positive = more. Both magnitudes are used: both maintained
    # far positions here are widely held and not represented by the partial
    # ones.
    *_cards(
        "background-checks-and-purchase-rules",
        (
            "universal-background-checks",
            "+1",
            "Require a background check for every firearm sale",
            "Extend the federal check to private, gun show and online sales, so the same "
            "verification applies however a buyer and seller find each other.",
        ),
        (
            "keep-private-sales-exempt",
            "-1",
            "Keep private sales out of the federal check system",
            "Leave the check requirement with licensed dealers only, so transfers between "
            "individuals, including family and private sales, stay unregulated federally.",
        ),
        (
            "licensing-and-waiting-periods",
            "+2",
            "Require a permit, training and a waiting period to buy",
            "Make purchase conditional on a state-issued license involving training, references "
            "and a multi-day waiting period before any firearm changes hands.",
        ),
        (
            "repeal-federal-check-requirement",
            "-2",
            "End the federal check requirement entirely",
            "Repeal the national instant check system and leave any verification of a buyer to "
            "state law and the seller's own judgement.",
        ),
    ),
    # Axis: limits on particular firearms and magazines. Negative = fewer
    # category limits, positive = more.
    *_cards(
        "firearm-categories-and-capacity-limits",
        (
            "ban-specified-semiautomatic-rifles",
            "+1",
            "Ban the sale of specified semiautomatic rifles",
            "Prohibit new sales of listed semiautomatic rifles and magazines holding more than "
            "ten rounds, while existing lawfully owned firearms may be kept.",
        ),
        (
            "no-category-or-capacity-limits",
            "-1",
            "Set no federal limits on firearm type or magazine size",
            "Keep any firearm and magazine lawful for an eligible buyer at the federal level, and "
            "preempt state bans on specific models or capacities.",
        ),
        (
            "regulate-devices-not-models",
            "0",
            "Keep the current line but regulate the add-ons",
            "Leave the existing federal categories alone while restricting devices that change a "
            "firearm's rate of fire, such as bump stocks and forced-reset triggers.",
        ),
    ),
    # Axis: where carrying is lawful and who decides. Negative = broader carry
    # entitlement and federal preemption of state limits, positive = more
    # state and local authority to restrict.
    *_cards(
        "carry-permits-and-preemption",
        (
            "national-reciprocity-for-carry-permits",
            "-1",
            "Make a carry permit valid in every state",
            "Require each state to honor carry permits issued by any other, the way it honors an "
            "out-of-state driver's license.",
        ),
        (
            "let-states-set-carry-rules",
            "+1",
            "Let each state set its own carry rules",
            "Leave permit standards, training requirements and sensitive-place restrictions to "
            "state and local law, with no federal reciprocity mandate.",
        ),
        (
            "permitless-carry-nationwide",
            "-2",
            "Allow carry without a permit nationwide",
            "Bar states from requiring any permit, training or registration to carry a firearm, "
            "treating lawful carry as needing no government authorization.",
        ),
    ),
    # === Foreign policy and defense ========================================
    # Axis: size of the defense budget and forward presence. Negative = larger
    # budget and bigger footprint, positive = smaller.
    *_cards(
        "defense-spending-and-force-posture",
        (
            "grow-the-defense-budget",
            "-1",
            "Grow the defense budget faster than inflation",
            "Raise military spending in real terms to fund shipbuilding, munitions stocks, "
            "missile defense and modernization of the nuclear triad.",
        ),
        (
            "reduce-spending-and-overseas-bases",
            "+1",
            "Cut the budget and close overseas bases",
            "Reduce military spending in real terms, close a share of permanent overseas bases "
            "and shift the savings to domestic programs and deficit reduction.",
        ),
        (
            "hold-spending-and-audit-procurement",
            "0",
            "Hold spending flat and fix how it is spent",
            "Keep the topline at roughly its current real level while enforcing a clean audit, "
            "breaking up sole-source contracts and cancelling programs behind schedule.",
        ),
    ),
    # Axis: willingness to use force and underwrite alliances. Negative = more
    # forward engagement, positive = more restraint.
    *_cards(
        "military-intervention-and-alliances",
        (
            "require-congressional-authorization",
            "+1",
            "Require a congressional vote before committing forces",
            "Bar funding for hostilities lasting beyond a short window without a specific "
            "authorization vote, and repeal the open-ended authorizations still in force.",
        ),
        (
            "maintain-alliance-commitments",
            "-1",
            "Keep treaty commitments and deter by forward presence",
            "Honor existing alliance guarantees in full, keep troops forward-deployed in Europe "
            "and Asia, and arm partners facing invasion.",
        ),
        (
            "end-open-ended-deployments",
            "+2",
            "Withdraw from open-ended deployments and renegotiate guarantees",
            "Bring forces home from long-running deployments, hand regional defense to the "
            "countries concerned, and reopen alliance terms that commit the US automatically.",
        ),
    ),
    # Axis: use of aid and sanctions. Negative = less aid, positive = more.
    *_cards(
        "foreign-aid-and-sanctions",
        (
            "expand-development-and-health-aid",
            "+1",
            "Expand development, health and humanitarian aid",
            "Raise spending on global health, famine relief and development lending, and fund it "
            "as a standing commitment rather than emergency supplementals.",
        ),
        (
            "cut-aid-and-tie-it-to-interests",
            "-1",
            "Cut foreign aid and tie what remains to US interests",
            "Reduce overall assistance and condition the rest on voting alignment, trade access, "
            "migration cooperation or basing rights for the United States.",
        ),
        (
            "sanction-rights-abusers-broadly",
            "+2",
            "Sanction abusive and corrupt officials aggressively",
            "Use asset freezes, visa bans and secondary sanctions widely against officials "
            "implicated in corruption or human rights abuses, accepting the diplomatic cost.",
        ),
        (
            "end-development-aid-altogether",
            "-2",
            "End development assistance altogether",
            "Close the development agency and stop non-emergency assistance entirely, keeping only "
            "disaster relief and spending the rest at home.",
        ),
    ),
    # === Healthcare ========================================================
    # Axis: federal intervention in drug prices. Negative = market pricing,
    # positive = government price setting. prd.md's own anchor example
    # ("lower out-of-pocket drug costs") sits at +1 here.
    *_cards(
        "prescription-drug-pricing",
        (
            "cap-out-of-pocket-costs",
            "+1",
            "Cap what patients pay at the pharmacy counter",
            "Set a legal ceiling on monthly out-of-pocket costs for insulin and other maintenance "
            "drugs, and cap annual drug spending for people on Medicare.",
        ),
        (
            "let-the-market-set-prices",
            "-1",
            "Leave drug prices to competition, not federal negotiation",
            "Repeal federal price negotiation authority and rely on generic entry, pharmacy "
            "benefit competition and faster approvals to bring prices down.",
        ),
        (
            "negotiate-all-prices-and-allow-imports",
            "+2",
            "Negotiate every drug price and allow imports",
            "Let Medicare negotiate the price of every drug it covers from launch, benchmark "
            "against other wealthy countries, and legalize personal and wholesale importation.",
        ),
        (
            "extend-exclusivity-to-reward-research",
            "-2",
            "Lengthen patent exclusivity to reward new research",
            "Extend market exclusivity on new medicines and bar any federal price setting, on the "
            "view that higher returns are what fund the next generation of drugs.",
        ),
    ),
    # Axis: how coverage is organized. Negative = private market with less
    # federal structure, positive = more public coverage.
    *_cards(
        "health-insurance-coverage",
        (
            "public-option-alongside-private-plans",
            "+1",
            "Offer a public plan alongside private insurance",
            "Create a government-run plan anyone may buy on the exchanges, keep the premium "
            "subsidies, and let employers and individuals choose between it and private cover.",
        ),
        (
            "single-public-plan-for-everyone",
            "+2",
            "Enroll everyone in a single public health plan",
            "Replace private insurance with one federal plan covering every resident for "
            "hospital, physician, drug and long-term care, funded by taxes rather than premiums.",
        ),
        (
            "deregulate-and-expand-catastrophic-plans",
            "-1",
            "Let people buy cheaper, less comprehensive plans",
            "Loosen federal benefit requirements so insurers can sell catastrophic, short-term "
            "and cross-state plans, paired with expanded health savings accounts.",
        ),
        (
            "end-the-exchanges-and-subsidies",
            "-2",
            "End the exchanges and the premium subsidies",
            "Repeal the federal exchange and subsidy structure entirely, replacing it with "
            "tax-free savings accounts and a deregulated individual insurance market.",
        ),
    ),
    # Axis: financing of the two big health entitlements. Negative = contain
    # federal cost, positive = expand federal commitment.
    *_cards(
        "medicare-and-medicaid-financing",
        (
            "expand-benefits-and-eligibility",
            "+1",
            "Add benefits and lower the Medicare eligibility age",
            "Cover dental, vision, hearing and long-term care under Medicare, lower the "
            "eligibility age, and fund Medicaid expansion in every state.",
        ),
        (
            "cap-federal-spending-per-enrollee",
            "-1",
            "Cap what the federal government spends per enrollee",
            "Convert Medicaid to a per-capita allotment and move Medicare toward a fixed federal "
            "contribution toward a plan the enrollee chooses.",
        ),
        (
            "raise-payroll-taxes-to-fund-current-benefits",
            "0",
            "Keep today's benefits and raise the money to pay for them",
            "Leave benefits and eligibility as they are and close the funding gap by lifting the "
            "payroll tax ceiling and applying the tax to investment income.",
        ),
        (
            "premium-support-vouchers",
            "-2",
            "Replace Medicare with a voucher toward a private plan",
            "Give each beneficiary a fixed federal payment to buy private coverage, ending the "
            "open-ended guarantee that Medicare pays for whatever care is delivered.",
        ),
    ),
    # === Higher education and student debt =================================
    # Axis: public funding of tuition. Negative = less federal subsidy,
    # positive = more.
    *_cards(
        "tuition-and-institutional-aid",
        (
            "double-pell-grants",
            "+1",
            "Double the Pell Grant and index it to costs",
            "Raise the maximum need-based grant substantially and index it to tuition inflation "
            "so low-income students can cover more of a degree without borrowing.",
        ),
        (
            "tuition-free-public-college",
            "+2",
            "Make public college tuition-free",
            "Fund state universities and community colleges so that tuition is free for in-state "
            "students, paid through a federal and state cost-sharing arrangement.",
        ),
        (
            "tie-aid-to-graduate-earnings",
            "-1",
            "Limit federal aid to programs whose graduates earn enough",
            "Cut off federal loans and grants to programs whose graduates do not out-earn a high "
            "school diploma, and cap borrowing by program rather than by student.",
        ),
    ),
    # Axis: treatment of existing student debt. Negative = borrowers repay in
    # full, positive = broad cancellation.
    *_cards(
        "student-loan-repayment-and-forgiveness",
        (
            "cap-payments-at-a-share-of-income",
            "+1",
            "Cap payments at a share of income and clear the rest",
            "Set every borrower's payment at a fixed low percentage of discretionary income, stop "
            "interest from outgrowing payments, and cancel the remainder after a set period.",
        ),
        (
            "cancel-balances-broadly",
            "+2",
            "Cancel most outstanding federal student debt",
            "Write off federal student loan balances up to a substantial per-borrower amount, "
            "regardless of the program studied or the borrower's current income.",
        ),
        (
            "repay-in-full-with-standard-terms",
            "-1",
            "Expect loans to be repaid on standard terms",
            "End broad cancellation and open-ended forgiveness, keeping income-driven plans "
            "narrow and requiring borrowers to repay what they agreed to borrow.",
        ),
    ),
    # Axis: where federal training money goes. Negative = employer and market
    # led, positive = publicly funded programs.
    *_cards(
        "workforce-and-vocational-training",
        (
            "fund-apprenticeships-and-community-colleges",
            "+1",
            "Fund apprenticeships and community college training",
            "Pay for registered apprenticeships, short-term credentials and community college "
            "workforce programs tied to local employer demand.",
        ),
        (
            "let-employers-and-learners-choose",
            "-1",
            "Let employers and learners direct training money",
            "Replace federal workforce programs with tax credits and individual training accounts "
            "learners spend wherever they choose, public or private.",
        ),
        (
            "guarantee-retraining-for-displaced-workers",
            "+2",
            "Guarantee paid retraining for displaced workers",
            "Give any worker who loses a job to trade, automation or plant closure an entitlement "
            "to income support and tuition while retraining full time.",
        ),
        (
            "end-federal-training-programs",
            "-2",
            "End federal job training programs entirely",
            "Close the federal workforce programs on the evidence that they rarely raise earnings, "
            "and return the money to taxpayers rather than redirecting it.",
        ),
    ),
    # === Housing ===========================================================
    # Axis: federal role in increasing supply. Negative = local control over
    # land use, positive = federal pressure and funding for more building.
    *_cards(
        "housing-supply-and-construction",
        (
            "condition-funds-on-zoning-changes",
            "+1",
            "Tie federal funds to allowing more homes per lot",
            "Condition transportation and housing grants on states and cities legalizing "
            "duplexes, apartments near transit and smaller lots by right.",
        ),
        (
            "keep-land-use-local",
            "-1",
            "Leave land use decisions to local communities",
            "Keep zoning and permitting entirely with cities and counties, with no federal "
            "conditions on grants aimed at changing local density rules.",
        ),
        (
            "build-public-and-nonprofit-housing",
            "+2",
            "Build publicly owned and nonprofit housing at scale",
            "Fund a large federal program to construct permanently affordable homes owned by "
            "public agencies, land trusts and nonprofits rather than subsidizing private rents.",
        ),
    ),
    # Axis: regulation of rents and tenancies. Negative = fewer limits on
    # landlords, positive = more tenant-side regulation.
    *_cards(
        "rent-levels-and-tenant-law",
        (
            "cap-rent-increases",
            "+1",
            "Cap how fast rents can rise",
            "Allow or require limits on annual rent increases for existing tenants, with "
            "exemptions for new construction and a pass-through for documented costs.",
        ),
        (
            "no-rent-caps",
            "-1",
            "Leave rents to the market and keep eviction quick",
            "Bar rent caps as counterproductive to supply, and keep eviction for nonpayment a "
            "fast, predictable process for owners.",
        ),
        (
            "federal-tenant-standards",
            "+2",
            "Write tenant standards into federal law",
            "Guarantee a right to counsel in eviction cases, require good cause for ending a "
            "tenancy, and bar rent-setting software and blanket screening practices.",
        ),
    ),
    # Axis: approach to homelessness. Negative = enforcement and
    # treatment-first conditions, positive = housing-first and more funding.
    *_cards(
        "homelessness-services",
        (
            "fund-housing-first-placements",
            "+1",
            "Put people into housing first, then add services",
            "Fund permanent supportive housing with no sobriety or treatment precondition, "
            "pairing a unit with voluntary case management and health care.",
        ),
        (
            "require-treatment-and-enforce-camping-rules",
            "-1",
            "Require treatment or shelter and enforce camping rules",
            "Fund shelter and mandated mental-health and addiction treatment, and let cities "
            "clear encampments once a bed has been offered.",
        ),
        (
            "fund-prevention-and-rental-assistance",
            "0",
            "Keep the current mix and stop people losing homes",
            "Leave the service model broadly as it is while funding emergency rental assistance, "
            "eviction diversion and short-term cash aid to prevent the first night outside.",
        ),
    ),
    # === Immigration and border policy =====================================
    # Axis: level of legal immigration. Negative = lower levels, positive =
    # higher.
    *_cards(
        "legal-immigration-levels-and-visas",
        (
            "raise-overall-visa-numbers",
            "+1",
            "Raise the number of visas issued each year",
            "Increase annual caps for employment, family and diversity visas, clear the existing "
            "backlogs, and lift the per-country limits that create decade-long waits.",
        ),
        (
            "reduce-levels-and-select-on-skills",
            "-1",
            "Lower total immigration and select on skills",
            "Cut annual admissions, end most extended-family and diversity categories, and select "
            "the smaller number on education, English and a job offer.",
        ),
        (
            "expand-work-visas-for-shortage-sectors",
            "0",
            "Keep overall levels but fix the shortage sectors",
            "Leave the total roughly as it is while reallocating visas toward agriculture, "
            "nursing, construction and other sectors with documented unfilled vacancies.",
        ),
    ),
    # Axis: border and asylum enforcement. Negative = more enforcement and
    # narrower asylum, positive = more access to asylum and less detention.
    *_cards(
        "border-enforcement-and-asylum",
        (
            "hire-judges-and-decide-claims-fast",
            "0",
            "Decide asylum claims quickly with more judges and officers",
            "Fund enough immigration judges and asylum officers to decide claims in months rather "
            "than years, keeping the current legal standard but ending the backlog.",
        ),
        (
            "restrict-asylum-and-expand-removals",
            "-1",
            "Narrow asylum eligibility and remove more people",
            "Require claims to be made at ports of entry, bar claims from those who crossed "
            "between them, detain through adjudication and expand expedited removal.",
        ),
        (
            "expand-legal-pathways-and-end-detention",
            "+1",
            "Widen lawful pathways and release claimants pending hearing",
            "Create refugee processing centers abroad, raise parole and humanitarian admissions, "
            "and replace detention with case management while a claim is heard.",
        ),
    ),
    # Axis: status for people here without authorization. Negative =
    # enforcement and departure, positive = legalization.
    *_cards(
        "status-of-long-term-residents",
        (
            "legalize-long-term-residents",
            "+1",
            "Offer legal status to long-settled residents",
            "Let people who have lived here for years without a serious criminal record apply for "
            "work authorization and permanent residence after paying fees and back taxes.",
        ),
        (
            "enforce-departure-and-workplace-rules",
            "-1",
            "Enforce the law and expect unlawful residents to leave",
            "Mandate electronic employment verification nationwide, restore interior enforcement "
            "against those with removal orders, and offer no new legalization.",
        ),
        (
            "status-for-childhood-arrivals-only",
            "0",
            "Settle status for childhood arrivals and no one else",
            "Give permanent status to people brought here as children who have grown up in the "
            "United States, while leaving the law unchanged for adults who came on their own.",
        ),
    ),
    # === Infrastructure and transportation =================================
    # Axis: what federal transport money buys. Negative = roads and highways
    # first, positive = transit and rail first.
    *_cards(
        "roads-bridges-and-transit",
        (
            "fund-highways-and-bridge-repair",
            "-1",
            "Put federal money into roads and bridges first",
            "Direct the highway trust fund to pavement, bridge repair and new road capacity, and "
            "leave transit operations to the cities and states that run the systems.",
        ),
        (
            "fund-transit-rail-and-street-redesign",
            "+1",
            "Fund transit, passenger rail and safer streets",
            "Shift a larger share of federal transport money into bus and rail service, intercity "
            "rail and street designs that cut pedestrian and cyclist deaths.",
        ),
        (
            "fix-it-first-across-modes",
            "0",
            "Fix what exists before building anything new",
            "Keep the current split between modes but require states to clear their repair "
            "backlog on roads, bridges and transit assets before funding expansion.",
        ),
    ),
    # Axis: how broadband gets built. Negative = private providers under light
    # regulation, positive = public funding, public networks and price rules.
    *_cards(
        "broadband-access",
        (
            "subsidize-private-buildout-to-rural-areas",
            "0",
            "Pay private carriers to reach the places left out",
            "Keep the current private market and use federal grants and reverse auctions to fund "
            "last-mile construction wherever no carrier will build unaided.",
        ),
        (
            "fund-municipal-and-cooperative-networks",
            "+1",
            "Let cities and co-ops build their own networks",
            "Fund municipal, tribal and cooperative broadband, preempt state laws that ban public "
            "networks, and require federally funded networks to offer a low-cost tier.",
        ),
        (
            "deregulate-and-let-carriers-compete",
            "-1",
            "Leave broadband to carrier competition",
            "Treat broadband as a lightly regulated information service, drop rate and "
            "net-neutrality rules, and rely on satellite, cable and wireless rivalry.",
        ),
    ),
    # Axis: federal investment in water and utility systems. Negative = local
    # and ratepayer funded, positive = federally funded.
    *_cards(
        "water-and-utility-systems",
        (
            "federal-grants-to-replace-pipes",
            "+1",
            "Pay federally to replace lead pipes and failing plants",
            "Fund replacement of lead service lines, treatment plant upgrades and sewer repair "
            "through grants rather than loans, so small systems can afford the work.",
        ),
        (
            "fund-through-rates-and-local-bonds",
            "-1",
            "Fund water systems through rates and local borrowing",
            "Keep water and sewer investment with the utilities and municipalities that own them, "
            "funded by user rates and local bonds rather than federal grants.",
        ),
        (
            "set-and-fund-contaminant-limits",
            "+2",
            "Set hard contaminant limits and pay for compliance",
            "Impose enforceable federal limits on lead, PFAS and other contaminants on a fixed "
            "deadline, with federal money covering the cost for every system that needs it.",
        ),
        (
            "privatize-and-deregulate-systems",
            "-2",
            "Let private operators run water systems under state rules",
            "Encourage municipalities to sell or lease systems to private operators, and leave "
            "contaminant standards and rate oversight to the states rather than federal rules.",
        ),
    ),
    # === Jobs, wages and labor law =========================================
    # Axis: federal wage and hour floors. Negative = lower or local floors,
    # positive = higher federal floors.
    *_cards(
        "minimum-wage-and-overtime",
        (
            "raise-the-federal-minimum-wage",
            "+1",
            "Raise the federal minimum wage and index it",
            "Lift the national wage floor substantially, phase out the separate tipped and youth "
            "minimums, and index the figure to median wages thereafter.",
        ),
        (
            "leave-wage-floors-to-states",
            "-1",
            "Leave the wage floor to states and local markets",
            "Keep the federal minimum where it is, or repeal it, and let states and cities set "
            "floors that match their own cost of living and labor market.",
        ),
        (
            "expand-overtime-eligibility",
            "+2",
            "Guarantee overtime pay to most salaried workers",
            "Raise the salary threshold for overtime exemption sharply, narrow the duties tests, "
            "and extend hour protections to contract and gig work.",
        ),
    ),
    # Axis: ease of organizing and bargaining. Negative = more employer
    # latitude, positive = more union-side rules.
    *_cards(
        "union-organizing-and-bargaining",
        (
            "make-organizing-easier",
            "+1",
            "Make it easier to form and keep a union",
            "Allow card-check recognition, penalize employers who retaliate during a campaign, "
            "and require first-contract arbitration when bargaining stalls.",
        ),
        (
            "keep-membership-and-dues-voluntary",
            "-1",
            "Keep union membership and dues voluntary",
            "Preserve state right-to-work laws, require a secret ballot for recognition, and bar "
            "any requirement that a worker pay dues to hold a job.",
        ),
        (
            "sectoral-bargaining-boards",
            "+2",
            "Set pay and conditions across whole sectors",
            "Create boards where unions and employers negotiate wage and hour standards covering "
            "every firm in an industry, whether or not its own workers are organized.",
        ),
        (
            "national-right-to-work",
            "-2",
            "Make union dues voluntary in every state",
            "Write right-to-work into federal law so no worker anywhere can be required to join or "
            "pay a union as a condition of employment, overriding state law to the contrary.",
        ),
    ),
    # Axis: workplace regulation. Negative = fewer federal standards,
    # positive = more.
    *_cards(
        "workplace-health-and-standards",
        (
            "issue-heat-and-injury-standards",
            "+1",
            "Set enforceable federal heat and injury standards",
            "Issue binding rules on heat exposure, ergonomic injury and workplace violence, and "
            "raise penalties for repeat violations that injure workers.",
        ),
        (
            "rely-on-employer-safety-programs",
            "-1",
            "Rely on employer programs over new federal mandates",
            "Favor voluntary partnerships, consultation and industry guidance over new federal "
            "standards, and keep penalties aimed at willful violations.",
        ),
        (
            "guarantee-paid-sick-and-family-leave",
            "+2",
            "Guarantee paid sick and family leave nationally",
            "Create a federal insurance program providing paid leave for illness, a new child or "
            "a sick relative, funded by contributions and covering every worker.",
        ),
        (
            "end-federal-standard-setting",
            "-2",
            "End federal workplace standard-setting",
            "Strip the federal regulator of authority to issue workplace standards and leave "
            "safety to state law, insurers, and liability for the injuries that do occur.",
        ),
    ),
    # === Social Security and retirement income =============================
    # Axis: how solvency is restored. Negative = restrain benefit growth,
    # positive = raise revenue and benefits.
    *_cards(
        "social-security-solvency",
        (
            "lift-the-payroll-tax-cap",
            "+1",
            "Tax high earnings to fund full benefits",
            "Apply the payroll tax to wages above the current ceiling so scheduled benefits are "
            "paid in full without reducing anyone's check.",
        ),
        (
            "slow-benefit-growth-for-higher-earners",
            "-1",
            "Slow benefit growth for higher lifetime earners",
            "Restore solvency by trimming the formula for people with higher career earnings and "
            "using a slower inflation measure, leaving low earners untouched.",
        ),
        (
            "raise-the-minimum-benefit",
            "+2",
            "Raise benefits, especially the minimum",
            "Increase checks across the board, set a minimum benefit above the poverty line for "
            "anyone with a full work history, and fund it with payroll and investment taxes.",
        ),
    ),
    # Axis: the retirement age. Negative = later eligibility, positive =
    # earlier or more flexible.
    *_cards(
        "retirement-age-and-eligibility",
        (
            "keep-the-current-ages",
            "0",
            "Leave the retirement ages exactly as they are",
            "Make no change to early or full retirement ages, and close any funding gap through "
            "revenue or formula changes rather than by making people work longer.",
        ),
        (
            "raise-the-full-retirement-age",
            "-1",
            "Raise the full retirement age gradually",
            "Phase the full retirement age up by a year or two for people now in mid-career, "
            "reflecting longer average lifespans, with no change for those near retirement.",
        ),
        (
            "lower-the-early-retirement-age",
            "+1",
            "Let people in physical work retire earlier",
            "Lower the early eligibility age, or create an early full benefit for workers with "
            "long careers in physically demanding jobs, without a lifetime reduction.",
        ),
    ),
    # Axis: regulation of private retirement saving. Negative = lighter rules
    # and more individual choice, positive = mandates and fiduciary duties.
    *_cards(
        "private-retirement-savings",
        (
            "auto-enroll-every-worker",
            "+1",
            "Auto-enroll every worker in a retirement account",
            "Require employers without a plan to enroll workers automatically in a portable "
            "account with default contributions they may opt out of.",
        ),
        (
            "expand-tax-free-saving-options",
            "-1",
            "Widen tax-advantaged accounts and keep saving voluntary",
            "Raise contribution limits, widen eligibility for individual accounts and universal "
            "savings vehicles, and impose no enrollment mandate on employers.",
        ),
        (
            "require-fiduciary-duty-and-fee-caps",
            "+2",
            "Hold advisers to a fiduciary duty and cap plan fees",
            "Require anyone advising on retirement money to act in the saver's interest, cap fees "
            "in employer plans, and bar complex products from default options.",
        ),
    ),
    # === Technology and data privacy =======================================
    # Axis: limits on data collection. Negative = lighter rules, positive =
    # stronger restrictions and user rights.
    *_cards(
        "personal-data-collection-rules",
        (
            "federal-privacy-law-with-user-rights",
            "+1",
            "Pass a federal privacy law with enforceable user rights",
            "Give people rights to see, correct, delete and port their data, require opt-in "
            "consent for sensitive categories, and let them sue over violations.",
        ),
        (
            "one-light-national-standard",
            "-1",
            "Keep rules light and preempt the state patchwork",
            "Set one minimal national standard that replaces state privacy laws, relying on "
            "disclosure and industry codes rather than consent requirements.",
        ),
        (
            "ban-sale-of-sensitive-data",
            "+2",
            "Ban the sale of location, health and biometric data",
            "Prohibit brokers from buying or selling precise location, health, biometric and "
            "children's data outright, rather than regulating how consent is obtained.",
        ),
    ),
    # Axis: platform responsibility for user content. Negative = platforms
    # left alone or barred from moderating, positive = platforms made liable.
    *_cards(
        "platform-liability-and-moderation",
        (
            "narrow-platform-immunity",
            "+1",
            "Make platforms answerable for what they amplify",
            "Narrow the liability shield so platforms can be sued over content their algorithms "
            "actively promote, while leaving ordinary hosting protected.",
        ),
        (
            "bar-platforms-from-viewpoint-moderation",
            "-1",
            "Stop large platforms from removing lawful speech",
            "Treat the biggest platforms as common carriers barred from removing or demoting "
            "lawful posts on the basis of viewpoint, with a user right of appeal.",
        ),
        (
            "require-transparency-and-researcher-access",
            "0",
            "Keep the current liability rules but open the black box",
            "Leave the immunity provisions as they are while requiring platforms to publish "
            "moderation and recommendation data and give vetted researchers access.",
        ),
    ),
    # Axis: regulation of AI systems. Negative = permissive, positive =
    # licensing and prohibition.
    *_cards(
        "artificial-intelligence-oversight",
        (
            "license-and-audit-frontier-models",
            "+1",
            "License and audit the largest AI systems before release",
            "Require developers of the most capable models to register, submit to independent "
            "safety evaluation and report incidents before and after deployment.",
        ),
        (
            "avoid-rules-that-slow-development",
            "-1",
            "Keep AI rules minimal and let development proceed",
            "Apply existing product, fraud and civil rights law rather than new AI-specific "
            "regulation, and avoid licensing requirements that entrench incumbents.",
        ),
        (
            "ban-high-risk-uses-outright",
            "+2",
            "Prohibit the highest-risk uses outright",
            "Ban AI in specified settings — mass biometric surveillance, autonomous weapons "
            "targeting, social scoring — regardless of what safety testing shows.",
        ),
        (
            "preempt-state-ai-laws",
            "-2",
            "Preempt state AI laws and set no federal ones",
            "Bar states from regulating AI systems and decline to impose federal rules either, so "
            "development proceeds under existing general law alone.",
        ),
    ),
    # === Veterans and military families ====================================
    # Axis: where veterans get care. Negative = private care paid for by the
    # VA, positive = care inside the VA system.
    *_cards(
        "veterans-health-care-access",
        (
            "invest-in-va-facilities-and-staff",
            "+1",
            "Build up VA hospitals, clinics and staffing",
            "Fund hiring, pay parity and facility construction inside the VA so veterans can be "
            "seen in the system built for them without long waits.",
        ),
        (
            "let-veterans-choose-private-providers",
            "-1",
            "Let veterans take VA funding to any provider",
            "Allow any enrolled veteran to see a private doctor at VA expense without a wait-time "
            "or distance test, shifting care out of VA facilities where they prefer.",
        ),
        (
            "keep-community-care-with-standards",
            "0",
            "Keep today's mix of VA and community care",
            "Leave the current eligibility rules for outside care in place while enforcing wait "
            "time and quality standards on both the VA and the community providers it pays.",
        ),
    ),
    # Axis: disability claims. Negative = tighter adjudication, positive =
    # more generous presumptions and faster approval.
    *_cards(
        "disability-and-benefit-claims",
        (
            "presume-service-connection-for-exposures",
            "+1",
            "Presume service connection for toxic exposures",
            "Add conditions linked to burn pits, Agent Orange and contaminated water to the "
            "presumptive list so claimants need not prove causation individually.",
        ),
        (
            "tighten-ratings-and-audit-claims",
            "-1",
            "Tighten rating criteria and audit existing awards",
            "Update the rating schedule to current medicine, require periodic reexamination for "
            "conditions that improve, and audit awards to control improper payments.",
        ),
        (
            "clear-the-backlog-with-staff-and-automation",
            "0",
            "Keep the criteria and clear the backlog",
            "Leave eligibility rules unchanged while hiring claims staff, automating evidence "
            "gathering and setting a statutory deadline for deciding an appeal.",
        ),
    ),
    # Axis: support for the move to civilian work. Negative = employer and
    # market led, positive = federally funded programs and mandates.
    *_cards(
        "transition-to-civilian-employment",
        (
            "fund-transition-training-and-placement",
            "+1",
            "Fund training and placement during transition",
            "Extend education benefits, pay for credentialing and apprenticeships, and fund "
            "counselors who place separating service members into jobs before discharge.",
        ),
        (
            "use-tax-credits-to-encourage-hiring",
            "-1",
            "Encourage hiring through tax credits, not programs",
            "Rely on employer tax credits and streamlined license recognition rather than federal "
            "transition programs, and let the labor market match veterans to work.",
        ),
        (
            "guarantee-federal-hiring-and-license-portability",
            "+2",
            "Guarantee federal hiring preference and license portability",
            "Require federal agencies and contractors to hire qualified veterans on preference "
            "and make every state recognize military training toward civilian licenses.",
        ),
    ),
)


def stance_rows() -> list[Stance]:
    """Every row the seed writes, in declaration order.

    Declaration order, not sorted: the order cards are written in is itself a
    neutrality decision (see `STANCES`' comment on why no group is in axis
    order), and sorting here would undo it for any client that reads back
    without shuffling.

    No reference ordering problem to solve, unlike story 3.1's parents-before-
    children: every `topic_id` points into `issue_topics`, which story 3.1's
    seed must already have written. That ordering constraint is between the two
    seeds, not within this one, and it is why `scripts/seed_stances.py`
    documents `scripts/seed_taxonomy.py` as a prerequisite rather than trying
    to enforce it.
    """
    return [card.row() for card in STANCES]


def stance_row_ids() -> set[str]:
    """Every id this module writes, for the live verifier to read back.

    Derived from `stance_rows()` rather than listed separately, so the two
    cannot drift — a verifier checking a stale id list would report PASS on
    rows the seed no longer writes.
    """
    return {str(row.id) for row in stance_rows()}


def stances_by_topic() -> dict[str, list[StanceCard]]:
    """The declared cards grouped by subtopic id, in declaration order.

    The shape every balance check wants: "does this subtopic's set span the
    axis" is a question about a group, and computing the grouping once here
    keeps the tests from each re-deriving it slightly differently.
    """
    grouped: dict[str, list[StanceCard]] = {}
    for card in STANCES:
        grouped.setdefault(card.topic_id, []).append(card)
    return grouped


#: The declared grouping, computed once. `stances_by_topic()` stays a function
#: because a caller that mutates the lists it gets back should not corrupt
#: everyone else's view; this is the module's own copy, used by the import-time
#: guard below.
STANCES_BY_TOPIC: dict[str, list[StanceCard]] = stances_by_topic()


def balance_complaints(codes_by_topic: dict[str, list[str]]) -> dict[str, list[str]]:
    """What is wrong with each subtopic's direction codes, if anything.

    Takes *codes* rather than cards, so the same function answers the question
    for the declared corpus, for a set of rows read back through `Stance`, and
    for raw `row_data` dicts off a live unfiltered read. That is the point of it
    existing: story 3.2's balance rule is asserted in four places — the
    import-time guard below, `tests/test_stance_cards_unit.py`,
    `tests/features/stance_cards.feature`'s step definitions and
    `scripts/verify_stance_cards_live.py` — and four hand-rolled versions of
    "spans the axis" would eventually disagree about an edge case, with the
    loosest one deciding what ships. One implementation, four callers, exactly
    as story 3.1 does with `contains_stance_presupposing_word`.

    A subtopic is complained about when any of four things holds. Each is
    argued in the module docstring, and each has a distinct failure mode that a
    reader of the message wants named:

      * **too few cards** — fewer than `MINIMUM_STANCES_PER_SUBTOPIC`, the
        acceptance criterion;
      * **one direction only** — every card coded the same, which is one
        position in three outfits rather than a choice;
      * **one side only** — no negative or no positive code. Two distinct
        values is not enough: `+1` and `+2` are one side twice, and a one-sided
        menu plus a neutral option is still one-sided;
      * **a repeated code** — two cards at the same point on the axis, which
        makes that point twice as likely to be picked by a voter skimming,
        a nudge even when both wordings are fair.

    Returns a mapping of topic id to the reasons for it, empty when the whole
    input is balanced — so a caller asserts on emptiness and prints the
    mapping, rather than discovering one problem at a time.
    """
    complaints: dict[str, list[str]] = {}
    for topic_id, raw_codes in codes_by_topic.items():
        reasons: list[str] = []
        codes = [int(code) for code in raw_codes]
        if len(codes) < MINIMUM_STANCES_PER_SUBTOPIC:
            reasons.append(f"only {len(codes)} stance(s), needs {MINIMUM_STANCES_PER_SUBTOPIC}")
        if len(set(codes)) < 2:
            reasons.append(f"every stance is coded {codes[0] if codes else 'nothing'}")
        elif not any(code < 0 for code in codes):
            reasons.append(f"no option on the negative side: {sorted(codes)}")
        elif not any(code > 0 for code in codes):
            reasons.append(f"no option on the positive side: {sorted(codes)}")
        repeated = sorted({code for code in codes if codes.count(code) > 1})
        if repeated:
            reasons.append(f"repeated direction code(s) {repeated}")
        if reasons:
            complaints[topic_id] = reasons
    return complaints


# ---------------------------------------------------------------------------
# The seeder
# ---------------------------------------------------------------------------


@dataclass
class StanceSeedReport:
    """What one stance seed run wrote, for the CLI's summary.

    Holds every row the run wrote, whether or not its stored contents changed —
    like `app.seed.SeedReport` and `app.seed_taxonomy.TaxonomySeedReport`, this
    seed restates its rows rather than skipping existing ones, so "written" is
    the only count it can honestly report. The idempotency claim is about the
    stored row *count* and about the second run issuing PUTs rather than POSTs,
    both of which the feature file and the live verifier check directly.
    """

    stances_written: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.stances_written)

    @property
    def topics_covered(self) -> int:
        """How many distinct subtopics the run wrote at least one card for.

        Reported because "180 rows written" and "60 subtopics covered" fail
        differently: the first number can be right while a subtopic got six
        cards and another got none.
        """
        return len({row_id.rsplit(":", 1)[0] for row_id in self.stances_written})


class StanceSeeder:
    """Writes every stance card, idempotently.

    `Repository.upsert` is the whole idempotency mechanism (see `app/seed.py`'s
    docstring, which this module follows exactly): it finds the row by its
    natural-key id and replaces it, or inserts when there is none, so running
    the seed any number of times leaves exactly one row per id. "Replaces", not
    "skips" — this module is the single source of truth for stance wording
    until story 3.3's panel workflow exists, so a hand-edited label is restored
    on the next run rather than preserved.

    Each write's reference check reads the card's subtopic out of
    `issue_topics`, so **story 3.1's seed is a hard prerequisite**: against a
    project with no taxonomy every card is refused. That is deliberate — a
    stance pointing at a topic that does not exist is unusable, and failing on
    the first card is a better outcome than 180 orphans.

    `pace` is the same injectable pacer `Bootstrapper`, `Seeder` and
    `TaxonomySeeder` use, and it matters more here than for any of them: ~180
    rows, each an upsert costing a find and a write, against a project observed
    to throttle at 60 requests per minute (datamodel.md "Open questions").

    **It defaults to the real pacer, not a no-op**, for the same reason
    `TaxonomySeeder` does: its only production caller is a CLI of its own, with
    nothing above it to supply one. Tests pass `pace=lambda: None` explicitly.

    Not atomic, like every other write path here: two concurrent seeds could
    both find nothing for the same id and both insert. Acceptable for a seed
    that runs manually per environment, and the next run collapses the
    duplicate it would find.
    """

    def __init__(self, client: ZeroDBClient, *, pace: Callable[[], None] | None = None) -> None:
        self._client = client
        self._pace = pace if pace is not None else default_pacer()
        self._stances: Repository[Stance] = Repository(client=client, model=Stance)

    def run(self) -> StanceSeedReport:
        """Write every stance card and report what was written.

        Raises:
            MissingReferenceError: a card's subtopic is not stored. In practice
                this means `scripts/seed_taxonomy.py` has not been run against
                this project, which is worth failing loudly on rather than
                seeding a partial corpus.
            ZeroDBError: a live call failed.
        """
        report = StanceSeedReport()
        for row in stance_rows():
            self._pace()
            self._stances.upsert(row.row_data())
            row_id = str(row.id)
            report.stances_written.append(row_id)
            logger.info("stance seed: wrote %r (%s)", row_id, row.label)
        logger.info(
            "stance seed: %d stance(s) written across %d subtopic(s)",
            report.row_count,
            report.topics_covered,
        )
        return report


# ---------------------------------------------------------------------------
# Guards on the editorial data itself, checked at import rather than only in
# the test suite: a seeder that writes an unbalanced corpus into the live
# project is worse than one that refuses to start. The unit tests assert the
# same properties and name the offending subtopic when they fail.
# ---------------------------------------------------------------------------

_COMPLAINTS = balance_complaints(
    {
        topic_id: [card.direction_code for card in cards]
        for topic_id, cards in STANCES_BY_TOPIC.items()
    }
)
assert not _COMPLAINTS, "the stance corpus is not balanced:\n  " + "\n  ".join(
    f"{topic_id}: {'; '.join(reasons)}" for topic_id, reasons in sorted(_COMPLAINTS.items())
)

assert len(STANCES_BY_TOPIC) == EXPECTED_SUBTOPIC_COUNT, (
    f"expected cards for all {EXPECTED_SUBTOPIC_COUNT} subtopics, got {len(STANCES_BY_TOPIC)}"
)

del _COMPLAINTS


__all__ = [
    "DIRECTION_CODES",
    "MINIMUM_STANCES_PER_SUBTOPIC",
    "PARTISAN_IDENTITY_WORDS",
    "STANCES",
    "STANCES_BY_TOPIC",
    "WORDING_VERSION",
    "StanceCard",
    "StanceSeedReport",
    "StanceSeeder",
    "balance_complaints",
    "contains_partisan_identity_word",
    "stance_row_ids",
    "stance_rows",
    "stances_by_topic",
]
