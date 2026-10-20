"""Senate vote typing, bundling and no-recorded-vote handling (8.3, 8.4, 8.5).

What this is, and why it is a module rather than three more branches
=====================================================================

Story 8.1 shipped a working Senate roll-call loader with its vote-type
classification written inline as `senate_rollcalls._classify_vote_type` -- a
six-branch function reading one field (`question`) off the parsed XML element.
That was the right shape for 8.1, whose acceptance criterion was about
ingest, provenance and ids. It is the wrong shape for 8.3, whose acceptance
criterion is **"at least 95% accuracy on a labeled sample"**: a number you
can only measure if the thing being measured takes plain values in and
returns a plain decision out, with no XML element, no network and no
repository anywhere near it.

So this module is 8.1's classifier pulled out, given a typed input
(`VoteSignals`), a typed output (`VoteTypeDecision`) that says WHICH RULE
fired, and then extended -- substantially -- to close the real accuracy gap
8.1 left. `senate_rollcalls._classify_vote_type` now delegates here; it is
kept as a thin wrapper so 8.1's own tests and its XML-element call site keep
working unchanged.

The real accuracy gap 8.1 left, measured live
==============================================

The live LIS index carries a `question` for every roll call, so the complete
real vocabulary is knowable rather than guessable. Fetching all four index
documents for congresses 118-119 during this story (2026-10-19) returned
**1,606 roll calls across exactly 34 distinct `question` values**. Running
8.1's `question`-only classifier over that real distribution:

    other               916   (57.4%)
    cloture             529
    final_passage       109
    procedural           35
    motion_to_recommit    7

**57% of every Senate vote in two Congresses landed in `other`.** Since
story 6.1 weights `other` at 0.2 -- the same as `procedural`, and
deliberately so, because "an unclassified vote is not evidence a scorer
should lean on" -- that is 916 real votes contributing a fifth of their
evidentiary weight for no reason other than the classifier not recognising
the question. The story's own So-That ("so that weights apply correctly") was
not true yet.

Where the 916 actually came from, and what each needed:

  * **446 "On the Nomination"** -- stays `other`, deliberately. See the
    nomination note below; this is a vocabulary gap in datamodel.md, not a
    classifier bug, and forcing it into `final_passage` would be a wrong
    answer rather than a missing one.
  * **180 "On the Amendment"** -- already rescued by 8.1's structural rule
    (`amendment_number` is populated on these), so they were only `other` in
    a `question`-only reading. Confirmed live: every sampled "On the
    Amendment" item carries both `amendment_number` and
    `document_type == "S.Amdt."`.
  * **117 "On the Motion to Proceed", 33 "On the Motion to Discharge",
    5 "On the Point of Order", 3 "On the Decision of the Chair", 2 "On the
    Motion to Refer", 2 "On the Motion for Attendance", 1 "On the Motion to
    Adjourn"** -- all parliamentary process, all `procedural`. 8.1's
    `_PROCEDURAL_PHRASES` listed only "motion to table", "motion to
    reconsider" and "previous question" (the last of which, confirmed live,
    the Senate never actually uses -- it is a House motion; 0 occurrences in
    1,606 roll calls, kept only for vocabulary parity with the House
    loader).
  * **105 "On the Motion"** -- the single most interesting bucket, and the
    reason this module reads a second field. The bare `question` is "On the
    Motion" for several completely different motions; only
    `vote_question_text` says which:

        "On the Motion (Motion to Close the Doors)"
        "On the Motion (Motion to Waive All Applicable Budgetary Discipline
         During the Consideration of H.R. 815)"
        "On the Motion (Motion to Concur in the House Amendment to the
         Senate Amendment to H.R. 6644 with an Amendment)"

    The first two are procedural; the third is a substantive legislative
    motion with no slot in datamodel.md's vocabulary. 8.1 read `question`
    alone and could not tell them apart at all.
  * **58 "On the Joint Resolution" / 5 "On the Concurrent Resolution" /
    8 "On the Resolution"** -- these 8.1 already got right (they are in its
    `_FINAL_PASSAGE_QUESTIONS`); listed here only because the earlier
    tabulation makes it look otherwise.
  * **5 "On Overriding the Veto", 1 "On the Conference Report", 1 "On the
    Resolution of Ratification"** -- final dispositions 8.1's exact-match
    list did not include.

Signals this module reads, and why each
========================================

`VoteSignals` is deliberately a flat record of STRINGS, not an XML element:
that is what makes the accuracy number measurable from a fixture, and it is
also what lets a future House-side refactor feed the same classifier from
Congress.gov's JSON without either source importing the other's parser.

  * **question** -- the short field. Still the primary signal; 8.1 was right
    that it is close to a controlled vocabulary.
  * **vote_question_text** -- the long field, which disambiguates "On the
    Motion" (above) and is where en-bloc bundles are visible (below). 8.1's
    docstring explicitly rejected this field as "repeats the document
    identifier redundantly". That was true of the common case and wrong
    about the hard one; reading BOTH, with `question` first, keeps 8.1's
    judgment where it held and fixes it where it did not.
  * **amendment_number** -- the structural amendment signal, unchanged from
    8.1 except that it is now outranked by more rules (see the priority
    order).
  * **document_type** / **document_number** -- `PN` for a nomination,
    `S.Amdt.` for an amendment, a bill prefix otherwise. Used as a
    corroborating signal only, never alone: confirmed live that
    `document_type == "S.Amdt."` appears on motions to table and on
    decisions of the chair too, so it cannot decide anything by itself.
  * **document_title** -- the measure's own title, which is where 8.4's
    omnibus markers live.
  * **vote_result** -- not used for typing at all; carried so 8.5's
    `unrecorded_outcome` can read the same record.

Priority order, and why it is this order
=========================================

Each rule below is checked in order and the first match wins. The ordering is
the whole design: almost every misclassification possible here is two rules
both matching and the wrong one being asked first.

  1. **cloture** -- `question` or `vote_question_text` contains "cloture".
     First, as in 8.1, and for the same reason: a cloture vote names the
     measure it is about (a bill, a nomination, an amendment), so every
     later rule would also match it. Confirmed live that all three shapes
     occur: "On the Cloture Motion" on a nomination (463 times), "On Cloture
     on the Motion to Proceed" on a bill (66), and "On the Cloture Motion"
     with `amendment_number` populated (119-2:240, a real cloture vote on
     S.Amdt. 6776). prd.md's "procedural vote" scenario is precisely this
     case: "B voted Nay on a cloture motion but Yea on final passage of the
     same bill / Then the verdict is 'Kept' with a 'procedural vote' note" --
     which can only work if the two votes on that same bill type
     differently, and only if cloture does not get swallowed by whichever
     rule the bill itself would match.

  2. **procedural** -- a parliamentary-motion phrase appears in either
     question field. **Checked BEFORE `amendment`, which is the one ordering
     change from 8.1 that moves real numbers.** Two real live cases force it:

       * 119-2:17 "On the Motion to Table" with `amendment_number`
         "S.Amdt. 4286". Tabling kills a measure WITHOUT a vote on its
         merits; reading it as the senator's position on that amendment's
         substance inverts the meaning of a Yea.
       * 119-2:159 "On the Motion", `vote_question_text` "(Motion to Waive
         All Applicable Budgetary Discipline Re: Cassidy Amdt. No. 5812)",
         `amendment_number` "S.Amdt. 5812". The vote is on whether to waive
         a budget point of order, not on the amendment.

     8.1's order (amendment before procedural) classifies both of these as
     `amendment` and weights them at 0.7 -- nearly as heavily as final
     passage -- for votes that are not about the policy at all.

  3. **motion_to_recommit** -- checked before the generic procedural catch
     would otherwise claim it, and kept as its own value because
     datamodel.md gives it one and story 6.1 weights it at 0.5, between
     `amendment` and `cloture`. 8.1 documented this branch as never
     triggered by any live sample it fetched; **this story found 7 real
     occurrences** ("On the Motion to Recommit", all in congress 119
     session 1, e.g. 119-1:396/398/403 on H.R. 4), so the branch is no
     longer theoretical. Ordered above `procedural` because "motion to
     recommit" would also match a broad "motion to" procedural test; it
     does not match any phrase in `_PROCEDURAL_PHRASES` as written, but
     relying on that would make the two lists silently coupled.

  4. **amendment** -- `amendment_number` is populated AND the question is
     about adopting the amendment rather than doing something procedural to
     it. The structural signal from 8.1, now with the question-shape
     condition the two cases in rule 2 showed it needs.

  5. **final_passage** -- the question is one of the measure's own final
     dispositions. Exact-match on a normalized question, as 8.1 chose, plus
     the three live shapes 8.1's list was missing (conference report, veto
     override, resolution of ratification). Exact rather than substring so
     that "On the Motion to Proceed to the Resolution" cannot match "on the
     resolution".

  6. **other** -- everything else, including a bare "On the Nomination" and
     a "Motion to Concur". Falling back rather than raising, as 8.1 chose.

Why nominations stay `other`
=============================

446 of 1,606 live Senate roll calls are a bare "On the Nomination" -- the
single largest question value in the chamber, and this classifier
deliberately leaves every one of them `other`, exactly as story 8.1 decided.
Restating the reasoning because it is the most load-bearing judgment call
here and the one most likely to be questioned:

datamodel.md's vocabulary is "`final_passage`, `amendment`, `cloture`,
`motion_to_recommit`, `procedural` or `other`" -- six values, none of which
means "confirmation". A nomination vote is not a vote on legislation; it is
the Senate's advice-and-consent function. Calling it `final_passage` would
make it weigh 1.0 as "the clearest evidence of a member's position on a
bill" (story 6.1's words) on a vote that is not about a bill, and would then
feed the alignment formula as if it were. Calling it `procedural` would be
worse: a confirmation vote is a substantive, consequential act.

`other` at 0.2 is the honest answer: the vote is shown (prd.md requires
votes be shown, not excluded) but a scorer does not lean on it. **The real
fix is a seventh `vote_type` value, `confirmation`, in datamodel.md and in
story 6.1's published `vote_types` weights** -- a data-model and
published-methodology change, which is not 8.3's to make unilaterally. It is
recorded as an open question on this story's PR.

8.4, the bundled (omnibus) flag
================================

Backlog 8.4: "Bills over a configurable size or with omnibus markers get
`is_bundled`; the UI explains it." Story 6.2 already CONSUMES the flag --
`scoring/positions.BUNDLED_WEIGHT_FACTOR` is 0.4, and that module's own note
says the constant was "left as a constant because 6.1 assigned it to this
story". So the scoring side is done and this story's job is strictly to set
the flag correctly at ingest time, with a documented rule and a stored reason
the UI can render. Nothing here changes how scoring uses it.

Two rules, checked in this order (so `bundled_reason` is deterministic -- it
is a stored, rendered field, and two runs must not disagree about which
reason to show):

  1. **`omnibus_marker`** -- the measure's own `document_title` contains a
     phrase that names it as an omnibus by construction. Every marker in
     `OMNIBUS_TITLE_MARKERS` was taken from a REAL `document_title` captured
     live during this story, not invented:

       "further consolidated appropriations"  H.R. 7148, 119-2:20
       "continuing appropriations"            H.R. 5371, 119-1:545
       "and related agencies"                 H.R. 4366, 118-2:83
       "for reconciliation pursuant to"       H.R. 1,    119-1:331
       "setting forth the congressional budget"  S.Con.Res. 7, 119-1:87
       "en bloc consideration"                S.Res. 377, 119-1:515

     Title text rather than a page count, because the LIS feed carries no
     bill length and this codebase ingests no Senate bill text (story 5.3 is
     House-only), so "bills over a configurable size" has no size to read
     yet. A title marker is the signal that IS available, and it is the more
     precise one anyway: a long single-subject authorization bill is not an
     omnibus, and a short continuing resolution is.

  2. **`measure_count_exceeds_threshold`** -- `vote_question_text` names
     more distinct measure identifiers than
     `DEFAULT_BUNDLED_MEASURE_THRESHOLD`. This is "over a configurable size"
     in the one unit the live feed actually gives: how many separate
     measures this single vote disposed of. The real case that motivated it
     is 119-1:524, whose `vote_question_text` lists **48 separate
     nominations** joined by " and " -- one vote, 48 measures, which is
     exactly prd.md's "a vote on a 1,000-page bill is not a vote for every
     clause" in its Senate form.

`count_measures` counts DISTINCT identifiers and is deliberately careful not
to read an amendment chain as a bundle: "S.Amdt. 5808 to S.Amdt. 5453 to
S. 2" names three identifiers but is one amendment's position in a chain, so
"to"-joined chains collapse to their first identifier.

The threshold is a parameter on every entry point (not a module-level
constant read directly), so a caller -- or a future `methodology_versions`
weight group -- can change it without editing this module. Its default is 3:
above 1 (or every ordinary single-measure vote would be flagged), above 2 (a
bill plus the amendment it amends is routinely two identifiers), and low
enough that a genuine en-bloc batch trips it.

`BUNDLED_REASON_EXPLANATIONS` is the "the UI explains it" half of the
acceptance criterion: a reason code with no renderable text is a flag the UI
cannot explain, so the mapping is total over `BundledReason` and a test pins
that it stays total.

8.5, no recorded vote
======================

prd.md, twice:

  * "Only recorded (roll-call) votes are scored. Voice votes and unanimous
    consent are shown as 'no recorded vote', never as support."
  * Scenario "No recorded vote": "Given a promise maps only to a bill passed
    by voice vote / Then the verdict is 'No opportunity' and the UI says 'no
    recorded vote'".

The structural point, and the reason this is only 2 points of work: **a voice
vote produces no roll call at all.** It has no roll number, so the LIS index
never lists it and `fetch_vote_numbers` can never return it. There is no live
record for this module to misclassify. What 8.5 must guarantee is therefore
negative and defensive:

  1. `unrecorded_outcome(text)` recognises a voice-vote or unanimous-consent
     disposition when some OTHER source hands one over -- a bill's action
     text from Congress.gov (story 5.3's `house_bills` reads exactly such
     text), a promise mapping, a correction submission. It returns the
     message prd.md quotes, the verdict prd.md names, and
     `scorable = False`.
  2. `guard_recorded_vote(text)` raises `SkippedUnrecordedOutcome` rather
     than letting such a disposition be minted into a `roll_calls` row.
     A `roll_calls` row for a voice vote would be the exact failure prd.md
     forbids: every senator would either appear with a position they never
     cast, or appear as `not_voting`, which a scorer reads as an abstention
     on a real vote rather than as "no vote happened".
  3. The line this module must NOT blur: an individual senator's `not_voting`
     or "Absent" on a REAL roll call is a position on a vote that WAS
     recorded. Treating that as "no recorded vote" would erase the roll call
     for the other 99. Pinned by test, and `unrecorded_outcome("Not Voting")`
     returns None on purpose.

All 75 real `vote_result` values in the labeled sample are asserted to come
back `None` from `unrecorded_outcome`, which is the false-positive guard that
matters: a false positive here deletes a real vote from the record.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Final

#: Exactly the string prd.md's "No recorded vote" scenario quotes: "the UI
#: says 'no recorded vote'". Not title-cased, not punctuated -- the scenario
#: is the executable definition of done (CLAUDE.md), so the wording is
#: pinned by test rather than left to a renderer's judgment.
NO_RECORDED_VOTE_MESSAGE: Final = "no recorded vote"

#: prd.md's "the verdict is 'No opportunity'", in the spelling
#: `repositories.models.VerdictValue` actually stores
#: ("kept, partial, broken, no_opportunity, absent, not_testable").
NO_RECORDED_VOTE_VERDICT: Final = "no_opportunity"

#: How many distinct measures one vote may dispose of before it is bundled.
#: See the module docstring's 8.4 section for why 3 and not 1 or 2. A
#: parameter on every entry point, so this is a default rather than a rule.
DEFAULT_BUNDLED_MEASURE_THRESHOLD: Final = 3


class BundledReason(enum.Enum):
    """Why a roll call was flagged `is_bundled`.

    Stored on the row and rendered by the UI (8.4: "the UI explains it"), so
    these are a stable vocabulary rather than free text --
    `BUNDLED_REASON_EXPLANATIONS` is the renderable half, and a test pins
    that every member of this enum has one.
    """

    #: The measure's own title names it as an omnibus by construction.
    OMNIBUS_MARKER = "omnibus_marker"
    #: This single vote disposed of more separate measures than the
    #: configured threshold (the live en-bloc case).
    MEASURE_COUNT_EXCEEDS_THRESHOLD = "measure_count_exceeds_threshold"


#: Plain-language text for each reason code, for the UI to render beside the
#: flag. 8.4's acceptance criterion is "the UI explains it", so a reason with
#: no text here is an unexplainable flag; the mapping is total over
#: `BundledReason` and `test_the_explanation_map_has_no_reason_the_enum_does_
#: not_define` keeps it that way.
#:
#: Written for a voter, not for an engineer: prd.md's standard for this kind
#: of text is "a plain-language explainer", and the sentence has to make sense
#: to someone who has never heard the word "omnibus".
BUNDLED_REASON_EXPLANATIONS: Final[dict[str, str]] = {
    BundledReason.OMNIBUS_MARKER.value: (
        "This vote was on a package bill that combines many unrelated measures "
        "into one — an omnibus. A vote for the package is not a vote for every "
        "part of it, so this vote counts for less than a vote on a single-subject bill."
    ),
    BundledReason.MEASURE_COUNT_EXCEEDS_THRESHOLD.value: (
        "The Senate disposed of several separate measures in this one vote, so it "
        "does not show a position on any single one of them. It counts for less "
        "than a vote held on one measure on its own."
    ),
}

#: Phrases in a measure's own `document_title` that name it as an omnibus.
#: EVERY ONE was taken from a real `document_title` captured live from
#: senate.gov during this story -- see the module docstring's 8.4 section for
#: the specific roll call each came from. Lowercase, because matching
#: lowercases the title first (a test pins that these stay lowercase, so a
#: future marker added in title case cannot silently never match).
OMNIBUS_TITLE_MARKERS: Final[tuple[str, ...]] = (
    # Appropriations packages: the canonical omnibus.
    "further consolidated appropriations",
    "consolidated appropriations",
    "continuing appropriations",
    "omnibus",
    # "...for military construction, the Department of Veterans Affairs, and
    # related agencies..." -- an appropriations bill spanning several agencies.
    "and related agencies",
    # Budget reconciliation: one bill carrying every committee's instructions.
    "for reconciliation pursuant to",
    # A budget resolution sets levels across the whole government at once.
    "setting forth the congressional budget",
    # "...authorizing the en bloc consideration ... of certain nominations..."
    "en bloc consideration",
)

#: `question` values (normalized: lowercased, whitespace collapsed) that are
#: a measure's own FINAL disposition. Exact match, not substring -- see the
#: module docstring's rule 5. Story 8.1's six, plus the three live shapes its
#: list was missing (conference report, veto override, treaty ratification).
_FINAL_PASSAGE_QUESTIONS: Final[frozenset[str]] = frozenset(
    {
        "on passage of the bill",
        "on passage of the joint resolution",
        "on passage",
        "on the joint resolution",
        "on the resolution",
        "on the concurrent resolution",
        "on the conference report",
        "on agreeing to the conference report",
        "on overriding the veto",
        "on the resolution of ratification",
    }
)

#: Phrases that make a vote a motion to recommit. Checked before the generic
#: procedural list so the two cannot silently couple -- see rule 3.
_MOTION_TO_RECOMMIT_PHRASES: Final[tuple[str, ...]] = ("motion to recommit",)

#: Phrases that make a vote parliamentary process rather than a vote on a
#: measure's substance. Checked against `question` AND `vote_question_text`,
#: which is what rescues the 105 live "On the Motion" roll calls whose
#: specific motion is named only in the long field.
#:
#: Each entry's live occurrence count across congresses 118-119 (1,606 roll
#: calls, tabulated during this story) is noted, so a reader can see which
#: are load-bearing and which are parity-only:
_PROCEDURAL_PHRASES: Final[tuple[str, ...]] = (
    "motion to table",  # 33 + the "(Motion to Table ...)" long-field forms
    "motion to proceed",  # 117 -- the largest single procedural family
    "motion to discharge",  # 33
    "motion to reconsider",  # 2
    "motion to refer",  # 2
    "motion to adjourn",  # 1 + 6 impeachment-court long-field forms
    "motion to postpone",  # 0 live; in the Senate's documented vocabulary
    "motion to commit",  # 0 live; documented
    "motion for attendance",  # 2
    "motion to close the doors",  # 2, long field only
    "motion to waive",  # budget-point-of-order waivers (long field only)
    "point of order",  # 5 + long-field forms
    "decision of the chair",  # 3
    "previous question",  # 0 live -- a House motion; kept for parity with
    # house_rollcalls' own list, and documented as
    # never-triggered rather than quietly dropped.
    "motion to instruct",  # 0 live; documented
    "motion to suspend the rules",  # 0 live in the Senate; House shape
)

#: Question shapes that mean "adopt this amendment", as opposed to doing
#: something procedural to it. Rule 4 requires one of these AND a populated
#: `amendment_number`; see the module docstring for the two real live cases
#: that showed `amendment_number` alone is not enough.
_AMENDMENT_QUESTION_PHRASES: Final[tuple[str, ...]] = (
    "on the amendment",
    "on the amendments",
    "on agreeing to the amendment",
    "on adopting the amendment",
)

#: Dispositions that mean the chamber acted with NO recorded vote. Matched as
#: substrings of a lowercased, whitespace-collapsed disposition string,
#: because the real sources that carry such text (Congress.gov bill actions,
#: a promise mapping, a correction) phrase it variously: "Passed Senate by
#: Voice Vote", "Agreed to in Senate by Unanimous Consent", bare "UC".
_UNRECORDED_PHRASES: Final[tuple[tuple[str, str], ...]] = (
    ("voice vote", "voice_vote"),
    ("unanimous consent", "unanimous_consent"),
    ("without objection", "unanimous_consent"),
    ("agreed to by uc", "unanimous_consent"),
    ("by uc", "unanimous_consent"),
)

#: A bare "UC" disposition -- matched as a whole token rather than a
#: substring, so that "UCLA" or a bill title containing "uc" cannot trip it.
_BARE_UC = re.compile(r"(?:^|\W)uc(?:$|\W)", re.IGNORECASE)

#: A measure identifier in `vote_question_text`: a nomination (PN1250,
#: PN730-11, PN12-7) or a bill/resolution/amendment designation (S. 316,
#: H.R. 7148, S.J.Res. 44, H.Con.Res. 89, S.Amdt. 5808, Treaty Doc. 112-8).
#: Used by `count_measures` only -- never for typing, where
#: `document_type` is the structural signal.
_MEASURE_IDENTIFIER = re.compile(
    r"""
    (?:
        PN\s?\d+(?:-\d+)?                      # a nomination
      | (?:S|H)\.\s?(?:J|Con|Amdt|Res)?\.?     # a bill/resolution/amendment
        (?:\s?Res)?\.?\s?\d+
      | Treaty\s+Doc\.\s?[\d-]+                # a treaty document
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class VoteSignals:
    """The source fields a classification reads, as plain strings.

    Flat strings rather than an XML element on purpose: that is what makes
    8.3's "95% accuracy on a labeled sample" measurable from a fixture with
    no network, and what would let a House-side caller feed the same
    classifier from Congress.gov's JSON. See the module docstring's "Signals"
    section for what each field is for.

    Frozen, so a decision cannot be attributed to signals that were mutated
    after the fact -- and so `classify` is safely memoizable by a caller.
    """

    question: str = ""
    vote_question_text: str = ""
    vote_result: str = ""
    vote_title: str = ""
    document_type: str = ""
    document_number: str = ""
    document_title: str = ""
    amendment_number: str = ""

    @classmethod
    def from_source_fields(
        cls,
        *,
        question: str | None = "",
        vote_question_text: str | None = "",
        vote_result: str | None = "",
        vote_title: str | None = "",
        document_type: str | None = "",
        document_number: str | None = "",
        document_title: str | None = "",
        amendment_number: str | None = "",
    ) -> VoteSignals:
        """Build from raw source fields, any of which may be absent.

        `None` becomes `""` rather than raising: every one of these fields is
        genuinely optional in the live LIS XML (confirmed -- `<present/>` is
        a self-closing empty element, `document` is absent entirely on some
        impeachment-court votes, `amendment` is empty on most), and a missing
        field must mean "this signal is unavailable", never a crash. Same
        "a gap is a skip, not a crash" posture as every loader here.
        """
        return cls(
            question=_normalize(question),
            vote_question_text=_normalize(vote_question_text),
            vote_result=_normalize(vote_result),
            vote_title=_normalize(vote_title),
            document_type=_normalize(document_type),
            document_number=_normalize(document_number),
            document_title=_normalize(document_title),
            amendment_number=_normalize(amendment_number),
        )

    @property
    def question_text(self) -> str:
        """Both question fields, lowercased, for a phrase test.

        One string rather than two tests at each call site: every procedural
        and cloture phrase must match in EITHER field (the long field is the
        only place the 105 live "On the Motion" roll calls name their actual
        motion), and joining once keeps that from being restated 20 times.
        """
        return f"{self.question}\n{self.vote_question_text}".lower()

    @property
    def normalized_question(self) -> str:
        """`question` lowercased with whitespace collapsed, for an EXACT
        match. The live index's `question` carries trailing newlines and
        padding -- confirmed: `'On the Cloture Motion\\n         '`."""
        return " ".join(self.question.lower().split())


@dataclass(frozen=True)
class VoteTypeDecision:
    """One classification, and the rule that decided it.

    `rule` exists because prd.md's posture is "evidence over opinion": a
    classification nobody can explain is one nobody can correct, and story
    12.x's correction workflow needs to be able to say WHY a vote was typed
    the way it was. It is also how the accuracy test reports a
    misclassification usefully rather than as a bare pair of strings.
    """

    #: One of datamodel.md's six `vote_type` values.
    vote_type: str
    #: Short, stable name of the rule that fired, e.g. "cloture_phrase".
    rule: str
    is_bundled: bool = False
    #: A `BundledReason` value, or None when `is_bundled` is False. Never set
    #: without `is_bundled` -- a reason on an unflagged vote is text the UI
    #: would render as the explanation for a flag that is not there.
    bundled_reason: str | None = None


@dataclass(frozen=True)
class UnrecordedOutcome:
    """A disposition that happened with no recorded vote (8.5).

    Carries all three things prd.md's scenario asks for in one object, so a
    caller cannot render the message while forgetting the verdict, or score
    the thing anyway:

      * `message` -- "no recorded vote", the exact string prd.md quotes.
      * `verdict` -- "no_opportunity", prd.md's "No opportunity".
      * `scorable` -- always False. Named rather than implied, because
        "never as support" is the whole rule and `if outcome.scorable` is
        harder to get wrong than remembering that this object's existence
        means "do not score".
    """

    reason: str
    message: str = NO_RECORDED_VOTE_MESSAGE
    verdict: str = NO_RECORDED_VOTE_VERDICT
    scorable: bool = False


class SkippedUnrecordedOutcome(Exception):
    """A voice-vote or unanimous-consent disposition was offered as a roll call.

    Raised by `guard_recorded_vote`, so an ingest path cannot mint a
    `roll_calls` row for an outcome that produced no recorded positions. See
    the module docstring's 8.5 section for why a row like that is worse than
    no row: every senator would appear either with a position they never cast
    or as `not_voting`, which a scorer reads as an abstention on a real vote.
    """

    def __init__(self, outcome: UnrecordedOutcome) -> None:
        self.outcome = outcome
        self.reason = outcome.reason
        super().__init__(
            f"{outcome.message}: this outcome was reached by "
            f"{outcome.reason.replace('_', ' ')}, so there are no recorded "
            f"positions and no roll call to write"
        )


def _normalize(value: str | None) -> str:
    return value.strip() if value else ""


# --- 8.3: classification ------------------------------------------------------


def classify(
    signals: VoteSignals,
    *,
    bundled_measure_threshold: int = DEFAULT_BUNDLED_MEASURE_THRESHOLD,
) -> VoteTypeDecision:
    """Type one roll call, and flag it bundled, from its source fields.

    Returns a `VoteTypeDecision` naming the rule that fired. Never raises:
    an unrecognized question falls back to `other`, same posture as story
    8.1 and `house_rollcalls`, because a classifier that crashes on a
    question shape the Senate invented last week would stop the whole ingest.

    See the module docstring for the priority order and the live evidence
    behind each rule. `bundled_measure_threshold` is 8.4's "configurable
    size"; see `DEFAULT_BUNDLED_MEASURE_THRESHOLD`.
    """
    vote_type, rule = _classify_type(signals)
    is_bundled, bundled_reason = _classify_bundling(
        signals, bundled_measure_threshold=bundled_measure_threshold
    )
    return VoteTypeDecision(
        vote_type=vote_type,
        rule=rule,
        is_bundled=is_bundled,
        bundled_reason=bundled_reason,
    )


def _classify_type(signals: VoteSignals) -> tuple[str, str]:
    """datamodel.md's `vote_type`, and the name of the rule that decided it.

    The priority order IS the design -- see the module docstring. Each rule
    returns on match, so the first one wins.
    """
    text = signals.question_text

    # 1. cloture, first: a cloture vote names the measure it is about, so
    #    every later rule would also match it.
    if "cloture" in text:
        return "cloture", "cloture_phrase"

    # 3. motion to recommit, before the generic procedural list, so the two
    #    lists cannot silently couple. (Numbered 3 in the docstring; checked
    #    here before 2 only because it is the narrower test of the two and
    #    neither can match the other's phrases.)
    if any(phrase in text for phrase in _MOTION_TO_RECOMMIT_PHRASES):
        return "motion_to_recommit", "motion_to_recommit_phrase"

    # 2. procedural, BEFORE amendment -- the one ordering change from story
    #    8.1 that moves real numbers. A motion to table an amendment, or a
    #    motion to waive a budget point of order about one, is not a vote on
    #    that amendment's substance.
    for phrase in _PROCEDURAL_PHRASES:
        if phrase in text:
            return "procedural", f"procedural_phrase:{phrase.replace(' ', '_')}"

    # 4. amendment: the structural signal, but only under a question that is
    #    actually about adopting the amendment.
    if signals.amendment_number and any(phrase in text for phrase in _AMENDMENT_QUESTION_PHRASES):
        return "amendment", "amendment_number_and_question"

    # 5. final passage: the measure's own final disposition, exact-matched.
    if signals.normalized_question in _FINAL_PASSAGE_QUESTIONS:
        return "final_passage", "final_disposition_question"

    # 6. other: honestly unclassified, including every bare "On the
    #    Nomination" -- see the module docstring for why that is deliberate.
    return "other", "unrecognized_question"


# --- 8.4: bundling ------------------------------------------------------------


def count_measures(vote_question_text: str | None) -> int:
    """How many DISTINCT measures one vote's question text names.

    8.4's "size", in the one unit the live LIS feed actually provides. The
    real case this exists for is 119-1:524, whose `vote_question_text` lists
    48 separate nominations joined by " and ".

    Deliberately careful about amendment CHAINS: "S.Amdt. 5808 to S.Amdt.
    5453 to S. 2" names three identifiers but is one amendment's place in a
    chain, not three bundled measures, so a "to"-joined chain collapses to
    its first identifier. Without this, every ordinary second-degree
    amendment vote in the chamber would be flagged bundled and down-weighted
    to 0.4x for no reason.

    Returns 0 for empty or absent text -- a vote whose question text is
    missing is not evidence of bundling.
    """
    if not vote_question_text:
        return 0
    # Split on " and " first: the en-bloc form joins measures with "and",
    # while a chain joins them with "to". Each "and"-separated clause
    # contributes its FIRST identifier only, so a chain inside one clause
    # counts once.
    identifiers: set[str] = set()
    for clause in re.split(r"\band\b", vote_question_text, flags=re.IGNORECASE):
        match = _MEASURE_IDENTIFIER.search(clause)
        if match:
            # Normalize whitespace and case so "PN 1250" and "PN1250" are
            # one measure, not two.
            identifiers.add(re.sub(r"\s+", "", match.group(0)).upper())
    return len(identifiers)


def _classify_bundling(
    signals: VoteSignals, *, bundled_measure_threshold: int
) -> tuple[bool, str | None]:
    """8.4's flag and its reason. Marker first, then size -- see the module
    docstring for why the precedence must be deterministic."""
    title = signals.document_title.lower()
    if any(marker in title for marker in OMNIBUS_TITLE_MARKERS):
        return True, BundledReason.OMNIBUS_MARKER.value

    if count_measures(signals.vote_question_text) > bundled_measure_threshold:
        return True, BundledReason.MEASURE_COUNT_EXCEEDS_THRESHOLD.value

    return False, None


def explain_bundled_reason(reason: str | None) -> str | None:
    """Plain-language text for a stored `bundled_reason`, for the UI.

    Returns None for None (an unbundled vote has nothing to explain) and
    None for an unknown code rather than raising: a row written by a future
    adapter version with a reason this build does not know must render
    without the explanation, not 500 the page.
    """
    if reason is None:
        return None
    return BUNDLED_REASON_EXPLANATIONS.get(reason)


# --- 8.5: no recorded vote ----------------------------------------------------


def unrecorded_outcome(disposition: str | None) -> UnrecordedOutcome | None:
    """An `UnrecordedOutcome` if this disposition had no recorded vote, else None.

    `disposition` is whatever text a source gives for how the chamber
    disposed of a measure -- a `roll_calls.result`, a Congress.gov bill
    action, a promise mapping's note. Returns None for every recorded
    outcome, which is the side that matters: a false positive here deletes a
    real vote from the record, so all 75 real `vote_result` values in this
    story's labeled sample are asserted to come back None.

    An individual senator's "Not Voting" or "Absent" is NOT an unrecorded
    outcome -- see the module docstring's 8.5 section for why conflating the
    two would erase a real roll call for everyone else on it.
    """
    if not disposition:
        return None
    text = " ".join(disposition.lower().split())
    for phrase, reason in _UNRECORDED_PHRASES:
        if phrase in text:
            return UnrecordedOutcome(reason=reason)
    if _BARE_UC.search(text):
        return UnrecordedOutcome(reason="unanimous_consent")
    return None


def guard_recorded_vote(disposition: str | None) -> None:
    """Raise `SkippedUnrecordedOutcome` if this disposition had no recorded vote.

    The write-path half of 8.5: an ingest that calls this before minting a
    `roll_calls` row cannot create a row for a voice vote or a
    unanimous-consent outcome. Passes `None` and every recorded disposition
    straight through -- a record with no result at all is story 8.1's own
    `_SkipRollCall` case, and claiming it here would blame the wrong thing
    in the skip report.
    """
    outcome = unrecorded_outcome(disposition)
    if outcome is not None:
        raise SkippedUnrecordedOutcome(outcome)


__all__ = [
    "BUNDLED_REASON_EXPLANATIONS",
    "DEFAULT_BUNDLED_MEASURE_THRESHOLD",
    "NO_RECORDED_VOTE_MESSAGE",
    "NO_RECORDED_VOTE_VERDICT",
    "OMNIBUS_TITLE_MARKERS",
    "BundledReason",
    "SkippedUnrecordedOutcome",
    "UnrecordedOutcome",
    "VoteSignals",
    "VoteTypeDecision",
    "classify",
    "count_measures",
    "explain_bundled_reason",
    "guard_recorded_vote",
    "unrecorded_outcome",
]
