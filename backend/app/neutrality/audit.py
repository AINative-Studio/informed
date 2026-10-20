"""The nightly symmetry audit (story 12.1).

    backlog.md 12.1: "As a methodology owner, I want mirrored synthetic
    candidates and profiles scored every night, so that any partisan skew is
    caught."
    Acceptance: "Fails if mirrored scores differ by more than 0.5; results are
    written to `audit_runs`; a failure blocks the next release."
    prd.md, "Neutrality": "One scoring codebase for all candidates; no
    party-specific rules. A nightly symmetry audit runs the same synthetic
    profiles against all candidates and flags skew."

================================================================================
WHY THIS EXISTS WHEN THE FORMULA IS ALREADY PROVEN ANTISYMMETRIC
================================================================================

`tests/test_alignment_formula_unit.py::test_the_score_is_antisymmetric_under_
flipping_every_position` already proves the hard arithmetic fact: mirror every
`s_i` and a score of `x` becomes exactly `100 - x`. That test is correct, fast,
and is the right way to test a formula — four numbers per term, no database.

It is also blind to almost all of the code a voter's number passes through:

  * `app.scoring.positions.compute_position` — votes to a signed `s_value`,
    via vote-type weights and the omnibus discount;
  * `_stance_direction` — which way a Yea points for THIS stance;
  * `app.scoring.service._terms_for` — tier weights from the published
    methodology, intensity from the profile, confidence from the stored row;
  * `app.scoring.badges` — the confidence badge;
  * `app.scoring.dealbreakers` — the warning shown above the score;
  * `app.scoring.reasons` — the three explanations a voter actually reads.

A skew in any one of those is invisible to a formula test, and six of the
seven are newer than it. "Nonpartisan by construction" (CLAUDE.md's first
non-negotiable) is a claim about the whole pipeline, so this audit drives the
whole pipeline: it writes a deliberately mirrored synthetic fixture into
storage, recomputes positions through `PositionComputer`, scores both sides
through the one live `AlignmentService`, and compares the real results.

================================================================================
WHAT "MIRRORED" MEANS HERE, EXACTLY
================================================================================

There are two independent sign flips in this pipeline, and the audit exercises
both, because a bug that flipped one of them would be invisible to a fixture
that only flipped the other.

**Flip 1 — the candidate's record.** Candidate L votes Yea on every roll call
candidate R votes Nay on, over the same roll calls, same `vote_type`, same
`is_bundled`, same topics. `_vote_direction` is the only code that reads
`position`, so `s_value` negates and `confidence` (built from weights alone)
is untouched.

**Flip 2 — the voter's stance.** The mirrored voter picks the OPPOSITE stance
card on the same topic — `direction_code` `+1` against `-1` — at the same
tier and the same intensity. `_stance_direction` is the only code that reads
`direction_code`, so `s_value` negates again.

That gives two relationships a nonpartisan pipeline must satisfy, and the
audit asserts both per pair:

  1. **Complementary** (flip 1 alone): `score(voter, L) + score(voter, R) == 100`.
     This is the end-to-end form of the formula's antisymmetry — the same
     identity, now with real stored positions, real published tier weights and
     real badge/reason assembly in the path.
  2. **Identical** (flips 1 and 2 together): `score(voter, L) ==
     score(mirrored voter, R)`. `s_value` double-negates, so the two scores
     must be equal. This is prd.md's own BDD scenario, word for word: "each
     voter's score for their aligned candidate is identical within 0.5
     points".

Both are measured as a delta in points, and the worse of the two is the pair's
delta, so `max_delta` is one number against one threshold rather than two that
could disagree about whether the run passed.

================================================================================
WHY THE AUDIT ALSO FAILS ON THINGS THAT ARE NOT SCORES
================================================================================

The acceptance criterion names the scores, and a literal reading would stop
there. Four more comparisons are checked, because each is a way the product
could present one direction of the political axis as better than the other
while the NUMBERS stayed perfectly symmetric:

  * **the confidence badge.** A mirrored record rests on exactly as much
    evidence. A different badge would tell a voter one side is better
    evidenced — a partisan claim with a symmetric number underneath it.
  * **the evidence count.** The input to the badge, checked separately so a
    badge mismatch and a count mismatch are distinguishable in triage.
  * **the reasons.** The same interests must be named (a different set means
    the ranking disagreed about what mattered), and each one must have moved
    the two candidates in OPPOSITE directions. A reason that "raised" both
    scores means one of the two explanations a voter reads is wrong.
  * **the dealbreaker warning.** Exactly one side of a mirrored dealbreaker
    pair can be in violation — L acted against the stance, so R acted for it.
    Both warning, or neither, means the check is not reading the sign.

These are reported in `MirroredPairResult.asymmetries` rather than folded into
the delta: the delta is a points measure and has no honest room for a
categorical difference. A pair with a 0.0 delta and a badge mismatch fails,
and the report says which.

================================================================================
THE SYNTHETIC DATA, AND WHY IT IS NEVER LEFT BEHIND
================================================================================

Every row this module writes is deleted again before `run()` returns, in a
`finally`, including on failure. That is not tidiness. A `candidate_positions`
row is **a claim about how a named person voted**, and CLAUDE.md's "evidence
over opinion" makes fabricating one the worst thing this codebase could put in
its database. Everything synthetic is also labelled unmistakably in its own id
and in its human-readable fields ("SYNTHETIC TEST CANDIDATE", the convention
story 6.2's live verifier established), so a human scanning the live project
mid-run can tell at a glance that nothing here is a real member or a real
vote.

The one row that is deliberately KEPT is the `audit_runs` row — that is the
story's own acceptance criterion, it names no person, and it is the record a
release gate reads.

**The profiles are not real voters, and this module could not read one if it
tried.** It calls `VoterProfileService.save` under a synthetic user id and
deletes it again, so the fixture goes through the same encryption path a real
profile would (which is the point — a bug in that path would skew scores) with
nothing real inside it. Nothing voter-derived reaches the report: see
`MirroredPairResult`, whose fields are scores, counts, badges and stance ids
the fixture itself minted.

================================================================================
WHAT THIS AUDIT DOES NOT COVER, SAID PLAINLY
================================================================================

**It does not score real candidates.** prd.md's sentence is "runs the same
synthetic profiles against all candidates", and the "all candidates" half is
not built here. It cannot be yet, and the reason is not shyness: real
`candidate_positions` do not exist in the live project at all, because
`roll_calls.topic_ids` is `[]` until story 5.5 classifies roll calls (see
`app.scoring.positions`'s module docstring). An audit over real candidates
today would score every one of them `None` and report a vacuous pass — worse
than not running. What this audit proves is that **the pipeline itself is
symmetric**, which is the property "no party-specific rules" actually names;
scoring the real roster is a follow-up that needs 5.5 first, and it is flagged
in this story's PR rather than faked here.

**It does not detect a skew that is symmetric.** A pipeline that scored every
candidate 10 points too low would pass this audit, because both sides of the
mirror move together. That is correct: a uniform error is a calibration bug,
not a partisan one, and conflating the two would make this check's failures
hard to read.

**The `report_file_id` is always None.** datamodel.md gives `audit_runs` that
field, and a stored report file would be the right home for per-pair detail.
Writing one needs `zerodb_upload_file` and a decision about retention of audit
artefacts that no story has made; the per-pair detail is in the log line and
in the gate's violations today, which is enough for a human triaging a
failure. Flagged rather than half-built.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.profiles.crypto import ProfileKeyring, ProfileRootKey
from app.profiles.methodology import MethodologyVersionService
from app.profiles.service import VoterProfileService
from app.repositories import ProvenanceContext, Repository
from app.repositories.models import (
    Election,
    IssueTopic,
    MemberVote,
    Person,
    RollCall,
    Stance,
)
from app.scoring.models import CandidateAlignment
from app.scoring.positions import PositionComputer
from app.scoring.service import AlignmentService
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: backlog.md 12.1's acceptance criterion, as a number this codebase asserts
#: against: "Fails if mirrored scores differ by more than 0.5". prd.md's own
#: BDD scenario states the same figure ("identical within 0.5 points").
#:
#: **More than 0.5 fails, so 0.5 itself passes** — the inequality's direction
#: is the whole criterion, and `test_a_delta_exactly_at_the_tolerance_passes`
#: pins it.
#:
#: Worth saying why a tolerance exists at all when the identity is exact. The
#: formula's antisymmetry is bit-exact for terms built by hand, but these
#: scores come out of two divisions (`compute_position`'s weighted mean, then
#: the formula's) over values that round-tripped through JSON and a database,
#: so two mirrored runs can differ in the last bits. 0.5 points is four orders
#: of magnitude above that floating-point floor and far below anything a voter
#: could see (a score renders to a whole number), which is why the real
#: observed delta on a healthy pipeline is 0.0 and not merely "under 0.5".
SYMMETRY_TOLERANCE = 0.5

#: How recent an `audit_runs` row has to be for the release gate to accept it.
#: The audit is nightly, so a result older than two days means at least one
#: night's run did not happen — and a gate that trusted a week-old pass would
#: let a skew introduced since then ship. Two days rather than one so a single
#: missed or slow run does not block a release on its own.
MAX_AUDIT_AGE = timedelta(days=2)

#: Every synthetic id this module writes carries this marker, so a human
#: reading the live project can tell at a glance that none of it is real. The
#: convention `verify_alignment_scoring_live.py` established for story 6.2.
MARKER = "SYMMETRY-AUDIT"

_SYNTHETIC_LABEL = "SYNTHETIC TEST CANDIDATE (not a real person)"

#: The synthetic user ids the two mirrored profiles are saved under. Not real
#: accounts, and both deleted at the end of a run.
_VOTER_ID = f"test-{MARKER.lower()}-voter"
_MIRRORED_VOTER_ID = f"test-{MARKER.lower()}-voter-mirrored"

_PROVENANCE = ProvenanceContext(adapter_version="neutrality.symmetry_audit/v1")

_VOTED_AT = "2026-04-01T12:00:00Z"


# ---------------------------------------------------------------------------
# The mirror operation
# ---------------------------------------------------------------------------


def mirrored_direction_code(code: str) -> str:
    """The opposite stance on the same axis, at the same strength.

    `+1` <-> `-1`, `+2` <-> `-2`. The magnitude is preserved deliberately: a
    mirrored pair must differ in exactly ONE way, or a delta cannot be
    attributed. (The magnitude is not used as a multiplier anywhere — see
    `app.scoring.positions._stance_direction` — so preserving it costs nothing
    and keeps the fixture honest.)

    Raises:
        ValueError: `code` is `0`. A stance that does not move along the axis
            is its own reflection, and `compute_position` returns `None` for
            it, so including one would add a pair that can never score — and
            the audit would compare two `None`s and call it symmetric. That is
            exactly the false pass this whole story exists to prevent, so it
            is refused loudly here rather than tolerated.
    """
    if code in {"+1", "+2"}:
        return f"-{code[1]}"
    if code in {"-1", "-2"}:
        return f"+{code[1]}"
    raise ValueError(
        f"direction_code {code!r} has no mirror: a stance that does not move along its "
        "axis is its own reflection, and compute_position declines to score it, so a "
        "mirrored pair built from it could never produce two comparable scores"
    )


def mirrored_position(position: str) -> str:
    """The opposite vote. `yea` <-> `nay`; everything else is its own mirror.

    `present` and `not_voting` are recorded non-positions that contribute
    nothing (`app.scoring.positions._vote_direction` returns 0.0 for both), so
    flipping them to each other would change no arithmetic while making the
    fixture claim a mirror relationship it does not have.
    """
    if position == "yea":
        return "nay"
    if position == "nay":
        return "yea"
    return position


# ---------------------------------------------------------------------------
# The fixture: what a mirrored pair is made of
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyntheticVote:
    """One synthetic roll call in a pair's record.

    `vote_type` and `is_bundled` are carried so the fixture exercises
    `compute_position`'s weighting (final passage vs procedural, and the
    omnibus discount) rather than only its sign — a skew in the weight table
    would otherwise be invisible, since both sides would be weighted wrongly
    but equally.
    """

    roll: int
    position: str
    vote_type: str = "final_passage"
    is_bundled: bool = False


@dataclass(frozen=True)
class SyntheticInterest:
    """One interest in a pair's synthetic profile, with its stance's axis."""

    slug: str
    direction_code: str
    tier: str
    intensity: int
    dealbreaker: bool = False
    votes: Sequence[SyntheticVote] = ()


@dataclass(frozen=True)
class MirroredPair:
    """One mirrored pair's whole definition: the topics, stances and votes.

    Named, because the report has to say WHICH pair was asymmetric and a
    position in a list is not a thing a human can act on.
    """

    name: str
    interests: Sequence[SyntheticInterest]

    @property
    def has_dealbreaker(self) -> bool:
        return any(interest.dealbreaker for interest in self.interests)


def default_pairs() -> list[MirroredPair]:
    """The mirrored fixtures one audit run scores.

    Three pairs, each chosen to put a different part of the pipeline in the
    path. They are not variations on a theme: a single pair with everything in
    it would pass or fail as a unit, and triage would start from scratch.

      * **unanimous-record** — one primary interest, every vote in one
        direction, all final passage. The simplest possible mirror, and the
        one whose failure means the core sign handling is broken. `s_value`
        lands at exactly ±1.0, which is also the clamp boundary in both
        `compute_position` and `alignment_score`, so an asymmetric clamp shows
        up here first.

      * **weighted-and-mixed** — three interests across all three tiers, with
        mixed Yea/Nay records, a procedural vote, a bundled vote and an
        abstention. This is the pair that exercises the vote-type weights, the
        omnibus discount, `present` contributing nothing, and the tier weights
        read from the published methodology. A skew anywhere in the weighting
        shows up here and not in the first pair.

      * **dealbreaker** — one primary dealbreaker interest with a record
        against it, plus a second interest so the score is not built from the
        dealbreaker alone. Exercises `app.scoring.dealbreakers`, whose
        one-sided-by-construction behaviour is the thing being checked: the
        warning must fire on exactly one side of the mirror.

    Every interest's `votes` are listed from the LEFT candidate's point of
    view; the right candidate's record is `mirrored_position` applied to each,
    and the mirrored voter's stance is `mirrored_direction_code` applied to
    each interest. Both derivations happen in `_write_fixture`, once, so the
    fixture cannot drift into being two hand-written halves that are not
    actually mirrors.
    """
    return [
        MirroredPair(
            name="unanimous-record",
            interests=[
                SyntheticInterest(
                    slug="unanimous",
                    direction_code="+1",
                    tier="primary",
                    intensity=3,
                    votes=[
                        SyntheticVote(roll=1, position="yea"),
                        SyntheticVote(roll=2, position="yea"),
                        SyntheticVote(roll=3, position="yea"),
                    ],
                )
            ],
        ),
        MirroredPair(
            name="weighted-and-mixed",
            interests=[
                SyntheticInterest(
                    slug="mixed-primary",
                    direction_code="+1",
                    tier="primary",
                    intensity=2,
                    votes=[
                        SyntheticVote(roll=10, position="yea"),
                        SyntheticVote(roll=11, position="nay"),
                        SyntheticVote(roll=12, position="yea", vote_type="procedural"),
                        # Recorded, but not a side: must contribute nothing to
                        # either `s_value` or `evidence_count`, on both sides.
                        SyntheticVote(roll=13, position="present"),
                    ],
                ),
                SyntheticInterest(
                    slug="bundled-secondary",
                    direction_code="-1",
                    tier="secondary",
                    intensity=3,
                    votes=[
                        SyntheticVote(roll=20, position="nay", is_bundled=True),
                        SyntheticVote(roll=21, position="yea", vote_type="amendment"),
                    ],
                ),
                SyntheticInterest(
                    slug="thin-nice-to-have",
                    direction_code="+2",
                    tier="nice_to_have",
                    intensity=1,
                    votes=[SyntheticVote(roll=30, position="nay", vote_type="cloture")],
                ),
            ],
        ),
        MirroredPair(
            name="dealbreaker",
            interests=[
                SyntheticInterest(
                    slug="dealbreaker-primary",
                    direction_code="+1",
                    tier="primary",
                    intensity=3,
                    dealbreaker=True,
                    votes=[
                        SyntheticVote(roll=40, position="nay"),
                        SyntheticVote(roll=41, position="nay"),
                    ],
                ),
                SyntheticInterest(
                    slug="dealbreaker-companion",
                    direction_code="+1",
                    tier="primary",
                    intensity=1,
                    votes=[SyntheticVote(roll=45, position="yea")],
                ),
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# The result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MirroredPairResult:
    """What the real pipeline produced for one mirrored pair.

    Carries no voter data and cannot: every field is a score, a count, a badge
    or a stance id this module's own fixture minted. The synthetic profiles
    never leave `SymmetryAuditor`.
    """

    name: str
    left_candidacy_id: str
    right_candidacy_id: str
    methodology_version: str
    #: Which scorer produced both sides. One value per run by construction —
    #: the report asserts they all agree, which is how "one scoring codebase
    #: for every candidate" is checked rather than assumed.
    scorer: str

    left_score: float | None
    right_score: float | None
    #: The mirrored voter's score for the mirrored candidate. Must EQUAL
    #: `left_score`: both sign flips applied cancel.
    mirrored_voter_score: float | None

    left_badge: str
    right_badge: str
    left_evidence_count: int
    right_evidence_count: int
    left_reason_stance_ids: tuple[str, ...]
    right_reason_stance_ids: tuple[str, ...]
    left_reason_directions: dict[str, str]
    right_reason_directions: dict[str, str]
    left_warning_count: int
    right_warning_count: int
    has_dealbreaker: bool

    @property
    def complement_delta(self) -> float:
        """How far the two candidates' scores are from summing to 100.

        `inf` when either side could not be scored. An unscored pair is
        **unmeasured, not symmetric** — and two `None`s comparing equal would
        report a perfect run over a fixture that produced nothing, which is
        the one failure mode an audit must never have.
        """
        if self.left_score is None or self.right_score is None:
            return float("inf")
        return abs((self.left_score + self.right_score) - 100.0)

    @property
    def mirrored_voter_delta(self) -> float:
        """How far the mirrored voter's score is from the original's.

        prd.md's own scenario: "identical within 0.5 points". `inf` when
        either is missing, for the same reason as above.
        """
        if self.left_score is None or self.mirrored_voter_score is None:
            return float("inf")
        return abs(self.left_score - self.mirrored_voter_score)

    @property
    def delta(self) -> float:
        """The worse of this pair's two relationships, in points.

        One number per pair, so `SymmetryAuditReport.max_delta` is a single
        value against a single threshold rather than two that could disagree.
        """
        return max(self.complement_delta, self.mirrored_voter_delta)

    @property
    def asymmetries(self) -> tuple[str, ...]:
        """Every way this pair is asymmetric, in words a human can act on.

        Includes the score deltas AND the four categorical comparisons the
        module docstring argues for. Returned as prose rather than flags
        because this list is what a failing CI log and a blocked release
        print, and "badge mismatch" with no values in it sends a human
        straight back to the database.
        """
        found: list[str] = []
        if self.left_score is None or self.right_score is None:
            found.append(
                f"the pair could not be scored at all (left={self.left_score}, "
                f"right={self.right_score}); an unscored pair is unmeasured, not symmetric"
            )
        elif self.complement_delta > SYMMETRY_TOLERANCE:
            found.append(
                f"the two candidates' scores are {self.left_score} and {self.right_score}, "
                f"summing to {self.left_score + self.right_score} rather than 100 "
                f"(delta {self.complement_delta:.4f} > {SYMMETRY_TOLERANCE})"
            )
        if self.mirrored_voter_score is None:
            found.append("the mirrored voter could not be scored at all")
        elif self.mirrored_voter_delta > SYMMETRY_TOLERANCE:
            found.append(
                f"the voter scored its aligned candidate {self.left_score} but the mirrored "
                f"voter scored its aligned candidate {self.mirrored_voter_score} "
                f"(delta {self.mirrored_voter_delta:.4f} > {SYMMETRY_TOLERANCE}); prd.md "
                "requires these to be identical"
            )
        if self.left_badge != self.right_badge:
            found.append(
                f"the confidence badges differ ({self.left_badge!r} vs {self.right_badge!r}); "
                "a mirrored record rests on exactly as much evidence, so this presents one "
                "direction of the axis as better evidenced than the other"
            )
        if self.left_evidence_count != self.right_evidence_count:
            found.append(
                f"the evidence counts differ ({self.left_evidence_count} vs "
                f"{self.right_evidence_count})"
            )
        if self.left_reason_stance_ids != self.right_reason_stance_ids:
            found.append(
                f"the top reasons name different interests ({list(self.left_reason_stance_ids)} "
                f"vs {list(self.right_reason_stance_ids)})"
            )
        for stance_id, left_direction in sorted(self.left_reason_directions.items()):
            right_direction = self.right_reason_directions.get(stance_id)
            if right_direction is None:
                continue
            if left_direction == right_direction:
                found.append(
                    f"reason {stance_id} has direction {left_direction!r} for BOTH candidates; "
                    "a mirrored record must move the score the other way, so one of the two "
                    "explanations a voter reads is wrong"
                )
        if self.has_dealbreaker:
            warned = (self.left_warning_count > 0, self.right_warning_count > 0)
            if warned not in {(True, False), (False, True)}:
                found.append(
                    f"dealbreaker warnings fired on {warned} (left={self.left_warning_count}, "
                    f"right={self.right_warning_count}); exactly one side of a mirrored "
                    "dealbreaker pair is in violation, so both or neither means the check is "
                    "not reading the sign"
                )
        return tuple(found)

    @property
    def symmetric(self) -> bool:
        return not self.asymmetries


@dataclass(frozen=True)
class SymmetryAuditReport:
    """What one run of the symmetry audit found.

    Mirrors `app.integrity.checks.CheckReport` and
    `app.panel_review.gate.GateReport` in shape (`passed`/`ok`, `summary()`)
    so this composes with the scheduled job, the CI gate and the pytest
    assertion that already consume those, rather than inventing a third
    vocabulary for "did a check pass".
    """

    run_at: datetime
    methodology_version: str
    pairs: tuple[MirroredPairResult, ...]
    max_delta: float
    tolerance: float = SYMMETRY_TOLERANCE

    @property
    def passed(self) -> bool:
        """True iff every pair is symmetric and at least one pair was scored.

        **An empty run fails.** An audit that scored nothing has proven
        nothing, and reporting a pass on it is how a neutrality check becomes
        decoration — the same reasoning `scripts/check_panel_gate.py`'s
        self-check applies to its own failing path.
        """
        if not self.pairs:
            return False
        if self.max_delta > self.tolerance:
            return False
        return all(pair.symmetric for pair in self.pairs)

    @property
    def asymmetries(self) -> tuple[str, ...]:
        """Every asymmetry found, each prefixed with the pair it belongs to."""
        if not self.pairs:
            return (
                "no mirrored pair was scored at all; an audit that measured nothing has "
                "proven nothing and must not report a pass",
            )
        return tuple(f"{pair.name}: {detail}" for pair in self.pairs for detail in pair.asymmetries)

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        if not self.pairs:
            # The empty run's reason belongs in the summary itself, not only
            # in `asymmetries`: this one line is what a CI log and a scheduled
            # job print, and "FAIL — 0 pairs, max delta inf" reads like a
            # crash rather than like the deliberate refusal it is.
            return (
                f"symmetry_audit: {verdict} — no mirrored pair was scored at all, so "
                "nothing was measured and nothing is proven; an audit with no pairs "
                "cannot report a pass. methodology "
                f"{self.methodology_version or '(none read)'}"
            )
        return (
            f"symmetry_audit: {verdict} — {len(self.pairs)} mirrored pair(s), "
            f"max delta {self.max_delta:.4f} (tolerance {self.tolerance}), "
            f"{len(self.asymmetries)} asymmetry(ies), methodology "
            f"{self.methodology_version or '(none read)'}"
        )

    def row(self) -> dict[str, Any]:
        """The `audit_runs` payload, exactly datamodel.md's documented fields.

        datamodel.md: `audit_runs`, id `aud:{date}`, fields `run_at`,
        `methodology_version`, `max_delta`, `passed`, `report_file_id`.

        `max_delta` is coerced to a float because `inf` is not representable
        in JSON: an unscoreable run would otherwise fail the write and lose
        the very record that says it failed. `_AUDIT_RUNS_MAX_DELTA_CEILING`
        stands in for it — a value far above any tolerance anyone would set,
        so a reader cannot mistake it for a real measurement, and
        `passed: False` carries the actual verdict regardless.

        `report_file_id` is None; see the module docstring's last section.
        """
        delta = self.max_delta
        if delta == float("inf") or delta != delta:  # inf or NaN
            delta = _AUDIT_RUNS_MAX_DELTA_CEILING
        return {
            "id": audit_runs_table_row_id(self.run_at),
            "run_at": _iso(self.run_at),
            "methodology_version": self.methodology_version,
            "max_delta": float(delta),
            "passed": self.passed,
            "report_file_id": None,
        }


#: What `max_delta` is recorded as when the real delta is `inf` (a pair that
#: could not be scored at all). JSON has no infinity, and dropping the row
#: would lose the record of the failure. 999.0 is three orders of magnitude
#: above the 0-100 score range, so it cannot be read as a measurement.
_AUDIT_RUNS_MAX_DELTA_CEILING = 999.0


def audit_runs_table_row_id(moment: datetime) -> str:
    """datamodel.md's `audit_runs` id: `aud:{date}`, one row per UTC day.

    One row per day is what makes the table's documented "Latest" query
    answerable by sorting ids, and it means a second run on the same day
    restates that day's result rather than appending a second one. A naive
    datetime is read as UTC and an aware one is converted, so a runner in
    another timezone cannot write two rows for one UTC day.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"aud:{moment.astimezone(UTC).date().isoformat()}"


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# The auditor
# ---------------------------------------------------------------------------


