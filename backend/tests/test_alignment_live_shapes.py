"""Live-confirmed behaviour the scorer depends on (6.2, 6.4-6.7).

Same purpose as `test_public_methodology_live_shapes.py` and
`test_profiles_live_shapes.py`: each test here pins something measured against
the LIVE "informed" project, not inferred from the docs or from the fake. The
run output is in this story's PR.

What was measured, on 2026-10-19, with
`scripts/verify_alignment_scoring_live.py` against the live project:

  * **`candidate_positions` accepted the row shape story 6.2 writes**,
    including `methodology_version` — whose column this story had to ADD to
    `app.bootstrap.TABLE_SCHEMAS`. It was missing: the model has always
    required the field and datamodel.md's id pattern includes it, but nothing
    had ever written to the table, so the omission was invisible. This is the
    sort of thing only a live write finds.
  * **`append_version`'s id is composed from the model's NATURAL key, not its
    `version_key`.** The stored id came back as
    `person:TEST-SCORING-000:en-US:test-scoring-topic-0:v1:for:2026.3:v1` —
    person, stance, methodology version, then `:v1`. The verification script's
    first draft predicted `{person}:{stance}:v1` and was wrong, which is worth
    recording because the two-field `version_key` makes the shorter form the
    natural guess.
  * **`s_value` and `confidence` round-trip as real `float`s.** `-1.0` came
    back `-1.0`, not `"-1.0"` and not `-1`. The same hazard story 12.2 pinned
    for tier weights, now for the two numbers every alignment score is built
    from — a string here would render fine and break the first multiplication.
  * **`is_current` moved correctly on the compound `(person_id, stance_id)`
    key** across two non-atomic live requests: two versions stored, exactly
    one current, and the superseded row still reporting the `s_value` it was
    computed with.
  * **A recompute over unchanged votes appended nothing**, so the live table
    does not accumulate an identical version per scheduled run.
  * **The alignment endpoint answered 200 over a live ZeroDB client** and its
    score matched a hand computation against the live methodology's weights to
    the last bit: `50 * (1 + 0.45/4.05) = 55.55555555555556`.
  * **Three identical calls returned a byte-identical response**, which is
    the check the fake cannot make meaningfully — a live store returns rows in
    whatever order it chooses.
  * **The request wrote nothing**: live row counts unchanged across it, no new
    event emitted, and no live row in `candidate_positions`,
    `candidate_cards` or `member_votes` naming both the voter's profile row id
    and the candidate's person id.
  * **The badge came from the live `methodology_versions` row.** Before
    publishing `2026.3` the live current version was 6.1's `2026.2`, which
    carries no `badge_thresholds` — so the badge fell back to the published
    default, which `badge_for` documents as the intended behaviour for a
    version predating story 6.5 rather than a failure. After publishing, the
    live row's own `{'low': 1.0, 'high': 10.0, 'medium': 4.0}` produced the
    badge.
  * **Every synthetic row was deleted and confirmed gone** by a re-read, and
    then again by a separate process that never wrote any of them.
    `candidate_positions` came back to 0 rows.

Nothing here makes a live call. The measurements are replayed through the fake,
which is what makes these runnable in CI without credentials.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.bootstrap import TABLE_SCHEMAS
from app.repositories.models import CandidatePosition
from app.scoring.badges import DEFAULT_BADGE_THRESHOLDS, badge_for
from app.scoring.formula import ScoredTerm, alignment_score

#: Copied verbatim from the live `candidate_positions` read on 2026-10-19
#: (project id redacted). Synthetic data, deleted live immediately after — kept
#: here as a measurement of the row SHAPE, which is what this file is about.
LIVE_POSITION: dict[str, Any] = {
    "id": "person:TEST-SCORING-000:en-US:test-scoring-topic-0:v1:for:2026.3:v1",
    "person_id": "person:TEST-SCORING-000",
    "stance_id": "en-US:test-scoring-topic-0:v1:for",
    "s_value": -1.0,
    "confidence": 0.3,
    "evidence_count": 3,
    "basis": "votes",
    "methodology_version": "2026.3",
    "is_current": True,
    "computed_at": "2026-10-19T00:00:00Z",
}

#: The live `methodology_versions` row's badge thresholds after publishing
#: `2026.3`, read back verbatim. Note the key ORDER differs from the module
#: constant's — ZeroDB does not preserve it — which is why nothing in the
#: scorer iterates these in order.
LIVE_BADGE_THRESHOLDS: dict[str, float] = {"low": 1.0, "high": 10.0, "medium": 4.0}


def test_the_live_row_validates_as_a_candidate_position() -> None:
    position = CandidatePosition.model_validate(LIVE_POSITION)
    assert position.s_value == -1.0
    assert position.methodology_version == "2026.3"
    assert position.is_current is True


def test_live_s_value_and_confidence_are_real_floats_not_strings() -> None:
    """Measured live: `-1.0` came back `-1.0`.

    Asserted on the raw payload rather than through the model, because Pydantic
    would coerce `"-1.0"` to `-1.0` on the way in and hide exactly the thing
    being measured. A string here would render fine on a page and break the
    first time the formula multiplied by it.
    """
    assert isinstance(LIVE_POSITION["s_value"], float)
    assert isinstance(LIVE_POSITION["confidence"], float)
    assert not isinstance(LIVE_POSITION["s_value"], bool)


def test_live_is_current_is_a_real_bool() -> None:
    """A truthy string in this column would make every version look current,
    and "which position is live" would have no answer."""
    assert LIVE_POSITION["is_current"] is True


def test_the_live_id_is_composed_from_the_natural_key_not_the_version_key() -> None:
    """The measurement that corrected an assumption.

    `CandidatePosition.version_key` is `(person_id, stance_id)`, which makes
    `{person}:{stance}:v{n}` the natural guess — and it is wrong.
    `Repository._version_id_prefix` uses the model's NATURAL key, which also
    includes `methodology_version`, so the live id carries the version before
    the `:v{n}` suffix.

    Pinned because the difference is invisible until something parses the id:
    a reader counting colon-separated segments to recover a stance id would be
    off by one.
    """
    row_id = LIVE_POSITION["id"]
    assert row_id.startswith(f"{LIVE_POSITION['person_id']}:{LIVE_POSITION['stance_id']}:")
    assert row_id.endswith(f":{LIVE_POSITION['methodology_version']}:v1")
    assert set(CandidatePosition.natural_key) == {
        "person_id",
        "stance_id",
        "methodology_version",
    }
    assert CandidatePosition.version_key == ("person_id", "stance_id")


def test_the_table_schema_carries_the_methodology_version_column() -> None:
    """The omission this story's live write found.

    `CandidatePosition` has always declared `methodology_version` as required
    and datamodel.md's id pattern is `{person_id}:{stance_id}:{method_ver}`,
    but `TABLE_SCHEMAS["candidate_positions"]` had no such column — invisible
    because nothing wrote to the table until story 6.2's scorer.
    """
    assert "methodology_version" in TABLE_SCHEMAS["candidate_positions"]
    assert TABLE_SCHEMAS["candidate_positions"]["methodology_version"] == "string"


def test_the_live_hand_computed_score_is_reproduced_exactly() -> None:
    """The live run's own arithmetic, replayed.

    The endpoint answered `55.55555555555556` and a hand computation against
    the live methodology's tier weights gave `50 * (1 + 0.45/4.05)` — equal to
    the last bit. Reproduced here from the same inputs so a change to the
    formula fails this test rather than only the live script.

    The live profile was: stance 0 primary k=2 (s=-1, c=0.3), stance 1 primary
    k=3 (s=+1, c=0.2), stance 2 secondary k=3 (s=+1, c=0.1), with the live
    tier weights primary 3.0 and secondary 1.5.
    """
    terms = [
        ScoredTerm(
            stance_id="en-US:test-scoring-topic-0:v1:for",
            tier_weight=3.0,
            intensity=2,
            confidence=0.3,
            s_value=-1.0,
        ),
        ScoredTerm(
            stance_id="en-US:test-scoring-topic-1:v1:for",
            tier_weight=3.0,
            intensity=3,
            confidence=0.2,
            s_value=1.0,
        ),
        ScoredTerm(
            stance_id="en-US:test-scoring-topic-2:v1:for",
            tier_weight=1.5,
            intensity=3,
            confidence=0.1,
            s_value=1.0,
        ),
    ]
    assert alignment_score(terms) == pytest.approx(55.55555555555556)


def test_the_live_badge_thresholds_round_trip_as_floats_in_any_key_order() -> None:
    """Measured live: the keys came back in a different order than written.

    ZeroDB does not preserve key order inside an object column, so nothing in
    the scorer may depend on it — `badge_for` resolves each threshold by name.
    Pinned by badging the same count against both orderings.
    """
    assert set(LIVE_BADGE_THRESHOLDS) == set(DEFAULT_BADGE_THRESHOLDS)
    for name, value in LIVE_BADGE_THRESHOLDS.items():
        assert isinstance(value, float), name
        assert value == DEFAULT_BADGE_THRESHOLDS[name]
    for count in (0, 1, 4, 6, 10, 99):
        assert badge_for(count, LIVE_BADGE_THRESHOLDS) == badge_for(count, DEFAULT_BADGE_THRESHOLDS)


def test_the_live_badge_for_six_votes_of_evidence_was_medium() -> None:
    """The exact badge the live run produced, from the live row's thresholds."""
    assert badge_for(6, LIVE_BADGE_THRESHOLDS) == "medium"


def test_a_version_predating_story_6_5_falls_back_rather_than_failing() -> None:
    """Measured live before publishing `2026.3`: the current version was 6.1's
    `2026.2`, which carries no `badge_thresholds` at all.

    The scorer badged from `DEFAULT_BADGE_THRESHOLDS`, which is what
    `badge_for` documents for this case — a version that predates the group
    must still badge, exactly as story 4.2's reader seeds rather than fails.
    """
    assert badge_for(6, {}) == "medium"
    assert badge_for(6, None) == "medium"
