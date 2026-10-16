"""Unit tests for the issue taxonomy and its model (story 3.1).

The feature file asserts on the taxonomy *as stored*; these assert on the
taxonomy *as declared*, which is where the editorial constraints actually live.
A failure here names the offending topic before any ZeroDB machinery is
involved, which is the error an author editing `TOPICS` wants to see.

The neutrality tests are the ones that matter most. prd.md's first product
principle is "nonpartisan by construction", and for a *taxonomy* that means the
topic labels themselves must not presuppose a stance: "Gun safety" and "Gun
rights" name the same policy area from two sides, and a voter who disagrees
with the framing of the label has already been told what the app thinks before
they pick anything. These tests are the executable form of that rule, so a
later story adding a topic cannot quietly reintroduce a framed label.
"""

from __future__ import annotations

import re

import pytest

from app.repositories.models import IssueTopic
from app.seed_taxonomy import (
    EXPECTED_SUBTOPIC_COUNT,
    EXPECTED_TOPIC_COUNT,
    STANCE_PRESUPPOSING_WORDS,
    TAXONOMY_LOCALE,
    TAXONOMY_VERSION,
    TOPICS,
    Topic,
    taxonomy_row_ids,
    taxonomy_rows,
)

#: datamodel.md: `issue_topics` ids are `{locale}:{slug}:v{n}`.
ID_PATTERN = re.compile(r"^[a-z]{2}-[A-Z]{2}:[a-z0-9]+(?:-[a-z0-9]+)*:v\d+$")


def _all_topics() -> list[Topic]:
    return [topic for parent in TOPICS for topic in (parent, *parent.subtopics)]


# --- the counts the acceptance criterion states -----------------------------


def test_there_are_twenty_top_level_topics() -> None:
    """"About 20 topics" — pinned at exactly 20 so a 21st is a deliberate edit."""
    assert len(TOPICS) == EXPECTED_TOPIC_COUNT == 20


def test_there_are_sixty_subtopics() -> None:
    subtopics = [sub for parent in TOPICS for sub in parent.subtopics]
    assert len(subtopics) == EXPECTED_SUBTOPIC_COUNT == 60


def test_every_topic_has_exactly_three_subtopics() -> None:
    """Three each, so no topic is visibly thinner than its neighbours.

    An uneven tree reads as editorial emphasis — a topic with one subtopic
    looks like an afterthought next to one with six — and prd.md's neutrality
    principle covers the shape of the taxonomy as well as its wording.
    """
    uneven = {parent.name: len(parent.subtopics) for parent in TOPICS if len(parent.subtopics) != 3}
    assert not uneven, f"topics without exactly 3 subtopics: {uneven}"


# --- naming neutrality ------------------------------------------------------


@pytest.mark.parametrize("topic", _all_topics(), ids=lambda topic: topic.slug)
def test_no_topic_name_presupposes_a_stance(topic: Topic) -> None:
    """One parametrised case per topic, so a failure names the offender."""
    lowered = topic.name.lower()
    hits = [
        word for word in STANCE_PRESUPPOSING_WORDS if re.search(rf"\b{re.escape(word)}\b", lowered)
    ]
    assert not hits, f"{topic.name!r} contains stance-presupposing word(s) {hits}"


def test_the_stance_word_list_is_not_empty_or_trivially_satisfied() -> None:
    """A guard on the guard.

    `STANCE_PRESUPPOSING_WORDS` emptied out — by a merge, or by someone making
    a failing test pass the easy way — would turn every neutrality assertion
    above into a tautology. Pinning a lower bound and spot-checking that the
    list really does reject the canonical examples from the story brief keeps
    the check honest.
    """
    assert len(STANCE_PRESUPPOSING_WORDS) >= 15
    for framed in ("safety", "rights", "reform", "protection"):
        assert framed in STANCE_PRESUPPOSING_WORDS


@pytest.mark.parametrize("framed_name", ["Gun safety", "Gun rights", "Immigration reform"])
def test_the_neutrality_check_would_reject_a_framed_name(framed_name: str) -> None:
    """The brief's own counter-examples, asserted to fail the check.

    Without this, "no topic name presupposes a stance" could pass because the
    check is broken rather than because the names are neutral.
    """
    lowered = framed_name.lower()
    assert any(
        re.search(rf"\b{re.escape(word)}\b", lowered) for word in STANCE_PRESUPPOSING_WORDS
    ), f"{framed_name!r} should have been caught as stance-presupposing"


def test_topic_names_are_unique() -> None:
    names = [topic.name for topic in _all_topics()]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"duplicate names: {duplicates}"


def test_topic_names_are_sentence_case_not_title_case() -> None:
    """One capitalisation convention, so no topic is typographically louder.

    Proper nouns and acronyms are allowed a capital mid-name (`Social Security`
    is a programme name; `U.S.` is an abbreviation), so the rule is only that
    the name starts with a capital and is not SHOUTED.
    """
    for topic in _all_topics():
        assert topic.name[0].isupper(), f"{topic.name!r} does not start with a capital"
        assert topic.name != topic.name.upper(), f"{topic.name!r} is upper-case"


# --- slugs and ids ----------------------------------------------------------


