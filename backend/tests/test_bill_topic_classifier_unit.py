"""Unit tests for the bill-to-stance classifier (story 5.5).

Three kinds of test live here, and the distinction matters:

  1. **The precision measurement** against the real labeled set at
     tests/fixtures/bill_topic_labeled.json. Story 5.5's stated acceptance
     criterion is "at least 85% precision on a 200-bill labeled set", and the
     real measured figure is **54.2%** -- the bar is NOT met. These tests
     therefore pin the measurement and the failure rather than the bar:
     `test_the_measured_precision_is_below_the_bar_and_recorded_as_such`
     explains why that is the right alarm, and
     `test_tightening_the_rule_does_not_reach_the_bar_either` pins the
     load-bearing negative result (no threshold fixes it).
  2. **Integrity tests on the mapping table** -- that every mapped slug is a
     real taxonomy subtopic, that every mapped CRS term was actually OBSERVED
     in real congress.gov data rather than invented, that every CRS policy area
     is a recorded decision. These exist because the first version of the
     mapping table written for this story contained a term
     ("Broadband and internet access") that does not exist in CRS's
     vocabulary at all; a plausible-looking invented key is silently dead code
     that inflates a reviewer's confidence in the table's coverage, and nothing
     but a check against real data catches it. Eighteen such keys were caught
     and corrected this way.
  3. **Rule tests** pinning each confidence rule, the breadth rule, the QA
     routing and the advances/opposes symmetry in isolation. Note that these
     pass `accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD` explicitly: production
     accepts nothing (`ACCEPT_THRESHOLD is None`), so a test of what the
     threshold DOES must supply one. That split is deliberate -- it keeps the
     confidence rules tested and ready for the day a better signal earns them
     the right to gate a write, without pretending they gate one today.

No network, no credentials: both fixtures were captured live during this story
and are fixtures from here on (CLAUDE.md -- CI must not depend on
api.congress.gov being reachable).
"""

from __future__ import annotations

import collections
import json
from pathlib import Path
from typing import Any

import pytest

from app.ingest.bill_topic_classifier import (
    ACCEPT_THRESHOLD,
    BASE_TERM_CONFIDENCE,
    CLASSIFIER_VERSION,
    DOCUMENTED_ACCEPT_THRESHOLD,
    MAX_SUBTOPIC_SPREAD,
    MEASURED_PRECISION_PERCENT,
    POLICY_AREA_CORROBORATION,
    REQUIRED_PRECISION_PERCENT,
    SECOND_TERM_CORROBORATION,
    BillDirection,
    QaReason,
    TopicTag,
    classify_bill,
    stance_edges_for_tag,
)
from app.ingest.bill_topic_mapping import (
    CRS_POLICY_AREAS,
    MAPPING_VERSION,
    POLICY_AREA_TO_TOPIC,
    SPANNING_TERMS_LEFT_OUT,
    SUBJECT_TERM_TO_SUBTOPIC,
    all_mapped_subtopic_slugs,
    normalise_term,
    topic_id,
)
from app.repositories.models import Stance
from app.seed_taxonomy import TOPICS

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LABELED_PATH = FIXTURES / "bill_topic_labeled.json"
VOCAB_PATH = FIXTURES / "crs_subject_vocabulary.json"

#: The acceptance criterion's own numbers, as constants so a drifting test
#: cannot quietly lower the bar.
REQUIRED_LABELED_BILLS = 200


def _labeled() -> dict[str, Any]:
    return json.loads(LABELED_PATH.read_text())


def _observed_vocabulary() -> set[str]:
    """Every CRS legislative subject term observed live, normalised."""
    raw = json.loads(VOCAB_PATH.read_text())
    return {normalise_term(term) for term in raw["terms"]}


def _taxonomy_slugs() -> tuple[set[str], set[str]]:
    tops = {top.slug for top in TOPICS}
    subs = {sub.slug for top in TOPICS for sub in top.subtopics}
    return tops, subs


# =========================================================================
# 1. The acceptance criterion
# =========================================================================


def test_the_labeled_set_holds_at_least_200_real_bills() -> None:
    """backlog.md 5.5 says "a 200-bill labeled set", so 200 is the floor.

    A precision claim over 40 bills is a claim about 0.4 of a bill at the 85%
    boundary. Same reasoning as story 8.3's own
    `test_the_labeled_sample_is_large_enough_for_the_claim`.
    """
    bills = _labeled()["bills"]
    assert len(bills) >= REQUIRED_LABELED_BILLS, (
        f"the labeled set holds {len(bills)} bills; story 5.5's acceptance "
        f"criterion names {REQUIRED_LABELED_BILLS}"
    )


