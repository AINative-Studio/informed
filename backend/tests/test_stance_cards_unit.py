"""Unit tests for the stance cards and their model (story 3.2).

The feature file asserts on the stances *as stored*; these assert on them *as
declared*, which is where the editorial constraints actually live. A failure
here names the offending card before any ZeroDB machinery is involved, which
is the error an author editing `STANCES` wants to see.

The balance tests are the ones that matter most. This module is the single
highest-stakes neutrality surface in Informed: a voter's whole profile is built
by picking from these cards, and prd.md's "Dealbreaker overrides the average"
scenario and the alignment formula both rest on the premise that the set of
options for a subtopic is a fair map of the real disagreement rather than a
nudge. So the checks below are not about whether a card takes a position —
taking one is its entire job — but about whether the SET spans the axis, with
comparable seriousness and specificity on each side.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

from app.repositories.models import Stance
from app.seed_stances import (
    DIRECTION_CODES,
    MINIMUM_STANCES_PER_SUBTOPIC,
    PARTISAN_IDENTITY_WORDS,
    STANCES,
    WORDING_VERSION,
    StanceCard,
    contains_partisan_identity_word,
    stance_row_ids,
    stance_rows,
    stances_by_topic,
)
from app.seed_taxonomy import EXPECTED_SUBTOPIC_COUNT, TOPICS, taxonomy_rows

#: datamodel.md: `stances` ids are `{topic_id}:{slug}`, and a `topic_id` is
#: itself `{locale}:{slug}:v{n}`.
ID_PATTERN = re.compile(
    r"^[a-z]{2}-[A-Z]{2}:[a-z0-9]+(?:-[a-z0-9]+)*:v\d+:[a-z0-9]+(?:-[a-z0-9]+)*$"
)


def _subtopic_slugs() -> set[str]:
    return {sub.slug for parent in TOPICS for sub in parent.subtopics}


# --- the acceptance criterion: at least 3 stances per subtopic --------------


def test_every_subtopic_from_story_3_1_has_stances() -> None:
    """All 60, not "most": a subtopic with no cards is a dead end in the UI."""
    covered = set(stances_by_topic())
    expected = {str(row.id) for row in taxonomy_rows() if row.parent_id is not None}
    assert len(expected) == EXPECTED_SUBTOPIC_COUNT
    missing = sorted(expected - covered)
    assert not missing, f"subtopics with no stance cards: {missing}"


def test_no_stance_hangs_off_a_top_level_topic() -> None:
    """Cards attach to leaves only, so a voter's pick is always specific.

    A stance on "Healthcare" rather than on "Prescription drug pricing" would
    be too coarse to map to a roll call, which is what epic 4's scoring needs.
    """
    top_level = {parent.slug for parent in TOPICS}
    offenders = sorted({card.topic_slug for card in STANCES if card.topic_slug in top_level})
    assert not offenders, f"stances attached to a top-level topic: {offenders}"


def test_every_stance_topic_slug_is_a_real_subtopic() -> None:
    """A typo'd topic slug would otherwise be caught only by the live seed.

    The repository's reference check does catch it, but at write time against a
    seeded project — which means the first place it surfaces is a half-written
    live seed. Catching it here costs nothing.
    """
    unknown = sorted({card.topic_slug for card in STANCES} - _subtopic_slugs())
    assert not unknown, f"stance cards naming a topic slug no subtopic has: {unknown}"


@pytest.mark.parametrize(
    "topic_id", sorted(str(row.id) for row in taxonomy_rows() if row.parent_id is not None)
)
def test_each_subtopic_has_at_least_three_stances(topic_id: str) -> None:
    """One parametrised case per subtopic, so a failure names the thin one."""
    cards = stances_by_topic().get(topic_id, [])
    assert len(cards) >= MINIMUM_STANCES_PER_SUBTOPIC, (
        f"{topic_id} has {len(cards)} stance(s), needs at least {MINIMUM_STANCES_PER_SUBTOPIC}"
    )


def test_the_minimum_is_the_acceptance_criterion_and_not_lower() -> None:
    """A guard on the guard: `MINIMUM_...` quietly set to 1 would void the lot."""
    assert MINIMUM_STANCES_PER_SUBTOPIC == 3


# --- spanning the spectrum --------------------------------------------------


@pytest.mark.parametrize(
    "topic_id", sorted(str(row.id) for row in taxonomy_rows() if row.parent_id is not None)
)
def test_each_subtopics_stances_span_both_sides_of_the_axis(topic_id: str) -> None:
    """The real content of "balanced", asserted per subtopic.

    Three checks in one, because they fail together and a reader wants all
    three facts at once:

      1. the codes are not all the same value — three cards coded `+1` are
         three flavours of one position dressed up as a choice;
      2. at least one is negative and at least one positive — `+1` and `+2`
         are one side twice, and a one-sided menu plus a neutral option is
         still one-sided;
      3. no code repeats — two cards both coded `-1` make that side twice as
         likely to be picked by a voter skimming, which is a nudge even when
         both wordings are fair.
    """
    codes = [int(card.direction_code) for card in stances_by_topic()[topic_id]]
    assert len(set(codes)) > 1, f"{topic_id}: every stance is coded {codes[0]}"
    assert any(code < 0 for code in codes), f"{topic_id}: no option on the negative side: {codes}"
    assert any(code > 0 for code in codes), f"{topic_id}: no option on the positive side: {codes}"
    duplicated = sorted({code for code in codes if codes.count(code) > 1})
    assert not duplicated, f"{topic_id}: repeated direction code(s) {duplicated}"


def test_direction_codes_come_from_the_documented_vocabulary() -> None:
    unknown = sorted({card.direction_code for card in STANCES} - set(DIRECTION_CODES))
    assert not unknown, f"direction codes outside the documented scheme: {unknown}"


def test_the_direction_vocabulary_is_a_signed_axis_through_zero() -> None:
    """Pinned so the scheme cannot drift into an unordered set of labels.

    Epic 4's scoring projects a candidate's measured axis position onto the
    stance the voter picked, and the projection needs the sign and the
    magnitude — see `app.seed_stances.DIRECTION_CODES`. A vocabulary of
    `"expand"`/`"restrict"` strings would read fine and quietly break that.
    """
    numeric = sorted(int(code) for code in DIRECTION_CODES)
    assert numeric == [-2, -1, 0, 1, 2]


def test_the_whole_corpus_is_not_tilted_to_one_side() -> None:
    """Balance across the taxonomy, not only within each subtopic.

    Every subtopic passing the span check individually would still leave a
    corpus with, say, 60 cards at `+2` and 60 at `-1`: each subtopic spans its
    axis, and the menu as a whole still offers the positive side more strongly.
    Signs must be within a few cards of even, and the two magnitudes must be
    offered about equally on each side.
    """
    codes = [int(card.direction_code) for card in STANCES]
    negative = sum(1 for code in codes if code < 0)
    positive = sum(1 for code in codes if code > 0)
    assert abs(negative - positive) <= 3, (
        f"the corpus leans: {negative} negative vs {positive} positive cards"
    )
    strong_negative = codes.count(-2)
    strong_positive = codes.count(2)
    assert abs(strong_negative - strong_positive) <= 3, (
        f"strong options are not offered evenly: {strong_negative} at -2 vs {strong_positive} at +2"
    )


def test_both_sides_get_comparable_wording_length() -> None:
    """The strawman check, measured.

    The classic way to rig a balanced-looking menu is to write your own side
    with specifics and the other side as a terse caricature ("ban them all").
    Length is a crude proxy for specificity, but it is the one proxy a test can
    actually compute, and a real tilt shows up in it: compared per side across
    ~190 cards, the mean description length of the negative and positive halves
    should be close.
    """
    negative = [card for card in STANCES if int(card.direction_code) < 0]
    positive = [card for card in STANCES if int(card.direction_code) > 0]
    assert negative and positive
    mean_negative = sum(len(card.description) for card in negative) / len(negative)
    mean_positive = sum(len(card.description) for card in positive) / len(positive)
    ratio = mean_negative / mean_positive
    assert 0.85 <= ratio <= 1.18, (
        f"one side is written at a different level of detail: negative mean "
        f"{mean_negative:.0f} chars, positive mean {mean_positive:.0f} chars"
    )


@pytest.mark.parametrize(
    "topic_slug",
    [
        "gestational-limits-and-exceptions",
        "background-checks-and-purchase-rules",
        "border-enforcement-and-asylum",
        "status-of-long-term-residents",
        "police-funding-and-oversight",
        "greenhouse-gas-emissions",
        "individual-income-tax-rates",
        "health-insurance-coverage",
    ],
)
def test_the_most_contested_subtopics_get_specific_wording_on_every_side(
    topic_slug: str,
) -> None:
    """The subtopics where a strawman would do the most damage.

    These are the ones where one side's framing most often gets written as "the
    reasonable moderate position" and the other as a caricature. Per-side
    length within one subtopic, plus a floor on every description, so no single
    card on a contested axis is a one-liner next to a paragraph.
    """
    cards = [card for card in STANCES if card.topic_slug == topic_slug]
    assert len(cards) >= MINIMUM_STANCES_PER_SUBTOPIC, f"{topic_slug} is thin"
    lengths = {card.slug: len(card.description) for card in cards}
    assert min(lengths.values()) >= 60, f"{topic_slug}: a terse card — {lengths}"
    assert max(lengths.values()) / min(lengths.values()) <= 2.0, (
        f"{topic_slug}: descriptions differ by more than 2x — {lengths}"
    )
    labels = {card.slug: len(card.label) for card in cards}
    assert max(labels.values()) / min(labels.values()) <= 2.5, (
        f"{topic_slug}: labels differ wildly in length — {labels}"
    )


# --- wording: an outcome, in plain words ------------------------------------


@pytest.mark.parametrize("card", STANCES, ids=lambda card: card.row_id)
def test_no_stance_label_or_description_names_a_side(card: StanceCard) -> None:
    """The stance-card analogue of story 3.1's topic-name neutrality check.

    Deliberately a DIFFERENT word list. Story 3.1 forbids a topic name from
    presupposing a stance, so `contains_stance_presupposing_word` would reject
    every row here — a card whose job is to say "restrict" cannot avoid words
    like that. What a card must never do is name the tribe that holds the
    position, or label the other one: a voter who agrees with the substance but
    rejects the badge has been pushed off their own view.
    """
    for text, field in ((card.label, "label"), (card.description, "description")):
        hit = contains_partisan_identity_word(text)
        assert hit is None, f"{card.row_id} {field} contains identity word {hit!r}: {text!r}"


def test_the_identity_word_list_is_not_empty_or_trivially_satisfied() -> None:
    """A guard on the guard, as in story 3.1.

    `PARTISAN_IDENTITY_WORDS` emptied out — by a merge, or by someone making a
    failing test pass the easy way — would turn the check above into a
    tautology. Both halves of each pair are spot-checked: a list that caught
    only one side's badges would itself be partisan.
    """
    assert len(PARTISAN_IDENTITY_WORDS) >= 20
    for both_sides in (
        ("liberal", "conservative"),
        ("progressive", "right-wing"),
        ("democrat", "republican"),
        ("woke", "maga"),
    ):
        for word in both_sides:
            assert word in PARTISAN_IDENTITY_WORDS, f"{word!r} is not on the list"


@pytest.mark.parametrize(
    "framed",
    [
        "Support commonsense liberal gun limits",
        "Stop the far-right assault on abortion access",
        "End the radical left's open-border agenda",
        "Back Republican tax cuts",
        "Defund the woke bureaucracy",
        "Protect Democrat priorities in the budget",
        "Resist MAGA attacks on Social Security",
        "Oppose socialist healthcare",
    ],
)
def test_the_identity_check_rejects_a_card_that_names_a_side(framed: str) -> None:
    """The counter-examples, from both directions on purpose."""
    assert contains_partisan_identity_word(framed) is not None, (
        f"{framed!r} should have been caught as naming a side"
    )


@pytest.mark.parametrize(
    "plain",
    [
        "Cap what patients pay at the pharmacy counter",
        "Let the market set drug prices without federal negotiation",
        "Require a background check for every firearm sale",
        "Keep private sales exempt from the background check system",
        "Allow abortion until viability, with later exceptions for health",
        "Limit abortion to early pregnancy, with exceptions for life and rape",
        "Raise the federal minimum wage to a national living-wage floor",
        "Leave the wage floor to states and local labor markets",
        # Words that merely contain a listed word must not trip the check.
        "Expand conservation easements on working farmland",
        "Fund independent research into grid reliability",
    ],
)
def test_the_identity_check_accepts_plain_outcome_wording(plain: str) -> None:
    """The other half: the check must not reject ordinary policy language.

    "Conservation" and "independent" are here because a substring match rather
    than a whole-word one would flag them on "conservative" and "dependent".
    """
    hit = contains_partisan_identity_word(plain)
    assert hit is None, f"{plain!r} was wrongly flagged on {hit!r}"


@pytest.mark.parametrize("card", STANCES, ids=lambda card: card.row_id)
def test_every_label_reads_as_an_outcome_a_voter_would_claim(card: StanceCard) -> None:
    """prd.md: "the outcome the voter wants, in plain words".

    What is checkable: the label is a short phrase in sentence case, not a
    sentence, not a question, not shouted, and not hedged into meaninglessness.
    The bound on length is what keeps a card from becoming an argument — a card
    that needs 90 characters to state its position is making a case, and a case
    is a nudge.
    """
    assert 15 <= len(card.label) <= 72, f"{card.row_id}: label is {len(card.label)} chars"
    assert card.label[0].isupper(), f"{card.row_id}: label does not start with a capital"
    assert card.label != card.label.upper(), f"{card.row_id}: label is shouted"
    assert not card.label.endswith("."), f"{card.row_id}: label ends with a full stop"
    assert "?" not in card.label, f"{card.row_id}: label is a question"


@pytest.mark.parametrize("card", STANCES, ids=lambda card: card.row_id)
def test_every_description_is_a_specific_sentence(card: StanceCard) -> None:
    """The description is what makes a position concrete enough to score.

    A card reading "better healthcare" cannot be mapped to a roll call, so
    every description states a mechanism. The floor on length is the cheapest
    proxy for that; the ceiling keeps it from becoming an essay that out-argues
    its neighbours.
    """
    assert 60 <= len(card.description) <= 240, (
        f"{card.row_id}: description is {len(card.description)} chars"
    )
    assert card.description[0].isupper(), f"{card.row_id}: description lacks a capital"
    assert card.description.endswith("."), f"{card.row_id}: description lacks a full stop"


def test_no_label_is_reused_across_the_corpus() -> None:
    """Two subtopics offering the identical label is a copy-paste, not a view."""
    labels = Counter(card.label for card in STANCES)
    duplicated = sorted(label for label, n in labels.items() if n > 1)
    assert not duplicated, f"labels used by more than one card: {duplicated}"


def test_no_description_is_reused_across_the_corpus() -> None:
    descriptions = Counter(card.description for card in STANCES)
    duplicated = [text for text, n in descriptions.items() if n > 1]
    assert not duplicated, f"{len(duplicated)} description(s) reused: {duplicated[:3]}"


def test_labels_within_a_subtopic_are_distinct() -> None:
    clashes = {
        topic_id: sorted(label for label, n in Counter(c.label for c in cards).items() if n > 1)
        for topic_id, cards in stances_by_topic().items()
    }
    offenders = {topic_id: dupes for topic_id, dupes in clashes.items() if dupes}
    assert not offenders, f"repeated labels within a subtopic: {offenders}"


# --- slugs and ids ----------------------------------------------------------


def test_every_row_id_matches_the_documented_pattern() -> None:
    bad = [str(row.id) for row in stance_rows() if not ID_PATTERN.match(str(row.id))]
    assert not bad, f"ids that do not match {{topic_id}}:{{slug}}: {bad[:5]}"


def test_every_row_id_starts_with_its_topic_id() -> None:
    """datamodel.md's pattern is literally the topic id plus a slug.

    Worth asserting separately from the regex: a card whose `topic_slug` and
    `row_id` disagreed would still match the pattern and would then be stored
    under an id that no `{topic_id}:` prefix query could find.
    """
    bad = [
        (str(row.id), row.topic_id)
        for row in stance_rows()
        if not str(row.id).startswith(f"{row.topic_id}:")
    ]
    assert not bad, f"ids that do not start with their topic_id: {bad[:5]}"


def test_every_slug_is_lowercase_kebab_case() -> None:
    kebab = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    bad = [card.slug for card in STANCES if not kebab.match(card.slug)]
    assert not bad, f"slugs that are not lowercase kebab-case: {bad}"


def test_slugs_are_unique_within_a_subtopic() -> None:
    """Across the corpus they need not be: the topic id disambiguates.

    `cap-costs` under two different subtopics gives two different ids, which is
    fine and keeps slugs readable. Within one subtopic a repeat would collide.
    """
    for topic_id, cards in stances_by_topic().items():
        slugs = [card.slug for card in cards]
        duplicated = sorted({slug for slug in slugs if slugs.count(slug) > 1})
        assert not duplicated, f"{topic_id}: duplicate slugs {duplicated}"


def test_row_ids_are_unique_across_the_corpus() -> None:
    rows = stance_rows()
    assert len(stance_row_ids()) == len(rows), "two cards compose the same id"


def test_no_slug_leaks_the_direction_code() -> None:
    """A slug like `option-1` or `pos-2` would make the axis visible in the id.

    The id reaches the client (a voter's saved profile stores `stance_id`), and
    an id that encodes "this is option 1" reintroduces exactly the ordering the
    shuffle exists to remove.
    """
    bad = [
        card.row_id
        for card in STANCES
        if re.search(r"\b(option|choice|opt|pos|neg)[-_]?\d\b", card.slug)
    ]
    assert not bad, f"slugs encoding a position in the list: {bad}"


# --- the rows the seed will write -------------------------------------------


def test_every_row_carries_the_wording_version() -> None:
    for row in stance_rows():
        assert row.wording_version == WORDING_VERSION == 1


def test_stance_rows_match_the_declaration_one_for_one() -> None:
    assert len(stance_rows()) == len(STANCES)


def test_the_corpus_is_at_least_three_times_the_subtopic_count() -> None:
    """~180+ cards, stated as an arithmetic consequence rather than a magic number."""
    assert len(STANCES) >= EXPECTED_SUBTOPIC_COUNT * MINIMUM_STANCES_PER_SUBTOPIC


def test_rows_are_ordered_so_no_subtopics_cards_are_adjacent_by_direction() -> None:
    """The stored write order is not the axis order.

    datamodel.md's `stances` main query is "by topic", and a read that comes
    back in insertion order is the order a client would show if it forgot to
    shuffle. Writing each subtopic's cards in a fixed non-monotonic order means
    that even a client with a broken shuffle does not show the axis in order —
    defence in depth behind `StanceService`'s own shuffle.
    """
    rows = stance_rows()
    by_topic: dict[str, list[int]] = {}
    for row in rows:
        by_topic.setdefault(row.topic_id, []).append(int(row.direction_code))
    monotonic = {
        topic_id: codes
        for topic_id, codes in by_topic.items()
        if codes == sorted(codes) or codes == sorted(codes, reverse=True)
    }
    assert not monotonic, (
        f"{len(monotonic)} subtopic(s) are written in axis order, so an unshuffled "
        f"read shows a ranked menu: {dict(list(monotonic.items())[:3])}"
    )


# --- the model ---------------------------------------------------------------


def test_stance_declares_its_table_and_reference() -> None:
    assert Stance.table_name == "stances"
    assert Stance.references == {"topic_id": "issue_topics"}
    assert Stance.version_key is None, "stances is not append-only"


def test_stance_has_no_default_selection_field() -> None:
    """The "no stance is preselected" criterion, as a property of the model.

    There is nothing to assert about a field that does not exist, so what this
    asserts is the absence itself — pinned, so a later story cannot add one
    without this failing and making someone justify it.
    """
    fields = set(Stance.model_fields)
    forbidden = {
        "default",
        "is_default",
        "preselected",
        "selected",
        "recommended",
        "default_choice",
        "suggested",
        "display_order",
        "rank",
        "sort_order",
        "position",
    }
    assert not fields & forbidden, (
        f"Stance carries selection/ordering field(s): {fields & forbidden}"
    )
    assert fields == {
        "id",
        "topic_id",
        "label",
        "description",
        "direction_code",
        "wording_version",
    }, f"Stance's fields drifted from datamodel.md: {sorted(fields)}"


def test_stance_rejects_an_unknown_field() -> None:
    """`extra="forbid"`: a `default: true` would otherwise be stored and then
    found and honoured by some later UI story."""
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        Stance.model_validate(
            {
                "id": "en-US:healthcare:v1:x",
                "topic_id": "en-US:healthcare:v1",
                "label": "A label that is long enough",
                "description": "A description.",
                "direction_code": "+1",
                "wording_version": 1,
                "default": True,
            }
        )


def test_stance_rejects_a_direction_code_outside_the_axis() -> None:
    for bad_code in ("+3", "-3", "left", "", "1.5", "++1"):
        with pytest.raises(ValueError):
            Stance(
                id="en-US:prescription-drug-pricing:v1:x",
                topic_id="en-US:prescription-drug-pricing:v1",
                label="A label that is long enough",
                description="A description long enough to be a real sentence about policy.",
                direction_code=bad_code,
                wording_version=1,
            )


def test_stance_rejects_an_id_that_does_not_extend_its_topic_id() -> None:
    """datamodel.md's id pattern, enforced where a typo is survivable.

    An id under the wrong topic prefix would store and then never be found by
    the `by topic` query, which is the one query datamodel.md documents for
    this table — a silent disappearance rather than an error.
    """
    with pytest.raises(ValueError, match="topic_id"):
        Stance(
            id="en-US:health-insurance-coverage:v1:cap-out-of-pocket-costs",
            topic_id="en-US:prescription-drug-pricing:v1",
            label="A label that is long enough",
            description="A description long enough to be a real sentence about policy.",
            direction_code="+1",
            wording_version=1,
        )


def test_stance_rejects_a_non_positive_wording_version() -> None:
    with pytest.raises(ValueError):
        Stance(
            id="en-US:prescription-drug-pricing:v1:x",
            topic_id="en-US:prescription-drug-pricing:v1",
            label="A label that is long enough",
            description="A description long enough to be a real sentence about policy.",
            direction_code="+1",
            wording_version=0,
        )


def test_stance_row_data_carries_every_field() -> None:
    """ZeroDB's PUT replaces `row_data` wholesale (story 1.2's finding).

    A payload missing `direction_code` would strip it on the second seed run,
    leaving a card with no place on the axis and so unscoreable.
    """
    row = Stance(
        id="en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs",
        topic_id="en-US:prescription-drug-pricing:v1",
        label="Cap what patients pay at the pharmacy counter",
        description=(
            "Set a legal ceiling on monthly out-of-pocket costs for insulin and other drugs."
        ),
        direction_code="+1",
        wording_version=1,
    )
    assert set(row.row_data()) == {
        "id",
        "topic_id",
        "label",
        "description",
        "direction_code",
        "wording_version",
    }


# --- the seeder's defaults ---------------------------------------------------


def test_the_seeder_paces_by_default() -> None:
    """~190 writes at a 60 req/min ceiling, so an unpaced default is a hazard.

    Same reasoning as story 3.1's `TaxonomySeeder`, and more so: this seed is
    more than twice the size. Pinned on identity rather than by timing a real
    sleep, so the test stays fast.
    """
    from app.seed_stances import StanceSeeder

    seeder = StanceSeeder(client=None)  # type: ignore[arg-type] - never called
    paced = seeder._pace  # noqa: SLF001 - asserting the injected default
    assert paced is not None
    assert paced.__name__ == "pace", "the default pacer is not app.bootstrap's"


def test_the_seeder_accepts_an_injected_no_op_pacer() -> None:
    from app.seed_stances import StanceSeeder

    def noop() -> None:
        return None

    seeder = StanceSeeder(client=None, pace=noop)  # type: ignore[arg-type]
    assert seeder._pace is noop  # noqa: SLF001 - asserting the injection