def test_slugs_are_unique_across_the_whole_tree() -> None:
    """Slugs are flat, not parent-prefixed, so they must be globally unique.

    A subtopic's id is `{locale}:{slug}:v{n}` with no parent segment — see
    `app.seed_taxonomy`'s module docstring for why — which means two subtopics
    under different parents sharing a slug would share an id and silently
    overwrite one another on seed.
    """
    slugs = [topic.slug for topic in _all_topics()]
    duplicates = sorted({slug for slug in slugs if slugs.count(slug) > 1})
    assert not duplicates, f"duplicate slugs: {duplicates}"


def test_every_slug_is_lowercase_kebab_case() -> None:
    bad = [topic.slug for topic in _all_topics() if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", topic.slug)]
    assert not bad, f"slugs that are not lowercase kebab-case: {bad}"


def test_every_row_id_matches_the_documented_pattern() -> None:
    bad = [str(row.id) for row in taxonomy_rows() if not ID_PATTERN.match(str(row.id))]
    assert not bad, f"ids that do not match {{locale}}:{{slug}}:v{{n}}: {bad}"


def test_row_ids_are_unique() -> None:
    rows = taxonomy_rows()
    assert len(taxonomy_row_ids()) == len(rows)


# --- the rows the seed will write -------------------------------------------


def test_taxonomy_rows_are_parents_before_their_children() -> None:
    """The order the seeder writes in, asserted on the data rather than the loop.

    `parent_id` is a declared reference, so the repository resolves it before
    writing: a child emitted before its parent is refused against a project
    that has never been seeded.
    """
    seen: set[str] = set()
    for row in taxonomy_rows():
        if row.parent_id is not None:
            assert row.parent_id in seen, f"{row.id} was emitted before its parent {row.parent_id}"
        seen.add(str(row.id))


def test_every_row_carries_the_locale_and_version() -> None:
    for row in taxonomy_rows():
        assert row.locale == TAXONOMY_LOCALE == "en-US"
        assert row.taxonomy_version == TAXONOMY_VERSION == 1


def test_top_level_rows_have_no_parent_and_subtopic_rows_do() -> None:
    rows = taxonomy_rows()
    roots = [row for row in rows if row.parent_id is None]
    children = [row for row in rows if row.parent_id is not None]
    assert len(roots) == EXPECTED_TOPIC_COUNT
    assert len(children) == EXPECTED_SUBTOPIC_COUNT
    root_ids = {str(row.id) for row in roots}
    assert all(row.parent_id in root_ids for row in children)


def test_the_tree_is_exactly_two_levels_deep() -> None:
    rows = taxonomy_rows()
    child_ids = {str(row.id) for row in rows if row.parent_id is not None}
    assert not [row for row in rows if row.parent_id in child_ids]


def test_prd_healthcare_example_is_present() -> None:
    """prd.md names "Healthcare → Prescription drug pricing" itself.

    The consistency anchor for the whole taxonomy: whatever else changes, the
    one parent/child pair the product requirements spell out has to resolve.
    """
    by_id = {str(row.id): row for row in taxonomy_rows()}
    healthcare = by_id["en-US:healthcare:v1"]
    drug_pricing = by_id["en-US:prescription-drug-pricing:v1"]
    assert healthcare.name == "Healthcare"
    assert healthcare.parent_id is None
    assert drug_pricing.name == "Prescription drug pricing"
    assert drug_pricing.parent_id == "en-US:healthcare:v1"


# --- the model ---------------------------------------------------------------


def test_issue_topic_declares_its_table_and_self_reference() -> None:
    assert IssueTopic.table_name == "issue_topics"
    assert IssueTopic.references == {"parent_id": "issue_topics"}
    assert IssueTopic.version_key is None, "issue_topics is not append-only"


def test_issue_topic_rejects_an_unknown_field() -> None:
    """`extra="forbid"`: a typo'd field would be stored and then invisible."""
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        IssueTopic.model_validate(
            {
                "id": "en-US:healthcare:v1",
                "nmae": "Healthcare",
                "locale": "en-US",
                "taxonomy_version": 1,
            }
        )


def test_issue_topic_rejects_a_topic_that_is_its_own_parent() -> None:
    """A self-parented row is an infinite tree, and the repository's reference
    check would happily resolve it (the row exists — it is itself)."""
    with pytest.raises(ValueError, match="own parent"):
        IssueTopic(
            id="en-US:healthcare:v1",
            parent_id="en-US:healthcare:v1",
            name="Healthcare",
            locale="en-US",
            taxonomy_version=1,
        )


def test_issue_topic_rejects_a_non_positive_taxonomy_version() -> None:
    with pytest.raises(ValueError):
        IssueTopic(
            id="en-US:healthcare:v0",
            name="Healthcare",
            locale="en-US",
            taxonomy_version=0,
        )


def test_issue_topic_row_data_carries_every_field() -> None:
    """ZeroDB's PUT replaces `row_data` wholesale (story 1.2's finding).

    A payload missing `parent_id` would strip it from a stored subtopic on the
    second seed run, orphaning it.
    """
    row = IssueTopic(
        id="en-US:prescription-drug-pricing:v1",
        parent_id="en-US:healthcare:v1",
        name="Prescription drug pricing",
        locale="en-US",
        taxonomy_version=1,
    )
    assert set(row.row_data()) == {"id", "parent_id", "name", "locale", "taxonomy_version"}
