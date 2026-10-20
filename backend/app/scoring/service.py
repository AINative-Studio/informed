"""Assembles one candidate's alignment response, per request (6.4-6.7).

What this module does, in order
===============================

1. Read the voter's decrypted profile through story 14.1's
   `VoterProfileService` — the only code in this codebase allowed to decrypt
   one.
2. Read the tier weights and badge thresholds from the published
   `methodology_versions` row (stories 4.2 / 6.1 / 6.5). Never a constant.
3. Read the candidate's current `candidate_positions` rows (story 6.2).
4. Build one `ScoredTerm` per interest that has a measured position, and run
   `app.scoring.formula.alignment_score` over them.
5. Run the dealbreaker check SEPARATELY (`app.scoring.dealbreakers`) and the
   reasons ranking (`app.scoring.reasons`), and badge the total evidence
   (`app.scoring.badges`).

Nothing is written. There is no write path in this module at all, which is
datamodel.md's rule made structural: "Per-user alignment scores are computed
per request and never stored... so no table links a person's priorities to a
candidate." A cache would be that table, which is why there is no cache
either — `tests/features/alignment_score.feature` pins that a request writes
no rows and emits no events.

Where the citations come from, and the cost that buys
======================================================

`candidate_positions` stores an `evidence_count`, not a list of roll calls
(datamodel.md's documented fields). Story 6.7 needs the roll calls. There were
two ways to get them:

  * **re-read `member_votes` per request** and re-derive which votes produced
    each position, which is what this module does; or
  * **store the citations on the position row**, which would mean inventing a
    field datamodel.md does not document, on an append-only table, for the
    convenience of a response.

The first is chosen. The cost is one paged `member_votes` read per alignment
request, filtered by person — a few hundred rows for a sitting member, where
`candidate_positions` would otherwise have to grow a denormalised list that
could drift from the votes it claims to cite. Drift between a position and its
own citations is the worst possible bug in a product whose first principle is
"evidence over opinion", and a re-read cannot drift.

It also means a citation is always the *current* truth about the votes, which
matters for the one case datamodel.md anticipates: a corrected roll call (its
retention table keeps `member_votes` and notes that a vote can be corrected).

What a voter's profile contributes, and what it never leaves with
=================================================================

In: `stance_id`, `tier`, `intensity`, `dealbreaker`. Out: nothing but stance
ids the voter themselves chose. The response models forbid extra fields and
declare none that could carry a note, a tier or a payload (see
`app.scoring.models`), and this module never logs any part of a profile — the
logger here carries candidacy ids and counts only.
"""

from __future__ import annotations

import logging

from app.profiles.methodology import MethodologyVersionService
from app.profiles.models import InterestProfile
from app.profiles.service import VoterProfileService
from app.repositories.base import Repository
from app.repositories.models import Candidacy, CandidatePosition, MemberVote, Stance
from app.scoring.badges import BADGE_THRESHOLDS_GROUP, badge_for
from app.scoring.dealbreakers import dealbreaker_warnings
from app.scoring.formula import ScoredTerm, alignment_score
from app.scoring.models import CandidateAlignment, Citation
from app.scoring.positions import PAGE_SIZE, votes_for_stance
from app.scoring.reasons import top_reasons
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)


class CandidacyNotFoundError(LookupError):
    """No `candidacies` row exists with the given id.

    Its own type, mapped to a 404 by the router. Deliberately NOT the same
    error as "no profile": an unknown candidacy is a bad URL, where an unknown
    profile is a voter who has not built one — see `ProfileNotFoundError`.
    """


class ProfileNotFoundError(LookupError):
    """The voter has no stored profile, so nothing can be scored.

    prd.md's acceptance criterion: "no candidate score is shown until at least
    one primary interest exists". A profile that exists at all has at least one
    primary interest — `InterestProfile` refuses to validate without one — so
    "has a profile" and "is scoreable" are the same condition, and this is the
    only check needed to satisfy that criterion.
    """


