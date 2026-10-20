"""Bill-to-stance classifier v0 (story 5.5).

backlog.md 5.5: "As the system, I want bills tagged with topics and with whether
they advance or oppose each stance, so that votes translate into stance
positions."
Acceptance: "At least 85% precision on a 200-bill labeled set; low-confidence
tags go to QA."
ZeroDB data: bills, graph: advances/opposes

===============================================================================
THE GAP THIS CLOSES
===============================================================================

`app.scoring.positions` (story 6.2) computes an incumbent's position per stance
by matching `member_votes.topic_ids[]` against `stances.topic_id`, and its own
docstring states the consequence plainly: "It currently finds nothing on live
data, and that is the correct answer... roll calls carry `topic_ids: []` until
story 5.5 classifies them."

This module is that classifier. Everything downstream of it — 6.2's positions,
6.4's alignment formula, 6.5's confidence badges, 6.7's cited reasons — was
built against an input nothing produced. The chain it completes is:

    bills.subjects[] (story 5.3, CRS)
      -> bills.topic_ids[]            (this module, `classify_bill`)
      -> roll_calls.topic_ids[]       (this module, `propagate_to_roll_calls`)
      -> member_votes.topic_ids[]     (denormalised copy, datamodel.md)
      -> candidate_positions          (story 6.2)

and, alongside it, the `bill -advances|opposes-> stance` edges datamodel.md's
predicate table reserves for exactly this story.

===============================================================================
WHAT "v0" HONESTLY MEANS HERE, AND WHAT IT DOES NOT ATTEMPT
===============================================================================

The story's title says v0, and this is the scope line, stated before the code
rather than discovered in it. **Two different questions hide inside "does this
bill advance or oppose this stance", and this module answers only the first:**

  1. **WHICH TOPIC is this bill about?** Attempted, from CRS's own controlled
     subject vocabulary, and **measured at 54.2% precision on a real 200-bill
     labeled set — well below story 5.5's 85% bar.** See "THE PRECISION
     MEASUREMENT" below for the number, the method, and why no threshold fixes
     it. The consequence is `ACCEPT_THRESHOLD = None`: this classifier accepts
     nothing automatically and routes every tag to QA.
  2. **WHICH WAY does this bill move that topic's axis?** NOT answered. It
     needs the bill's text or summary read against the specific stance, which
     is a reading-comprehension task, not a lookup. There is no LLM credential
     in this environment (story 9.3 established this three independent ways;
     re-confirmed for this story), and more importantly CRS subject terms
     **carry no direction at all**: `Prescription drug costs` is assigned
     identically to a bill capping insulin prices and to a bill repealing a
     price cap. No amount of rule-writing over a direction-free vocabulary can
     recover a direction.

So `DIRECTION_UNKNOWN` is this module's answer to question 2, and the
consequence is deliberate and visible: **a bill whose direction is unknown
writes NO `advances`/`opposes` edge.** It writes its topic tags (which is what
unblocks 6.2) and routes the direction question to QA. See
`stance_edges_for_tag` and the "THE advances/opposes HALF" section below for
why a guessed direction would be worse than no edge.

===============================================================================
THE PRECISION MEASUREMENT: WHAT WAS MEASURED, HOW, AND WHAT IT CAME TO
===============================================================================

**THE BAR IS NOT MET. 54.2%, not 85%.** This section is the most important one
in the module, because the measurement is the story's acceptance criterion and
it came out negative.

**What was measured.** `tests/fixtures/bill_topic_labeled.json`: 200 real
measures that received a recorded House roll-call vote in the 118th or 119th
Congress, captured live from api.congress.gov on 2026-10-19 with their real CRS
policy area and legislative subject terms. Stratified by CRS policy area (up to
12 per area, `random.seed(42)`) from all 889 distinct measures voted on in those
two Congresses, so all 30 policy areas that occur are represented. The fixture's
`_comment` carries the full draw and protocol.

**How each tag was labeled.** One question per proposed tag: *would a voter who
selected a stance under this taxonomy topic consider this bill's recorded vote
to be evidence about that stance?* Judged from the bill's own title plus its
full CRS subject list, both of which are stored in the fixture so every
judgment is re-checkable. Every FALSE label carries a `why_wrong` string naming
its specific reason, so a reviewer who disagrees has exactly one line to argue
with rather than a bulk verdict.

**The numbers.**

    proposed tags over the 200 bills      225
    judged correct                        122
    precision                             54.2%     (bar: 85%)
    precision, deliberately LENIENT       65.8%     (bar: 85%)
    bills getting any tag                 116 / 200

The lenient figure re-scores every tag rejected merely for being coarse, narrow
or ceremonial as correct, asking only "is this bill topically related at all".
**The finding is robust to the labeling standard**: 65.8% is still nowhere near
85%, so the gap cannot be argued away as labeler strictness.

**No threshold fixes it, and this is the load-bearing finding.** Nine
principled rule variants were measured on the same set:

    rule variant                                     n    precision  coverage
    ----------------------------------------------------------------------
    baseline (policy area + subject terms)          225      54.2%    116/200
    subtopic tags only (drop the coarse tag)        117      49.6%     84/200
    subtopic AND >=2 agreeing terms                  34      52.9%     28/200
    subtopic AND both corroborations (conf 1.0)      13      53.8%     12/200
    subtopic AND bill has <=6 CRS terms              54      50.0%     45/200
    subtopic AND <=6 terms AND >=2 agreeing          10      60.0%      9/200
    subtopic AND subtopic spread <=2                 70      47.1%     58/200
    subtopic AND subtopic spread <=1                 30      46.7%     30/200
    subtopic AND spread <=2 AND >=2 terms            14      50.0%     13/200

Precision stays flat at 47-60% as the rule tightens, while coverage collapses
from 116 bills to 9. **Tightening buys nothing.** A classifier whose precision
is insensitive to its own confidence does not have a threshold problem; the
confidence signal carries no information about correctness, which the per-tier
breakdown confirms directly — fully-corroborated tags scored 53.8%, barely
different from the 54.2% baseline.

**Why, mechanically.** CRS subject terms record what a bill **mentions**, not
what it is **about**, and nothing downstream of that vocabulary can recover the
difference. Three real cases from the set:

  * *Supporting America's Children and Families Act* carries `Correctional
    facilities and imprisonment` AND `Detention of persons` — parental
    incarceration as a child-welfare risk factor. Two independent agreeing
    terms, so every corroboration rule accepts it as a sentencing-policy tag.
    It is a child-welfare bill.
  * *Billie Jean King Congressional Gold Medal Act* carries `Sex, gender,
    sexual orientation discrimination` — the honoree's biography, not a
    provision. Tagged as anti-discrimination enforcement.
  * *Wounded Warrior Access Act* is filed under the `Armed Forces and National
    Security` policy area, which no mapping can distinguish from a
    defense-spending bill. Tagged as defense policy.

Each of these has CRS indexing that is entirely CORRECT. The information needed
to tell "mentions" from "is about" is in the bill's text and summary, and is not
present in any field this classifier can read.

**What that means for this module's behaviour.** `ACCEPT_THRESHOLD` is `None`.
Nothing is accepted automatically; every tag is routed to QA with a reason. That
is the posture the brief for this story names explicitly and that story 9.3 set
the precedent for: when a measured bar is not met, route everything to the human
path and report the real number, rather than lower the bar until the number
appears. The confidence model below is retained and still computed — it is
genuinely informative for *ordering a reviewer's queue* (a 1.0 tag is worth
looking at before a 0.5 one) — but it no longer gates a write, and the tests
assert that it does not.

**What would unblock the bar**, concretely, for whoever picks this up: the
signal has to come from the bill's TEXT or official SUMMARY, not its subject
index. `bills.summary` exists in the schema and is null for every row (story
5.3 does not fetch it); Congress.gov publishes it at
`/v3/bill/{congress}/{type}/{number}/summaries`. With a summary plus an LLM (no
credential exists in this environment — story 9.3 established that three
independent ways and this story re-confirmed it), the 85% bar is a reasonable
target, and this module's seams are already placed for it: swap `classify_bill`
for a summary-reading classifier and nothing else in the pipeline moves.

===============================================================================
THE CONFIDENCE MODEL: CORROBORATION (RETAINED FOR QUEUE ORDER, NOT FOR WRITES)
===============================================================================

A tag's confidence is built from how many INDEPENDENT pieces of CRS signal agree
that the bill is about that subtopic:

  * **The policy area agrees** (+`POLICY_AREA_CORROBORATION`): the bill's single
    CRS policy area maps to the subtopic's own parent topic.
  * **Two or more distinct subject terms agree** (+`SECOND_TERM_CORROBORATION`):
    two different librarian-assigned terms landing on one subtopic.
  * **Breadth rule** (`MAX_SUBTOPIC_SPREAD`): a bill whose subject list reaches
    many distinct subtopics is evidence about none of them in particular.
    prd.md's own omnibus rule: "a vote on a 1,000-page bill is not a vote for
    every clause". Measured live over 889 real voted measures, the median bill
    reaches 1 subtopic and EVERY bill reaching 8+ was an appropriations,
    continuing-resolution or reconciliation bill.

**It is kept, with its measured limits stated, rather than deleted.** Two
reasons. First, the breadth rule is the one part that demonstrably works: it
correctly held every one of the 63 omnibus-shaped measures in the live
population, including the NDAA (201 terms) and eight continuing resolutions
(249 terms each), and prd.md requires that behaviour independently of story
5.5's precision bar. Second, a reviewer queue of 225 items needs an order, and
confidence plus breadth reason is the best available one. What the measurement
forbids is using it as an ACCEPT gate, and `ACCEPT_THRESHOLD = None` is how
that is enforced in code rather than in a comment.

===============================================================================
THE advances/opposes HALF
===============================================================================

datamodel.md's predicate table: `bill` -> `advances` / `opposes` -> `stance`,
properties `confidence, method`. `app.evidence_graph.EvidenceGraphWriter`
(story 13.1) already has `write_bill_stance_evidence` with exactly that
signature, and this module calls it rather than touching
`graph_create_edge` — the established pattern, and the reason `effect` is
validated against `BILL_STANCE_EFFECTS` before a request is spent.

What this module supplies is the `effect`, and per the scope note above it
supplies it only when a direction is actually known. `BillDirection` has three
values and `DIRECTION_UNKNOWN` is the one v0 produces for a bill classified from
CRS terms alone. The other two exist because the seam matters: when a later
story reads bill text, it sets `direction` on the `TopicTag` and
`stance_edges_for_tag` immediately produces real edges with no change here.

`stance_edges_for_tag` projects a known bill direction onto each stance's own
signed `direction_code`, exactly as `app.scoring.positions._stance_direction`
does for votes, and for the same reason: two mirrored stances under one topic
must get exactly opposite effects from the same bill, so no direction of any
axis is advantaged by the arithmetic. prd.md's "nonpartisan by construction" is
that symmetry, and `test_two_mirrored_stances_get_opposite_effects` pins it. A
stance whose `direction_code` is `0` ("keep current policy") gets no edge at
all: a bill that changes policy is not evidence for or against a voter who asked
for no change, and inventing a sign for it would be the classifier taking a
political position.

===============================================================================
WHAT THIS MODULE DOES NOT DO
===============================================================================

  * **It does not write `people`, `bills` or `roll_calls` rows.** It updates
    `topic_ids` on rows some ingest story already wrote, and refuses a tag
    whose `issue_topics` row is not stored (`_resolve_topic`), so a tag can
    never name a topic a reader cannot resolve.
  * **It does not create a QA queue TABLE.** datamodel.md's QA queue is
    `promises.qa_pending` plus story 10.3's reviewer UI; it gives
    `bills` no QA field and this story does not invent one. QA-routed tags are
    returned on the report (`ClassificationReport.qa_queue`) and emitted as
    `bill.tags_pending_qa` events, which is what a reviewer queue reads. The
    judgment call and the alternative are in "OPEN QUESTIONS".
  * **It does not reclassify under a new taxonomy version.** Tags name
    `:v1` topic ids because that is the seeded taxonomy; a `:v2` taxonomy is a
    re-run, not a migration, because `issue_topics` rows are never mutated
    (`app.seed_taxonomy`, rule 2).

===============================================================================
OPEN QUESTIONS FOR REVIEW (flagged, not resolved here)
===============================================================================

  1. **THE ACCEPTANCE CRITERION IS NOT MET AND THIS STORY DOES NOT CLOSE IT.**
     54.2% measured against an 85% bar. The decision taken here is to ship the
     pipeline with every tag routed to QA, report the real number, and leave the
     bar open — not to lower it. **A human needs to decide what happens next**,
     and the three real options are:
       (a) fetch `bills.summary` from Congress.gov's `/summaries` endpoint and
           classify from the summary text, which needs an LLM credential this
           environment does not have (the route this module recommends, and the
           one its seams are placed for);
       (b) accept a human-in-the-loop classifier permanently, i.e. treat story
           10.3's reviewer queue as the real classifier and this module as its
           candidate generator — which is defensible and is what the code does
           today, but means story 6.2's scoring stays dark until reviewers work
           the queue;
       (c) re-scope story 5.5's criterion, if 85% precision on a
           *topic* tag was a tighter bar than the product actually needs.
     This module cannot choose between those; it can only make the choice
     visible with a real number attached.
  2. **No `advances`/`opposes` edge is written from live data at all**, because
     CRS terms carry no direction (see the scope note at the top). Story 5.5's
     title says "whether they advance or oppose each stance", and that half is
     entirely undone — the code path is complete and tested, with nothing to
     feed it. Reported as such rather than papered over with a guessed sign.
  3. **The three confidence constants are not in `methodology_versions`.**
     They are scoring inputs by the same argument that put `badge_thresholds`
     there (story 6.5) and `BUNDLED_WEIGHT_FACTOR`'s own comment makes the
     identical confession. A follow-up should move all of them together;
     widening the published methodology inside this story would mean a
     methodology version change nobody's acceptance criterion asked for. Lower
     priority than it looks, since none of them currently gates a write.
  4. **`bills` has no `qa_pending` field.** Story 10.3 builds the reviewer
     queue against `promises`; whether bill-stance tags belong in that same
     queue (and therefore want a field here) is a question for that story,
     which is why this one emits an event rather than adding a column. This is
     now more pressing than it was when written, because with nothing
     auto-accepted the QA queue is the ONLY output this module has.
  5. **`TAGS_PENDING_QA_TOPIC` is not in datamodel.md's Events table.** A new
     topic this story introduces; it belongs in that table, and adding it is a
     documentation change this branch flags rather than makes unilaterally.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.evidence_graph.writer import EvidenceGraphWriter
from app.ingest.bill_topic_mapping import (
    MAPPING_VERSION,
    normalise_term,
    subtopic_for_subject_term,
    topic_for_policy_area,
    topic_id,
)
from app.repositories.models import Bill, IssueTopic, MemberVote, RollCall, Stance
from app.seed_taxonomy import TOPICS
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: This classifier's own version, recorded on every tag so a precision figure
#: is attributable and a later version's regression is visible. Distinct from
#: `bill_topic_mapping.MAPPING_VERSION`: the code and the table change
#: independently, and `TopicTag.method` carries both.
CLASSIFIER_VERSION = "bill-topic-classifier/v0"

#: datamodel.md's Events table has no entry for this story, so this is a new
#: topic rather than a documented one -- named for what a reviewer queue
#: subscribes to. Flagged in this story's PR as a datamodel.md addition.
TAGS_PENDING_QA_TOPIC = "bill.tags_pending_qa"

#: Confidence a lone mapped CRS subject term produces, with nothing agreeing.
#:
#: Deliberately BELOW `ACCEPT_THRESHOLD`: measured on the real labeled set,
#: single-term-only tags are predominantly false positives (see the module
#: docstring's "honest shape of the result"). A lone term means a librarian
#: attached one index term to a bill that mentions a subject in passing, which
#: is exactly what an incidental mention looks like.
BASE_TERM_CONFIDENCE = 0.5

#: Added when the bill's CRS policy area maps to this subtopic's parent topic.
#: The strongest single signal: CRS assigns exactly one policy area per bill,
#: as a judgment about what the bill is primarily about.
POLICY_AREA_CORROBORATION = 0.35

#: Added when a SECOND distinct mapped CRS term lands on the same subtopic.
#: Two independent librarian-assigned terms agreeing is hard to produce by
#: accident. Capped at one bonus however many terms agree -- a third term is
#: not three times the evidence, and letting the bonus accumulate would let a
#: 249-term appropriations bill buy confidence by volume, which is precisely
#: the omnibus failure `breadth_factor` exists to prevent.
SECOND_TERM_CORROBORATION = 0.35

#: **None: this classifier accepts nothing automatically.**
#:
#: Not a disabled feature and not a placeholder — it is the measured result.
#: Story 5.5's acceptance criterion is 85% precision on a 200-bill labeled set;
#: the real measurement is **54.2%** (65.8% under a deliberately lenient
#: labeling standard), and nine principled rule variants all land between 47%
#: and 60% while coverage collapses. See the module docstring's precision
#: section for the full table and the mechanical reason: CRS subject terms
#: record what a bill MENTIONS, not what it is ABOUT.
#:
#: So every tag is routed to QA with a reason, which is exactly what
#: backlog.md 5.5's second acceptance criterion asks of a low-confidence tag,
#: applied to all of them because the measurement says all of them are
#: low-confidence. The precedent is story 9.3's promise extractor: when a
#: measured bar is not met, route to the human path and report the real number.
#:
#: **A float here re-enables automatic acceptance.** It is deliberately typed
#: `float | None` and threaded through `classify_bill` as a parameter so that
#: (a) the tests can pin what a threshold WOULD do without changing production
#: behaviour, and (b) whoever reaches the bar with a better signal turns this
#: on in one place. The number to use when that day comes is 0.85 — the same
#: threshold datamodel.md publishes for story 10.1's mapping agent ("anything
#: under 0.85 goes to the QA queue"), and the value of
#: `BASE_TERM_CONFIDENCE + POLICY_AREA_CORROBORATION`.
ACCEPT_THRESHOLD: float | None = None

#: What `ACCEPT_THRESHOLD` should be set to if a future classifier earns the
#: right to accept tags automatically. Named rather than left in a comment so
#: `test_the_documented_threshold_is_one_corroboration` can assert it still
#: equals one corroboration's worth of confidence.
DOCUMENTED_ACCEPT_THRESHOLD = 0.85

#: The measured precision of this classifier on `bill_topic_labeled.json`, and
#: the bar story 5.5 states. Constants rather than prose so
#: `test_the_measured_precision_is_recorded_and_below_the_bar` can assert the
#: module's own claim about itself matches what the labeled set actually says —
#: a docstring can drift from a fixture, a tested constant cannot.
MEASURED_PRECISION_PERCENT = 54.2
REQUIRED_PRECISION_PERCENT = 85.0

#: How many distinct subtopics a bill's subject list may reach before every tag
#: on it is held for QA as too broad to be evidence about anything.
#:
#: prd.md, Feature 2: "Omnibus bills: a vote on a 1,000-page bill is not a vote
#: for every clause." Measured live over 889 real House-voted measures
#: (2026-10-19): the median bill reaches 1 subtopic, p90 reaches 5, and EVERY
#: bill reaching 8 or more was an appropriations, continuing-resolution or
#: reconciliation bill. So 8 is where the real population separates, not a
#: round number -- 63 of 889 measures are above it.
MAX_SUBTOPIC_SPREAD = 8

#: How many rows one scan asks for. Same limit, and the same honest caveat,
#: as every other paged read in this codebase (see
#: `app.scoring.positions.PAGE_SIZE`).
PAGE_SIZE = 500


class QaReason(StrEnum):
    """Why a tag was held for review rather than accepted.

    An enum rather than a free string because `ClassificationReport.qa_queue`
    is what a reviewer queue groups by, and a queue cannot filter on prose. The
    values are the documented QA reason list story 5.5's feature file asserts
    against.
    """

    #: The bill's subject list reaches more than `MAX_SUBTOPIC_SPREAD`
    #: subtopics. An omnibus; evidence about nothing in particular.
    BREADTH = "breadth"
    #: One mapped term, nothing corroborating it.
    UNCORROBORATED = "uncorroborated"
    #: **The reason every tag carries today.** This classifier's measured
    #: precision (54.2%) is below story 5.5's 85% bar, so no tag is accepted
    #: automatically regardless of its confidence. Distinct from
    #: `UNCORROBORATED` on purpose: a reviewer seeing this reason is being told
    #: "the method is not trusted yet", not "this particular tag is thin", and
    #: the two call for different review effort. It disappears on its own if
    #: `ACCEPT_THRESHOLD` is ever set to a float.
    BELOW_MEASURED_PRECISION = "below_measured_precision"
    #: Topic tag accepted, but which way the bill moves the axis is unknown, so
    #: no `advances`/`opposes` edge can be written. v0's answer for every tag;
    #: see the module docstring's scope note.
    DIRECTION_UNKNOWN = "direction_unknown"


class BillDirection(StrEnum):
    """Which way a bill moves its topic's axis, if known.

    `UNKNOWN` is what v0 produces from CRS terms alone, because CRS subject
    terms carry no direction: `Prescription drug costs` is assigned identically
    to a bill capping insulin prices and to one repealing a price cap. The other
    two values exist so that a later story reading bill text sets this field and
    `stance_edges_for_tag` starts producing real edges with no other change.
    """

    #: The bill expands or strengthens federal action along the topic's axis.
    EXPANDS = "expands"
    #: The bill contracts or repeals federal action along the topic's axis.
    CONTRACTS = "contracts"
    #: Not determinable from the available signal. v0's answer.
    UNKNOWN = "unknown"


#: Shorthand for the module docstring's scope note.
DIRECTION_UNKNOWN = BillDirection.UNKNOWN

#: subtopic slug -> its parent top-level topic slug, from the seeded taxonomy.
#: Built from `app.seed_taxonomy.TOPICS` rather than restated, so a re-parented
#: subtopic cannot leave this module disagreeing with the tree.
_SUBTOPIC_PARENT: dict[str, str] = {sub.slug: top.slug for top in TOPICS for sub in top.subtopics}


@dataclass(frozen=True)
class TopicTag:
    """One proposed (bill, topic) tag, with the evidence that produced it.

    Frozen and evidence-carrying for the same reason
    `app.scoring.positions.VoteEvidence` is: the page that shows a voter why a
    vote counted toward a stance has to cite the reason, and re-deriving it
    later would mean re-reading `bills` on every request.
    """

    bill_id: str
    #: The `issue_topics` row id, e.g. `en-US:prescription-drug-pricing:v1`.
    topic_id: str
    #: The taxonomy slug, carried for readable logs and test assertions.
    slug: str
    confidence: float
    #: The CRS term(s) that produced this tag, so an accepted tag is auditable
    #: back to the source vocabulary. A policy-area match records the policy
    #: area string.
    matched_terms: tuple[str, ...]
    #: Whether this tag is a top-level topic (from the policy area) or a
    #: subtopic (from a subject term). Only subtopic tags can reach a stance,
    #: because `stances.topic_id` is always a subtopic.
    is_subtopic: bool
    direction: BillDirection = BillDirection.UNKNOWN
    #: Set when this tag was held rather than accepted.
    qa_reason: QaReason | None = None

    @property
    def accepted(self) -> bool:
        return self.qa_reason is None

    @property
    def method(self) -> str:
        """What produced this tag, for the graph edge's `method` property.

        Carries BOTH versions, because the code and the mapping table change
        independently and a precision figure has to be attributable to the pair
        that produced it.
        """
        return f"{CLASSIFIER_VERSION}+{MAPPING_VERSION}"


@dataclass
class ClassificationReport:
    """What one classification run did. Counts, ids and CRS terms only.

    Nothing voter-derived is reachable from this module at all -- a bill's CRS
    subjects are public record and a taxonomy slug is published reference data
    -- and this type is where that stays true if a later story is tempted
    otherwise. Same posture as `app.scoring.positions.RecomputeResult`.
    """

    bills_seen: int = 0
    bills_changed: int = 0
    accepted: list[TopicTag] = field(default_factory=list)
    qa_queue: list[TopicTag] = field(default_factory=list)
    #: Bill ids whose `topic_ids` this run rewrote.
    bills_written: list[str] = field(default_factory=list)
    #: Roll call / member vote ids this run propagated topics to.
    roll_calls_written: list[str] = field(default_factory=list)
    member_votes_written: list[str] = field(default_factory=list)
    edges_written: int = 0

    @property
    def coverage(self) -> float:
        """Fraction of bills seen that got at least one ACCEPTED tag.

        Reported beside precision wherever precision is reported, because the
        two trade against each other and a precision figure alone can always be
        improved by tagging less. See the module docstring.
        """
        if not self.bills_seen:
            return 0.0
        return len({tag.bill_id for tag in self.accepted}) / self.bills_seen


# ---------------------------------------------------------------------------
# The classification itself: pure, no I/O
# ---------------------------------------------------------------------------


def classify_bill(
    *,
    bill_id: str,
    policy_area: str | None,
    subjects: Sequence[str],
    accept_threshold: float | None = ACCEPT_THRESHOLD,
    max_spread: int = MAX_SUBTOPIC_SPREAD,
) -> list[TopicTag]:
    """Every topic tag one bill's CRS classification supports, accepted or held.

    Args:
        bill_id: the `bills` row id being classified.
        policy_area: the bill's single CRS policy area, or None.
        subjects: the bill's CRS subject terms. Story 5.3 stores the policy
            area as the FIRST element of `subjects[]` as well as in its own
            field, so this function de-duplicates against `policy_area` rather
            than double-counting it as a corroborating term -- a real bug this
            would otherwise have, since that would make every policy-area match
            self-corroborating and accept every single-term tag.
        accept_threshold: the confidence at or above which a tag is accepted,
            or `None` (the production default — see `ACCEPT_THRESHOLD`) to
            accept nothing and route every tag to QA. Parameterised so a test
            can pin what a threshold WOULD do without changing production
            behaviour, and so the day the bar is actually met is a one-line
            change.
        max_spread: the breadth rule's limit. Same reasoning.

    Returns:
        Every tag, accepted and held, in a stable order (by topic id). Both,
        not just the accepted ones, because backlog.md 5.5's second acceptance
        criterion is that the held ones go somewhere -- a function that dropped
        them could not satisfy it.

    Pure: no I/O, no clock. Whether a tag's topic actually exists as a stored
    row is `BillTopicClassifier`'s question, not this one's.
    """
    topic_slug = topic_for_policy_area(policy_area)

    # Count how many DISTINCT mapped CRS terms land on each subtopic, and
    # remember which ones, so an accepted tag is auditable back to the source.
    # Keyed on the normalised term so a list repeating a term in two casings
    # cannot buy itself a corroboration bonus.
    per_subtopic: dict[str, dict[str, str]] = {}
    policy_area_normalised = normalise_term(policy_area) if policy_area else None
    for term in subjects:
        if not term:
            continue
        normalised = normalise_term(term)
        if normalised == policy_area_normalised:
            # Story 5.3 puts the policy area at the head of `subjects[]`. It is
            # already counted as the policy-area signal; counting it again here
            # would make every policy-area match self-corroborating.
            continue
        slug = subtopic_for_subject_term(term)
        if slug is None:
            continue
        per_subtopic.setdefault(slug, {})[normalised] = term

    spread = len(per_subtopic)
    too_broad = spread > max_spread

    tags: list[TopicTag] = []

    # --- the top-level tag, from the policy area --------------------------
    if topic_slug is not None:
        tags.append(
            _tag(
                bill_id=bill_id,
                slug=topic_slug,
                # A policy area is CRS's own single judgment about what the bill
                # is primarily about, so it needs no corroboration to be worth
                # standing behind -- and it cannot get one, since there is only
                # ever one policy area. Its confidence is the sum of the base
                # and the policy-area bonus by construction.
                confidence=BASE_TERM_CONFIDENCE + POLICY_AREA_CORROBORATION,
                matched_terms=(policy_area,) if policy_area else (),
                is_subtopic=False,
                accept_threshold=accept_threshold,
                too_broad=too_broad,
            )
        )

    # --- the subtopic tags, from the subject terms -------------------------
    for slug, terms in per_subtopic.items():
        confidence = BASE_TERM_CONFIDENCE
        if topic_slug is not None and _SUBTOPIC_PARENT.get(slug) == topic_slug:
            confidence += POLICY_AREA_CORROBORATION
        if len(terms) >= 2:
            # Capped at one bonus however many terms agree: see
            # SECOND_TERM_CORROBORATION on why volume must not buy confidence.
            confidence += SECOND_TERM_CORROBORATION
        tags.append(
            _tag(
                bill_id=bill_id,
                slug=slug,
                confidence=min(1.0, confidence),
                matched_terms=tuple(sorted(terms.values())),
                is_subtopic=True,
                accept_threshold=accept_threshold,
                too_broad=too_broad,
            )
        )

    # Stable order so a run over the same bill produces the same report, for
    # the same reproducibility reason as `app.scoring.formula._canonical`.
    return sorted(tags, key=lambda tag: tag.topic_id)


def _tag(
    *,
    bill_id: str,
    slug: str,
    confidence: float,
    matched_terms: tuple[str, ...],
    is_subtopic: bool,
    accept_threshold: float | None,
    too_broad: bool,
) -> TopicTag:
    """One tag, with the accept/hold decision applied.

    Three rules, in this order, and the order is the point:

    1. **Breadth first**, overriding confidence entirely: an appropriations
       bill's `farm-subsidies-and-crop-insurance` tag can be corroborated twice
       over and still be worthless, because the bill is about everything.
       prd.md's omnibus rule is about the measure, not the tag. Checked first so
       that a reviewer looking at an omnibus bill is told it is an omnibus,
       which is the actionable fact, rather than told its tag was thin.
    2. **The measured-precision gate**, when `accept_threshold is None` — the
       production default today. Nothing is accepted, whatever its confidence,
       because 54.2% measured precision does not earn an automatic write. See
       `ACCEPT_THRESHOLD`.
    3. **The confidence threshold**, if a float is supplied.
    """
    if too_broad:
        reason: QaReason | None = QaReason.BREADTH
    elif accept_threshold is None:
        reason = QaReason.BELOW_MEASURED_PRECISION
    elif confidence < accept_threshold:
        reason = QaReason.UNCORROBORATED
    else:
        reason = None
    return TopicTag(
        bill_id=bill_id,
        topic_id=topic_id(slug),
        slug=slug,
        confidence=confidence,
        matched_terms=matched_terms,
        is_subtopic=is_subtopic,
        qa_reason=reason,
    )


# ---------------------------------------------------------------------------
# advances / opposes: the direction half
# ---------------------------------------------------------------------------


def stance_edges_for_tag(tag: TopicTag, stances: Iterable[Stance]) -> list[tuple[Stance, str]]:
    """The `(stance, effect)` pairs one accepted tag supports.

    `effect` is `"advances"` or `"opposes"` -- the two predicates
    datamodel.md's table gives `bill -> stance`, and the two values
    `EvidenceGraphWriter.write_bill_stance_evidence` accepts.

    Returns an EMPTY list when:

      * the tag is not accepted (a held tag writes no edge -- a QA-routed claim
        must not reach the evidence graph before a human has seen it); or
      * the tag's direction is `UNKNOWN`, which is every tag v0 produces. See
        the module docstring: CRS terms carry no direction, and a guessed
        `advances` edge is an uncitable claim in the evidence graph, which is
        the one thing prd.md's "evidence over opinion" principle forbids; or
      * a stance's `direction_code` is `0` -- "keep current policy". A bill that
        changes policy is not evidence for or against a voter who asked for no
        change in either direction.

    The projection is deliberately the same arithmetic as
    `app.scoring.positions._stance_direction`: the sign of the stance's own
    `direction_code` against the sign of the bill's movement. So two mirrored
    stances under one topic get exactly opposite effects from one bill, and no
    direction of any axis is advantaged. The magnitude of `direction_code`
    (±1 vs ±2) is NOT used: an effect is a direction, not a strength.
    """
    if not tag.accepted or tag.direction is BillDirection.UNKNOWN:
        return []

    bill_sign = 1 if tag.direction is BillDirection.EXPANDS else -1
    pairs: list[tuple[Stance, str]] = []
    for stance in stances:
        if stance.topic_id != tag.topic_id:
            continue
        stance_sign = _sign(int(stance.direction_code))
        if stance_sign == 0:
            continue
        pairs.append((stance, "advances" if bill_sign == stance_sign else "opposes"))
    return sorted(pairs, key=lambda pair: pair[0].id or "")


def _sign(value: int) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


# ---------------------------------------------------------------------------
# The run: read bills, write topic_ids, propagate, emit
# ---------------------------------------------------------------------------


class BillTopicClassifier:
    """Classifies stored bills and writes what it accepts.

    Holds no per-request state; safe to share like every other service here.
    """

    def __init__(
        self,
        client: ZeroDBClient,
        *,
        graph: EvidenceGraphWriter | None = None,
    ) -> None:
        self._client = client
        self._graph = graph
        #: topic id -> whether a stored `issue_topics` row exists. Cached across
        #: one classifier's life: the taxonomy is permanent reference data
        #: (`app.seed_taxonomy`), so a resolution cannot go stale within a run,
        #: and a 200-bill run would otherwise re-read the same ~20 topics 200
        #: times against a project observed to throttle at 60 requests/minute.
        self._topic_exists: dict[str, bool] = {}

    # --- the main entry point ---------------------------------------------

    def run(
        self,
        *,
        bills: Sequence[Bill] | None = None,
        write: bool = True,
    ) -> ClassificationReport:
        """Classify bills, write accepted tags, hold the rest for QA.

        Args:
            bills: which bills to classify. Defaults to a page of stored bills.
            write: False to classify and report without writing anything --
                what a dry run and the precision measurement use.

        Returns:
            A `ClassificationReport` whose `bills_changed` counts only bills
            whose `topic_ids` actually changed. Re-running over unchanged bills
            writes nothing: `bills` is an upsert table and a rewrite with
            identical content would churn the provenance log for no new
            information, the same reasoning as
            `PositionComputer._store`'s unchanged check.
        """
        rows = list(bills) if bills is not None else self._stored_bills()
        report = ClassificationReport(bills_seen=len(rows))

        for bill in rows:
            tags = classify_bill(
                bill_id=_require_id(bill),
                policy_area=bill.policy_area,
                subjects=bill.subjects,
            )
            resolvable = [tag for tag in tags if self._resolve_topic(tag.topic_id)]
            accepted = [tag for tag in resolvable if tag.accepted]
            held = [tag for tag in resolvable if not tag.accepted]

            report.accepted.extend(accepted)
            report.qa_queue.extend(held)

            if write and self._write_tags(bill, accepted):
                report.bills_changed += 1
                report.bills_written.append(_require_id(bill))

            if write and held:
                self._emit_qa(bill_id=_require_id(bill), held=held)

        logger.info(
            "bill_topic_classifier: %d bill(s), %d accepted tag(s), %d held for QA, "
            "coverage %.1f%%",
            report.bills_seen,
            len(report.accepted),
            len(report.qa_queue),
            100.0 * report.coverage,
        )
        return report

    # --- writes -----------------------------------------------------------

    def _write_tags(self, bill: Bill, accepted: Sequence[TopicTag]) -> bool:
        """Set `bills.topic_ids` to the accepted tags' topics. True if changed.

        Writes through `update_row` rather than the repository's `upsert`
        deliberately: an upsert would re-record provenance for a row whose
        SOURCE data (its CRS subjects, its content_hash) has not changed at all.
        What changed is this codebase's interpretation of unchanged source data,
        which is not a new sourced record. The `content_hash` is left exactly as
        story 5.3's loader computed it, so a later re-fetch still compares
        correctly against the source.
        """
        wanted = sorted({tag.topic_id for tag in accepted})
        if sorted(bill.topic_ids) == wanted:
            return False
        bill_id = _require_id(bill)
        current = self._client.get_row(Bill.table_name, bill_id)
        if current is None:
            logger.warning("bill %s is not stored; no topic_ids written", bill_id)
            return False
        row = dict(current.row_data)
        row["topic_ids"] = wanted
        self._client.update_row(Bill.table_name, bill_id, row)
        logger.info("bill_topic_classifier: %s topic_ids=%s", bill_id, wanted)
        return True

    def _emit_qa(self, *, bill_id: str, held: Sequence[TopicTag]) -> None:
        """Emit one event per bill carrying every tag held for review.

        One event per BILL, not per tag: a reviewer looks at a bill and decides
        about its tags together (an omnibus bill's 38 held tags are one
        judgment, not 38), and 63 of 889 real measures are omnibus-shaped.

        Public identifiers and CRS terms only. Nothing voter-derived exists in
        this module; see `ClassificationReport`.
        """
        self._client.create_event(
            topic=TAGS_PENDING_QA_TOPIC,
            payload={
                "bill_id": bill_id,
                "classifier_version": CLASSIFIER_VERSION,
                "mapping_version": MAPPING_VERSION,
                "tags": [
                    {
                        "topic_id": tag.topic_id,
                        "confidence": tag.confidence,
                        "qa_reason": tag.qa_reason.value if tag.qa_reason else None,
                        "matched_terms": list(tag.matched_terms),
                    }
                    for tag in held
                ],
            },
        )

    # --- propagation: what actually unblocks story 6.2 --------------------

    def propagate_to_roll_calls(
        self,
        *,
        bill_id: str,
        topic_ids: Sequence[str],
    ) -> tuple[list[str], list[str]]:
        """Copy a bill's topics onto its roll calls and their member votes.

        Returns `(roll_call_ids, member_vote_ids)` actually updated.

        **This is the step story 6.2 reads.** 6.2 matches
        `member_votes.topic_ids[]` against `stances.topic_id`, and
        `member_votes.topic_ids` is datamodel.md's documented denormalised copy
        of the roll call's own -- "so that scoring reads one table". Tagging a
        bill and stopping there would leave 6.2 exactly as blocked as it was.

        Both levels are written because datamodel.md denormalises deliberately
        and the nightly drift check
        (`app.integrity.checks.check_member_votes_drift`) compares them: writing
        the roll call alone would make that check fail by design.
        """
        roll_calls = self._roll_calls_for_bill(bill_id)
        wanted = sorted(set(topic_ids))
        updated_rolls: list[str] = []
        updated_votes: list[str] = []

        for roll_call_id, row_data in roll_calls:
            if sorted(row_data.get("topic_ids") or []) != wanted:
                row = dict(row_data)
                row["topic_ids"] = wanted
                self._client.update_row(RollCall.table_name, roll_call_id, row)
                updated_rolls.append(roll_call_id)
            updated_votes.extend(self._propagate_to_votes(roll_call_id, wanted))

        return updated_rolls, updated_votes

    def _propagate_to_votes(self, roll_call_id: str, topic_ids: Sequence[str]) -> list[str]:
        page = self._client.query_rows(
            MemberVote.table_name, filter={"roll_call_id": roll_call_id}, limit=PAGE_SIZE
        )
        if page.has_more or len(page.rows) >= PAGE_SIZE:
            logger.warning(
                "member_votes returned a full page of %d row(s) for roll call %s; some "
                "votes may not have been tagged",
                len(page.rows),
                roll_call_id,
            )
        written: list[str] = []
        for row in page.rows:
            data = row.row_data
            # Filtered locally too: story 1.4 found live ZeroDB's `/query`
            # filter unreliable, and tagging another roll call's votes with
            # this bill's topics is the worst version of that bug.
            if data.get("roll_call_id") != roll_call_id:
                continue
            vote_id = data.get("id")
            if not isinstance(vote_id, str) or not vote_id:
                continue
            if sorted(data.get("topic_ids") or []) == sorted(topic_ids):
                continue
            updated = dict(data)
            updated["topic_ids"] = sorted(topic_ids)
            self._client.update_row(MemberVote.table_name, vote_id, updated)
            written.append(vote_id)
        return written

    # --- graph edges ------------------------------------------------------

    def write_stance_edges(
        self,
        tags: Sequence[TopicTag],
        stances: Sequence[Stance],
    ) -> int:
        """Write `bill -advances|opposes-> stance` for every tag that supports one.

        Returns the number of edges written -- **zero for every tag v0
        produces**, because v0 knows no direction. That is not a wiring gap:
        the path is complete and exercised by this story's tests against tags
        with a known direction, and the moment a later story sets
        `TopicTag.direction` the same call writes real edges.

        Goes through `EvidenceGraphWriter.write_bill_stance_evidence` (story
        13.1) rather than `graph_create_edge`, so the predicate, the endpoint
        types and the canonical-name rule are all validated before a request is
        spent -- the established pattern, see that module's docstring.
        """
        if self._graph is None:
            raise ValueError(
                "no EvidenceGraphWriter was given; construct the classifier with "
                "graph=EvidenceGraphWriter(client) to write stance edges"
            )
        written = 0
        for tag in tags:
            for stance, effect in stance_edges_for_tag(tag, stances):
                self._graph.write_bill_stance_evidence(
                    bill_id=tag.bill_id,
                    stance_id=_require_stance_id(stance),
                    effect=effect,
                    confidence=tag.confidence,
                    method=tag.method,
                )
                written += 1
        return written

    # --- reads ------------------------------------------------------------

    def _stored_bills(self) -> list[Bill]:
        page = self._client.query_rows(Bill.table_name, limit=PAGE_SIZE)
        if page.has_more or len(page.rows) >= PAGE_SIZE:
            logger.warning(
                "bills returned a full page of %d row(s); some bills may not have been "
                "classified this run",
                len(page.rows),
            )
        return [Bill.model_validate(row.row_data) for row in page.rows]

    def _roll_calls_for_bill(self, bill_id: str) -> list[tuple[str, dict[str, object]]]:
        page = self._client.query_rows(
            RollCall.table_name, filter={"bill_id": bill_id}, limit=PAGE_SIZE
        )
        out: list[tuple[str, dict[str, object]]] = []
        for row in page.rows:
            data = row.row_data
            if data.get("bill_id") != bill_id:
                continue
            roll_call_id = data.get("id")
            if isinstance(roll_call_id, str) and roll_call_id:
                out.append((roll_call_id, data))
        return out

    def _resolve_topic(self, topic_row_id: str) -> bool:
        """Whether a stored `issue_topics` row exists for this tag's topic.

        A tag naming an unstored topic is refused rather than written, for the
        same reason `app.ingest.house_bills.resolve_roll_call_bill_id` refuses
        an unverified `bill_id`: datamodel.md rule 5 has no foreign keys, so an
        unresolvable reference is a permanent, silent hole in the evidence
        graph. Logged at warning, because it means the taxonomy was not seeded
        or this module's mapping table names a slug the tree does not have --
        both of which a developer needs to see.
        """
        cached = self._topic_exists.get(topic_row_id)
        if cached is not None:
            return cached
        exists = self._client.get_row(IssueTopic.table_name, topic_row_id) is not None
        if not exists:
            logger.warning(
                "topic %s is not a stored issue_topics row; no tag written for it. "
                "Seed the taxonomy (scripts/seed_taxonomy.py) or fix the mapping table.",
                topic_row_id,
            )
        self._topic_exists[topic_row_id] = exists
        return exists


def _require_id(bill: Bill) -> str:
    if not bill.id:
        raise ValueError("a bill must carry its id to be classified")
    return bill.id


def _require_stance_id(stance: Stance) -> str:
    if not stance.id:
        raise ValueError("a stance must carry its id to take an edge")
    return stance.id


__all__ = [
    "ACCEPT_THRESHOLD",
    "BASE_TERM_CONFIDENCE",
    "CLASSIFIER_VERSION",
    "DIRECTION_UNKNOWN",
    "DOCUMENTED_ACCEPT_THRESHOLD",
    "MAX_SUBTOPIC_SPREAD",
    "MEASURED_PRECISION_PERCENT",
    "POLICY_AREA_CORROBORATION",
    "REQUIRED_PRECISION_PERCENT",
    "SECOND_TERM_CORROBORATION",
    "TAGS_PENDING_QA_TOPIC",
    "BillDirection",
    "BillTopicClassifier",
    "ClassificationReport",
    "QaReason",
    "TopicTag",
    "classify_bill",
    "stance_edges_for_tag",
]
