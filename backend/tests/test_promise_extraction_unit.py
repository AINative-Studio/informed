"""Unit tests for story 9.3's promise-extraction pieces.

Three things get their own tests here rather than only a BDD scenario,
because each is a decision function whose edge cases are where extraction
quality actually lives:

  * `segment_sentences` -- the passage splitter. Abbreviations, dollar
    amounts and quoted punctuation are where a naive `split(".")` goes
    wrong, and a sentence split in the wrong place truncates a quote span.
  * `RuleBasedStubExtractor` -- the testable/non-testable decision. This is
    the stand-in, not the real extractor (see the module docstring), but its
    decision logic is still logic and is still covered.
  * `Promise` -- the row model's own invariants.

CLAUDE.md asks for 90%+ coverage on scoring and verdict logic; the
testable/non-testable split is classification logic in the same spirit and
is covered to the same standard here.
"""

from __future__ import annotations

import pytest

from app.ingest.promise_extraction import (
    DEFAULT_CONTEXT_SENTENCES,
    EXTRACTION_PROMPT_TEMPLATE,
    STUB_EXTRACTOR_VERSION,
    ExtractedCommitment,
    QuoteSpanError,
    RuleBasedStubExtractor,
    dedupe_key,
    passages_with_context,
    promise_id_for,
    segment_sentences,
    verify_quote_span,
)
from app.repositories.models import Promise

# ---------------------------------------------------------------------------
# segment_sentences
# ---------------------------------------------------------------------------


def test_segment_sentences_splits_on_terminal_punctuation() -> None:
    text = "I will vote to cap insulin at $35. I will oppose cuts to Medicaid."
    assert segment_sentences(text) == [
        "I will vote to cap insulin at $35.",
        "I will oppose cuts to Medicaid.",
    ]


def test_segment_sentences_keeps_a_dollar_amount_intact() -> None:
    """A decimal inside a figure is not a sentence boundary.

    `$35.50` split naively on "." produces two fragments and a quote span
    that stops mid-number -- precisely the kind of truncation the quote-span
    exactness criterion is about.
    """
    text = "I will cap co-pays at $35.50 per month. That is a specific number."
    assert segment_sentences(text)[0] == "I will cap co-pays at $35.50 per month."


def test_segment_sentences_does_not_split_a_common_abbreviation() -> None:
    text = "Sen. Ossoff will vote to cap insulin co-pays. He said so."
    assert segment_sentences(text)[0] == "Sen. Ossoff will vote to cap insulin co-pays."


def test_segment_sentences_handles_question_and_exclamation() -> None:
    assert len(segment_sentences("Will I fight? Yes I will! Every day.")) == 3


def test_segment_sentences_treats_newlines_as_boundaries() -> None:
    """Campaign-page text comes out of `extract_text` newline-separated.

    `extract_text` joins each HTML text node with "\\n" and those nodes are
    frequently headings or list items with NO terminal punctuation at all.
    Without treating a newline as a boundary, a whole page collapses into
    one "sentence".
    """
    text = "Cap insulin at $35\nProtect Social Security\nI will vote for both."
    assert segment_sentences(text) == [
        "Cap insulin at $35",
        "Protect Social Security",
        "I will vote for both.",
    ]


def test_segment_sentences_drops_empty_and_whitespace_only_fragments() -> None:
    assert segment_sentences("  \n\n   \n ") == []


def test_segment_sentences_returns_verbatim_substrings() -> None:
    """Every returned sentence must be findable in the original text.

    This is the property the whole quote-span guarantee rests on: if the
    splitter normalises whitespace or strips a character, every quote span
    derived from its output stops being a verbatim substring of the source.
    """
    text = "I’ll vote to cap insulin co-pays at $35/month — no exceptions. I mean it."
    for sentence in segment_sentences(text):
        assert sentence in text


def test_segment_sentences_on_empty_text() -> None:
    assert segment_sentences("") == []


# ---------------------------------------------------------------------------
# passages_with_context
# ---------------------------------------------------------------------------


def test_passages_with_context_pairs_each_sentence_with_its_neighbours() -> None:
    sentences = ["One.", "Two.", "Three.", "Four."]
    passages = passages_with_context(sentences, neighbours=1)
    assert [passage for passage, _ in passages] == sentences
    # The second sentence's context carries the one before and the one after.
    assert passages[1][1] == "One. Two. Three."


def test_passages_with_context_clamps_at_the_edges() -> None:
    passages = passages_with_context(["One.", "Two."], neighbours=2)
    assert passages[0][1] == "One. Two."
    assert passages[1][1] == "One. Two."


def test_passages_with_context_default_neighbour_count() -> None:
    passages = passages_with_context(["A.", "B.", "C."])
    assert len(passages) == 3
    assert DEFAULT_CONTEXT_SENTENCES >= 1