def test_every_labeled_bill_is_real_congress_gov_data() -> None:
    """No synthetic bill may enter the set that measures the criterion.

    A fabricated bill whose CRS terms were chosen to match the mapping table
    would produce a real-looking precision figure for a classifier nobody
    measured, which is the single most misleading thing this story could do.
    Each record therefore carries the congress, measure type and number it was
    fetched by, and this asserts they are shaped like real ones.
    """
    for bill in _labeled()["bills"]:
        assert bill["congress"] in (117, 118, 119), bill
        assert bill["type"].lower() in {
            "hr",
            "s",
            "hres",
            "sres",
            "hjres",
            "sjres",
            "hconres",
            "sconres",
        }, bill
        assert str(bill["number"]).isdigit(), bill
        assert bill["title"], bill
        # Every measure in the set reached a recorded House vote, which is the
        # population this story exists to serve (see the classifier's docstring
        # on why that population is the fully-CRS-indexed one).
        assert bill["roll_calls"], bill


def test_every_labeled_bill_carries_its_own_crs_classification() -> None:
    """A bill with neither a policy area nor subject terms measures nothing."""
    for bill in _labeled()["bills"]:
        assert bill["policy_area"] or bill["subjects"], bill


def test_the_labeled_set_spans_the_taxonomy_rather_than_one_corner() -> None:
    """A set drawn only from health bills would measure one row of the table.

    The set is stratified by CRS policy area (see the fixture's `_comment`), so
    this asserts the stratification actually reached a wide spread of the
    taxonomy's top-level topics.
    """
    reached = set()
    for bill in _labeled()["bills"]:
        for label in bill["labels"]:
            reached.add(label["slug"])
    tops, subs = _taxonomy_slugs()
    top_level_reached = reached & tops
    assert len(top_level_reached) >= 12, (
        f"the labeled set's true labels reach only {len(top_level_reached)} of the "
        f"taxonomy's {len(tops)} top-level topics: {sorted(top_level_reached)}"
    )


def test_the_measured_precision_is_below_the_bar_and_recorded_as_such() -> None:
    """**Story 5.5's acceptance criterion, and the honest result: NOT met.**

    Measured over the proposed tags, this classifier scores 54.2% where the
    criterion asks for 85%.

    This test asserts the FAILURE rather than the bar, which needs justifying.
    Asserting `precision >= 85` would be a red build on every run and tell a
    reader nothing they cannot read in the docstring; deleting the measurement
    would hide the only number that matters. So what is pinned instead is the
    measurement itself, to a tolerance:

      * it still comes out where the module SAYS it comes out
        (`MEASURED_PRECISION_PERCENT`), so a mapping-table edit that changes the
        real number cannot leave a stale figure in the docstring; and
      * it is still below `REQUIRED_PRECISION_PERCENT`, so the day a change
        actually reaches the bar, THIS TEST FAILS and forces whoever did it to
        turn `ACCEPT_THRESHOLD` on and re-read the docstring.

    A failing test is the right alarm for "the bar is now met", because that is
    a change in what this module is allowed to do.
    """
    measured = _measure()
    assert measured["proposed"] > 0
    precision = 100.0 * measured["correct"] / measured["proposed"]

    assert precision == pytest.approx(MEASURED_PRECISION_PERCENT, abs=0.1), (
        f"measured precision is now {precision:.1f}%, but the module docstring and "
        f"MEASURED_PRECISION_PERCENT say {MEASURED_PRECISION_PERCENT}%. Update both, "
        f"and the labeled fixture's `measured` block, so the claim matches the data."
    )
    assert precision < REQUIRED_PRECISION_PERCENT, (
        f"precision is now {precision:.1f}%, at or above story 5.5's "
        f"{REQUIRED_PRECISION_PERCENT}% bar. This is GOOD NEWS and this test is the "
        f"alarm for it: set ACCEPT_THRESHOLD to DOCUMENTED_ACCEPT_THRESHOLD, update "
        f"the module docstring's precision section, and replace this assertion with "
        f"the real `precision >= 85` check."
    )


def test_the_fixture_records_the_same_precision_the_module_claims() -> None:
    """The fixture, the module constant and the real computation must agree.

    Three places state this number and all three are checked against each other,
    because a precision figure that lives only in prose is a figure that silently
    rots.
    """
    recorded = _labeled()["measured"]
    assert recorded["precision_percent"] == pytest.approx(MEASURED_PRECISION_PERCENT, abs=0.1)
    measured = _measure()
    assert recorded["proposed_tags"] == measured["proposed"]
    assert recorded["correct"] == measured["correct"]


def test_no_tag_is_accepted_while_the_bar_is_unmet() -> None:
    """The consequence of the measurement, enforced in code.

    `ACCEPT_THRESHOLD is None`, so every tag over the whole real labeled set is
    routed to QA. This is the test that makes "we do not stand behind these
    tags" a property of the system rather than a sentence in a docstring.
    """
    assert ACCEPT_THRESHOLD is None
    for bill in _labeled()["bills"]:
        tags = classify_bill(
            bill_id=bill["bill_id"],
            policy_area=bill["policy_area"],
            subjects=bill["subjects"],
        )
        accepted = [tag for tag in tags if tag.accepted]
        assert not accepted, (
            f"{bill['bill_id']} accepted {[t.slug for t in accepted]} while the "
            f"classifier's measured precision is below story 5.5's bar"
        )


