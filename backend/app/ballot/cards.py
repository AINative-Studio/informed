"""The `candidate_cards` read model, rebuilt from events (story 7.2).

    backlog.md 7.2: "As the system, I want a per-candidacy read model rebuilt
    from events, so that the ballot view needs one query."
    Acceptance: "Rebuilt on `verdict.published` and `positions.recomputed`;
    never aggregates at request time."
    ZeroDB data: candidate_cards

===============================================================================
WHAT "NEVER AGGREGATES AT REQUEST TIME" MEANS CONCRETELY
===============================================================================

It is the acceptance criterion, so it is worth saying exactly what is and is
not claimed, because the word "query" is doing two different jobs in the
story's own sentence ("the ballot view needs one query").

**What the read path does.** `CandidateCardReader.card_for` is one
`get_row("candidate_cards", candidacy_id)`; `cards_for_race` is one
`query_rows("candidate_cards", filter={election_id, district_id})`. Neither
reads `candidate_positions`, `verdicts`, `member_votes`, `promises`, `people`
or `districts`, and neither sums, counts or averages anything. The card's
`verdict_counts`, `say_do_gap` and `positions_summary` are stored values. The
feature file pins this by counting the ZeroDB calls a read makes and asserting
that none of them touched the aggregated tables — a stronger check than
asserting a latency, because it fails the moment somebody adds a convenience
join rather than only when the table gets big.

**What it is NOT.** One *table* read is not one *HTTP request* against live
ZeroDB, and pretending otherwise would be the kind of claim this codebase
keeps catching itself making. Live ZeroDB's server-side `/query` filter
matches nothing (datamodel.md's open question, re-measured in story 1.4), so
`ZeroDBClient` falls back to paging `/rows` client-side: a single logical read
of one row is one POST plus one or more GETs today. That is a client-level
defect with a named escalation (AINative-Studio/core#8932) and one place to
fix, not something this read model can route around — and it is the same cost
every other read in this codebase already pays. What story 7.2 removes is the
*fan-out*: five tables per candidate and an aggregation over two of them,
times 6-15 candidates on a ballot, becomes one filtered read of one table for
the whole race.

===============================================================================
THE TWO TRIGGERS, AND THE HONEST ASYMMETRY BETWEEN THEM
===============================================================================

datamodel.md's Events table gives this read model two producers:

    `verdict.published`    | verdict_id, person_id, verdict | `candidate_cards`
                                                              rebuild; graph
                                                              edge update;
                                                              follower alerts
    `positions.recomputed` | person_id, methodology_version | `candidate_cards`
                                                              rebuild

**`positions.recomputed` is real.** Story 6.2's
`PositionComputer.recompute_person` emits it whenever a position actually
changes, it is wired into the hourly `recompute_candidate_positions` job
(`app.scheduling.jobs`), and the live project holds real events of that topic.
`CandidateCardRebuildConsumer` consumes it for real, and
`scripts/verify_candidate_cards_live.py` drives that path end to end against
live ZeroDB.

**`verdict.published` does not exist.** Epic 11 — the verdict engine — is
entirely unbuilt: nothing in this codebase emits the topic, `verdicts` holds
zero live rows, and the live event log holds zero events of it (both measured
2026-10-19). So the consumer below handles it, and its tests drive a fixture
event, but **no live verification of that trigger was possible and this story
does not claim one.** Story 11.6 ("`verdict.published` rebuilds candidate
cards and graph edges... cards update within 5 minutes of publish") is the
story that can finally make that claim; what 7.2 owes it is a consumer that
needs no redesign when the events start arriving, which is why the handler is
written against the documented payload rather than against a guess at what
epic 11 will send.

That asymmetry is also why this module is deliberately NOT structured as "one
rebuild function that recomputes everything". See the next section.

===============================================================================
WHY A REBUILD IS PARTIAL, AND WHY THAT IS NOT A SHORTCUT
===============================================================================

The obvious design is a single `rebuild(candidacy_id)` that reads every source
table and restates the whole card. It was written, and then rejected, for a
reason that matters more than the reads it would save:

**It would have to fabricate.** A card has verdict-derived fields
(`verdict_counts`, `say_do_gap`) and position-derived fields
(`positions_summary`). With `verdicts` empty — which is the live state today
and until epic 11 ships — a full rebuild triggered by a real positions
recompute would read zero verdicts and have to write *something* into
`verdict_counts`. Every available option is a lie:

  * `{kept: 0, partial: 0, broken: 0, ...}` reads to a voter as "this
    candidate kept none of their promises". It is the exact failure prd.md's
    first principle forbids — "evidence over opinion" — and the one
    `app.summary_card.models` already refused for the same field, in the same
    words, two stories ago.
  * `say_do_gap: 0.0` reads as "this candidate's votes perfectly match their
    promises", which is a flattering unearned claim about a named person.

So each trigger fills only the half of the card it has evidence for, and
merges onto whatever the other trigger stored, with `None` meaning "nothing
has computed this yet". `CandidateCardBuilder.rebuild_from_positions` and
`.rebuild_from_verdicts` are the two halves; `_merge` is the one place that
knows a rebuild is additive. A consequence worth naming: a card is
*eventually* complete rather than atomically complete, and a card that has
only ever seen a positions recompute shows no verdict data at all. That is the
truthful rendering of the system's actual knowledge.

The identity fields (`person_name`, `party`, `district`, `photo_url`) are
restated by **both** triggers, because they are cheap, they come from tables
that are authoritative at any moment (`candidacies`, `people`, `districts`),
and a stale display name on a ballot is its own bug. That is the one part of
the card that *is* a full restatement on every rebuild.

===============================================================================
THE say_do_gap DEFINITION, WHICH THIS STORY HAD TO SETTLE
===============================================================================

prd.md, Feature 2's context rules, is the only definition in the three source
documents: "**Say/Do gap** per incumbent: alignment computed from promises vs.
from votes — the gap is shown as its own number." datamodel.md gives it a
column on this table and no formula. No story before this one computed it.

prd.md's sentence describes the *finished* number: a difference between two
alignment scores, one computed over promises and one over votes. That needs
promise-derived alignment, which needs verdicts on mapped promises, which is
epic 10 and epic 11. It is not computable today and this story does not fake
it.

What IS computable the moment epic 11 lands, from the one input
`verdict.published` actually carries, is the measure the verdicts themselves
make: **of the promises where this candidate had a real, testable opportunity
to act, what share did they not keep.** See `say_do_gap_for` for the formula,
the weighting, and — more importantly — for which verdict values are excluded
from the denominator and why excluding them is required rather than
convenient.

Two properties make it safe to ship ahead of the fuller number:

  1. **It is 0 when every testable promise was kept and 1 when every testable
     promise was broken**, so it is on the scale prd.md's phrase implies and
     the UI can label it without a second vocabulary.
  2. **It is symmetric.** Two candidates with mirrored records get gaps
     mirrored about 0.5, which `candidate_cards.feature`'s neutrality
     scenarios pin. prd.md's "nonpartisan by construction" is about exactly
     that, and a read model is as capable of smuggling in a party rule as a
     scorer is — it is the row the ballot view actually renders.

When the promise-vs-votes difference becomes computable, `say_do_gap_for` is
the one function to replace, and `SAY_DO_GAP_BASIS` is the string that records
which definition a stored card was built under so the two cannot be silently
confused.

===============================================================================
WHAT THIS MODULE DOES NOT DO
===============================================================================

  * **No graph edge update and no follower alerts.** datamodel.md lists three
    consumers for `verdict.published`; this is the `candidate_cards` one.
    `app.evidence_graph.writer` owns edges and story 15.2 owns follower
    alerts; story 11.6 is where all three are wired to the real event.
  * **No API endpoint.** Story 7.1 is the ballot view and 7.3 the candidate
    detail; this story is the read model they read. `CandidateCardReader` is
    the seam they take, so neither has to learn this table's shape.
  * **No backfill of existing candidacies.** A card appears when an event
    about its candidate arrives. `scripts/rebuild_candidate_cards.py` is the
    manual path for a cold start or a repair, and it is a script rather than a
    startup hook for the same reason every other load in this codebase is: an
    unattended full-table write on boot is how a deployment surprises its
    operator.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.repositories import Repository
from app.repositories.models import (
    Candidacy,
    CandidateCard,
    CandidatePosition,
    District,
    Person,
    PositionSummaryEntry,
    Verdict,
    VerdictCounts,
)
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: datamodel.md's Events table entries this module consumes.
VERDICT_PUBLISHED_TOPIC = "verdict.published"
POSITIONS_RECOMPUTED_TOPIC = "positions.recomputed"

#: Both topics, in the order a mixed poll reads them. Verdicts first is
#: deliberate but currently inconsequential: a card merges either way, and
#: nothing emits the first topic yet.
REBUILD_TOPICS = (VERDICT_PUBLISHED_TOPIC, POSITIONS_RECOMPUTED_TOPIC)

#: Which definition of `say_do_gap` a stored card was computed under.
#:
#: `kept_share_of_testable` is the measure `say_do_gap_for` implements — see
#: the module docstring's own section. prd.md's fuller "promises-alignment
#: minus votes-alignment" number is not computable until epics 10 and 11 land,
#: and when it is, cards computed under the two definitions must be
#: distinguishable rather than silently comparable. Recorded in the log line
#: rather than on the row today: adding a column for a value with exactly one
#: possible setting would be speculative schema, and the day there are two the
#: column is a one-line change with a real reason behind it.
SAY_DO_GAP_BASIS = "kept_share_of_testable"

#: How much of the say/do gap a `partial` verdict contributes.
#:
#: 0.5, and the reasoning is the vocabulary's own: `VerdictValue` orders
#: `kept`, `partial`, `broken` as a three-point scale of the same quantity —
#: did the candidate do what they said. `kept` is no gap (0.0) and `broken` is
#: the whole gap (1.0), so the value between them is the midpoint unless there
#: is evidence to place it elsewhere, and there is none: no story has defined
#: what fraction of a promise a `partial` keeps, and inventing 0.3 or 0.7
#: would be a political judgment dressed as a constant (0.3 flatters every
#: candidate with partial records; 0.7 penalises them).
#:
#: The midpoint is also the only value that keeps the scale **symmetric**: a
#: candidate whose record is all `partial` scores 0.5, exactly halfway, which
#: is the same answer their mirror image gets. Any other weight makes
#: "mirrored records get mirrored gaps" false, and that property is what
#: prd.md's "nonpartisan by construction" means here.
#:
#: Deliberately NOT in `methodology_versions`, and this is the same judgment
#: call story 6.2 made and flagged for `BUNDLED_WEIGHT_FACTOR`: it belongs
#: there by the argument that put `badge_thresholds` there (story 6.5), and a
#: follow-up should move both together. It is a constant here because widening
#: the published methodology for a number no story's acceptance criterion
#: mentions is how a methodology version becomes hard to review. Flagged in
#: this branch's PR.
PARTIAL_VERDICT_GAP_WEIGHT = 0.5

#: The verdict values that count toward the say/do gap at all — the ones where
#: the candidate had a real opportunity to act on a testable promise and
#: either took it, half took it, or did not.
#:
#: See `say_do_gap_for` for why the other three are excluded from the
#: DENOMINATOR and not merely scored at zero.
GAP_SCORED_VERDICTS: dict[str, float] = {
    "kept": 0.0,
    "partial": PARTIAL_VERDICT_GAP_WEIGHT,
    "broken": 1.0,
}

#: Rows one paged read of a source table asks for. The same page size the rest
#: of this codebase uses for a per-person read, and the same standing caveat:
#: a person's `candidate_positions` and `verdicts` are both bounded by the
#: ~200 seeded stance cards and their own promise count, well inside one page,
#: but a page is still a page and overflow is logged rather than silently
#: truncating a card.
PAGE_SIZE = 500

#: Hard cap on the cards one race read returns. A race has 6-15 candidates
#: (prd.md); 500 is headroom for a California primary ballot, not a budget.
RACE_PAGE_SIZE = 500


@dataclass(frozen=True)
class RebuildResult:
    """What one rebuild pass did. Counts and public ids only.

    Carries no field a voter's data could reach, for the same structural
    reason `app.scoring.positions.RecomputeResult` does not: this object is
    logged and returned from a scheduled job, and nothing voter-derived is
    reachable from a read model over public candidate records. This type is
    where that stays true if a later story is tempted otherwise.
    """

    candidacy_ids: tuple[str, ...] = ()
    #: Candidacies an event named a person for, but whose card could not be
    #: written — a missing `people` row, a failed reference check. Counted
    #: rather than raised so one bad candidacy does not abandon the rest of a
    #: poll, and surfaced so it is not silently zero.
    skipped: tuple[str, ...] = ()

    @property
    def cards_rebuilt(self) -> int:
        return len(self.candidacy_ids)

    def summary(self) -> str:
        return (
            f"{self.cards_rebuilt} card(s) rebuilt, {len(self.skipped)} skipped "
            f"(say_do_gap basis {SAY_DO_GAP_BASIS})"
        )


# ---------------------------------------------------------------------------
# The two computations, pure
# ---------------------------------------------------------------------------


def verdict_counts_for(verdicts: Sequence[Verdict]) -> VerdictCounts:
    """Tally a person's current verdicts by value.

    Pure: no I/O, no clock. Takes the verdicts the caller has already decided
    are current, because "which verdict is live for this promise" is
    `is_current`'s job and `Repository.current` already owns it — re-deciding
    it here would be a second implementation of datamodel.md's "exactly one
    `is_current = true` row per promise" invariant.

    An unrecognised `verdict` value cannot reach here: `Verdict.verdict` is a
    `Literal` over `VerdictValue`, so a row carrying a seventh spelling fails
    validation at the read. That is why this function can `setattr` by name
    without a fallback bucket — there is no value to fall back from.
    """
    counts = VerdictCounts()
    tallied = counts.model_dump()
    for verdict in verdicts:
        tallied[verdict.verdict] = tallied[verdict.verdict] + 1
    return VerdictCounts.model_validate(tallied)


def say_do_gap_for(counts: VerdictCounts) -> float | None:
    """The share of testable promises this candidate did not keep, or None.

    0.0 means every promise they had a chance to act on, they kept; 1.0 means
    they broke every one; `PARTIAL_VERDICT_GAP_WEIGHT` per `partial` in
    between. See the module docstring for how this relates to prd.md's fuller
    "promises-alignment minus votes-alignment" number, which is not computable
    until epics 10 and 11 land.

    **Returns None when the denominator is empty**, which is a different
    answer from 0.0 and the distinction is the whole point: a candidate whose
    every verdict is `no_opportunity` has not been measured, and reporting 0.0
    for them would be a commendation ("votes perfectly match promises") earned
    by a lack of evidence. The same `None`-is-not-zero rule
    `app.scoring.formula.alignment_score` applies to an unmeasured alignment,
    and `app.summary_card.models` applies to this very field.

    ===========================================================================
    WHY THREE VERDICT VALUES ARE EXCLUDED FROM THE DENOMINATOR
    ===========================================================================

    `GAP_SCORED_VERDICTS` holds `kept`, `partial` and `broken`. The other
    three are excluded from the denominator entirely — not scored at 0.0,
    which would quietly reward a candidate for every promise nobody could
    check:

      * **`no_opportunity`** — the promise was testable but no relevant vote
        ever reached the floor. prd.md is explicit that this must not read as
        either support or failure ("Voice votes and unanimous consent are
        shown as 'no recorded vote', never as support"), and a candidate
        cannot be faulted for a vote their chamber never held. Counting it as
        kept would mean a candidate in a chamber that voted on nothing scored
        a perfect 0.0 gap.
      * **`not_testable`** — vague rhetoric. prd.md: "Vague rhetoric is tagged
        *non-testable* and shown but not scored." Scoring it here would
        contradict that rule in the one place a voter actually reads a number,
        and would make a campaign of pure aspiration score better than one
        with checkable commitments — a perverse incentive built into the
        product.
      * **`absent`** — the member did not vote on the measure. The hardest of
        the three, and excluded for the reason prd.md gives directly:
        "Procedural votes, amendments, absences and ambiguous promises are
        labelled, not silently scored." An absence has many causes (illness,
        a paired vote, a deliberate protest) and the record does not say
        which, so folding it into a kept/broken number would make the number
        assert what the evidence does not. It is counted and displayed in
        `verdict_counts` — the voter sees it — and it is not scored.

    The exclusions are all in one direction only in appearance: each one
    removes a promise from BOTH numerator and denominator, so no candidate is
    advantaged by having many of them, which is what makes the measure
    symmetric under the mirroring `candidate_cards.feature` pins.
    """
    tallied = counts.model_dump()
    denominator = sum(tallied[value] for value in GAP_SCORED_VERDICTS)
    if denominator == 0:
        return None
    numerator = sum(tallied[value] * weight for value, weight in GAP_SCORED_VERDICTS.items())
    gap = numerator / denominator
    # Clamped against a last-bit floating-point overshoot, for the same reason
    # `app.scoring.positions.compute_position` clamps its own: `CandidateCard`
    # bounds `say_do_gap` to [0, 1] and would refuse 1.0000000000000002,
    # turning a rounding artefact into a failed write.
    return min(1.0, max(0.0, gap))


def positions_summary_for(positions: Sequence[CandidatePosition]) -> list[PositionSummaryEntry]:
    """The ballot view's position rows, from a person's current positions.

    Sorted by `stance_id`, so two rebuilds over the same positions produce a
    byte-identical row. The same reasoning as
    `app.scoring.positions.votes_for_stance` and `app.scoring.formula`'s
    canonical ordering: story 6.4 promises the same inputs give the same
    score, and a read model whose array order depended on whatever order
    ZeroDB happened to return rows in would make an otherwise-unchanged card
    look changed on every rebuild.
    """
    return sorted(
        (
            PositionSummaryEntry(
                stance_id=position.stance_id,
                s_value=position.s_value,
                confidence=position.confidence,
                evidence_count=position.evidence_count,
            )
            for position in positions
        ),
        key=lambda entry: entry.stance_id,
    )


# ---------------------------------------------------------------------------
# The read side: what the ballot view uses
# ---------------------------------------------------------------------------


class CandidateCardReader:
    """Reads stored cards. The seam stories 7.1 and 7.3 take.

    Holds no per-request state and is safe to share, like every other service
    here. Deliberately has **no** method that reads a source table: this class
    is where "never aggregates at request time" is enforced structurally
    rather than by review, and a reader that could reach `candidate_positions`
    would be one convenience method away from doing so on a hot path. A caller
    that needs a card which does not exist yet asks `CandidateCardBuilder` to
    rebuild it, which is a write path and names itself as one.
    """

    def __init__(self, client: ZeroDBClient) -> None:
        self._cards: Repository[CandidateCard] = Repository(client=client, model=CandidateCard)

    def card_for(self, candidacy_id: str) -> CandidateCard | None:
        """One card by candidacy id, or None when it has never been built.

        None rather than an exception, and the distinction matters to the
        caller: "no card yet" is an ordinary state for a candidacy no event
        has touched, and story 7.1's ballot view renders it as a candidate
        with no computed data rather than as an error.
        """
        return self._cards.get(candidacy_id)

    def cards_for_race(self, *, election_id: str, district_id: str) -> list[CandidateCard]:
        """Every card for one seat in one election — datamodel.md's own query.

        "By (election, district)", in one filtered read of one table. The
        filter is applied to the returned page as well as sent, because live
        ZeroDB's server-side filter matches nothing (datamodel.md's open
        question; story 1.4 measured it) — so correctness rests on the
        client's fallback and a card from the wrong race must not reach a
        voter's ballot.

        Sorted by candidate name, which is a stable, explainable order and
        **never a ranked one**: prd.md's "the app never recommends who to vote
        for" makes any score-ordered default a product bug, and
        `app.ballot.service` already sorts a delegation the same way for the
        same reason.
        """
        rows = self._cards.query(
            filter={"election_id": election_id, "district_id": district_id},
            limit=RACE_PAGE_SIZE,
        )
        matched = [
            card
            for card in rows
            if card.election_id == election_id and card.district_id == district_id
        ]
        if len(rows) >= RACE_PAGE_SIZE:
            logger.error(
                "candidate_cards returned a full page of %d row(s) for (%s, %s); a "
                "candidate may be missing from a ballot",
                len(rows),
                election_id,
                district_id,
            )
        return sorted(matched, key=lambda card: (card.person_name, card.candidacy_id))


# ---------------------------------------------------------------------------
# The write side: rebuilding a card
# ---------------------------------------------------------------------------


class CandidateCardBuilder:
    """Rebuilds `candidate_cards` rows from the tables that own the facts.

    Two entry points, one per trigger, because a rebuild is partial by
    construction — see the module docstring's "WHY A REBUILD IS PARTIAL"
    section for why a single all-sources rebuild would have to fabricate a
    verdict count.
    """

    def __init__(self, client: ZeroDBClient, *, now: Callable[[], str] | None = None) -> None:
        self._client = client
        self._cards: Repository[CandidateCard] = Repository(client=client, model=CandidateCard)
        self._candidacies: Repository[Candidacy] = Repository(client=client, model=Candidacy)
        self._people: Repository[Person] = Repository(client=client, model=Person)
        self._districts: Repository[District] = Repository(client=client, model=District)
        self._positions: Repository[CandidatePosition] = Repository(
            client=client, model=CandidatePosition
        )
        self._verdicts: Repository[Verdict] = Repository(client=client, model=Verdict)
        #: Injected so a test can pin `updated_at` without freezing the
        #: process clock, the same seam `app.ballot.service` takes for `today`.
        self._now = now or _now

    # --- the two triggers -------------------------------------------------

    def rebuild_from_positions(
        self, person_id: str, *, methodology_version: str | None = None
    ) -> RebuildResult:
        """Rebuild every card of one person from their current positions.

        What `positions.recomputed` means for this read model. Fills
        `positions_summary` and `methodology_version`; leaves
        `verdict_counts` and `say_do_gap` exactly as they were stored, which
        for a candidate with no published verdicts means leaving them null
        rather than writing a zero nobody has evidence for.

        `methodology_version` comes from the event's own payload when there is
        one, and falls back to the version the positions themselves name. The
        event's copy wins because it is the producer's statement of what it
        recomputed under; the rows' copy is the fallback for a manual rebuild
        or a backfill with no event behind it.
        """
        positions = self._current_positions(person_id)
        summary = positions_summary_for(positions)
        version = methodology_version or _version_from(positions)
        return self._rebuild_cards_of(
            person_id,
            positions_summary=summary,
            methodology_version=version,
        )

    def rebuild_from_verdicts(self, person_id: str) -> RebuildResult:
        """Rebuild every card of one person from their current verdicts.

        What `verdict.published` means for this read model. Fills
        `verdict_counts` and `say_do_gap`; leaves `positions_summary` as it
        was stored.

        **Recomputed from the person's whole current verdict set, not
        incremented by the one verdict the event names.** The event carries a
        `verdict_id` and its value, so a counter bump looks cheaper — and it
        is wrong twice over: a published verdict usually *supersedes* an
        earlier one for the same promise (`verdicts` is append-only with
        `is_current`), so incrementing would double-count the promise and
        leave the old value standing; and a counter that drifts from the rows
        has no way back, where a recompute is self-healing and idempotent.
        This is a read model — correctness against the source tables is the
        only property it has.
        """
        verdicts = self._current_verdicts(person_id)
        counts = verdict_counts_for(verdicts)
        return self._rebuild_cards_of(
            person_id,
            verdict_counts=counts,
            say_do_gap=say_do_gap_for(counts),
        )

    def rebuild_candidacy(self, candidacy_id: str) -> RebuildResult:
        """Rebuild one card from every source table.

        The manual and cold-start path — `scripts/rebuild_candidate_cards.py`
        and a repair — not an event consumer. Safe to restate both halves here
        precisely because it is NOT triggered by an event that knows only one
        of them: it reads both source tables itself, so an empty
        `verdict_counts` here means "this person's verdicts were read and
        there were none", which is a measured zero rather than an
        uncomputed one.

        It still writes `None` rather than an all-zero `VerdictCounts` when a
        person has no verdicts at all, for the reason
        `app.summary_card.models` gives: `kept: 0` on a candidate nobody has
        published a verdict about reads as "kept none of their promises".
        """
        candidacy = self._candidacies.get(candidacy_id)
        if candidacy is None:
            logger.warning(
                "no candidacy %r to rebuild a card for; it may not be ingested yet",
                candidacy_id,
            )
            return RebuildResult(skipped=(candidacy_id,))

        positions = self._current_positions(candidacy.person_id)
        verdicts = self._current_verdicts(candidacy.person_id)
        counts = verdict_counts_for(verdicts) if verdicts else None
        written = self._write_card(
            candidacy,
            positions_summary=positions_summary_for(positions) if positions else None,
            methodology_version=_version_from(positions),
            verdict_counts=counts,
            say_do_gap=say_do_gap_for(counts) if counts is not None else None,
        )
        if written is None:
            return RebuildResult(skipped=(candidacy_id,))
        return RebuildResult(candidacy_ids=(candidacy_id,))

    # --- the shared rebuild ------------------------------------------------

    def _rebuild_cards_of(
        self,
        person_id: str,
        **updates: object,
    ) -> RebuildResult:
        """Apply one trigger's half of the card to every candidacy of a person.

        Every candidacy, not one: a person can run for two seats across two
        elections (a House member seeking a Senate seat, a primary and a
        general in the same cycle), and both ballots show the same record. The
        events carry a person and no candidacy, so this fan-out is where "one
        event, every affected card" lives.
        """
        candidacies = self._candidacies_of(person_id)
        if not candidacies:
            # Not an error, and not logged as one. Most people in `people` are
            # sitting members with no candidacy row yet (story 2.6 ingests
            # candidacies for a fraction of them), so a recompute for one of
            # them legitimately rebuilds nothing.
            logger.debug("no candidacy for %s; no card to rebuild", person_id)
            return RebuildResult()

        rebuilt: list[str] = []
        skipped: list[str] = []
        for candidacy in candidacies:
            card_id = candidacy.id or ""
            if self._write_card(candidacy, **updates) is None:
                skipped.append(card_id)
            else:
                rebuilt.append(card_id)
        return RebuildResult(candidacy_ids=tuple(rebuilt), skipped=tuple(skipped))

    def _write_card(self, candidacy: Candidacy, **updates: object) -> CandidateCard | None:
        """Merge one trigger's fields onto a card and store it.

        Returns None when the card could not be written — a missing `people`
        row, a reference check the repository refused. Returning rather than
        raising is what keeps one corrupt candidacy from abandoning a whole
        poll; the failure is counted in `RebuildResult.skipped` and logged, so
        it is visible rather than silent.
        """
        candidacy_id = candidacy.id
        if not candidacy_id:
            logger.error("a candidacy with no id reached the card builder; skipped")
            return None

        person = self._people.get(candidacy.person_id)
        if person is None:
            # datamodel.md's own integrity check — "every
            # `candidacies.person_id` resolves to a `people` row" — says this
            # cannot happen, and `Candidacy.references` enforces it at write
            # time. Handled anyway because a card is a *derived* row: if the
            # invariant is broken upstream, a loud skip plus the nightly audit
            # is a better outcome than a 500 on a ballot view.
            logger.error(
                "candidacy %s names person %s, which has no row; card skipped",
                candidacy_id,
                candidacy.person_id,
            )
            return None

        stored = self._cards.get(candidacy_id)
        row = self._merge(
            stored,
            candidacy=candidacy,
            person=person,
            updates=updates,
        )
        return self._cards.upsert(row)

    def _merge(
        self,
        stored: CandidateCard | None,
        *,
        candidacy: Candidacy,
        person: Person,
        updates: dict[str, object],
    ) -> dict[str, object]:
        """The row to write: identity restated, computed fields merged.

        The one place that knows a rebuild is additive. Three groups of field,
        each with a different rule:

          1. **Identity** (`person_name`, `party`, `district`, `photo_url` and
             the four ids) is restated from `candidacies`, `people` and
             `districts` on every rebuild. Cheap, authoritative at any moment,
             and a stale display name on a ballot is its own bug.
          2. **This trigger's computed fields** come from `updates`.
          3. **The other trigger's computed fields** are carried forward from
             `stored`, so a positions rebuild never clears a verdict count and
             a verdict rebuild never clears a positions summary.

        `updates` may legitimately carry `None` — a positions rebuild for a
        person whose positions were all retracted has an empty summary — so
        the merge keys on *which fields this trigger owns*, never on whether
        the value is falsy. A `None` from the trigger that owns the field
        wins; a field the trigger does not own is never touched.
        """
        previous = stored.model_dump() if stored is not None else {}
        row: dict[str, object] = {
            "id": candidacy.id,
            "candidacy_id": candidacy.id,
            "election_id": candidacy.election_id,
            "district_id": candidacy.district_id,
            "person_id": candidacy.person_id,
            # From the candidacy, not the person: `candidacies.person_name` is
            # the name as *filed for this race* (datamodel.md rule 3's
            # denormalisation), which is the name on the ballot a voter is
            # holding. `people.canonical_name` can legitimately differ.
            "person_name": candidacy.person_name,
            "party": candidacy.party,
            "district": self._district_name(candidacy.district_id),
            # `candidacies` carries no photo, so this is the one identity
            # field that comes from `people` — the join story 14.6 noted as
            # future work and this read model is the place to do once, at
            # rebuild time, rather than per request.
            "photo_url": person.photo_url,
            "updated_at": self._now(),
        }
        for field in ("verdict_counts", "say_do_gap", "positions_summary", "methodology_version"):
            if field in updates:
                row[field] = updates[field]
            elif field in previous:
                row[field] = previous[field]
        return row

    # --- source reads ------------------------------------------------------

    def _candidacies_of(self, person_id: str) -> list[Candidacy]:
        """Every candidacy one person holds.

        Filtered locally as well as server-side, like every other read in this
        codebase since story 1.4: rebuilding one person's card from another
        person's candidacy would put the wrong name on a ballot.
        """
        rows = self._candidacies.query(filter={"person_id": person_id}, limit=PAGE_SIZE)
        if len(rows) >= PAGE_SIZE:
            logger.warning(
                "candidacies returned a full page of %d row(s) for one person; a card "
                "may not have been rebuilt",
                len(rows),
            )
        return [row for row in rows if row.person_id == person_id]

    def _current_positions(self, person_id: str) -> list[CandidatePosition]:
        """One person's CURRENT positions.

        `is_current` is filtered here rather than trusted from the query,
        because a superseded position reaching a card would show a voter a
        number the scorer has already replaced — and because the filter the
        query sends does nothing against live ZeroDB anyway.
        """
        rows = self._positions.query(filter={"person_id": person_id}, limit=PAGE_SIZE)
        if len(rows) >= PAGE_SIZE:
            logger.warning(
                "candidate_positions returned a full page of %d row(s) for one person; a "
                "card's positions_summary may be incomplete",
                len(rows),
            )
        return [row for row in rows if row.person_id == person_id and row.is_current is True]

    def _current_verdicts(self, person_id: str) -> list[Verdict]:
        """One person's CURRENT verdicts, one per promise.

        Same `is_current` reasoning as `_current_positions`, and it bites
        harder here: `verdicts` is append-only, so a promise whose verdict
        moved from `broken` to `kept` has both rows stored, and counting both
        would tell a voter the candidate made two promises where they made
        one.
        """
        rows = self._verdicts.query(filter={"person_id": person_id}, limit=PAGE_SIZE)
        if len(rows) >= PAGE_SIZE:
            logger.warning(
                "verdicts returned a full page of %d row(s) for one person; a card's "
                "verdict_counts may be incomplete",
                len(rows),
            )
        return [row for row in rows if row.person_id == person_id and row.is_current is True]

    def _district_name(self, district_id: str) -> str:
        """A readable district name, falling back to the raw OCD-ID.

        The same fallback `SummaryCardService._district_name` makes, for the
        same reason: a candidacy whose district row is unreadable still has a
        card worth rendering, and the raw id is legible and honest where a 500
        on a cosmetic string is not.
        """
        district = self._districts.get(district_id)
        return district.name if district is not None else district_id


# ---------------------------------------------------------------------------
# The event consumer
# ---------------------------------------------------------------------------


def emit_verdict_published(
    client: ZeroDBClient,
    *,
    verdict_id: str,
    person_id: str,
    verdict: str,
) -> None:
    """Emit datamodel.md's `verdict.published` event.

    Defined here, beside the consumer, exactly as story 6.2 defined
    `emit_rollcall_ingested` beside its own: the documented payload gets one
    definition rather than one per producer.

    **Nothing in this codebase calls this yet, and that is the honest state of
    the trigger.** Epic 11's verdict engine is the producer datamodel.md
    intends, and story 11.6 is the story that wires it. This function exists
    so that when it does, the payload has one spelling and story 11.6 adds a
    call rather than inventing a second shape — the same gap-closing pattern
    story 5.7 followed for `rollcall.ingested`.
    """
    client.create_event(
        topic=VERDICT_PUBLISHED_TOPIC,
        payload={"verdict_id": verdict_id, "person_id": person_id, "verdict": verdict},
    )


class CandidateCardRebuildConsumer:
    """datamodel.md's "`candidate_cards` rebuild" consumer of both events.

    Polls rather than subscribes, for the reason
    `app.scoring.positions.PositionRecomputeConsumer` already documents:
    `ZeroDBClient.subscribe_events` registers a webhook and needs a publicly
    reachable URL this deployment does not have, while a poll needs nothing,
    is testable against the in-memory fake, and runs from the scheduled-job
    runner this codebase already has. When a webhook endpoint exists,
    `handle_event` is what it calls and only the loop changes.

    **The 5-minute promise is story 11.6's, not this story's.** backlog.md
    11.6 says "cards update within 5 minutes of publish", which is a property
    of the registered interval rather than of this class — the same shape as
    story 5.6's 2-hour publication promise
    (`app.scheduling.jobs.HOUSE_PUBLICATION_LAG_PROMISE`). This consumer is
    registered at a cadence that leaves room for it; 11.6 is where the promise
    is asserted against a real producer.
    """

    def __init__(
        self,
        client: ZeroDBClient,
        *,
        builder: CandidateCardBuilder | None = None,
        pace: Callable[[], None] | None = None,
    ) -> None:
        self._client = client
        self._builder = builder or CandidateCardBuilder(client)
        self._pace = pace or (lambda: None)
        #: Event ids already handled. In-process and lost on restart, which is
        #: deliberately tolerable for the same reason story 6.2's consumer
        #: tolerates it: a rebuild is idempotent (it restates a row from the
        #: source tables), so a replayed event costs reads and changes
        #: nothing. A durable cursor belongs with a durable scheduler, which
        #: this codebase does not have either.
        self._handled: set[str] = set()

    def run(self, *, limit: int = 100) -> RebuildResult:
        """Rebuild cards for every unhandled event on either topic.

        Returns one `RebuildResult` covering both topics, so a scheduled job
        logs one line.
        """
        rebuilt: list[str] = []
        skipped: list[str] = []
        for topic in REBUILD_TOPICS:
            page = self._client.list_events(topic=topic, limit=limit)
            if page.has_more:
                # Logged rather than looped, exactly as story 6.2's consumer
                # does: a consumer that paged forever on a busy window would
                # hold the job runner, and the next run picks up where this
                # one stopped.
                logger.warning(
                    "more %s events are waiting than this run read (limit %d); the next "
                    "run will pick them up",
                    topic,
                    limit,
                )
            for event in page.events:
                # `event_id` can be absent (the live endpoint has been
                # observed to omit it, and the fake does not always mint one),
                # in which case de-duplication simply does not apply to that
                # event — safe, because the rebuild is idempotent.
                event_id = event.event_id or ""
                if event_id and event_id in self._handled:
                    continue
                result = self.handle_event(topic, dict(event.event_payload or {}))
                if event_id:
                    self._handled.add(event_id)
                rebuilt.extend(
                    candidacy_id
                    for candidacy_id in result.candidacy_ids
                    if candidacy_id not in rebuilt
                )
                skipped.extend(
                    candidacy_id for candidacy_id in result.skipped if candidacy_id not in skipped
                )
                self._pace()
        return RebuildResult(candidacy_ids=tuple(rebuilt), skipped=tuple(skipped))

    def handle_event(self, topic: str, payload: dict[str, object]) -> RebuildResult:
        """Rebuild the cards one event affects.

        Both documented payloads carry `person_id` and no candidacy id, so
        both resolve to "every card of this person" — see
        `CandidateCardBuilder._rebuild_cards_of`.

        An unknown topic returns an empty result rather than raising: this
        method is the webhook entry point as well as the poll's, and an
        endpoint that 500s on an event it does not recognise is a worse
        failure than one that ignores it and says so.
        """
        person_id = str(payload.get("person_id") or "")
        if not person_id:
            logger.warning("%s event carried no person_id; ignored", topic)
            return RebuildResult()

        if topic == POSITIONS_RECOMPUTED_TOPIC:
            version = payload.get("methodology_version")
            return self._builder.rebuild_from_positions(
                person_id,
                methodology_version=str(version) if version else None,
            )
        if topic == VERDICT_PUBLISHED_TOPIC:
            return self._builder.rebuild_from_verdicts(person_id)

        logger.warning("no candidate_cards rebuild is defined for topic %r; ignored", topic)
        return RebuildResult()


def _version_from(positions: Sequence[CandidatePosition]) -> str | None:
    """The methodology version a person's current positions were computed under.

    None when there are no positions. When positions disagree — which they can
    during a partial recompute, since `candidate_positions` is current per
    (person, stance) and a recompute can touch a subset of stances — the
    **highest** version string wins, and the disagreement is logged.

    The highest rather than the first-read, because row order is unspecified
    against live ZeroDB (story 1.4 measured it) and a card whose declared
    methodology changed between two identical rebuilds would be an unexplained
    diff. The highest rather than "mixed", because the field's job is to let a
    reader find the weights behind the number, and naming the newest in play is
    more useful than naming none.
    """
    versions = {position.methodology_version for position in positions}
    if not versions:
        return None
    if len(versions) > 1:
        logger.warning(
            "current positions span %d methodology versions (%s); the card names the "
            "highest. A full recompute under one version should follow.",
            len(versions),
            ", ".join(sorted(versions)),
        )
    return max(versions)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "GAP_SCORED_VERDICTS",
    "PAGE_SIZE",
    "PARTIAL_VERDICT_GAP_WEIGHT",
    "POSITIONS_RECOMPUTED_TOPIC",
    "RACE_PAGE_SIZE",
    "REBUILD_TOPICS",
    "SAY_DO_GAP_BASIS",
    "VERDICT_PUBLISHED_TOPIC",
    "CandidateCardBuilder",
    "CandidateCardReader",
    "CandidateCardRebuildConsumer",
    "RebuildResult",
    "emit_verdict_published",
    "positions_summary_for",
    "say_do_gap_for",
    "verdict_counts_for",
]