# ---------------------------------------------------------------------------
# verify_quote_span
# ---------------------------------------------------------------------------


def test_verify_quote_span_accepts_a_verbatim_substring() -> None:
    verify_quote_span("I will vote to cap insulin.", source_text="Yesterday: I will vote to cap insulin. OK.")


def test_verify_quote_span_rejects_a_paraphrase() -> None:
    with pytest.raises(QuoteSpanError) as excinfo:
        verify_quote_span("He promised to cap insulin", source_text="I will vote to cap insulin.")
    assert "verbatim" in str(excinfo.value)


def test_verify_quote_span_rejects_an_empty_span() -> None:
    with pytest.raises(QuoteSpanError):
        verify_quote_span("", source_text="anything at all")


def test_verify_quote_span_rejects_a_span_whose_punctuation_was_normalised() -> None:
    """A curly apostrophe silently straightened is not a verbatim quote."""
    with pytest.raises(QuoteSpanError):
        verify_quote_span("I'll vote to cap insulin", source_text="I’ll vote to cap insulin")


# ---------------------------------------------------------------------------
# RuleBasedStubExtractor -- the testable/non-testable decision
# ---------------------------------------------------------------------------


@pytest.fixture
def stub() -> RuleBasedStubExtractor:
    return RuleBasedStubExtractor()


def _only(stub: RuleBasedStubExtractor, sentence: str) -> ExtractedCommitment:
    found = stub(sentence, context=sentence)
    assert len(found) == 1, f"expected one commitment from {sentence!r}, got {len(found)}"
    return found[0]


@pytest.mark.parametrize(
    "sentence",
    [
        "I will vote to cap insulin co-pays at $35 per month.",
        "I will vote to require background checks on all gun sales.",
        "I will cosponsor the Social Security Fairness Act.",
        "I will vote to repeal the cap on state and local tax deductions.",
        "I voted to lower prescription drug costs by $2,000 a year.",
    ],
)
def test_specific_checkable_commitments_are_testable(
    stub: RuleBasedStubExtractor, sentence: str
) -> None:
    assert _only(stub, sentence).testable is True


@pytest.mark.parametrize(
    "sentence",
    [
        "I'll fight for working families every single day.",
        "I believe in standing up for Georgia.",
        "I will work tirelessly for the people of this state.",
        "I support stronger gun safety laws.",
        "I am committed to a brighter future for our children.",
        "I will always stand up for what is right.",
    ],
)
def test_aspirational_rhetoric_is_not_testable(
    stub: RuleBasedStubExtractor, sentence: str
) -> None:
    assert _only(stub, sentence).testable is False


def test_the_borderline_pair_is_split_the_documented_way(stub: RuleBasedStubExtractor) -> None:
    """The case the prompt reasons through explicitly, asserted as behaviour.

    "I support stronger gun safety laws" names no bill, number or mechanism,
    so no future vote can be matched to it -> not testable. "I will vote to
    require background checks on all gun sales" names the mechanism, so a
    roll call on exactly that can confirm or contradict it -> testable.
    Same topic, opposite classification: the distinction is specificity, not
    subject matter, which is what keeps the rule nonpartisan.
    """
    assert _only(stub, "I support stronger gun safety laws.").testable is False
    assert (
        _only(stub, "I will vote to require background checks on all gun sales.").testable is True
    )


def test_direction_is_support_for_an_advancing_commitment(stub: RuleBasedStubExtractor) -> None:
    assert _only(stub, "I will vote to cap insulin co-pays at $35.").direction == "support"


@pytest.mark.parametrize(
    "sentence",
    [
        "I will vote against any bill that cuts Medicaid funding.",
        "I will oppose the repeal of the Affordable Care Act.",
        "I will vote no on the Republican tax bill.",
    ],
)
def test_direction_is_oppose_for_a_blocking_commitment(
    stub: RuleBasedStubExtractor, sentence: str
) -> None:
    assert _only(stub, sentence).direction == "oppose"


def test_the_quote_span_is_the_passage_verbatim(stub: RuleBasedStubExtractor) -> None:
    sentence = "I’ll vote to cap insulin co-pays at $35/month — no exceptions."
    assert _only(stub, sentence).quote_span == sentence


def test_a_passage_with_no_commitment_language_yields_nothing(
    stub: RuleBasedStubExtractor,
) -> None:
    assert stub("Paid for by Ossoff for Senate.", context="") == []
    assert stub("Donate today!", context="") == []


def test_a_testable_commitment_gets_a_higher_confidence_than_rhetoric(
    stub: RuleBasedStubExtractor,
) -> None:
    testable = _only(stub, "I will vote to cap insulin co-pays at $35 per month.")
    vague = _only(stub, "I'll fight for working families every single day.")
    assert testable.confidence > vague.confidence
    assert 0.0 <= vague.confidence <= 1.0
    assert 0.0 <= testable.confidence <= 1.0