def test_tightening_the_rule_does_not_reach_the_bar_either() -> None:
    """**The load-bearing negative result**, pinned so it cannot be forgotten.

    The obvious response to 54% precision is "raise the threshold". This test
    measures what that actually buys on the real labeled set, and the answer is
    nothing: requiring BOTH corroborations (confidence 1.0, the strictest
    threshold the confidence model can express) leaves precision essentially
    unchanged while cutting the tags by an order of magnitude.

    That is the evidence that the confidence signal carries no information about
    correctness -- which is why the fix is a better INPUT (the bill's summary
    text), not a better threshold. See the module docstring's nine-variant table.
    """
    lenient = _measure(accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD)
    strictest = _measure(accept_threshold=1.0)

    lenient_precision = 100.0 * lenient["correct"] / lenient["proposed"]
    strict_precision = 100.0 * strictest["correct"] / strictest["proposed"]

    # Tightening to the maximum cuts volume hard...
    assert strictest["proposed"] < lenient["proposed"] / 5
    # ...and still does not reach the bar.
    assert strict_precision < REQUIRED_PRECISION_PERCENT, (
        f"requiring both corroborations now reaches {strict_precision:.1f}%, above the "
        f"{REQUIRED_PRECISION_PERCENT}% bar. The negative result in the module "
        f"docstring is stale -- re-measure the variant table and update it."
    )
    # And it is not even much better than accepting everything, which is the
    # actual finding: the confidence model does not discriminate.
    assert strict_precision < lenient_precision + 20.0, (
        f"strictest {strict_precision:.1f}% vs lenient {lenient_precision:.1f}%: the "
        f"confidence model has become genuinely discriminative. Re-measure the "
        f"variant table -- a threshold may now be worth having."
    )


def test_coverage_would_be_real_if_the_bar_were_met() -> None:
    """What the pipeline would deliver the day a better signal reaches the bar.

    Measured with the threshold forced off, so it describes the candidate
    generator rather than today's accept-nothing behaviour. Asserted because a
    classifier that proposed tags for three bills would make the whole question
    academic, and that degeneration would otherwise be invisible while
    everything routes to QA.
    """
    measured = _measure(accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD)
    bills = _labeled()["bills"]
    coverage = 100.0 * len(measured["bill_ids"]) / len(bills)
    assert coverage >= 40.0, (
        f"coverage {coverage:.1f}% -- only {len(measured['bill_ids'])} of {len(bills)} "
        f"labeled bills get any candidate tag at all"
    )


def test_the_documented_threshold_is_one_corroboration() -> None:
    """`DOCUMENTED_ACCEPT_THRESHOLD` is the number to use if the bar is met.

    Pinned so that the three confidence constants and the documented threshold
    cannot drift apart while the threshold is switched off and therefore
    untested by anything else.
    """
    assert (
        pytest.approx(BASE_TERM_CONFIDENCE + POLICY_AREA_CORROBORATION)
        == DOCUMENTED_ACCEPT_THRESHOLD
    )
    assert (
        pytest.approx(BASE_TERM_CONFIDENCE + SECOND_TERM_CORROBORATION)
        == DOCUMENTED_ACCEPT_THRESHOLD
    )
    assert BASE_TERM_CONFIDENCE < DOCUMENTED_ACCEPT_THRESHOLD


def _measure(*, accept_threshold: float | None = None) -> dict[str, Any]:
    """Run the classifier over the labeled set and tally against the labels.

    One helper, several callers, so every figure quoted anywhere in this file is
    computed from exactly the same run rather than from near-identical loops that
    could drift apart.

    Args:
        accept_threshold: which tags to score, as a confidence threshold handed
            to the classifier. Defaults to `DOCUMENTED_ACCEPT_THRESHOLD` -- the
            threshold the confidence model WOULD use if it were allowed to gate
            writes -- because that is the set of tags the labeled set labels and
            therefore the only set its precision figure describes. A different
            float probes what a tighter or looser rule would buy.

    **Why the denominator is the documented-threshold set and not every tag the
    mapping table touches.** The classifier proposes 627 raw tags over these
    200 bills, of which 239 are held by the breadth rule (every one of them on
    an appropriations, continuing-resolution, NDAA or reconciliation measure --
    the rule that demonstrably works). Scoring those 239 as false positives
    would measure a classifier nobody proposes shipping and would flatter
    nothing: it would make the number LOOK worse while telling a reader less,
    because the question story 5.5 asks is how good the tags are that the
    confidence model stands behind. So the figure is computed over the 225 tags
    one corroboration would accept, which is the strongest honest reading of
    the method, and it is still only 54.2%.
    """
    if accept_threshold is None:
        accept_threshold = DOCUMENTED_ACCEPT_THRESHOLD
    out: dict[str, Any] = {
        "proposed": 0,
        "correct": 0,
        "false_examples": [],
        "bill_ids": set(),
        "truth": 0,
    }
    for bill in _labeled()["bills"]:
        labels = {label["slug"]: label["correct"] for label in bill["labels"]}
        out["truth"] += sum(1 for correct in labels.values() if correct)
        tags = classify_bill(
            bill_id=bill["bill_id"],
            policy_area=bill["policy_area"],
            subjects=bill["subjects"],
            accept_threshold=accept_threshold,
        )
        for tag in tags:
            if not tag.accepted:
                continue
            if tag.slug not in labels:
                # A tag the labeled set does not judge. Cannot happen while the
                # fixture is generated from this same classifier, and is checked
                # rather than assumed: a mapping-table edit that produces a NEW
                # tag would otherwise be silently scored as correct.
                raise AssertionError(
                    f"{bill['bill_id']} -> {tag.slug} is not labeled. The mapping "
                    f"table has changed since the labeled set was built; the set "
                    f"must be re-labeled before its precision figure means anything."
                )
            out["proposed"] += 1
            out["bill_ids"].add(bill["bill_id"])
            if labels[tag.slug]:
                out["correct"] += 1
            else:
                out["false_examples"].append(f"{bill['bill_id']} -> {tag.slug}")
    return out