class AlignmentService:
    """Computes one candidate's alignment with one voter's profile.

    Holds no per-request state; safe to share across requests like every other
    `*Service` in this codebase.
    """

    def __init__(self, *, client: ZeroDBClient, profiles: VoterProfileService) -> None:
        self._client = client
        self._profiles = profiles
        self._methodology = MethodologyVersionService(client)
        self._candidacies: Repository[Candidacy] = Repository(client=client, model=Candidacy)
        self._stances: Repository[Stance] = Repository(client=client, model=Stance)

    def alignment_for(self, candidacy_id: str, *, user_id: str) -> CandidateAlignment:
        """One candidate's alignment with one voter's profile.

        Args:
            candidacy_id: a `candidacies` row id.
            user_id: whose profile to score against. The published query
                parameter is `profile_id` (prd.md's Core API table); this
                codebase keys a profile by the user id it HMACs into the row id
                (see `app.profiles.crypto.ProfileKeyring`), so the two name the
                same thing and the router does the translation once, where it
                is visible.

        Raises:
            CandidacyNotFoundError: no such candidacy.
            ProfileNotFoundError: that voter has no stored profile.

        Deterministic: the same profile and the same stored positions always
        produce the same response, byte for byte. Every ordering in it is
        canonical (by stance id, or by contribution with stance id breaking
        ties) rather than inherited from whatever order ZeroDB returned rows
        in — see `app.scoring.formula`'s docstring on why that is a property of
        the code and not of the store.
        """
        candidacy = self._candidacies.get(candidacy_id)
        if candidacy is None:
            raise CandidacyNotFoundError(f"no candidacy {candidacy_id!r} is known")

        profile = self._profiles.load(user_id)
        if profile is None:
            raise ProfileNotFoundError(
                "no stored profile for this voter; a score needs at least one primary interest"
            )

        version = self._methodology.current_version()
        tier_weights = version.tier_weights
        thresholds = version.weights.get(BADGE_THRESHOLDS_GROUP, {})

        positions = self._current_positions(candidacy.person_id)
        citations = self._citations_for(candidacy.person_id, positions)

        terms = _terms_for(profile, positions, tier_weights)
        score = alignment_score(terms)
        evidence_count = sum(positions[term.stance_id].evidence_count for term in terms)

        warnings = dealbreaker_warnings(
            dealbreaker_stance_ids=[interest.stance_id for interest in profile.dealbreakers],
            positions={
                stance_id: (row.s_value, row.confidence, row.evidence_count)
                for stance_id, row in positions.items()
            },
            citations=citations,
        )

        logger.info(
            # Candidacy id and counts only. Never a stance id, never a tier,
            # never anything that would let a log reader reconstruct a voter's
            # politics — which a stance id would, since the voter chose it.
            "alignment computed for candidacy %s: %d scored interest(s), %d vote(s) of "
            "evidence, %d dealbreaker warning(s)",
            candidacy_id,
            len(terms),
            evidence_count,
            len(warnings),
        )

        return CandidateAlignment(
            candidacy_id=candidacy_id,
            person_id=candidacy.person_id,
            score=score,
            confidence_badge=badge_for(evidence_count, thresholds),
            evidence_count=evidence_count,
            scored_interest_count=len(terms),
            dealbreaker_warnings=warnings,
            reasons=top_reasons(terms, citations),
            methodology_version=version.id,
        )

    # --- reads ------------------------------------------------------------

    def _current_positions(self, person_id: str) -> dict[str, CandidatePosition]:
        """This person's CURRENT `candidate_positions`, by stance id.

        Filtered locally as well as server-side: story 1.4 found live ZeroDB's
        `/query` filter unreliable, and scoring one candidate from another
        candidate's positions is the worst form of that bug.

        Only `is_current` rows. A superseded position is history — it exists so
        a score computed in March can be reproduced — and scoring today's
        request from it would make the number depend on how many times the
        recompute had run.
        """
        page = self._client.query_rows(
            CandidatePosition.table_name, filter={"person_id": person_id}, limit=PAGE_SIZE
        )
        if page.has_more or len(page.rows) >= PAGE_SIZE:
            logger.warning(
                "candidate_positions returned a full page of %d row(s) for one person; "
                "an alignment may be computed from a truncated set of positions",
                len(page.rows),
            )
        current: dict[str, CandidatePosition] = {}
        for row in page.rows:
            if row.row_data.get("person_id") != person_id:
                continue
            if row.row_data.get("is_current") is not True:
                continue
            position = CandidatePosition.model_validate(row.row_data)
            current[position.stance_id] = position
        return current

    def _citations_for(
        self,
        person_id: str,
        positions: dict[str, CandidatePosition],
    ) -> dict[str, list[Citation]]:
        """The roll calls behind each of this person's current positions.

        Re-derived from `member_votes` rather than stored on the position row —
        see the module docstring for why, and for what that costs. Returns an
        empty dict when there are no positions, so the common "challenger with
        no record" case does no reads at all.
        """
        if not positions:
            return {}

        votes = self._votes_for(person_id)
        if not votes:
            return {}

        stances = self._stances_by_id(list(positions))
        citations: dict[str, list[Citation]] = {}
        for stance_id in sorted(positions):
            stance = stances.get(stance_id)
            if stance is None:
                # The position cites a stance that is no longer stored. Skipped
                # rather than guessed: without the stance's `topic_id` there is
                # no way to know which votes produced it, and
                # `tests/features/alignment_score.feature` requires every
                # citation to name a real stored roll call. `app.integrity`'s
                # reference checks are what surface this as the data problem it
                # is.
                continue
            relevant = votes_for_stance(votes, stance)
            cited = [
                Citation(
                    roll_call_id=vote.roll_call_id,
                    position=vote.position,
                    vote_type=vote.vote_type,
                    voted_at=vote.voted_at,
                    bill_id=vote.bill_id,
                    is_bundled=vote.is_bundled,
                    # Story 8.4: the reason travels with the flag, so the page
                    # can explain a discounted vote rather than just mark it.
                    bundled_reason=vote.bundled_reason,
                )
                # Only the votes that actually evidenced the position: an
                # abstention is in `member_votes` for this stance's topic but
                # contributed nothing (see `app.scoring.positions`), so citing
                # it would show a voter a "reason" the number did not use.
                for vote in relevant
                if vote.position in {"yea", "nay"}
            ]
            if cited:
                citations[stance_id] = cited
        return citations

    def _votes_for(self, person_id: str) -> list[MemberVote]:
        page = self._client.query_rows(
            MemberVote.table_name, filter={"person_id": person_id}, limit=PAGE_SIZE
        )
        if page.has_more or len(page.rows) >= PAGE_SIZE:
            logger.warning(
                "member_votes returned a full page of %d row(s) for one person; some "
                "citations may be missing from this alignment",
                len(page.rows),
            )
        votes = [MemberVote.model_validate(row.row_data) for row in page.rows]
        return [vote for vote in votes if vote.person_id == person_id]

    def _stances_by_id(self, stance_ids: list[str]) -> dict[str, Stance]:
        """The `stances` rows named by these ids.

        One `get` per id rather than a paged scan of the whole table: a voter
        has at most 5 primary + 8 secondary + some nice-to-have interests, so
        this is a dozen point reads against a table story 3.2 seeds with ~180
        rows — and a point read cannot silently truncate the way a page can.
        """
        found: dict[str, Stance] = {}
        for stance_id in sorted(set(stance_ids)):
            row = self._client.get_row(Stance.table_name, stance_id)
            if row is not None:
                found[stance_id] = Stance.model_validate(row.row_data)
        return found