def test_a_non_testable_commitment_still_carries_its_quote(
    stub: RuleBasedStubExtractor,
) -> None:
    """prd.md: vague rhetoric is "shown but not scored" -- so it needs a quote."""
    vague = _only(stub, "I'll fight for working families every single day.")
    assert vague.quote_span == "I'll fight for working families every single day."
    assert vague.testable is False


def test_the_stub_version_marker_is_unmistakably_a_placeholder() -> None:
    """Nothing downstream may mistake stub output for a real extraction.

    Story 9.4 (embedding) and 10.1 (bill mapping) both read `promises`. If
    this version string looked like a model name, stub-quality rows would be
    indistinguishable from real ones in the live table.
    """
    assert STUB_EXTRACTOR_VERSION == "stub-rule-based-v0"
    assert "stub" in STUB_EXTRACTOR_VERSION


# ---------------------------------------------------------------------------
# the prompt design -- it cannot be executed here, but it can be checked
# ---------------------------------------------------------------------------


def test_the_extraction_prompt_states_the_output_schema() -> None:
    """The prompt is a deliverable of this story; these are its invariants."""
    for field in ("actor", "action", "object", "direction", "testable", "confidence", "quote_span"):
        assert field in EXTRACTION_PROMPT_TEMPLATE, f"prompt does not name {field!r}"


def test_the_extraction_prompt_carries_few_shot_examples_of_both_classes() -> None:
    assert '"testable": true' in EXTRACTION_PROMPT_TEMPLATE
    assert '"testable": false' in EXTRACTION_PROMPT_TEMPLATE


def test_the_extraction_prompt_demands_a_verbatim_quote_span() -> None:
    assert "verbatim" in EXTRACTION_PROMPT_TEMPLATE.lower()


def test_the_extraction_prompt_has_a_slot_for_the_passage() -> None:
    assert "{passage}" in EXTRACTION_PROMPT_TEMPLATE
    assert "{context}" in EXTRACTION_PROMPT_TEMPLATE


def test_the_extraction_prompt_is_nonpartisan_about_the_speaker() -> None:
    """prd.md non-negotiable: one scoring codebase, no party-specific rules.

    The prompt must not name a party, and must not tell the model to treat
    any speaker differently -- a prompt that mentioned one party would be a
    party-specific rule hiding in a string literal.
    """
    lowered = EXTRACTION_PROMPT_TEMPLATE.lower()
    for party_word in ("democrat", "republican", "liberal", "conservative", "left-wing"):
        assert party_word not in lowered, f"the prompt names {party_word!r}"


# ---------------------------------------------------------------------------
# idempotency: the dedupe key and the derived id
# ---------------------------------------------------------------------------


def _commitment(**overrides: object) -> ExtractedCommitment:
    base: dict[str, object] = {
        "actor": "Jon Ossoff",
        "action": "vote to cap",
        "object": "insulin co-pays at $35 per month",
        "direction": "support",
        "testable": True,
        "confidence": 0.9,
        "quote_span": "I will vote to cap insulin co-pays at $35 per month.",
    }
    base.update(overrides)
    return ExtractedCommitment(**base)  # type: ignore[arg-type]


def test_the_same_commitment_from_the_same_source_has_the_same_id() -> None:
    first = promise_id_for("src:abc", _commitment())
    second = promise_id_for("src:abc", _commitment())
    assert first == second
    assert first.startswith("prm:")


def test_the_same_commitment_from_a_different_source_gets_a_different_id() -> None:
    assert promise_id_for("src:abc", _commitment()) != promise_id_for("src:def", _commitment())


@pytest.mark.parametrize(
    "overrides",
    [
        {"quote_span": "I will vote to cap insulin co-pays at $70 per month."},
        {"actor": "Congress"},
        {"action": "cosponsor a bill to cap"},
        {"object": "insulin co-pays at $70 per month"},
        {"direction": "oppose"},
    ],
)
def test_a_changed_commitment_part_changes_the_id(overrides: dict[str, object]) -> None:
    """Every part of the dedupe tuple must actually be in the hash.

    A part left out of the key would make two genuinely different
    commitments collide onto one row and silently lose one of them.
    """
    assert promise_id_for("src:abc", _commitment()) != promise_id_for(
        "src:abc", _commitment(**overrides)
    )