# =========================================================================
# 2. Integrity of the mapping table
# =========================================================================


def test_every_mapped_slug_is_a_real_taxonomy_subtopic() -> None:
    """A typo'd or renamed slug would write an unresolvable `topic_id`."""
    _, subs = _taxonomy_slugs()
    stray = all_mapped_subtopic_slugs() - subs
    assert not stray, f"subject terms map to slugs the taxonomy does not have: {sorted(stray)}"


def test_every_mapped_policy_area_slug_is_a_real_top_level_topic() -> None:
    tops, _ = _taxonomy_slugs()
    stray = {slug for slug in POLICY_AREA_TO_TOPIC.values() if slug is not None} - tops
    assert not stray, f"policy areas map to slugs the taxonomy does not have: {sorted(stray)}"


def test_every_crs_policy_area_is_a_recorded_decision() -> None:
    """The table must cover the whole published policy-area list, Nones included.

    An absent policy area classifies to nothing *by accident*; a policy area
    mapped to None classifies to nothing *on purpose*, and the difference is
    the whole value of listing them. `CRS_POLICY_AREAS` is transcribed
    independently for this comparison, the same device
    `app.evidence_graph.DOCUMENTED_PREDICATE_TABLE` uses.
    """
    assert set(POLICY_AREA_TO_TOPIC) == set(CRS_POLICY_AREAS), {
        "missing_from_table": sorted(set(CRS_POLICY_AREAS) - set(POLICY_AREA_TO_TOPIC)),
        "not_a_crs_policy_area": sorted(set(POLICY_AREA_TO_TOPIC) - set(CRS_POLICY_AREAS)),
    }


def test_every_mapped_crs_term_was_observed_in_real_data() -> None:
    """**No invented keys.** Every term must exist in CRS's real vocabulary.

    This test exists because the first draft of the mapping table written for
    this story contained "Broadband and internet access", which is not a CRS
    term at all -- it reads exactly like one, and it would have sat in the table
    as dead code forever, making the table look like it covered broadband when
    it did not. `crs_subject_vocabulary.json` is the vocabulary observed live
    across ~1,400 real bills of the 117th-119th Congresses, and a key outside
    it is either a typo or an invention. Both are bugs.
    """
    observed = _observed_vocabulary()
    unknown = sorted(
        term for term in SUBJECT_TERM_TO_SUBTOPIC if normalise_term(term) not in observed
    )
    assert not unknown, (
        f"{len(unknown)} mapped term(s) do not appear in the CRS vocabulary observed "
        f"live; each is a typo or an invention: {unknown}"
    )


def test_no_crs_term_maps_to_more_than_one_subtopic() -> None:
    """Rule 2 of the mapping table: a spanning term is dropped, not duplicated.

    The dict's own shape already guarantees one value per key; what this checks
    is the thing a dict cannot -- that two keys normalising to the same term
    have not been given different subtopics.
    """
    by_normalised: dict[str, set[str]] = collections.defaultdict(set)
    for term, slug in SUBJECT_TERM_TO_SUBTOPIC.items():
        by_normalised[normalise_term(term)].add(slug)
    conflicts = {term: slugs for term, slugs in by_normalised.items() if len(slugs) > 1}
    assert not conflicts, f"terms mapped to two subtopics: {conflicts}"


def test_no_spanning_term_is_also_mapped() -> None:
    """A term recorded as deliberately dropped must not also be in the table.

    Keeps `SPANNING_TERMS_LEFT_OUT` honest: adding one of these to the mapping
    table requires first removing it from here, and therefore reading the reason
    it was dropped.
    """
    both = set(SPANNING_TERMS_LEFT_OUT) & set(SUBJECT_TERM_TO_SUBTOPIC)
    assert not both, f"terms recorded as deliberately unmapped are also mapped: {sorted(both)}"


def test_the_mapping_table_reaches_most_of_the_taxonomy() -> None:
    """A table covering three subtopics would pass every other test here."""
    _, subs = _taxonomy_slugs()
    covered = all_mapped_subtopic_slugs()
    assert len(covered) >= 40, (
        f"the table reaches only {len(covered)} of the taxonomy's {len(subs)} subtopics; "
        f"uncovered: {sorted(subs - covered)}"
    )


