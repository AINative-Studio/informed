"""An incumbent's position per stance, computed from their votes (story 6.2).

    backlog.md 6.2: "As the system, I want each incumbent's position (-1 to +1)
    per stance computed from their votes, weighted by vote type, so that
    alignment has a basis."
    Acceptance: "Final passage outweighs procedural and bundled votes; results
    are append-only with `is_current`; recomputed on `rollcall.ingested`."

================================================================================
THE SCOPING DECISION: how a vote is associated with a stance
================================================================================

prd.md's finished pipeline routes a vote to a stance through two components,
neither of which exists in this codebase:

  * story 5.5, the bill-to-stance classifier, which needs story 5.3's
    `bills.subjects[]` to classify against; and
  * story 10.1, promise-to-vote mapping, which fills `promises.mappings[]`.

This story does not build either, and does not wait for them. It computes from
the association datamodel.md *already* gives it:

    a vote is evidence about a stance when
    `member_votes.topic_ids[]` contains `stances.topic_id`

`member_votes.topic_ids` is a denormalised copy of the roll call's own
`topic_ids[]` — datamodel.md puts it there explicitly so that "scoring reads
one table" — and `stances.topic_id` is a declared reference to `issue_topics`.
So the join is a field comparison with no new table and no classifier.

**Why that is honest rather than a shortcut.** Three things:

1. It is strictly coarser than the finished pipeline, in a direction that is
   visible rather than hidden. A topic-level association says "this vote was
   about prescription drug pricing"; the classifier will later say "this vote
   advances *this particular stance* on prescription drug pricing". The coarse
   version therefore cannot distinguish two opposed stances under one topic,
   which is why `_stance_direction` below projects onto the stance's own signed
   `direction_code` rather than assuming every vote on a topic supports every
   stance under it.
2. **It currently finds nothing on live data, and that is the correct answer.**
   Story 5.1's roll-call ingest writes `topic_ids: []` on every row it loads,
   and says so in its own module docstring ("Mapping a roll call to
   `issue_topics` needs the bill's subject terms, which do not exist yet"). So
   against the live project this engine computes zero positions and writes zero
   rows. It does not invent a position for an incumbent whose votes have not
   been classified — which is the one failure mode prd.md's "evidence over
   opinion" principle makes unacceptable.
3. Replacing it is a one-function change. `votes_for_stance` is the only place
   that knows how a vote reaches a stance. When story 5.5 lands, it returns
   classified votes with a per-vote effect instead of a topic match, and
   nothing else in this module moves.

================================================================================
HOW A VOTE BECOMES A SIGNED POSITION
================================================================================

`s_value` runs -1 (acted against the voter's stance) to +1 (acted for it), per
prd.md Feature 3. Each qualifying vote contributes:

    direction  = +1 for `yea`, -1 for `nay`
    weight     = vote_type weight  x  bundled factor
    s_value    = sum(direction * weight * stance_alignment) / sum(weight)
    confidence = min(1, sum(weight) / CONFIDENCE_SATURATION_WEIGHT)

where `stance_alignment` is +1 when the stance's `direction_code` points the
same way as the measure and -1 when it points the other way — see
`_stance_direction` for what this story does and does not claim to know about
that, which is the honest heart of the scoping decision above.

`present` and `not_voting` contribute nothing. They are recorded
non-positions: the member was counted but took no side, and prd.md is explicit
that an absence must never read as support ("Procedural votes, amendments,
absences and ambiguous promises are labelled, not silently scored").

**Why a weighted mean rather than a weighted sum clamped to [-1, 1].** A mean
makes the vote-type weighting do what backlog.md 6.2 asks of it. Under a sum,
five procedural votes at 0.2 each would reach the same +1.0 as one
final-passage vote, so "final passage outweighs procedural" would hold only
until a member had enough procedural votes — which is backwards. Under a mean,
the weights set each vote's *share* of the position, so one final-passage Yea
and one procedural Nay lands at +0.67, and no quantity of procedural votes can
overwhelm a consistent final-passage record.

Confidence is where quantity belongs, and it is separate for exactly that
reason: five procedural votes produce the same *direction* as one but a
genuinely better-evidenced one.

================================================================================
THE `rollcall.ingested` CONSUMER, AND THE GAP IT CLOSES
================================================================================

datamodel.md's Events table: `rollcall.ingested`, payload
"roll_call_id, bill_id, topic_ids", consumers "Position recompute; mapping
agent; follower alerts".

Story 6.1 found the mirror image of this gap and said so: it emitted
`methodology.published` and recorded that "no consumer exists for it yet".
When this module shipped, the PRODUCER was the missing half — nothing in the
codebase emitted `rollcall.ingested`, so the consumer below was live code
with no input.

**Story 5.7 closed that.** `app.ingest.house_rollcalls.HouseRollCallLoader`
now calls `emit_rollcall_ingested` (below) for every roll call whose content
actually changed, and the live project holds real events. Confirmed end to
end on 2026-10-19: a real roll call ingested from Congress.gov emitted
exactly one event, and `PositionRecomputeConsumer.run()` read it back from
the live event log.

What has NOT changed is that no position comes out of it yet — and the reason
is the scoping decision at the top of this docstring, not a wiring gap: roll
calls carry `topic_ids: []` until story 5.5 classifies them, and
`votes_for_stance` matches on `topic_ids`. So the consumer now exercises its
real path (reads events, resolves voters, recomputes) and correctly declines
to invent a position.

So this story ships both halves of the wiring rather than half of each:

  * `recompute_person_positions` is the callable recompute — usable by a job,
    a script, a backfill, or a human, with no event involved;
  * `PositionRecomputeConsumer` is the event-driven path: it reads
    `rollcall.ingested` events, finds who voted on each named roll call, and
    recomputes those people. It polls `list_events` rather than registering a
    webhook (`ZeroDBClient.subscribe_events` exists but needs a publicly
    reachable URL this deployment does not have), and it is registered as a
    scheduled job in `app.scheduling.jobs` so it runs without one.
  * `emit_rollcall_ingested` is here so the producing side has one definition
    of the documented payload to call. Wiring it into the House loader was
    left to the story that owned that ingest path, and story 5.7 did exactly
    that — it calls this function rather than spelling the payload a second
    time, which is why there is still only one definition of it.

Idempotence across runs is the consumer's own: it tracks the last event it
processed in-process and, more importantly, `recompute_person_positions` writes
a new `candidate_positions` version only when the computed position actually
differs from the current one. Two recomputes over unchanged votes leave one
row, which `candidate_positions.feature` pins.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.profiles.methodology import MethodologyVersionService
from app.repositories import Repository
from app.repositories.models import CandidatePosition, MemberVote, RollCall, Stance
from app.scoring.badges import DEFAULT_BADGE_THRESHOLDS
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: datamodel.md's Events table entry this module consumes.
ROLLCALL_INGESTED_TOPIC = "rollcall.ingested"

#: datamodel.md's Events table entry this module emits: `positions.recomputed`,
#: payload "person_id, methodology_version", consumer "`candidate_cards`
#: rebuild".
POSITIONS_RECOMPUTED_TOPIC = "positions.recomputed"

#: `basis` on a `candidate_positions` row: which evidence produced it.
#: `votes` here, because that is all this story reads. Story 6.3 adds the blend
#: and will write `blend` (or similar) for a position mixing sponsorships and
#: statements in, which is why the field is a string rather than a bool.
BASIS_VOTES = "votes"

#: How much a vote on an omnibus/bundled measure is discounted.
#:
#: prd.md, Feature 2's context rules: "Omnibus bills: a vote on a 1,000-page
#: bill is not a vote for every clause — flagged as *bundled*, low weight."
#: backlog.md 6.2: "Final passage outweighs procedural and bundled votes."
#:
#: Story 6.1 deliberately left this out of its `vote_types` map and said why:
#: "`is_bundled` is NOT in this map: datamodel.md models it as a separate
#: boolean on the roll call, so an omnibus discount composes with the
#: vote-type weight rather than needing six more entries. Its own factor
#: belongs to story 6.2."
#:
#: **0.4, and the reasoning.** The constraint 6.2's acceptance criterion
#: actually states is an ordering: a bundled final-passage vote must weigh less
#: than an unbundled one. 0.4 puts a bundled final passage (1.0 x 0.4 = 0.4) at
#: roughly a cloture vote's weight (0.4) — i.e. "about a specific measure, but
#: weak evidence about any one provision in it", which is precisely what prd.md
#: says an omnibus vote is. Above `procedural` (0.2), because a bundled vote is
#: still a vote on policy; well below an unbundled final passage, because the
#: provision the voter cares about is one of hundreds.
#:
#: It is a multiplier rather than a replacement weight so that bundling
#: discounts every vote type consistently — a bundled amendment is discounted
#: in the same proportion as a bundled final passage, with no second table.
#:
#: Deliberately NOT in `methodology_versions` yet, and this is a judgment call
#: worth naming: it belongs there by the same argument that put
#: `badge_thresholds` there (story 6.5), and a follow-up should move it. It is
#: here for now because story 6.1 assigned it to this story as a scorer
#: constant, and widening the published methodology twice in one branch —
#: once legitimately for 6.5's own acceptance criterion, once for a number no
#: story's criterion mentions — is the kind of scope creep that makes a
#: methodology version hard to review. Flagged in this branch's PR.
BUNDLED_WEIGHT_FACTOR = 0.4

#: The total vote weight at which `confidence` reaches 1.0.
#:
#: Set to the published `high` badge threshold (10 relevant votes) so that the
#: two numbers cannot drift: a candidate whose record earns prd.md's "High
#: (>=10 relevant votes)" badge is also a candidate whose per-stance confidence
#: has saturated. Read from `DEFAULT_BADGE_THRESHOLDS` rather than written as
#: `10.0`, so republishing the threshold moves both together.
#:
#: Because the weights are per-vote shares in (0, 1], ten *final-passage* votes
#: saturate confidence and ten *procedural* votes (10 x 0.2 = 2.0) reach only
#: 0.2 — which is the intended behaviour: prd.md's "relevant votes" for a
#: confidence judgment means votes that actually evidence a position.
CONFIDENCE_SATURATION_WEIGHT = DEFAULT_BADGE_THRESHOLDS["high"]

#: How many rows one scan of `member_votes` asks for. `member_votes` reaches
#: ~600k rows per Congress (datamodel.md), so a full-table read is not viable
#: at scale and is not what this module does: it reads a page at a time,
#: filtered by `person_id`. The same open question datamodel.md already tracks
#: for every other paged read in this codebase applies — see
#: `_votes_for_person`'s note.
PAGE_SIZE = 500


@dataclass(frozen=True)
class VoteEvidence:
    """One vote that contributed to a position, with what it contributed.

    Kept rather than discarded after summing, because story 6.7 has to cite the
    roll calls behind a reason and `candidate_positions` stores only a count.
    Re-deriving the citations later would mean re-reading `member_votes` on
    every alignment request; carrying them out of the computation costs
    nothing.
    """

    roll_call_id: str
    position: str
    vote_type: str
    is_bundled: bool
    voted_at: str
    bill_id: str | None
    #: The signed, weighted amount this vote contributed to the numerator.
    contribution: float
    #: This vote's share of the denominator: vote-type weight x bundled factor.
    weight: float


@dataclass(frozen=True)
class ComputedPosition:
    """A computed position for one (person, stance), before it is stored.

    Separate from `CandidatePosition` (the row) because this carries the
    evidence that produced it, and a row must not: datamodel.md's
    `candidate_positions` fields are `s_value`, `confidence`, `evidence_count`
    and `basis` — a count, not a list. Story 6.7 reads `evidence` from here.
    """

    person_id: str
    stance_id: str
    s_value: float
    confidence: float
    evidence: Sequence[VoteEvidence] = field(default_factory=tuple)
    basis: str = BASIS_VOTES

    @property
    def evidence_count(self) -> int:
        return len(self.evidence)

    def row(self, methodology_version: str, *, computed_at: str | None = None) -> dict[str, object]:
        """The `candidate_positions` payload for `Repository.append_version`.

        No `id` and no `is_current`: both are the repository's to assign, which
        is what keeps "exactly one current row per (person, stance)" true. See
        `Repository.append_version`.
        """
        return {
            "person_id": self.person_id,
            "stance_id": self.stance_id,
            "s_value": self.s_value,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "basis": self.basis,
            "methodology_version": methodology_version,
            "computed_at": computed_at or _now(),
        }


# ---------------------------------------------------------------------------
# The computation
# ---------------------------------------------------------------------------


def votes_for_stance(votes: Iterable[MemberVote], stance: Stance) -> list[MemberVote]:
    """The votes that are evidence about `stance`.

    **This is the one function that knows how a vote reaches a stance**, and the
    one place story 5.5's classifier replaces. See the module docstring's
    scoping section: today a vote qualifies when its denormalised
    `topic_ids[]` contains the stance's `topic_id`.

    Returns them in a stable order (by roll call id), because the position they
    produce is summed in floating point and story 6.4 promises the same inputs
    give the same score — the same reasoning as
    `app.scoring.formula._canonical`.
    """
    matched = [vote for vote in votes if stance.topic_id in vote.topic_ids]
    return sorted(matched, key=lambda vote: vote.roll_call_id)


def compute_position(
    *,
    person_id: str,
    stance: Stance,
    votes: Iterable[MemberVote],
    vote_type_weights: dict[str, float],
    bundled_factor: float = BUNDLED_WEIGHT_FACTOR,
) -> ComputedPosition | None:
    """One person's position on one stance, from their votes.

    Args:
        person_id: a `people` row id.
        stance: the `stances` row being scored. Its `topic_id` selects the
            relevant votes and its `direction_code` orients them.
        votes: the person's `member_votes`. Filtered here, so a caller can
            pass the person's whole record once and score every stance from it
            without re-reading.
        vote_type_weights: the live
            `methodology_versions.weights.vote_types` — story 6.1's published
            map. Required rather than defaulted: a position computed under
            unknown weights is not reproducible, which is the whole point of
            versioning them.
        bundled_factor: prd.md's omnibus discount. See
            `BUNDLED_WEIGHT_FACTOR`.

    Returns:
        A `ComputedPosition`, or `None` when no vote in `votes` is evidence
        about this stance. `None` rather than a zero-confidence position with
        `s_value = 0.0`: "no evidence" and "measured, and found neutral" are
        different claims, and `CandidatePosition` would refuse the row anyway
        (its `_evidence_supports_a_confident_position` validator). The same
        distinction `alignment_score` draws between `None` and 50.

    Pure: no I/O, no clock, no randomness. The storage decision is
    `PositionComputer`'s.
    """
    stance_direction = _stance_direction(stance)
    if stance_direction == 0.0:
        # A `direction_code` of `0` — "this position does not move along the
        # axis" (keep current policy, or route the decision elsewhere). No vote
        # to change policy is evidence for or against it, so there is nothing
        # to compute and the answer is "no position", not "neutral position".
        #
        # Checked here, before the votes are even looked at, rather than
        # falling out of the arithmetic: multiplying every contribution by 0.0
        # produced an `s_value` of 0.0 with a non-zero `evidence_count`, which
        # would have stored as "measured this candidate on 12 votes and found
        # them exactly neutral" — a confident claim built from no information,
        # and a confidence badge to match. See `_stance_direction` for why this
        # case is a real voter position rather than a gap.
        return None

    relevant = votes_for_stance(votes, stance)

    evidence: list[VoteEvidence] = []
    for vote in relevant:
        direction = _vote_direction(vote.position)
        if direction == 0.0:
            # `present` / `not_voting`: recorded, but not a side. prd.md:
            # absences are "labelled, not silently scored". Excluded from the
            # evidence entirely rather than counted at zero, because
            # `evidence_count` is what story 6.5's badge reads — an abstention
            # must not inflate a confidence badge.
            continue
        weight = _vote_weight(vote, vote_type_weights, bundled_factor)
        if weight <= 0.0:
            # A methodology that weighted a vote type at 0 has excluded it.
            # Skipped rather than carried at zero, for the same reason as
            # above: it must not count toward `evidence_count`.
            continue
        evidence.append(
            VoteEvidence(
                roll_call_id=vote.roll_call_id,
                position=vote.position,
                vote_type=vote.vote_type,
                is_bundled=vote.is_bundled,
                voted_at=vote.voted_at,
                bill_id=vote.bill_id,
                contribution=direction * weight * stance_direction,
                weight=weight,
            )
        )

    if not evidence:
        return None

    total_weight = sum(item.weight for item in evidence)
    numerator = sum(item.contribution for item in evidence)
    # A weighted MEAN, not a sum — see the module docstring for why no quantity
    # of procedural votes may outweigh a final-passage record.
    s_value = numerator / total_weight
    confidence = min(1.0, total_weight / CONFIDENCE_SATURATION_WEIGHT)

    return ComputedPosition(
        person_id=person_id,
        stance_id=_require_stance_id(stance),
        # Clamped against a last-bit floating-point overshoot, for the same
        # reason `alignment_score` clamps: `CandidatePosition` bounds `s_value`
        # to [-1, 1] and would refuse 1.0000000000000002, turning a rounding
        # artefact into a failed write.
        s_value=min(1.0, max(-1.0, s_value)),
        confidence=min(1.0, max(0.0, confidence)),
        evidence=tuple(evidence),
    )


def _vote_direction(position: str) -> float:
    """+1 for a Yea, -1 for a Nay, 0 for a recorded non-position.

    `present` and `not_voting` return 0 and are then dropped by the caller.
    datamodel.md: "`position` takes `yea`, `nay`, `present` or `not_voting`".
    """
    if position == "yea":
        return 1.0
    if position == "nay":
        return -1.0
    return 0.0


def _vote_weight(
    vote: MemberVote,
    vote_type_weights: dict[str, float],
    bundled_factor: float,
) -> float:
    """This vote's share of the position: vote-type weight x bundled factor.

    An unknown `vote_type` falls back to the published `other` weight — the
    weight story 6.1 chose for "anything the classifier could not type" — and
    not to 1.0. Defaulting an unrecognised type to full final-passage weight
    would make a schema change silently promote every unclassified vote to the
    strongest evidence there is.
    """
    base = vote_type_weights.get(
        vote.vote_type,
        vote_type_weights.get("other", 0.0),
    )
    return base * (bundled_factor if vote.is_bundled else 1.0)


def _stance_direction(stance: Stance) -> float:
    """Which way a Yea on a measure about this topic points, for this stance.

    **This is the honest limit of story 6.2.** `stances.direction_code` is a
    signed position on the subtopic's own axis (-2..+2, see
    `app.seed_stances`), and what a full scorer needs is the sign of the
    *measure* on that same axis — "does a Yea here move the axis positive or
    negative" — which is exactly what story 5.5's bill-to-stance classifier
    will supply and nothing in this codebase supplies today.

    So this story uses the sign of the stance alone, and the consequence is
    stated plainly: a vote is read as supporting a positively-signed stance and
    opposing a negatively-signed one on the same topic. Under the topic-level
    association this story computes from, that is the only self-consistent
    choice available, and it has the property that matters most — it is
    **symmetric**. Two mirrored stances on one topic produce exactly opposite
    positions from the same vote, so no direction of any axis is advantaged by
    the arithmetic. prd.md's "nonpartisan by construction" is about precisely
    that symmetry, and `test_two_mirrored_stances_get_opposite_positions` pins
    it.

    A `direction_code` of `0` ("this position does not move along the axis" —
    keep current policy, or route the decision elsewhere) returns 0.0, so no
    vote is evidence for or against it and `compute_position` returns `None`.
    That is the right answer rather than a gap: a vote to change policy is not
    evidence about a voter who asked for no change in either direction, and
    inventing a sign for it would be the scorer taking a political position.

    The magnitude of `direction_code` (±1 vs ±2) is deliberately NOT used as a
    multiplier. `s_value` is defined against the stance ("acts for it" / "acts
    against it"), so a strongly-worded stance a candidate fully supports is
    still +1.0, not +2.0 — and prd.md puts the voter's own strength of feeling
    in `intensity`, which is the voter's to set, not the stance card's.
    """
    code = int(stance.direction_code)
    if code > 0:
        return 1.0
    if code < 0:
        return -1.0
    return 0.0


def _require_stance_id(stance: Stance) -> str:
    if not stance.id:
        raise ValueError("a stance must carry its id to be scored")
    return stance.id


# ---------------------------------------------------------------------------
# Storage: append-only, with is_current
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecomputeResult:
    """What one recompute did. Counts and ids only — never a voter anything.

    This object is logged and returned from a scheduled job, so it carries no
    field that could hold voter data. There is nothing voter-derived reachable
    from this module at all (positions are computed from public votes against
    public stance cards), and this type is where that stays true if a later
    story is tempted otherwise.
    """

    person_ids: tuple[str, ...] = ()
    positions_written: int = 0
    positions_unchanged: int = 0
    methodology_version: str = ""

    @property
    def people_recomputed(self) -> int:
        return len(self.person_ids)


class PositionComputer:
    """Computes and stores `candidate_positions`, append-only.

    Holds no per-request state; safe to share like every other service here.
    """

    def __init__(self, client: ZeroDBClient) -> None:
        self._client = client
        self._positions: Repository[CandidatePosition] = Repository(
            client=client, model=CandidatePosition
        )
        self._votes: Repository[MemberVote] = Repository(client=client, model=MemberVote)
        self._stances: Repository[Stance] = Repository(client=client, model=Stance)
        self._methodology = MethodologyVersionService(client)

    # --- reads ------------------------------------------------------------

    def current_position(self, *, person_id: str, stance_id: str) -> CandidatePosition | None:
        """The live position for one (person, stance), or None."""
        return self._positions.current(person_id=person_id, stance_id=stance_id)

    def positions_for(self, person_id: str) -> list[CandidatePosition]:
        """Every CURRENT position stored for one person.

        A paged read filtered locally: live ZeroDB's server-side filter was
        observed to drop matching rows (story 1.4), so the filter is applied to
        what comes back rather than trusted to have narrowed the page.
        """
        rows = self._positions.query(filter={"person_id": person_id}, limit=PAGE_SIZE)
        return [row for row in rows if row.person_id == person_id and row.is_current is True]

    # --- the recompute ----------------------------------------------------

    def recompute_person(
        self,
        person_id: str,
        *,
        stances: Sequence[Stance] | None = None,
    ) -> RecomputeResult:
        """Recompute every stance position for one person, and store changes.

        Args:
            person_id: a `people` row id.
            stances: which stances to score. Defaults to every stored stance —
                correct but expensive (story 3.2 seeds ~180 cards), so a
                caller that knows which topics a roll call touched should pass
                only those stances. `PositionRecomputeConsumer` does exactly
                that, from the event's own `topic_ids`.

        Returns:
            A `RecomputeResult` whose `positions_written` counts only positions
            that actually CHANGED. A recompute over unchanged votes writes
            nothing: `candidate_positions` is the reproducibility record, and a
            new version per nightly run with identical numbers would bury the
            real changes in noise. Pinned by
            `candidate_positions.feature`'s "Recomputing with no new evidence
            does not append a duplicate version".
        """
        version = self._methodology.current_version()
        weights = version.weights.get("vote_types", {})
        if not weights:
            # Refused rather than defaulted. A position computed under unknown
            # vote-type weights is not reproducible, and `candidate_positions`
            # stores the version it claims to have been computed under — a row
            # citing `2026.3` but computed with weights that row does not
            # carry is worse than no row.
            raise PositionComputeError(
                f"methodology version {version.id!r} publishes no vote_types weights; "
                "a position computed under unknown weights is not reproducible"
            )

        votes = self._votes_for_person(person_id)
        candidates = list(stances) if stances is not None else self._all_stances()

        written = 0
        unchanged = 0
        for stance in candidates:
            computed = compute_position(
                person_id=person_id,
                stance=stance,
                votes=votes,
                vote_type_weights=weights,
            )
            if computed is None:
                # No evidence about this stance. Deliberately NOT written as a
                # zero-confidence row and deliberately NOT clearing an existing
                # one: a stance that had evidence yesterday still has it today
                # (`member_votes` is append-only in practice), so an absence
                # here means "this recompute saw no votes for it", which is not
                # a reason to retract a stored position.
                continue
            if self._store(computed, methodology_version=version.id):
                written += 1
            else:
                unchanged += 1

        if written:
            # datamodel.md: `positions.recomputed`, payload "person_id,
            # methodology_version", consumer "`candidate_cards` rebuild".
            # Emitted only when something changed — an event per no-op run
            # would trigger a card rebuild for nothing.
            self._client.create_event(
                topic=POSITIONS_RECOMPUTED_TOPIC,
                payload={"person_id": person_id, "methodology_version": version.id},
            )

        return RecomputeResult(
            person_ids=(person_id,),
            positions_written=written,
            positions_unchanged=unchanged,
            methodology_version=version.id,
        )

    def _store(self, computed: ComputedPosition, *, methodology_version: str) -> bool:
        """Append `computed` as the new current version, if it differs.

        Returns True when a row was written.

        Uses `Repository.append_version` (story 1.4), which inserts the new row
        with `is_current = True` and then clears the flag on every previously
        current row for this (person, stance) — the compound version key
        `CandidatePosition` declares. Neither `id` nor `is_current` is passed:
        both are the repository's to assign, which is what makes "exactly one
        current row per (person, stance)" an invariant rather than a
        convention.
        """
        current = self.current_position(person_id=computed.person_id, stance_id=computed.stance_id)
        if current is not None and _unchanged(current, computed, methodology_version):
            return False
        self._positions.append_version(computed.row(methodology_version))
        return True

    # --- ZeroDB reads -----------------------------------------------------

    def _votes_for_person(self, person_id: str) -> list[MemberVote]:
        """One person's `member_votes`, one page.

        PAGE_SIZE is a real limit, and the honest note is that `member_votes`
        reaches ~600k rows per Congress (datamodel.md), of which one member's
        share is ~1-2k. So a single page can truncate a full-term record, and
        the cursor-based read that fixes it is the same open question
        datamodel.md already tracks for every other paged scan in this
        codebase. It does not bite today — the live table holds no rows with
        `topic_ids` set at all (see the module docstring) — and it is logged
        rather than silently accepted when the page comes back full.
        """
        page = self._client.query_rows(
            MemberVote.table_name, filter={"person_id": person_id}, limit=PAGE_SIZE
        )
        if page.has_more or len(page.rows) >= PAGE_SIZE:
            logger.warning(
                "member_votes returned a full page of %d row(s) for one person; a "
                "position may be computed from a truncated voting record",
                len(page.rows),
            )
        votes = [MemberVote.model_validate(row.row_data) for row in page.rows]
        # Filtered locally as well as server-side: story 1.4 found live
        # ZeroDB's `/query` filter unreliable, and scoring one person's
        # position from another person's votes is the worst possible version of
        # that bug.
        return [vote for vote in votes if vote.person_id == person_id]

    def _all_stances(self) -> list[Stance]:
        page = self._client.query_rows(Stance.table_name, limit=PAGE_SIZE)
        if page.has_more or len(page.rows) >= PAGE_SIZE:
            logger.warning(
                "stances returned a full page of %d row(s); some stances may not have "
                "been scored this run",
                len(page.rows),
            )
        return [Stance.model_validate(row.row_data) for row in page.rows]

    def stances_for_topics(self, topic_ids: Sequence[str]) -> list[Stance]:
        """Every stored stance under any of `topic_ids`.

        What the `rollcall.ingested` consumer narrows its recompute to: a roll
        call about prescription drug pricing cannot have changed anyone's
        position on public lands, so scoring all ~180 seeded stance cards per
        event would be ~180x the reads for no new information.
        """
        if not topic_ids:
            return []
        wanted = set(topic_ids)
        return [stance for stance in self._all_stances() if stance.topic_id in wanted]


class PositionComputeError(RuntimeError):
    """A recompute was refused before anything was written.

    Its own type, like `MethodologyPublishError`, so a caller can tell "this
    was rejected on its inputs, nothing happened" from a ZeroDB failure midway
    through — the first needs a corrected methodology, the second needs the
    integrity check run.
    """


def _unchanged(
    current: CandidatePosition,
    computed: ComputedPosition,
    methodology_version: str,
) -> bool:
    """Whether storing `computed` would be a no-op.

    Compared on every field a row carries that this story computes, including
    `methodology_version`: a position computed under a NEW methodology is a new
    position even when the numbers land identically, because the row's whole
    job is to say which weights produced it.

    Floats are compared with a tolerance rather than `==`. The numbers come out
    of a division, and two runs that read the same votes in a different order
    can differ in the last bit; appending a new version for a 1e-16 difference
    would make the history unreadable. The tolerance is far tighter than
    anything a voter could see (`s_value` renders to at most two decimals).
    """
    return (
        current.methodology_version == methodology_version
        and current.evidence_count == computed.evidence_count
        and current.basis == computed.basis
        and abs(current.s_value - computed.s_value) < 1e-9
        and abs(current.confidence - computed.confidence) < 1e-9
    )


# ---------------------------------------------------------------------------
# The rollcall.ingested consumer
# ---------------------------------------------------------------------------


def emit_rollcall_ingested(
    client: ZeroDBClient,
    *,
    roll_call_id: str,
    bill_id: str | None,
    topic_ids: Sequence[str],
) -> None:
    """Emit datamodel.md's `rollcall.ingested` event.

    Defined here, beside the consumer, so the documented payload has one
    definition rather than one per producer.

    **Callers.** `app.ingest.house_rollcalls.HouseRollCallLoader` (story 5.7),
    once per roll call whose content actually changed — it keys the call off
    the repository's content-addressed provenance write, so the hourly sync
    re-fetching the same window does not re-emit. A Senate-side producer
    (`app.ingest.senate_rollcalls`) is the obvious next caller and does not
    emit yet; that is a real remaining gap, flagged in story 5.7's PR rather
    than fixed there, because 5.7's acceptance criterion is about the House
    ingest and quietly widening it to another chamber's adapter is how a
    story grows a second story inside it.

    `bill_id` and `topic_ids` are passed through as the producer stored them.
    Both are still null/empty from the House ingest until stories 5.3 and 5.5
    land; the consumer tolerates an empty `topic_ids` by re-reading the stored
    row (see `PositionRecomputeConsumer.handle_event`).
    """
    client.create_event(
        topic=ROLLCALL_INGESTED_TOPIC,
        payload={
            "roll_call_id": roll_call_id,
            "bill_id": bill_id,
            "topic_ids": list(topic_ids),
        },
    )


class PositionRecomputeConsumer:
    """datamodel.md's "Position recompute" consumer of `rollcall.ingested`.

    Polls rather than subscribes. `ZeroDBClient.subscribe_events` exists and
    registers a webhook, which needs a publicly reachable URL this deployment
    does not have; a poll needs nothing, is testable against the in-memory
    fake, and runs from the scheduled-job runner this codebase already has
    (`app.scheduling.jobs`). When a webhook endpoint exists, `handle_event`
    below is what it calls — the polling loop is the only part that changes.
    """

    def __init__(
        self,
        client: ZeroDBClient,
        *,
        computer: PositionComputer | None = None,
        pace: Callable[[], None] | None = None,
    ) -> None:
        self._client = client
        self._computer = computer or PositionComputer(client)
        self._pace = pace or (lambda: None)
        #: Event ids already handled, so a second poll over the same window
        #: does not recompute what the first one did. In-process and therefore
        #: lost on restart, which is deliberately tolerable: the recompute is
        #: idempotent at the row level (`PositionComputer._store` writes only
        #: on a real change), so a replayed event costs reads and writes
        #: nothing. A durable cursor belongs with the durable job scheduler,
        #: which this codebase does not have either (see
        #: `app.scheduling.registry`).
        self._handled: set[str] = set()

    def run(self, *, limit: int = 100) -> RecomputeResult:
        """Recompute positions for everyone affected by unhandled events.

        Returns one `RecomputeResult` covering every person touched, so a
        scheduled job logs one line rather than one per member of Congress.
        """
        page = self._client.list_events(topic=ROLLCALL_INGESTED_TOPIC, limit=limit)
        if page.has_more:
            # Logged rather than looped: a consumer that paged forever on a
            # busy window would hold the scheduled-job runner, and the next
            # run picks up where this one stopped. The window is bounded by
            # `limit` on purpose.
            logger.warning(
                "more %s events are waiting than this run read (limit %d); the next "
                "run will pick them up",
                ROLLCALL_INGESTED_TOPIC,
                limit,
            )
        person_ids: list[str] = []
        written = 0
        unchanged = 0
        version = ""

        for event in page.events:
            # `event_id` is the live API's own id for the event. It can be
            # absent (the fake transport does not always mint one, and the live
            # endpoint has been observed to omit it), in which case the
            # de-duplication below simply does not apply to that event — which
            # is safe, because the recompute is idempotent at the row level.
            event_id = event.event_id or ""
            if event_id and event_id in self._handled:
                continue
            result = self.handle_event(dict(event.event_payload or {}))
            if event_id:
                self._handled.add(event_id)
            for person_id in result.person_ids:
                if person_id not in person_ids:
                    person_ids.append(person_id)
            written += result.positions_written
            unchanged += result.positions_unchanged
            version = result.methodology_version or version
            self._pace()

        return RecomputeResult(
            person_ids=tuple(person_ids),
            positions_written=written,
            positions_unchanged=unchanged,
            methodology_version=version,
        )

    def handle_event(self, payload: dict[str, object]) -> RecomputeResult:
        """Recompute the positions one `rollcall.ingested` event affects.

        The event's documented payload is "roll_call_id, bill_id, topic_ids".
        `topic_ids` narrows which stances are rescored; `roll_call_id` finds who
        voted and therefore whose positions could have moved.

        `topic_ids` is read from the stored roll call rather than trusted from
        the payload when the payload's copy is empty, because story 5.1 writes
        `topic_ids: []` today and a later classification pass may fill the row
        without re-emitting the event. The payload wins when it carries values:
        it is the producer's statement of what changed.
        """
        roll_call_id = str(payload.get("roll_call_id") or "")
        if not roll_call_id:
            logger.warning("rollcall.ingested event carried no roll_call_id; ignored")
            return RecomputeResult()

        topic_ids = [str(topic) for topic in (payload.get("topic_ids") or [])]
        if not topic_ids:
            topic_ids = self._topic_ids_from_row(roll_call_id)

        stances = self._computer.stances_for_topics(topic_ids)
        voters = self._voters_on(roll_call_id)

        person_ids: list[str] = []
        written = 0
        unchanged = 0
        version = ""
        for person_id in voters:
            result = self._computer.recompute_person(person_id, stances=stances)
            person_ids.append(person_id)
            written += result.positions_written
            unchanged += result.positions_unchanged
            version = result.methodology_version or version
            self._pace()

        return RecomputeResult(
            person_ids=tuple(person_ids),
            positions_written=written,
            positions_unchanged=unchanged,
            methodology_version=version,
        )

    def _topic_ids_from_row(self, roll_call_id: str) -> list[str]:
        row = self._client.get_row(RollCall.table_name, roll_call_id)
        if row is None:
            return []
        return [str(topic) for topic in (row.row_data.get("topic_ids") or [])]

    def _voters_on(self, roll_call_id: str) -> list[str]:
        """Who has a recorded `member_votes` row on this roll call.

        Sorted and de-duplicated so a recompute is deterministic in which
        order it touches people — which matters only for reproducible logs, but
        costs nothing.
        """
        page = self._client.query_rows(
            MemberVote.table_name, filter={"roll_call_id": roll_call_id}, limit=PAGE_SIZE
        )
        if page.has_more or len(page.rows) >= PAGE_SIZE:
            logger.warning(
                "member_votes returned a full page of %d row(s) for roll call %s; some "
                "members' positions may not have been recomputed",
                len(page.rows),
                roll_call_id,
            )
        seen: set[str] = set()
        for row in page.rows:
            # Filtered locally too; see `_votes_for_person`.
            if row.row_data.get("roll_call_id") != roll_call_id:
                continue
            person_id = row.row_data.get("person_id")
            if isinstance(person_id, str) and person_id:
                seen.add(person_id)
        return sorted(seen)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "BASIS_VOTES",
    "BUNDLED_WEIGHT_FACTOR",
    "CONFIDENCE_SATURATION_WEIGHT",
    "POSITIONS_RECOMPUTED_TOPIC",
    "ROLLCALL_INGESTED_TOPIC",
    "ComputedPosition",
    "PositionComputeError",
    "PositionComputer",
    "PositionRecomputeConsumer",
    "RecomputeResult",
    "VoteEvidence",
    "compute_position",
    "emit_rollcall_ingested",
    "votes_for_stance",
]