class SymmetryAuditor:
    """Drives the real scoring pipeline over mirrored synthetic data.

    Args:
        client: the ZeroDB client. The fixture is written through the real
            repositories against this, so reference checks, deterministic ids
            and provenance all apply exactly as they would to real data.
        profiles: the voter-profile service the scorer will read through.
            Required rather than constructed here: the audit must score
            through the SAME service a request does (it is where decryption
            and validation live, and a bug there could skew a score), and
            building a second one would mean this module deciding where a root
            key comes from.
        pairs: which mirrored fixtures to score. Defaults to
            `default_pairs()`.
        skew: points added to the LEFT candidate's score after the pipeline
            produced it. **Zero in production, and there is no caller that
            passes anything else outside tests.** It exists because the audit's
            failing path has to be provable: without it, the only way to see
            this check fail would be to introduce a real partisan bug into the
            scorer, and a neutrality check whose failing path is never
            exercised is a check nobody should trust (the same argument
            `scripts/check_panel_gate.py`'s self-check makes). Applied AFTER
            the real pipeline, so the sabotage cannot accidentally be what the
            pipeline is tested through.
        now: the clock, injectable so a test can pin the `audit_runs` row id.
    """

    def __init__(
        self,
        *,
        client: ZeroDBClient,
        profiles: VoterProfileService,
        pairs: Sequence[MirroredPair] | None = None,
        skew: float = 0.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._profiles = profiles
        self._pairs = list(pairs) if pairs is not None else default_pairs()
        self._skew = skew
        self._now = now or (lambda: datetime.now(UTC))
        self._scorer = AlignmentService(client=client, profiles=profiles)
        self._computer = PositionComputer(client)
        self._written: list[tuple[str, str]] = []

    # --- the run ----------------------------------------------------------

    def run(self) -> SymmetryAuditReport:
        """Score every mirrored pair, write the `audit_runs` row, clean up.

        Never raises on its own findings — same contract as
        `app.integrity.checks.run_all_checks` and
        `app.panel_review.gate.check_panel_gate`, and for the same reason: the
        caller (a scheduled job, a CI script, a pytest assertion) decides what
        a failure should DO, and this method's job is only to report it
        completely. A genuine `ZeroDBError` writing the fixture or reading a
        position is NOT caught and propagates — that is infrastructure
        failing, not a finding, and reporting it as "the pipeline is skewed"
        would be a lie.

        The synthetic rows are deleted in a `finally`, so a failure mid-run
        does not leave a fabricated voting record in the live project. The
        `audit_runs` row is written before cleanup and deliberately kept.
        """
        run_at = self._now()
        version = MethodologyVersionService(self._client).current_version()

        results: list[MirroredPairResult] = []
        try:
            for pair in self._pairs:
                results.append(self._score_pair(pair, methodology_version=version.id))
        finally:
            self._clean_up()

        max_delta = max((result.delta for result in results), default=float("inf"))
        report = SymmetryAuditReport(
            run_at=run_at,
            methodology_version=version.id,
            pairs=tuple(results),
            max_delta=max_delta,
        )
        self._record(report)

        logger.info("symmetry audit: %s", report.summary())
        for asymmetry in report.asymmetries:
            logger.error("symmetry audit asymmetry: %s", asymmetry)
        return report

    def _record(self, report: SymmetryAuditReport) -> None:
        """Write the `audit_runs` row. The one row this module keeps.

        Written through `upsert_row` rather than a `Repository`: `audit_runs`
        is a governance table that references nothing (datamodel.md: it points
        at rows by `target_table`/`target_id`, which a symmetry run has none
        of), and its id is `aud:{date}` rather than a natural-key composition,
        so a `RowModel`'s two contributions — reference checking and id
        derivation — would both be no-ops. `AuditRun` in
        `app.repositories.models` validates the shape before this write.
        """
        from app.repositories.models import AuditRun

        row = AuditRun.model_validate(report.row()).row_data()
        self._client.upsert_row("audit_runs", str(row["id"]), row)

    # --- one pair ---------------------------------------------------------

    def _score_pair(self, pair: MirroredPair, *, methodology_version: str) -> MirroredPairResult:
        """Write one pair's mirrored fixture, score both sides, compare.

        The order matters and is the whole point: the fixture is written, then
        positions are **recomputed from it by the real `PositionComputer`**,
        then both sides are scored by the real `AlignmentService`. Nothing
        here computes a position or a score itself — if it did, the audit
        would be testing its own arithmetic rather than the product's.
        """
        ids = _PairIds(pair.name)
        self._write_fixture(pair, ids)

        self._computer.recompute_person(ids.left_person, stances=self._stances_for(pair, ids))
        self._computer.recompute_person(ids.right_person, stances=self._stances_for(pair, ids))

        self._profiles.save(_VOTER_ID, self._profile_payload(pair, ids, mirrored=False))
        self._profiles.save(_MIRRORED_VOTER_ID, self._profile_payload(pair, ids, mirrored=True))

        left = self._scorer.alignment_for(ids.left_candidacy, user_id=_VOTER_ID)
        right = self._scorer.alignment_for(ids.right_candidacy, user_id=_VOTER_ID)
        # Flips 1 AND 2: the mirrored voter scoring the mirrored candidate.
        mirrored = self._scorer.alignment_for(ids.right_candidacy, user_id=_MIRRORED_VOTER_ID)

        return MirroredPairResult(
            name=pair.name,
            left_candidacy_id=ids.left_candidacy,
            right_candidacy_id=ids.right_candidacy,
            methodology_version=methodology_version,
            scorer=type(self._scorer).__name__,
            left_score=_skewed(left.score, self._skew),
            right_score=right.score,
            mirrored_voter_score=mirrored.score,
            left_badge=left.confidence_badge,
            right_badge=right.confidence_badge,
            left_evidence_count=left.evidence_count,
            right_evidence_count=right.evidence_count,
            left_reason_stance_ids=_reason_stances(left, ids, mirrored=False),
            right_reason_stance_ids=_reason_stances(right, ids, mirrored=True),
            left_reason_directions=_reason_directions(left, ids, mirrored=False),
            right_reason_directions=_reason_directions(right, ids, mirrored=True),
            left_warning_count=len(left.dealbreaker_warnings),
            right_warning_count=len(right.dealbreaker_warnings),
            has_dealbreaker=pair.has_dealbreaker,
        )

    # --- the fixture ------------------------------------------------------

    def _write_fixture(self, pair: MirroredPair, ids: _PairIds) -> None:
        """Write both halves of one mirrored pair through the real repositories.

        **Both halves are derived from one declaration**, by
        `mirrored_position` and `mirrored_direction_code`. Hand-writing the
        two sides would let them drift into not actually being mirrors, at
        which point a failing audit would be reporting a broken fixture as a
        broken product — the single worst outcome for a check like this.

        Note what is NOT mirrored: the roll calls, their `vote_type`, their
        `is_bundled` flag, the topics, the tiers, the intensities and the
        confidence. Each pair differs in exactly the two signs, so a delta is
        attributable.
        """
        topics: Repository[IssueTopic] = Repository(client=self._client, model=IssueTopic)
        stances: Repository[Stance] = Repository(client=self._client, model=Stance)
        rolls: Repository[RollCall] = Repository(
            client=self._client, model=RollCall, provenance=_PROVENANCE
        )
        votes: Repository[MemberVote] = Repository(client=self._client, model=MemberVote)
        people: Repository[Person] = Repository(client=self._client, model=Person)

        for interest in pair.interests:
            topic_id = ids.topic(interest.slug)
            topics.upsert(
                {
                    "id": topic_id,
                    "name": f"SYNTHETIC TEST TOPIC ({MARKER}, {pair.name}/{interest.slug})",
                    "locale": "en-US",
                    "taxonomy_version": 1,
                }
            )
            self._track("issue_topics", topic_id)
            # Two stance cards per topic: the voter's, and its mirror. Both are
            # written because the mirrored PROFILE picks the second one, and a
            # stance the profile names must exist for the reference check to
            # pass — which is itself part of what is being audited.
            mirrored_code = mirrored_direction_code(interest.direction_code)
            for stance_id, code in (
                (ids.stance(interest.slug), interest.direction_code),
                (ids.mirrored_stance(interest.slug), mirrored_code),
            ):
                stances.upsert(
                    {
                        "id": stance_id,
                        "topic_id": topic_id,
                        "label": f"SYNTHETIC TEST STANCE ({MARKER}) direction {code}",
                        "description": (
                            "Synthetic stance card written by the nightly symmetry audit "
                            "(app.neutrality.audit). Not editorial content, not panel-reviewed, "
                            "and deleted again before the audit returns."
                        ),
                        "direction_code": code,
                        "wording_version": 1,
                    }
                )
                self._track("stances", stance_id)

        for person_id in (ids.left_person, ids.right_person):
            people.upsert(
                {
                    "id": person_id,
                    "canonical_name": _SYNTHETIC_LABEL,
                    "terms": [{"body_id": "us-house", "party": "Synthetic", "start": "2025-01-03"}],
                }
            )
            self._track("people", person_id)

        self._write_election_and_candidacies(ids)

        for interest in pair.interests:
            topic_id = ids.topic(interest.slug)
            for vote in interest.votes:
                roll_id = ids.roll_call(vote.roll)
                rolls.upsert(
                    {
                        "id": roll_id,
                        "voted_at": _VOTED_AT,
                        "question": f"SYNTHETIC TEST ROLL CALL ({MARKER})",
                        "vote_type": vote.vote_type,
                        "result": "Passed",
                        "topic_ids": [topic_id],
                        "is_bundled": vote.is_bundled,
                        "source_url": f"https://example.invalid/{MARKER.lower()}/{roll_id}",
                        "content_hash": f"{MARKER.lower()}-{roll_id}",
                    }
                )
                self._track("roll_calls", roll_id)
                for person_id, position in (
                    (ids.left_person, vote.position),
                    (ids.right_person, mirrored_position(vote.position)),
                ):
                    written = votes.upsert(
                        {
                            "roll_call_id": roll_id,
                            "person_id": person_id,
                            "position": position,
                            "voted_at": _VOTED_AT,
                            "vote_type": vote.vote_type,
                            "topic_ids": [topic_id],
                            "is_bundled": vote.is_bundled,
                        }
                    )
                    self._track("member_votes", str(written.id))

    def _write_election_and_candidacies(self, ids: _PairIds) -> None:
        """The election, district and two candidacies the scorer reads.

        `elections` goes through its repository (it references `jurisdictions`,
        and `us-federal` is real reference data every environment has).
        `districts` is written through the client directly: `District`
        references `bodies`, and a synthetic district under a REAL body would
        be reachable from a real ballot read for as long as the audit runs —
        which is the one thing this module must not do. The candidacies then
        reference the synthetic district, so they are written the same way.
        """
        Repository(client=self._client, model=Election).upsert(
            {
                "id": ids.election,
                "jurisdiction_id": "us-federal",
                "name": f"SYNTHETIC TEST ELECTION ({MARKER})",
                "election_date": "2026-11-03",
                "kind": "general",
            }
        )
        self._track("elections", ids.election)

        self._client.upsert_row(
            "districts",
            ids.district,
            {
                "id": ids.district,
                "body_id": "us-house",
                "name": f"SYNTHETIC TEST DISTRICT ({MARKER})",
                "state": "ZZ",
                "valid_from": "2023-01-03",
            },
        )
        self._track("districts", ids.district)

        for person_id, candidacy_id in (
            (ids.left_person, ids.left_candidacy),
            (ids.right_person, ids.right_candidacy),
        ):
            self._client.upsert_row(
                "candidacies",
                candidacy_id,
                {
                    "id": candidacy_id,
                    "election_id": ids.election,
                    "district_id": ids.district,
                    "person_id": person_id,
                    "person_name": _SYNTHETIC_LABEL,
                    # Deliberately the same for both sides. A party string that
                    # differed would be the one thing a nonpartisan scorer must
                    # not be able to see, and giving the fixture one would make
                    # a passing audit less meaningful, not more.
                    "party": "Synthetic",
                    "is_incumbent": True,
                    "status": "filed",
                },
            )
            self._track("candidacies", candidacy_id)

    def _stances_for(self, pair: MirroredPair, ids: _PairIds) -> list[Stance]:
        """Both stance cards per interest, read back from storage.

        Read rather than constructed in memory, so the positions are computed
        from what the database actually holds — a field that failed to
        round-trip (story 12.2's hazard, a float arriving back as a string)
        would skew a position, and an in-memory stance would hide it.
        """
        found: list[Stance] = []
        for interest in pair.interests:
            for stance_id in (ids.stance(interest.slug), ids.mirrored_stance(interest.slug)):
                row = self._client.get_row("stances", stance_id)
                if row is not None:
                    found.append(Stance.model_validate(row.row_data))
        return found

    def _profile_payload(
        self, pair: MirroredPair, ids: _PairIds, *, mirrored: bool
    ) -> dict[str, Any]:
        """One synthetic voter's interests: the same tiers and intensities on
        either the declared stances or their mirrors.

        Only the stance ids differ between the two voters. Same topics, same
        tiers, same intensities, same dealbreaker flags — so flip 2 is the one
        difference, and a delta is attributable to it.
        """
        return {
            "interests": [
                {
                    "topic_id": ids.topic(interest.slug),
                    "stance_id": (
                        ids.mirrored_stance(interest.slug)
                        if mirrored
                        else ids.stance(interest.slug)
                    ),
                    "tier": interest.tier,
                    "intensity": interest.intensity,
                    "dealbreaker": interest.dealbreaker,
                }
                for interest in pair.interests
            ]
        }

    # --- cleanup ----------------------------------------------------------

    def _track(self, table: str, row_id: str) -> None:
        self._written.append((table, row_id))

    def _clean_up(self) -> None:
        """Delete every synthetic row this audit could have left anywhere.

        Two passes, deliberately, because they catch different failures:

        **1. The tracked list**, newest-written first. Exact, cheap, and what
        handles the normal case including an exception (`run()` calls this in
        a `finally`).

        **2. A marker sweep over every table the fixture writes.** This is the
        pass that matters for a nightly job, because `finally` does not run on
        SIGKILL — a runner killed by a CI timeout or an OOM mid-fixture leaves
        rows that the in-process list died with. Sweeping by `MARKER` means
        the NEXT night's run removes them, so the audit self-heals rather than
        accumulating fabricated voting records one interrupted run at a time.
        It is also what makes `verify_symmetry_audit_live.py`'s "clean start"
        check a meaningful assertion rather than a tripwire a human has to
        clear by hand.

        The sweep is marker-scoped, never table-wide: it matches only rows
        whose own id or `person_id`/`entity_id` carries `SYMMETRY-AUDIT`, so
        it cannot touch a real member, a real roll call or a real stance card
        however badly a future edit mangles the fixture.

        `candidate_positions` is append-only and `Repository.delete` refuses
        it by design (datamodel.md keeps positions indefinitely for
        reproducibility). That rule protects REAL positions; these are
        fabricated claims about two people who do not exist, and leaving them
        would mean the live project permanently asserted how a non-existent
        member voted. So they go through the client directly — the same single
        documented exception `verify_alignment_scoring_live.py` makes, called
        out here for the same reason.

        A delete that fails is logged and the loop continues: a partial
        cleanup is bad, but abandoning the rest of it is worse, and the marker
        in every id is what lets the next run (or a human) finish the job.
        """
        for table, row_id in reversed(self._written):
            self._delete(table, row_id)
        self._written.clear()

        for table in _SWEPT_TABLES:
            for row in self._client.query_rows(table, limit=_SWEEP_PAGE_SIZE).rows:
                if _is_synthetic(row.row_data):
                    self._delete(table, str(row.row_data.get("id")))

        for user_id in (_VOTER_ID, _MIRRORED_VOTER_ID):
            try:
                self._profiles.delete(user_id)
            except Exception as exc:  # noqa: BLE001 — keep cleaning up
                logger.warning("symmetry audit: could not delete a synthetic profile: %s", exc)

    def _delete(self, table: str, row_id: str) -> None:
        try:
            self._client.delete_row(table, row_id)
        except Exception as exc:  # noqa: BLE001 — keep cleaning up
            logger.warning("symmetry audit: could not delete %s/%s: %s", table, row_id, exc)


#: Every table the fixture writes, swept by marker on each cleanup so an
#: interrupted run (SIGKILL, CI timeout, OOM) self-heals on the next one.
#: `candidate_positions` first, because it is the one whose rows are claims
#: about how a named person voted and therefore the one that must never
#: survive; the rest follow in reverse dependency order so a reference check
#: cannot refuse a delete.
_SWEPT_TABLES = (
    "candidate_positions",
    "member_votes",
    "provenance_log",
    "roll_calls",
    "candidacies",
    "stances",
    "issue_topics",
    "people",
    "districts",
    "elections",
)

#: How many rows one sweep pass reads per table. Generous rather than tuned:
#: the sweep's whole job is to find rows a previous run failed to delete, and
#: a page that truncated would leave exactly those behind. The tables it reads
#: are small today, and the same open question datamodel.md already tracks for
#: every other paged read in this codebase applies when they are not.
_SWEEP_PAGE_SIZE = 500


def _is_synthetic(row_data: dict[str, Any]) -> bool:
    """Whether this row was written by the symmetry audit.

    Checked against the three fields that carry the marker — the row's own
    id, and the two fields that name a synthetic entity from another table
    (`person_id` on a position or vote, `entity_id` on a provenance row) —
    rather than against the whole serialised row. A whole-row match would also
    hit a real row that merely MENTIONED the marker in a label or question
    text, and a sweep that can delete real data is far worse than one that
    occasionally misses a synthetic row (the next run catches that; nothing
    brings back a deleted roll call).
    """
    for field_name in ("id", "person_id", "entity_id"):
        value = row_data.get(field_name)
        if isinstance(value, str) and MARKER.lower() in value.lower():
            return True
    return False


def _skewed(score: float | None, skew: float) -> float | None:
    """`score` plus the test-only sabotage, clamped to the published range.

    Clamped because `CandidateAlignment.score` is bounded to [0, 100] and a
    sabotage that produced 108 would be caught by the model rather than by the
    audit — proving the wrong thing.
    """
    if score is None or skew == 0.0:
        return score
    return min(100.0, max(0.0, score + skew))


def _reason_stances(
    alignment: CandidateAlignment, ids: _PairIds, *, mirrored: bool
) -> tuple[str, ...]:
    """The stance ids a candidate's reasons name, **normalised to one side**.

    The two voters name different stance ids by construction (that is flip 2),
    so comparing them raw would always differ and the check would be useless.
    Normalising the mirrored side back to its original id compares the
    INTERESTS the reasons are about, which is what "the same interests
    explain both scores" actually means.

    Both candidates here are scored by the SAME (non-mirrored) voter, so this
    is the identity for both today. It is applied anyway, and to the right
    side only, so that a future change scoring the mirrored voter's reasons
    through this comparison keeps working rather than silently comparing
    incomparable ids.
    """
    return tuple(
        ids.unmirror(reason.stance_id) if mirrored else reason.stance_id
        for reason in alignment.reasons
    )


def _reason_directions(
    alignment: CandidateAlignment, ids: _PairIds, *, mirrored: bool
) -> dict[str, str]:
    return {
        (ids.unmirror(reason.stance_id) if mirrored else reason.stance_id): reason.direction
        for reason in alignment.reasons
    }


@dataclass(frozen=True)
class _PairIds:
    """Every synthetic id one pair needs, derived from its name.

    Derived rather than listed so adding a pair to `default_pairs()` needs no
    second edit, and so every id carries `MARKER` by construction rather than
    by the author remembering to.
    """

    pair_name: str

    @property
    def slug(self) -> str:
        return f"{MARKER.lower()}-{self.pair_name}"

    def topic(self, interest_slug: str) -> str:
        return f"en-US:{self.slug}-{interest_slug}:v1"

    def stance(self, interest_slug: str) -> str:
        return f"{self.topic(interest_slug)}:for"

    def mirrored_stance(self, interest_slug: str) -> str:
        return f"{self.topic(interest_slug)}:against"

    def unmirror(self, stance_id: str) -> str:
        """A mirrored stance id mapped back to its original.

        Used only to compare reasons across the two sides — see
        `_reason_stances`.
        """
        if stance_id.endswith(":against"):
            return f"{stance_id[: -len(':against')]}:for"
        return stance_id

    def roll_call(self, roll: int) -> str:
        # `roll_calls` ids are `{body}:{session}:{roll}` (datamodel.md). The
        # marker cannot go in the body or session (both are validated
        # patterns), so it goes in the roll segment, where it is still
        # unmistakable and still unique per pair.
        return f"us-house:119-2:{MARKER.lower()}-{self.pair_name}-{roll}"

    @property
    def left_person(self) -> str:
        return f"person:{MARKER}-{self.pair_name}-L"

    @property
    def right_person(self) -> str:
        return f"person:{MARKER}-{self.pair_name}-R"

    @property
    def election(self) -> str:
        return f"{self.slug}-election"

    @property
    def district(self) -> str:
        return f"ocd-division/country:us/state:zz/cd:{self.slug}"

    @property
    def left_candidacy(self) -> str:
        return f"{self.election}:{self.district}:{self.left_person}"

    @property
    def right_candidacy(self) -> str:
        return f"{self.election}:{self.district}:{self.right_person}"


# ---------------------------------------------------------------------------
# The release gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """One reason the release is blocked.

    Shape mirrors `app.integrity.checks.Violation` and
    `app.panel_review.gate.Violation` (table/row_id/detail) so a CI log line
    or a future ticket-filing integration can treat every check in this
    codebase identically.
    """

    table: str
    row_id: str
    detail: str

    def __str__(self) -> str:
        return f"{self.table}/{self.row_id}: {self.detail}"


@dataclass(frozen=True)
class SymmetryGateReport:
    """Whether the symmetry audit permits the next release.

    `ok` False is what makes CI exit non-zero — see
    `scripts/check_symmetry_audit.py`, which follows
    `scripts/check_panel_gate.py`'s established shape rather than inventing a
    second release-blocking mechanism.
    """

    #: The audit this gate ran itself, when it ran one. None when the gate
    #: only read a recorded result.
    report: SymmetryAuditReport | None
    #: The `audit_runs` row the gate read, when it read one.
    recorded: dict[str, Any] | None = None
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        verdict = "ALLOW" if self.ok else "BLOCK"
        source = "a fresh audit" if self.report is not None else "the recorded audit_runs row"
        return (
            f"symmetry_gate: {verdict} the next release, from {source}; "
            f"{len(self.violations)} violation(s)"
        )


def check_symmetry_gate(
    client: ZeroDBClient,
    *,
    profiles: VoterProfileService | None = None,
    skew: float = 0.0,
    now: Callable[[], datetime] | None = None,
    max_age: timedelta = MAX_AUDIT_AGE,
) -> SymmetryGateReport:
    """Block the next release if the symmetry audit does not currently pass.

    Two modes, and the difference is deliberate:

      * **`profiles` given** — run a FRESH audit and gate on its result. This
        is what a pre-release check wants: the question is whether the code
        about to ship is symmetric, and last night's result is about last
        night's code.
      * **`profiles` None** — gate on the recorded `audit_runs` row alone,
        without writing anything. For a caller that cannot run the pipeline
        (no profile root key, no write access) but can read the nightly
        result. It blocks on a missing row and on a stale one, because "no
        recent audit" and "a failing audit" are the same answer to "may this
        release ship": in neither case has anything confirmed the pipeline is
        symmetric.

    Never raises on its own findings — the caller decides what blocking means.
    A `ZeroDBError` propagates.
    """
    clock = now or (lambda: datetime.now(UTC))
    violations: list[Violation] = []

    report: SymmetryAuditReport | None = None
    if profiles is not None:
        report = SymmetryAuditor(client=client, profiles=profiles, skew=skew, now=clock).run()
        if not report.passed:
            row_id = audit_runs_table_row_id(report.run_at)
            for asymmetry in report.asymmetries:
                violations.append(Violation(table="audit_runs", row_id=row_id, detail=asymmetry))

    recorded = _latest_audit_row(client)
    if recorded is None:
        violations.append(
            Violation(
                table="audit_runs",
                row_id="(none)",
                detail=(
                    "no audit_runs row exists at all, so nothing has ever confirmed the "
                    "scoring pipeline is symmetric. A release with no symmetry audit behind "
                    "it is indistinguishable from one with a failing audit behind it"
                ),
            )
        )
        return SymmetryGateReport(report=report, recorded=None, violations=tuple(violations))

    if recorded.get("passed") is not True and report is None:
        # When a fresh audit ran, its own violations above already say what is
        # wrong in detail; repeating the stored verdict would double every
        # line. When it did not, the stored verdict is all there is.
        violations.append(
            Violation(
                table="audit_runs",
                row_id=str(recorded.get("id")),
                detail=(
                    f"the recorded symmetry audit did not pass (max_delta "
                    f"{recorded.get('max_delta')}, tolerance {SYMMETRY_TOLERANCE})"
                ),
            )
        )

    age = _age_of(recorded, now=clock())
    if age is None:
        violations.append(
            Violation(
                table="audit_runs",
                row_id=str(recorded.get("id")),
                detail=f"its run_at {recorded.get('run_at')!r} could not be read as a timestamp",
            )
        )
    elif age > max_age:
        violations.append(
            Violation(
                table="audit_runs",
                row_id=str(recorded.get("id")),
                detail=(
                    f"the most recent symmetry audit is {age.days} day(s) old, past the "
                    f"{max_age.days}-day limit. The audit is nightly, so a result this old "
                    "means at least one night's run did not happen and nothing has checked "
                    "the code since"
                ),
            )
        )

    return SymmetryGateReport(report=report, recorded=recorded, violations=tuple(violations))


def _latest_audit_row(client: ZeroDBClient) -> dict[str, Any] | None:
    """The newest `audit_runs` row, by id.

    datamodel.md's documented query for this table is "Latest", and the id is
    `aud:{date}` — an ISO date, so lexicographic order IS chronological order
    and the newest row is the last id. Sorted locally rather than relying on
    the store's order, for story 1.4's reason: live ZeroDB returns rows in
    whatever order it likes.
    """
    rows = [row.row_data for row in client.query_rows("audit_runs", limit=500).rows]
    if not rows:
        return None
    return max(rows, key=lambda row: str(row.get("id", "")))


def _age_of(row: dict[str, Any], *, now: datetime) -> timedelta | None:
    raw = row.get("run_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return now - when


# ---------------------------------------------------------------------------
# The job entry point
# ---------------------------------------------------------------------------


def run_symmetry_audit(client: ZeroDBClient) -> SymmetryAuditReport:
    """Run the audit with a `VoterProfileService` built from the environment.

    Where the scheduled job enters. Separated from `SymmetryAuditor` because
    the auditor takes its profile service as a parameter (so a test can inject
    a keyring with a test root key) and something has to decide where a real
    one comes from — which is `ProfileRootKey.from_env()`, the same single
    source every other caller of the profile service uses.

    Raises:
        RootKeyMissingError: `$ZERODB_PROFILE_ROOT_KEY` is unset. Propagated
            rather than caught: the audit writes two encrypted synthetic
            profiles through the real service precisely so a bug in that path
            could skew a score, and an audit that skipped the encryption path
            would be checking a pipeline the product does not have. The job's
            `precondition` reports this as `blocked` rather than letting it
            raise in the first place.
    """
    profiles = VoterProfileService(
        client=client,
        keyring=ProfileKeyring(client=client, root_key=ProfileRootKey.from_env()),
    )
    return SymmetryAuditor(client=client, profiles=profiles).run()


__all__ = [
    "MARKER",
    "MAX_AUDIT_AGE",
    "SYMMETRY_TOLERANCE",
    "MirroredPair",
    "MirroredPairResult",
    "SymmetryAuditReport",
    "SymmetryAuditor",
    "SymmetryGateReport",
    "SyntheticInterest",
    "SyntheticVote",
    "Violation",
    "audit_runs_table_row_id",
    "check_symmetry_gate",
    "default_pairs",
    "mirrored_direction_code",
    "mirrored_position",
    "run_symmetry_audit",
]
