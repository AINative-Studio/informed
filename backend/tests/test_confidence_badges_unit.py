"""Confidence badges against prd.md's published thresholds (story 6.5).

Pure arithmetic over an evidence count, like
`tests/test_alignment_formula_unit.py` — no client, no fixtures. The ZeroDB
half (that the thresholds actually come from a live `methodology_versions` row
rather than from `DEFAULT_BADGE_THRESHOLDS`) is covered by
`tests/step_defs/test_alignment_score_steps.py`.
"""

from __future__ import annotations

import pytest

from app.methodology.models import DOCUMENTED_WEIGHT_GROUPS
from app.methodology.publish import default_weights
from app.scoring.badges import (
    BADGE_THRESHOLDS_GROUP,
    DEFAULT_BADGE_THRESHOLDS,
    badge_for,
)

#: backlog.md 6.5: "Badges are High, Medium, Low or Insufficient."
DOCUMENTED_BADGES = frozenset({"high", "medium", "low", "insufficient"})


# --- the thresholds against prd.md ---------------------------------------


def test_high_starts_at_prd_mds_ten_relevant_votes() -> None:
    """prd.md, Feature 3: "High (>=10 relevant votes or clear commitments)".

    The one number in this map prd.md actually publishes, so it is pinned
    against prd.md's own figure rather than against whatever the constant says.
    """
    assert DEFAULT_BADGE_THRESHOLDS["high"] == 10.0
    assert badge_for(10) == "high"
    assert badge_for(9) != "high"


def test_the_thresholds_are_strictly_ordered() -> None:
    """high > medium > low. Unordered thresholds would make the badge depend on
    the order `badge_for` happens to test them in, which is the kind of bug
    that shows as "every candidate is Medium"."""
    assert (
        DEFAULT_BADGE_THRESHOLDS["high"]
        > DEFAULT_BADGE_THRESHOLDS["medium"]
        > DEFAULT_BADGE_THRESHOLDS["low"]
    )


def test_low_starts_at_one_so_it_cannot_overlap_insufficient() -> None:
    """With zero evidence the formula returns no score at all, so `low` must
    begin at 1 — otherwise `insufficient` and `low` would both claim 0 and the
    badge would stop being a function of the count."""
    assert DEFAULT_BADGE_THRESHOLDS["low"] == 1.0
    assert badge_for(0) == "insufficient"
    assert badge_for(1) == "low"


# --- the mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "insufficient"),
        (1, "low"),
        (3, "low"),
        (4, "medium"),
        (9, "medium"),
        (10, "high"),
        (250, "high"),
    ],
)
def test_the_badge_for_each_boundary(count: int, expected: str) -> None:
    assert badge_for(count) == expected


def test_only_the_four_documented_badges_are_ever_returned() -> None:
    """A fifth badge value would reach the UI and the frontend's own enum, and
    neither would notice until a voter saw a blank chip."""
    produced = {badge_for(count) for count in range(0, 40)}
    assert produced <= DOCUMENTED_BADGES
    assert produced == DOCUMENTED_BADGES, "every documented badge should be reachable"


def test_the_badge_rises_monotonically_with_evidence() -> None:
    """More evidence can never produce a weaker badge."""
    rank = {"insufficient": 0, "low": 1, "medium": 2, "high": 3}
    ranks = [rank[badge_for(count)] for count in range(0, 30)]
    assert ranks == sorted(ranks)


# --- thresholds from the methodology, 6.5's acceptance criterion ---------


def test_published_thresholds_override_the_defaults() -> None:
    """backlog.md 6.5: "with thresholds from the methodology".

    A methodology that decided three votes is enough for High must produce
    High at three, with no code change.
    """
    strict = {"high": 3.0, "medium": 2.0, "low": 1.0}
    assert badge_for(3, strict) == "high"
    assert badge_for(3) == "low"  # unchanged under the published default


def test_an_empty_threshold_group_falls_back_to_the_published_defaults() -> None:
    """The live `2026.1` row predates this story and carries no
    `badge_thresholds`, so it must still badge. Same reasoning as story 4.2's
    seeding: a fresh or older environment must not be un-badgeable.
    """
    assert badge_for(10, {}) == "high"
    assert badge_for(10, None) == "high"


def test_a_partially_published_group_falls_back_per_key() -> None:
    """A methodology that published only `high` must keep the default `medium`
    and `low`, NOT treat them as zero — which would make every scoreable
    candidate Medium and silently retire the Low badge."""
    partial = {"high": 20.0}
    assert badge_for(20, partial) == "high"
    assert badge_for(19, partial) == "medium"
    assert badge_for(4, partial) == "medium"
    assert badge_for(1, partial) == "low"
    assert badge_for(0, partial) == "insufficient"


def test_a_fractional_threshold_is_floored_rather_than_raising() -> None:
    """`weights` is typed `dict[str, dict[str, float]]`, so a hand-edited row
    can carry 10.5. It must behave as 10 rather than raising on the product's
    most-requested endpoint."""
    assert badge_for(10, {"high": 10.5, "medium": 4.0, "low": 1.0}) == "high"


# --- the methodology extension itself ------------------------------------


def test_the_badge_thresholds_group_is_a_published_weight_group() -> None:
    """The extension is only honest if the public page reports it. Naming it in
    `DOCUMENTED_WEIGHT_GROUPS` is what makes `/v1/methodology` publish it, and
    what makes story 12.2's `weights_not_yet_published` report it for the
    older `2026.1` row that predates it.
    """
    assert BADGE_THRESHOLDS_GROUP in DOCUMENTED_WEIGHT_GROUPS


def test_publishing_a_methodology_writes_the_badge_thresholds() -> None:
    assert default_weights()[BADGE_THRESHOLDS_GROUP] == DEFAULT_BADGE_THRESHOLDS


def test_badge_for_is_pure() -> None:
    """Part of a response story 6.4 promises is reproducible, so it cannot read
    a clock or anything else for itself."""
    assert len({badge_for(7) for _ in range(50)}) == 1
