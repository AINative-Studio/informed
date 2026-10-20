"""The dealbreaker check that must never touch the average (story 6.6).

    prd.md, Feature 3: "any dealbreaker violation is shown above the score
    rather than averaged away."
    prd.md's BDD scenario: "Given a voter marks 'Prescription drug pricing' as
    a dealbreaker / And candidate A scores 82 overall but acted against that
    stance on 3 final-passage votes / When the voter views the ballot / Then
    candidate A shows a dealbreaker warning above the score."
    backlog.md 6.6: "Matches the PRD Gherkin scenario 'Dealbreaker overrides
    the average'."

Why this is a module and not three lines inside the scorer
===========================================================

Read prd.md's scenario again: candidate A **scores 82**. The score is not
reduced, not zeroed, not capped. It stays 82, and a warning appears above it.
That is the entire requirement, and it is a requirement about what the scorer
must NOT do.

So the check lives in its own module that `app.scoring.formula` cannot see, and
the dependency runs one way only: `service.py` imports both, `formula.py`
imports neither. "The warning never silently changes the number" is therefore a
fact about the import graph rather than a property someone has to re-verify
every time the scorer is edited. The same technique story 14.6 used to make
"no voter data can reach this endpoint" checkable by reading a function
signature.

**Why not average it away?** Because averaging is exactly what the voter asked
us not to do. A dealbreaker is the voter saying "this one is not tradeable". A
candidate who is excellent on four of five primary interests and acted against
the fifth will average to a high score, and that average is *correct* — it is
what the weighted mean of those five positions is. What it is not is the whole
answer, and reducing the number would answer a question the voter did not ask
while hiding the one they did.

**Why not zero the score?** Two reasons. It would destroy information the voter
can use (how does this candidate compare on everything else?), and it would
make the app's output read as "do not vote for this candidate" — which prd.md
forbids in its very first product principle.

What counts as a violation
==========================

A dealbreaker interest whose measured position is **strictly negative**. Three
cases are deliberately NOT violations:

  * `s_value == 0.0` — measured, and found neutral. The candidate did not act
    against the stance; they have a mixed or balanced record on it. A warning
    here would be an accusation the evidence does not support.
  * no measured position at all — the candidate has no record on this stance.
    prd.md's "Show uncertainty" principle: silence is not opposition, and
    warning on an absence would systematically penalise challengers, who have
    no voting record by definition. That would be a party-neutral rule with a
    deeply non-neutral effect.
  * a negative position on a non-dealbreaker interest — the voter said that one
    IS tradeable. `app.profiles.models.InterestProfile` already refuses a
    dealbreaker on a non-primary interest, for the matching reason.

The threshold is `< 0`, not `<= -0.5` or similar. prd.md's scenario is "acted
against that stance", and any negative measured position is exactly that;
picking a severity cutoff would be this module deciding how much acting-against
is enough to tell a voter about, which is the voter's call and not ours.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.scoring.models import Citation, DealbreakerWarning

#: A measured position at or above this is not a violation. Zero, so only a
#: strictly negative position warns. Named rather than inlined because it is a
#: policy decision (see the module docstring) and
#: `test_a_neutral_position_on_a_dealbreaker_raises_no_warning` cites it.
VIOLATION_THRESHOLD = 0.0


def dealbreaker_warnings(
    *,
    dealbreaker_stance_ids: Iterable[str],
    positions: dict[str, tuple[float, float, int]],
    citations: dict[str, Sequence[Citation]],
) -> list[DealbreakerWarning]:
    """Every dealbreaker this candidate acted against.

    Args:
        dealbreaker_stance_ids: the stance ids the voter flagged. Ids only —
            this function never sees the voter's tiers, intensities, notes or
            anything else from their profile, so there is nothing here that
            could leak into a warning.
        positions: stance id -> (s_value, confidence, evidence_count), the
            candidate's measured positions. A stance absent from this dict has
            no measured position and therefore cannot be a violation.
        citations: stance id -> the roll calls behind that position. A stance
            with a violating position but NO citations is skipped with no
            warning, because `DealbreakerWarning` cannot be built without one
            — see the note in the body for why skipping is the right failure
            mode.

    Returns:
        A warning per violated dealbreaker, ordered by stance id so the
        response is reproducible (story 6.4's determinism criterion covers the
        whole response, not only the number). Empty when there is no violation.

    Pure: no I/O, no clock. The reads are the service's.
    """
    warnings: list[DealbreakerWarning] = []
    for stance_id in sorted(set(dealbreaker_stance_ids)):
        measured = positions.get(stance_id)
        if measured is None:
            # No record on this stance. Silence is not opposition — see the
            # module docstring on why warning here would penalise challengers
            # as a class.
            continue
        s_value, confidence, evidence_count = measured
        if s_value >= VIOLATION_THRESHOLD:
            continue
        cited = list(citations.get(stance_id) or ())
        if not cited:
            # A violating position whose citations could not be recovered. No
            # warning, deliberately: prd.md's "Evidence over opinion" means an
            # accusation without its evidence is worse than no accusation, and
            # `DealbreakerWarning` would refuse to be constructed anyway.
            # Skipping rather than raising keeps the whole response available —
            # the score and the reasons are still correct and still useful —
            # which is the better failure for the voter.
            continue
        warnings.append(
            DealbreakerWarning(
                stance_id=stance_id,
                s_value=s_value,
                confidence=confidence,
                evidence_count=evidence_count,
                citations=cited,
            )
        )
    return warnings


__all__ = ["VIOLATION_THRESHOLD", "dealbreaker_warnings"]