def test_no_mapping_rule_names_a_party_or_a_candidate() -> None:
    """prd.md: "nonpartisan by construction... no party-specific rules."

    Checked against the mapping table's own text rather than trusted, because
    a party name reaching a classifier's rules is the failure prd.md's first
    principle exists to prevent, and it would be invisible in a diff.
    """
    forbidden = {
        "democrat",
        "democratic",
        "republican",
        "gop",
        "liberal",
        "conservative",
        "progressive",
        "left-wing",
        "right-wing",
    }
    haystack = " ".join(
        [
            *SUBJECT_TERM_TO_SUBTOPIC.keys(),
            *(slug for slug in SUBJECT_TERM_TO_SUBTOPIC.values()),
            *POLICY_AREA_TO_TOPIC.keys(),
            *(slug or "" for slug in POLICY_AREA_TO_TOPIC.values()),
        ]
    ).lower()
    hits = sorted(word for word in forbidden if word in haystack)
    assert not hits, f"the mapping table names a political tribe: {hits}"


# =========================================================================
# 3. The rules, pinned in isolation
# =========================================================================


def test_a_policy_area_alone_produces_an_accepted_top_level_tag() -> None:
    tags = classify_bill(
        bill_id="us-house:119:hr-1",
        policy_area="Health",
        subjects=["Health"],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    healthcare = [tag for tag in tags if tag.slug == "healthcare"]
    assert len(healthcare) == 1
    assert healthcare[0].accepted
    assert healthcare[0].is_subtopic is False
    assert healthcare[0].topic_id == topic_id("healthcare")


def test_a_subject_term_under_its_own_policy_area_is_accepted() -> None:
    """The corroborated case: policy area and subject term agree."""
    tags = classify_bill(
        bill_id="us-house:119:hr-2",
        policy_area="Health",
        subjects=["Health", "Prescription drugs"],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    drug = _one(tags, "prescription-drug-pricing")
    assert drug.accepted
    assert drug.is_subtopic is True
    assert drug.confidence == pytest.approx(BASE_TERM_CONFIDENCE + POLICY_AREA_CORROBORATION)
    assert drug.matched_terms == ("Prescription drugs",)


def test_a_lone_uncorroborated_term_is_routed_to_qa_not_accepted() -> None:
    """The rule that makes 85% reachable; see the classifier's docstring.

    `Medicare` under the `Taxation` policy area is one librarian-assigned term
    with nothing agreeing -- an incidental mention, which the real labeled set
    showed is usually what that pattern means.
    """
    tags = classify_bill(
        bill_id="us-house:119:hr-3",
        policy_area="Taxation",
        subjects=["Taxation", "Medicare"],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    medicare = _one(tags, "medicare-and-medicaid-financing")
    assert not medicare.accepted
    assert medicare.qa_reason is QaReason.UNCORROBORATED
    assert medicare.confidence == pytest.approx(BASE_TERM_CONFIDENCE)
    assert medicare.confidence < DOCUMENTED_ACCEPT_THRESHOLD


def test_two_agreeing_terms_corroborate_without_the_policy_area() -> None:
    """Two independent librarian-assigned terms are the other way to be sure."""
    tags = classify_bill(
        bill_id="us-house:119:hr-4",
        policy_area="Taxation",
        subjects=["Taxation", "Medicare", "Medicaid"],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    medicare = _one(tags, "medicare-and-medicaid-financing")
    assert medicare.accepted
    assert medicare.confidence == pytest.approx(BASE_TERM_CONFIDENCE + SECOND_TERM_CORROBORATION)
    assert medicare.matched_terms == ("Medicaid", "Medicare")


def test_a_third_agreeing_term_buys_no_further_confidence() -> None:
    """Volume must not buy confidence; that is the omnibus failure mode."""
    two = classify_bill(
        bill_id="us-house:119:hr-5",
        policy_area="Taxation",
        subjects=["Taxation", "Medicare", "Medicaid"],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    three = classify_bill(
        bill_id="us-house:119:hr-5",
        policy_area="Taxation",
        subjects=[
            "Taxation",
            "Medicare",
            "Medicaid",
            "Health programs administration and funding",
        ],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    assert _one(two, "medicare-and-medicaid-financing").confidence == pytest.approx(
        _one(three, "medicare-and-medicaid-financing").confidence
    )


def test_the_policy_area_is_not_double_counted_as_a_subject_term() -> None:
    """Story 5.3 stores the policy area as `subjects[0]` as well as its own field.

    Counting it twice would make every policy-area match self-corroborating and
    accept every single-term tag -- which would both break the precision bar and
    make the corroboration rule meaningless. Pinned because the bug is invisible
    from the outside: the confidences would simply all read 0.85.
    """
    tags = classify_bill(
        bill_id="us-house:119:hr-6",
        policy_area="Health",
        # "Health" twice: once as the policy area, once at the head of
        # `subjects[]` exactly as `house_bills._subjects_from` writes it.
        subjects=["Health", "Health"],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    # No subtopic tag at all: "Health" is a recorded spanning term, not a
    # subtopic mapping, so nothing but the top-level tag can come out.
    assert [tag.slug for tag in tags] == ["healthcare"]
    assert tags[0].confidence == pytest.approx(BASE_TERM_CONFIDENCE + POLICY_AREA_CORROBORATION)


def test_a_repeated_term_in_two_casings_buys_no_corroboration() -> None:
    tags = classify_bill(
        bill_id="us-house:119:hr-7",
        policy_area="Taxation",
        subjects=["Taxation", "Medicare", "MEDICARE", "medicare"],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    assert _one(tags, "medicare-and-medicaid-financing").confidence == pytest.approx(
        BASE_TERM_CONFIDENCE
    )


def test_an_unmapped_crs_term_contributes_nothing() -> None:
    """Rule 4: an unmapped term is a gap, never a nearest-neighbour guess."""
    tags = classify_bill(
        bill_id="us-house:119:hr-8",
        policy_area=None,
        subjects=["Alaska"],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    assert tags == []


def test_a_bill_with_no_crs_classification_produces_no_tag_and_no_qa_entry() -> None:
    tags = classify_bill(
        bill_id="us-house:119:hr-9",
        policy_area=None,
        subjects=[],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    assert tags == []


def test_a_policy_area_mapped_to_none_produces_no_top_level_tag() -> None:
    """`Economics and Public Finance` is the omnibus policy area; see its note."""
    tags = classify_bill(
        bill_id="us-house:119:hr-10",
        policy_area="Economics and Public Finance",
        subjects=["Economics and Public Finance", "Medicare", "Medicaid"],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    assert not [tag for tag in tags if not tag.is_subtopic]
    # The subtopic tag still stands on its own two agreeing terms.
    assert _one(tags, "medicare-and-medicaid-financing").accepted


# --- the breadth rule ------------------------------------------------------


def test_an_omnibus_bill_has_every_tag_routed_to_qa_with_the_breadth_reason() -> None:
    """prd.md: "a vote on a 1,000-page bill is not a vote for every clause."

    The subject list below reaches more than `MAX_SUBTOPIC_SPREAD` subtopics,
    which measured live is the signature of an appropriations or
    continuing-resolution bill.
    """
    wide = [
        "Medicare",
        "Prescription drug costs",
        "Abortion",
        "Firearms and explosives",
        "Oil and gas",
        "Air quality",
        "Income tax rates",
        "Student aid and college costs",
        "Landlord and tenant",
        "Roads and highways",
        "Wages and earnings",
        "Right of privacy",
    ]
    tags = classify_bill(
        bill_id="us-house:119:hr-11",
        policy_area="Economics and Public Finance",
        subjects=wide,
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    assert len(tags) > MAX_SUBTOPIC_SPREAD
    assert all(not tag.accepted for tag in tags)
    assert {tag.qa_reason for tag in tags} == {QaReason.BREADTH}


def test_breadth_overrides_a_doubly_corroborated_tag() -> None:
    """An omnibus bill's well-corroborated tag is still worthless.

    The breadth rule is about the MEASURE, not the tag: a 249-term
    appropriations bill really does fund farm subsidies, and a vote on it is
    still not evidence about a voter's farm-subsidy stance.
    """
    wide = [
        "Medicare",
        "Medicaid",
        "Abortion",
        "Firearms and explosives",
        "Oil and gas",
        "Air quality",
        "Income tax rates",
        "Student aid and college costs",
        "Landlord and tenant",
        "Roads and highways",
    ]
    tags = classify_bill(
        bill_id="us-house:119:hr-12",
        policy_area="Health",
        subjects=wide,
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    medicare = _one(tags, "medicare-and-medicaid-financing")
    # Fully corroborated by both rules...
    assert medicare.confidence == pytest.approx(1.0)
    # ...and held anyway.
    assert medicare.qa_reason is QaReason.BREADTH


def test_a_bill_exactly_at_the_spread_limit_is_not_held_for_breadth() -> None:
    """The limit is "more than", so the boundary case must pass.

    An off-by-one here would hold every bill at the limit for QA, which is a
    silent recall loss nobody would notice.
    """
    terms = [
        "Medicare",
        "Abortion",
        "Firearms and explosives",
        "Oil and gas",
        "Air quality",
        "Income tax rates",
        "Student aid and college costs",
        "Landlord and tenant",
    ]
    tags = classify_bill(
        bill_id="us-house:119:hr-13",
        policy_area=None,
        subjects=terms,
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    assert len({tag.slug for tag in tags}) == MAX_SUBTOPIC_SPREAD
    assert not [tag for tag in tags if tag.qa_reason is QaReason.BREADTH]


# --- QA routing ------------------------------------------------------------


def test_every_held_tag_names_a_documented_reason_and_carries_its_evidence() -> None:
    """backlog.md 5.5: "low-confidence tags go to QA."

    A queue entry with no reason and no matched term cannot be reviewed -- story
    10.3's reviewer needs the evidence side by side -- so this pins that every
    held tag carries both.
    """
    tags = classify_bill(
        bill_id="us-house:119:hr-14",
        policy_area="Taxation",
        subjects=["Taxation", "Medicare"],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    held = [tag for tag in tags if not tag.accepted]
    assert held
    for tag in held:
        assert tag.qa_reason in set(QaReason)
        assert tag.matched_terms
        assert 0.0 < tag.confidence < 1.0


def test_held_tags_are_returned_rather_than_dropped() -> None:
    """A classifier that dropped them could not satisfy the criterion at all."""
    tags = classify_bill(
        bill_id="us-house:119:hr-15",
        policy_area="Taxation",
        subjects=["Taxation", "Medicare"],
        accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD,
    )
    assert any(not tag.accepted for tag in tags)


def test_production_accepts_nothing_and_says_why() -> None:
    """`ACCEPT_THRESHOLD is None`, and the QA reason names the real cause.

    The reason matters as much as the refusal: a reviewer looking at the queue
    must be able to tell "the METHOD is not trusted yet" from "this particular
    tag is thin", because those call for different review effort.
    `BELOW_MEASURED_PRECISION` is the first; `UNCORROBORATED` is the second.
    """
    assert ACCEPT_THRESHOLD is None
    tags = classify_bill(
        bill_id="us-house:119:hr-200",
        policy_area="Health",
        subjects=["Health", "Medicare", "Medicaid"],
    )
    assert tags
    assert all(not tag.accepted for tag in tags)
    # Fully corroborated (confidence 1.0) and still held, with the method-level
    # reason rather than the tag-level one.
    medicare = _one(tags, "medicare-and-medicaid-financing")
    assert medicare.confidence == pytest.approx(1.0)
    assert medicare.qa_reason is QaReason.BELOW_MEASURED_PRECISION


# =========================================================================
# 4. advances / opposes
# =========================================================================


def _stance(slug: str, direction: str, *, topic_slug: str = "healthcare") -> Stance:
    return Stance(
        id=f"{topic_id(topic_slug)}:{slug}",
        topic_id=topic_id(topic_slug),
        label=f"Synthetic stance {slug}",
        description="A synthetic stance card used only by this test suite.",
        direction_code=direction,
        wording_version=1,
    )


def _tag(direction: BillDirection, *, slug: str = "healthcare") -> TopicTag:
    return TopicTag(
        bill_id="us-house:119:hr-100",
        topic_id=topic_id(slug),
        slug=slug,
        confidence=0.9,
        matched_terms=("Health",),
        is_subtopic=False,
        direction=direction,
    )


def test_v0_writes_no_stance_edge_because_it_knows_no_direction() -> None:
    """**The honest centre of this story's scope.** See the module docstring.

    CRS subject terms carry no direction: `Prescription drug costs` is assigned
    identically to a bill capping insulin prices and to one repealing a cap. So
    every tag v0 produces has `direction=UNKNOWN` and writes no edge, rather
    than guessing a direction and putting an uncitable claim in the evidence
    graph.
    """
    tag = TopicTag(
        bill_id="us-house:119:hr-101",
        topic_id=topic_id("healthcare"),
        slug="healthcare",
        confidence=0.9,
        matched_terms=("Health",),
        is_subtopic=False,
    )
    assert tag.direction is BillDirection.UNKNOWN
    assert stance_edges_for_tag(tag, [_stance("for", "+1")]) == []


def test_a_bill_expanding_a_positively_signed_stance_advances_it() -> None:
    pairs = stance_edges_for_tag(_tag(BillDirection.EXPANDS), [_stance("for", "+1")])
    assert [effect for _, effect in pairs] == ["advances"]


def test_a_bill_expanding_a_negatively_signed_stance_opposes_it() -> None:
    pairs = stance_edges_for_tag(_tag(BillDirection.EXPANDS), [_stance("against", "-1")])
    assert [effect for _, effect in pairs] == ["opposes"]


def test_a_bill_contracting_a_negatively_signed_stance_advances_it() -> None:
    pairs = stance_edges_for_tag(_tag(BillDirection.CONTRACTS), [_stance("against", "-1")])
    assert [effect for _, effect in pairs] == ["advances"]


def test_two_mirrored_stances_get_opposite_effects() -> None:
    """prd.md's "nonpartisan by construction" is exactly this symmetry.

    The same arithmetic `app.scoring.positions._stance_direction` uses, pinned
    the same way `test_two_mirrored_stances_get_opposite_positions` pins it
    there: no direction of any axis may be advantaged by the rule.
    """
    stances = [_stance("for", "+1"), _stance("against", "-1")]
    effects = {
        stance.id: effect
        for stance, effect in stance_edges_for_tag(_tag(BillDirection.EXPANDS), stances)
    }
    assert set(effects.values()) == {"advances", "opposes"}
    mirrored = {
        stance.id: effect
        for stance, effect in stance_edges_for_tag(_tag(BillDirection.CONTRACTS), stances)
    }
    for stance_id, effect in effects.items():
        assert mirrored[stance_id] != effect, (
            "reversing the bill's direction must reverse every effect, or the rule "
            "advantages one direction of the axis"
        )


def test_the_magnitude_of_direction_code_does_not_change_the_effect() -> None:
    """An effect is a direction, not a strength. ±2 behaves like ±1."""
    strong = stance_edges_for_tag(_tag(BillDirection.EXPANDS), [_stance("far", "+2")])
    weak = stance_edges_for_tag(_tag(BillDirection.EXPANDS), [_stance("near", "+1")])
    assert [effect for _, effect in strong] == [effect for _, effect in weak] == ["advances"]


def test_a_zero_direction_stance_gets_no_edge() -> None:
    """`0` is "keep current policy" -- a real voter position, not a gap.

    A bill that changes policy is not evidence for or against a voter who asked
    for no change in either direction, and inventing a sign for it would be the
    classifier taking a political position. Same decision
    `compute_position` makes for a `0`-coded stance.
    """
    assert stance_edges_for_tag(_tag(BillDirection.EXPANDS), [_stance("status-quo", "0")]) == []


def test_a_held_tag_writes_no_edge_even_with_a_known_direction() -> None:
    """A QA-routed claim must not reach the evidence graph before review."""
    held = TopicTag(
        bill_id="us-house:119:hr-102",
        topic_id=topic_id("healthcare"),
        slug="healthcare",
        confidence=0.5,
        matched_terms=("Health",),
        is_subtopic=False,
        direction=BillDirection.EXPANDS,
        qa_reason=QaReason.UNCORROBORATED,
    )
    assert stance_edges_for_tag(held, [_stance("for", "+1")]) == []


def test_a_stance_under_a_different_topic_gets_no_edge() -> None:
    other = _stance("for", "+1", topic_slug="firearms-policy")
    assert stance_edges_for_tag(_tag(BillDirection.EXPANDS), [other]) == []


def test_a_tag_carries_both_versions_as_its_method() -> None:
    """The graph edge's `method` property has to attribute a precision figure.

    Both versions, because the code and the mapping table change independently
    and a figure measured under one pair is not a figure under another.
    """
    method = _tag(BillDirection.EXPANDS).method
    assert CLASSIFIER_VERSION in method
    assert MAPPING_VERSION in method


def _one(tags: list[TopicTag], slug: str) -> TopicTag:
    matching = [tag for tag in tags if tag.slug == slug]
    assert len(matching) == 1, f"expected exactly one {slug} tag, got {[t.slug for t in tags]}"
    return matching[0]


# =========================================================================
# 5. The stale-row idempotence bug, found by the live run
# =========================================================================


def test_the_unchanged_check_reads_the_stored_row_not_the_argument() -> None:
    """Regression: `_write_tags` compared against a stale in-memory `Bill`.

    Found by `scripts/verify_bill_stance_classifier_live.py` on 2026-10-19, and
    invisible to every test that existed at the time because they all re-read
    the bill between runs.

    The bug: the unchanged-check read `bill.topic_ids` off the `Bill` object the
    caller passed in. That object goes stale the moment the method writes, so a
    caller classifying the SAME in-memory list twice -- the live script, and any
    backfill that loads a page of bills and classifies it -- got
    `bills_changed=1` on both runs and rewrote a byte-identical row. The fix
    reads the stored row, so idempotence is a property of the data rather than
    of how the caller happened to load it.

    Pinned by passing the same `Bill` instance to two runs, which is exactly the
    shape that broke.
    """
    from app.bootstrap import Bootstrapper
    from app.ingest.bill_topic_classifier import BillTopicClassifier
    from app.repositories import ProvenanceContext, Repository
    from app.repositories.models import Bill, IssueTopic
    from app.zerodb import ZeroDBClient
    from app.zerodb.testing import FakeZeroDBTransport

    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(client, pace=lambda: None).run()
    Repository(client=client, model=IssueTopic).upsert(
        {
            "id": topic_id("healthcare"),
            "name": "Healthcare",
            "locale": "en-US",
            "taxonomy_version": 1,
        }
    )
    stored = Repository(
        client=client,
        model=Bill,
        provenance=ProvenanceContext(adapter_version="idempotence-regression/v1"),
    ).upsert(
        {
            "id": "us-house:119:hr-999998",
            "title": "A synthetic bill for the idempotence regression",
            "introduced_on": "2025-01-03",
            "policy_area": "Health",
            "subjects": ["Health"],
            "topic_ids": [],
            "sponsors": [],
            "source_url": "https://example.invalid/idempotence-regression",
            "content_hash": "1" * 64,
        }
    )

    classifier = BillTopicClassifier(client)
    # The SAME instance both times, deliberately: `stored.topic_ids` is still
    # `[]` after the first run writes, which is what made the bug invisible.
    first = classifier.run(bills=[stored], accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD)
    second = classifier.run(bills=[stored], accept_threshold=DOCUMENTED_ACCEPT_THRESHOLD)

    assert first.bills_changed == 1, first
    assert second.bills_changed == 0, (
        "the second run rewrote an identical row: the unchanged-check is reading the "
        "stale argument again, not the stored row"
    )
