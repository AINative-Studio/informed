"""Alignment scoring: candidate positions, the formula, badges and reasons (epic 6).

Five modules, one per concern, in the order evidence flows through them:

    positions.py  — story 6.2: a person's votes -> a signed position per stance
    formula.py    — story 6.4: prd.md's 50*(1 + weighted mean) and nothing else
    badges.py     — story 6.5: how much evidence a score rests on
    dealbreakers.py — story 6.6: the check that must NEVER touch the average
    reasons.py    — story 6.7: the top contributions, each with its citations
    service.py    — assembles the four above into one response
    router.py     — GET /v1/candidates/{candidacy_id}/alignment

`formula.py` deliberately imports nothing from this package or any other: see
its docstring. Everything else may read the methodology and ZeroDB.

The two rules that shape this package, both from prd.md and datamodel.md:

  * **A per-user score is never stored.** datamodel.md: "Per-user alignment
    scores are computed per request and never stored... so no table links a
    person's priorities to a candidate." `service.py` therefore has no write
    path at all, and `tests/features/alignment_score.feature` pins that a
    request writes no rows and emits no events.
  * **A dealbreaker is not an adjustment.** prd.md: "any dealbreaker violation
    is shown above the score rather than averaged away". `dealbreakers.py` is a
    separate module that `formula.py` cannot see, which is what makes "the
    warning never silently changes the number" a fact about the import graph.

This package deliberately re-exports only the **pure** pieces — the formula and
the badge function. `router`, `service` and `positions` are imported from their
own modules by the one caller each has (`app.main`, the router,
`app.scheduling.jobs`), because re-exporting them here would make
`import app.scoring.positions` drag in FastAPI and the profile keyring, and the
scheduled job that recomputes positions needs neither.
"""

from app.scoring.badges import ConfidenceBadge, badge_for
from app.scoring.formula import (
    MAX_ALIGNMENT,
    MIN_ALIGNMENT,
    NEUTRAL_ALIGNMENT,
    ScoredTerm,
    alignment_score,
    weighted_mean_position,
)

__all__ = [
    "MAX_ALIGNMENT",
    "MIN_ALIGNMENT",
    "NEUTRAL_ALIGNMENT",
    "ConfidenceBadge",
    "ScoredTerm",
    "alignment_score",
    "badge_for",
    "weighted_mean_position",
]