def _terms_for(
    profile: InterestProfile,
    positions: dict[str, CandidatePosition],
    tier_weights: dict[str, float],
) -> list[ScoredTerm]:
    """One `ScoredTerm` per interest that has a measured position.

    An interest with no measured position is **omitted**, not included at
    confidence 0. The two are arithmetically identical (a zero-confidence term
    contributes nothing to either sum — `test_a_zero_confidence_term_cannot_
    move_the_score`), and the difference is in what the response says:
    `scored_interest_count` is what tells a voter how much of their profile the
    candidate actually has a record on, and padding it with unmeasured
    interests would make a candidate with no record look fully assessed.

    Tier weights come from the published methodology. An unrecognised tier
    falls back to 0.0 — excluded rather than defaulted to a middle weight,
    because a methodology that does not publish a weight for a tier has not
    decided what that tier is worth, and guessing would silently score a
    voter's interests under a weight nobody published.
    """
    terms: list[ScoredTerm] = []
    for interest in profile.interests:
        position = positions.get(interest.stance_id)
        if position is None:
            continue
        weight = tier_weights.get(interest.tier, 0.0)
        if weight <= 0.0:
            continue
        terms.append(
            ScoredTerm(
                stance_id=interest.stance_id,
                tier_weight=weight,
                intensity=interest.intensity,
                confidence=position.confidence,
                s_value=position.s_value,
            )
        )
    return _deduplicated(terms)


def _deduplicated(terms: list[ScoredTerm]) -> list[ScoredTerm]:
    """Keep the first term per stance id.

    `InterestProfile._one_stance_per_topic` already forbids one TOPIC twice,
    and `alignment_score` refuses duplicate STANCE ids outright. Those two
    rules leave one gap: two interests on different topics could name the same
    stance id, which would be a data error rather than a voter's choice, and
    it would reach the formula as a `ValueError` and a 500 on the product's
    most-requested endpoint. Collapsed here instead, so the request succeeds
    with that stance counted once — which is what the formula's rule wants
    anyway.
    """
    seen: set[str] = set()
    unique: list[ScoredTerm] = []
    for term in terms:
        if term.stance_id in seen:
            continue
        seen.add(term.stance_id)
        unique.append(term)
    return unique


__all__ = [
    "AlignmentService",
    "CandidacyNotFoundError",
    "ProfileNotFoundError",
]