def test_confidence_and_testability_are_not_part_of_the_identity() -> None:
    """Re-extraction that changes only a judgement updates, never duplicates.

    A better extractor re-reading the same sentence and giving it a different
    confidence -- or reclassifying it as testable -- is the SAME commitment
    restated, so it must land on the same row rather than appearing twice.
    """
    assert promise_id_for("src:abc", _commitment(confidence=0.4)) == promise_id_for(
        "src:abc", _commitment(confidence=0.95)
    )
    assert promise_id_for("src:abc", _commitment(testable=False)) == promise_id_for(
        "src:abc", _commitment(testable=True)
    )


def test_the_dedupe_key_is_stable_and_order_independent() -> None:
    assert dedupe_key("src:abc", _commitment()) == dedupe_key("src:abc", _commitment())


# ---------------------------------------------------------------------------
# the Promise row model
# ---------------------------------------------------------------------------


def test_promise_declares_its_table_and_references() -> None:
    assert Promise.table_name == "promises"
    assert Promise.references == {
        "candidacy_id": "candidacies",
        "person_id": "people",
        "source_id": "promise_sources",
    }


def test_promise_is_not_append_only() -> None:
    """A promise is a fact about a source, not a judgement that accumulates.

    `verdicts` is the append-only table (datamodel.md rule 6); re-extracting
    a source restates the same commitments, so `promises` is upserted.
    """
    assert Promise.version_key is None


def test_promise_has_no_natural_key_so_the_caller_supplies_the_id() -> None:
    assert Promise.natural_key == ()
    assert Promise.build_id({"source_id": "src:abc"}) is None


def _promise_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "prm:0123456789abcdef",
        "candidacy_id": "cand:1",
        "person_id": "person:bioguide-O000175",
        "source_id": "src:abc",
        "quote_span": "I will vote to cap insulin co-pays at $35 per month.",
        "actor": "Jon Ossoff",
        "action": "vote to cap",
        "object": "insulin co-pays at $35 per month",
        "direction": "support",
        "testable": True,
        "extractor_version": "stub-rule-based-v0",
        "confidence": 0.9,
    }
    row.update(overrides)
    return row


def test_promise_defaults_leave_the_downstream_fields_unset() -> None:
    promise = Promise.model_validate(_promise_row())
    assert promise.topic_id is None
    assert promise.stance_id is None
    assert promise.vector_id is None
    assert promise.mappings == []
    assert promise.qa_pending is False


def test_promise_row_data_keeps_every_field_including_nulls() -> None:
    row_data = Promise.model_validate(_promise_row()).row_data()
    for field in (
        "id",
        "candidacy_id",
        "person_id",
        "source_id",
        "quote_span",
        "actor",
        "action",
        "object",
        "direction",
        "topic_id",
        "stance_id",
        "testable",
        "extractor_version",
        "confidence",
        "vector_id",
        "mappings",
        "qa_pending",
    ):
        assert field in row_data, f"{field!r} missing from the stored row"


def test_promise_rejects_an_unknown_field() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Promise.model_validate(_promise_row(promise_text="a typo'd field name"))


def test_promise_rejects_an_out_of_vocabulary_direction() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Promise.model_validate(_promise_row(direction="maybe"))


def test_promise_rejects_a_confidence_outside_zero_to_one() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Promise.model_validate(_promise_row(confidence=1.5))
    with pytest.raises(ValidationError):
        Promise.model_validate(_promise_row(confidence=-0.1))


def test_promise_rejects_an_empty_quote_span() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Promise.model_validate(_promise_row(quote_span="  "))


def test_a_non_testable_promise_may_omit_the_four_parts() -> None:
    """prd.md shows vague rhetoric without scoring it, so it needs no object.

    "I'll fight for working families" has no checkable object by definition
    -- requiring one would force the extractor to invent it, which is the
    opposite of evidence-over-opinion.
    """
    promise = Promise.model_validate(
        _promise_row(
            testable=False,
            quote_span="I'll fight for working families every single day.",
            actor="Jon Ossoff",
            action="fight for",
            object=None,
            direction="unclear",
            confidence=0.4,
        )
    )
    assert promise.testable is False
    assert promise.object is None


def test_a_testable_promise_must_carry_an_action_and_an_object() -> None:
    """Testable means checkable, and nothing can be checked with no object.

    A row marked testable with no object would reach story 10.1's mapping
    agent as an unmappable promise and come back as a false "no opportunity"
    verdict -- a wrong published answer, not a missing one.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Promise.model_validate(_promise_row(testable=True, object=None))
    with pytest.raises(ValidationError):
        Promise.model_validate(_promise_row(testable=True, action=None))


def test_a_non_testable_promise_is_never_qa_pending() -> None:
    """Mapping QA (story 10.3) reviews mappings, and a non-testable promise
    is never mapped -- so flagging one for QA would put a row in a reviewer
    queue with nothing to review."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Promise.model_validate(_promise_row(testable=False, object=None, qa_pending=True))
