"""Turn promise-source text into atomic commitments (story 9.3).

What this is, and why it exists
================================

prd.md's Promise vs. Record pipeline, step 2: "an agent turns text into
atomic, testable commitments: actor, action, object, direction ('vote to cap
insulin at $35/month'). Vague rhetoric is tagged *non-testable* and shown but
not scored." Story 9.1 collected and archived the source text; this module is
the extraction step that reads it.

backlog.md story 9.3's acceptance criteria: "At least 90% precision on a
300-promise labeled set; vague rhetoric is tagged not testable; the quote span
is kept."

Two of those three are met and tested here. The first is not, and cannot be
in this environment — stated plainly below rather than quietly approximated,
because the whole value of this pipeline is that a voter can trust what it
says about a candidate.

===============================================================================
THE 90% PRECISION ACCEPTANCE CRITERION: NOT MET, AND WHY
===============================================================================

**There is no LLM credential in this environment, and this module does not
pretend otherwise.** Confirmed three independent ways during this story
(2026-10-17), not assumed from the brief:

  1. `.env.example` documents every credential this project uses —
     `CONGRESS_GOV_API_KEY`, `FEC_API_KEY`, `WAYBACK_ACCESS_KEY`/
     `WAYBACK_SECRET_KEY`, `ZERODB_PROFILE_ROOT_KEY` — and has **no entry of
     any kind** for an LLM provider. (This story adds one; see that file's new
     "Promise extraction" section.)
  2. The real, gitignored `local.env` holds exactly three variables —
     `ZERODB_WORKSPACE_ID`, `ZERODB_PROJECT_ID`, `ZERODB_API_KEY` — confirmed
     by listing variable NAMES only, never values. No `ANTHROPIC_API_KEY`, no
     `OPENAI_API_KEY`, nothing equivalent.
  3. ZeroDB's own client was checked for a text-generation capability, since
     "AINative Agent Swarm" is this project's stated agent runtime (CLAUDE.md).
     `app.zerodb.client.ZeroDBClient` exposes `embed_and_store`,
     `semantic_search`, `generate_embeddings` and the graph/RAG search calls —
     **embeddings and retrieval only**. There is no chat, completion or
     generation method on it, and none in the ZeroDB MCP tool surface. So
     ZeroDB can embed a promise (which is story 9.4's job) but cannot extract
     one.

That is a genuine credential gap of exactly the shape story 5.1 hit with
Congress.gov and story 9.1 hit with Wayback SPN2: a one-time human signup this
agent cannot complete.

**Why the precision bar is specifically unmeasurable without it**, rather than
merely harder: a precision measurement needs (a) an extractor producing
candidate extractions and (b) a human labelling each as correct or not.
Precision is `correct / extracted`, so with no real extractor there is no
denominator to measure. The only way to produce 300 labelled promises without
an extractor is 300 blind manual extractions by a person — which measures that
person, not this pipeline. Measuring the stand-in extractor below instead would
produce a real number for a thing nobody intends to ship, and reporting it as
"story 9.3's precision" would be the single most misleading thing this module
could do.

**So what ships here is the pipeline, not the extractor**: schema-correct,
idempotent, quote-span-exact, with the LLM call point isolated behind one
swappable interface (`PromiseExtractor`) and verified end-to-end against real
archived campaign-site text using the explicitly-labeled
`RuleBasedStubExtractor`. The stub is scaffolding and says so in its own
docstring; its `extractor_version` is `"stub-rule-based-v0"`, which cannot be
mistaken for a model name by story 9.4, story 10.1, or a human reading the
table.

**What unblocks the acceptance criterion**, concretely, for whoever adds the
key: add `ANTHROPIC_API_KEY` to the environment, implement the
`PromiseExtractor` protocol as one function that posts
`EXTRACTION_PROMPT_TEMPLATE` (below — written in full, with its few-shot
examples and its output schema, as this story's actual extraction design) and
parses the JSON back into `ExtractedCommitment` records, and pass it to
`PromiseExtractionPipeline` with `extractor_version="claude-promise-extractor-v1"`.
Nothing else in this module changes — that is what the seam is for. The
300-promise labelled set can then be built from story 9.1's three archived
campaign sites plus story 9.2's debate transcripts and questionnaires, and
precision measured per `extractor_version` so a regression is visible.

===============================================================================
A SECOND LIVE FINDING (SINCE FIXED): THE ARCHIVED BYTES WERE NOT READABLE BACK
===============================================================================

This shapes the module's signature, so it is here and not in a footnote.
`extract_from_source` takes the source text as a parameter rather than
fetching it from `promise_sources.archived_file_id`. The original reason was
that **the archived bytes could not be read back out of ZeroDB** — found live
against the real "informed" project during this story (2026-10-17): all three
of story 9.1's real `promise_sources` rows existed with real
`archived_file_id` values, `generate_presigned_url` minted a real signed URL
for each, and a GET of it 404ed for all three, because
`ZeroDBClient.upload_file` posted file *metadata* to ZeroDB's metadata-only
route and never sent any bytes.

**That is fixed.** `upload_file` now posts the bytes as multipart to ZeroDB's
binary endpoint, `ZeroDBClient.download_file` reads them back, and story 9.1's
three real captures were re-archived so they are genuinely retrievable. See
`ZeroDBClient.upload_file` for the two-endpoint trap that caused it.

**This module's signature does not change, and that was the point of the
seam.** `extract_from_source` takes `source_text` explicitly, making the text
an injected input exactly like `page_fetcher` in `campaign_sites.py`: the
pipeline runs in tests with no network and no file store, and in production
against whatever the caller supplies. What changed is only the caller —
`scripts/extract_promises.py --require-archive` now reads the archived
snapshot through `download_file` and verifies its sha256 against the capture,
instead of refusing to run. The default path (re-fetching the row's own
recorded url) remains available for archives captured before the fix.

===============================================================================
THE EXTRACTOR SEAM
===============================================================================

`PromiseExtractor` is one callable: given a passage and its surrounding
context, return zero or more `ExtractedCommitment` records. That is the entire
LLM call point, and it is a constructor argument with **no default** —
deliberately, since a default would have to point at either a real LLM (none
exists) or the stub (which would make stub output the silent default in
production). `PromiseExtractionPipeline` raises on `extractor=None` rather than
guessing.

The pipeline hands the extractor one sentence at a time, each paired with its
immediate neighbours as context (`DEFAULT_CONTEXT_SENTENCES`). One sentence,
because a commitment is atomic and this story's unit of output is one sentence's
worth of claim; with neighbours, because real campaign copy resolves its own
pronouns across sentence boundaries ("Insulin costs too much. I'll cap it at
$35." — the second sentence alone has an uninterpretable object). The context
is for *reading* the passage; the `quote_span` must still come from the passage
itself, which `verify_quote_span` enforces.

===============================================================================
IDEMPOTENCY
===============================================================================

A promise's id is the sha256 of `(source_id, quote_span, actor, action,
object, direction)`, truncated, prefixed `prm:` — see `Promise`'s own docstring
for the full reasoning and the honest note about deviating from datamodel.md's
literal `prm:{uuid}`. Re-extracting the same source with the same extractor
writes the same ids and so upserts in place: no duplicates, and
`ExtractionReport.new_promises` reports 0. A changed source, or a better
extractor finding a commitment the old one missed, adds rows without disturbing
the ones already there.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.repositories import Repository
from app.repositories.models import Candidacy, Promise, PromiseSource
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: Named so a `promises` row is traceable to the code that produced it, like
#: `campaign_sites.ADAPTER_VERSION`. This is the PIPELINE's version, which is
#: not the same thing as `extractor_version` — the pipeline can be fixed
#: without the extractor changing, and vice versa.
ADAPTER_VERSION = "promise_extraction/v1"

#: The `extractor_version` the stand-in extractor stamps on every row it
#: produces. Deliberately unmistakable: "stub" in the name, "v0" not "v1", and
#: nothing resembling a model identifier. Story 9.4 (embedding) and 10.1
#: (mapping) both read `promises`, and this string is the only thing that
#: tells them — or a human reading the table — that a row came from
#: scaffolding rather than from a real extraction. See this module's docstring.
STUB_EXTRACTOR_VERSION = "stub-rule-based-v0"

#: How many sentences either side of a passage are passed as context. One is
#: enough for the pronoun/antecedent case real campaign copy actually
#: produces ("Insulin costs too much. I'll cap it at $35.") and keeps the
#: prompt small; more context mostly adds unrelated page furniture.
DEFAULT_CONTEXT_SENTENCES = 1

#: Mirrors every other loader's pacing constant against the ~60 req/min
#: ZeroDB ceiling datamodel.md records.
DEFAULT_MIN_INTERVAL_SECONDS = 1.1

#: How many hex characters of the identity sha256 go into an id. 32 hex chars
#: is 128 bits — the same width as the uuid datamodel.md's `prm:{uuid}`
#: pattern asks for, so a stored id is the same shape and length a reader
#: expects, while being deterministic. See `Promise`'s docstring.
_ID_HEX_WIDTH = 32


class PromiseExtractionError(Exception):
    """Extraction could not be completed for a source."""


class QuoteSpanError(PromiseExtractionError):
    """An extractor returned a quote that is not verbatim in the source text.

    Its own exception class, and a hard failure rather than a warning,
    because this is the one error that would silently corrupt a citation.
    prd.md's evidence-over-opinion rule is that every claim links to a primary
    source; a `quote_span` that has been paraphrased, re-punctuated or
    "tidied" still LOOKS like a quote in the UI while no longer being one, and
    nothing downstream could detect it. An extraction that cannot quote
    accurately is refused, not stored with a flag.
    """


@dataclass(frozen=True)
class ExtractedCommitment:
    """One atomic commitment as an extractor read it, before it is a row.

    Deliberately NOT a `Promise`: an extractor knows what the text says, and
    nothing about which candidacy, person or source it came from — the
    pipeline supplies those. Keeping them apart is what lets a real LLM
    extractor be written against a four-field-plus-quote contract without
    knowing anything about ZeroDB.

    `actor`, `action` and `object` are optional because non-testable rhetoric
    genuinely has no object (see `Promise`'s docstring); `Promise`'s own
    validator is what requires them once `testable` is True.
    """

    quote_span: str
    direction: str
    testable: bool
    confidence: float
    actor: str | None = None
    action: str | None = None
    object: str | None = None  # noqa: A003 - matches datamodel.md's field name


@dataclass
class ExtractionReport:
    """What one `extract_from_source` call actually did.

    `new_promises` vs. `updated_promises` is the idempotency signal a caller
    (and this story's live-verification script) needs: a no-op re-run reports
    0 new, which is how "this source was already extracted" is distinguished
    from "this source had nothing in it".
    """

    source_id: str
    candidacy_id: str
    person_id: str
    extractor_version: str
    passages_considered: int = 0
    new_promises: int = 0
    updated_promises: int = 0
    testable_promises: int = 0
    non_testable_promises: int = 0
    promise_ids: list[str] = field(default_factory=list)

    @property
    def total_promises(self) -> int:
        return self.new_promises + self.updated_promises


class PromiseExtractor(Protocol):
    """The LLM call point. One callable, injected, with no default.

    Given one passage and the surrounding `context` text, return zero or more
    commitments the passage makes. Zero is a normal, common answer: most
    sentences on a campaign page are donation furniture, biography or
    navigation, and an extractor that felt obliged to find a commitment in
    every sentence would manufacture promises nobody made.

    Contract the pipeline relies on, and enforces:

      * every `quote_span` is a **verbatim substring of `passage`** — not of
        `context`, and not a cleaned-up version of either. See
        `verify_quote_span` and `QuoteSpanError`.
      * `confidence` is in [0, 1].
      * `direction` is one of `support`, `oppose`, `unclear`.

    Implementations: `RuleBasedStubExtractor` (scaffolding, in this module)
    and — once an LLM credential exists — a real one built from
    `EXTRACTION_PROMPT_TEMPLATE`. See this module's docstring.
    """

    def __call__(self, passage: str, *, context: str) -> Sequence[ExtractedCommitment]: ...


# ===========================================================================
# The prompt design
# ===========================================================================

#: The real extraction prompt, written in full as this story's extraction
#: design even though no credential exists to execute it (see the module
#: docstring). It is a module constant rather than prose in a doc so that a
#: real extractor implementation uses exactly this text, and so the tests can
#: assert its invariants — that it names every output field, carries few-shot
#: examples of both classes, demands a verbatim span, and names no political
#: party (prd.md's nonpartisan-by-construction rule applies to prompt text as
#: much as to scoring code: a party named in a prompt is a party-specific rule
#: hiding in a string literal).
#:
#: THE TESTABLE/NON-TESTABLE HEURISTIC, in prose, because it is the judgement
#: the whole feature turns on:
#:
#: A commitment is **testable** when it names a specific, checkable action and
#: object that some future legislative act could be matched against — a
#: mechanism ("require background checks"), a number ("$35 a month", "2,000
#: dollars a year"), a named bill or program ("the Social Security Fairness
#: Act", "repeal the SALT cap"), or a concrete procedural act ("cosponsor",
#: "vote against cloture on"). The test is operational, not grammatical: could
#: a reader, handed a list of roll-call votes, point at one and say "that vote
#: is for or against this"? If yes, it is testable.
#:
#: It is **non-testable** when it is an aspiration, a value statement, or an
#: expression of concern with no checkable object: "fight for", "stand up
#: for", "believe in", "work tirelessly", "always put X first", "be a voice
#: for". These are shown to the voter (prd.md: "shown but not scored") and
#: never mapped to legislation, because any mapping would be the system's
#: opinion about what the candidate meant rather than evidence of what they
#: said.
#:
#: The distinction is **specificity, never subject matter**. "I support
#: stronger gun safety laws" and "I will vote to require background checks on
#: all gun sales" are the same topic and opposite classifications. That is the
#: property that keeps this nonpartisan: the rule cannot be applied more
#: harshly to one side's rhetoric, because it never looks at whose rhetoric it
#: is.
EXTRACTION_PROMPT_TEMPLATE = """\
You extract political commitments from candidate and officeholder source text
for a nonpartisan voter-information system. Your output is used to check what
a candidate said against how they later voted, so a reader can judge the
record themselves.

Two rules outrank everything else:

1. QUOTE VERBATIM. Every `quote_span` you return MUST be an exact,
   character-for-character substring of the PASSAGE below. Do not fix
   spelling, straighten curly quotes, expand contractions, change dashes,
   trim punctuation, or reword anything. If you cannot find an exact
   substring that carries the commitment, return nothing for it.
2. DO NOT INVENT. Extract only commitments the passage actually makes. Most
   passages make none — they are biography, donation appeals, navigation or
   general comment. Returning an empty list is the correct and common answer.
   Never infer a commitment the speaker did not state, and never guess which
   way an unclear statement points.

Judge every passage by the same standard regardless of who is speaking, which
party they belong to, or what position they take. The classification below
turns on how specific a statement is, never on its subject or its side.

TASK

Read the PASSAGE. CONTEXT is the surrounding text, given only to help you
resolve pronouns and antecedents — never quote from CONTEXT. For each distinct
commitment the PASSAGE makes, emit one object with these fields:

  actor        Who is committed to acting. The speaker when they commit
               themselves ("I will vote..." -> the speaker). Another body when
               the sentence commits that body instead ("Congress must act").
               Null if the passage names no actor at all.
  action       The committed act, as a short verb phrase in the passage's own
               terms: "vote to cap", "cosponsor", "vote against", "introduce".
  object       What the action applies to: "insulin co-pays at $35 per month",
               "the Social Security Fairness Act". Null when the passage names
               no checkable object (see `testable` below).
  direction    "support"  a kept version of this advances the object
               "oppose"   a kept version of this blocks it
               "unclear"  the passage commits to something about the object
                          but does not say which way
  testable     true when the commitment names a specific, checkable action and
               object that a future vote, bill or sponsorship could be matched
               against — a mechanism, a number, a named bill or program, or a
               concrete procedural act.
               false when it is an aspiration or value statement with no
               checkable object: "fight for", "stand up for", "believe in",
               "work tirelessly", "be a voice for". These are kept and shown
               to voters, but never scored, so do NOT invent an object to make
               one look testable.
               The test is: handed a list of roll-call votes, could a reader
               point at one and say "that vote is for or against this"?
  confidence   0.0 to 1.0. How sure you are that this is what the passage
               commits to. Lower it for ambiguous phrasing, an unclear actor,
               or a commitment that may be reported speech rather than a
               promise.
  quote_span   The exact substring of PASSAGE carrying this commitment. Rule 1.

Return ONLY a JSON array, with no prose before or after it. An empty array is
`[]`.

EXAMPLES

Passage: I will vote to cap insulin co-pays at $35 per month.
[{"actor": "the speaker", "action": "vote to cap",
  "object": "insulin co-pays at $35 per month", "direction": "support",
  "testable": true, "confidence": 0.95,
  "quote_span": "I will vote to cap insulin co-pays at $35 per month."}]
# Testable: names a mechanism (a co-pay cap) and a number ($35/month). A
# future roll call on an insulin price cap can confirm or contradict it.

Passage: I'll fight for working families every single day.
[{"actor": "the speaker", "action": "fight for", "object": null,
  "direction": "unclear", "testable": false, "confidence": 0.9,
  "quote_span": "I'll fight for working families every single day."}]
# Not testable: "fight for" names no act and "working families" is not a
# checkable object. No vote can be for or against it. Kept and shown anyway,
# because it is what the candidate said — but never scored. Note `object` is
# null rather than invented, and `direction` is "unclear" rather than guessed
# as "support".

Passage: I support stronger gun safety laws.
[{"actor": "the speaker", "action": "support", "object": null,
  "direction": "unclear", "testable": false, "confidence": 0.85,
  "quote_span": "I support stronger gun safety laws."}]
# THE BORDERLINE CASE, reasoned through: this states a position, and it is
# tempting to call it testable because the topic is legislative. It is not.
# "Stronger" names no mechanism, no number and no bill, so there is no vote a
# reader could point at and say "that one is for or against this" — any
# mapping would be the system deciding which gun bill the speaker must have
# meant, which is opinion, not evidence. Compare the next example: same topic,
# opposite classification, and the only difference is specificity.

Passage: I will vote to require background checks on all gun sales.
[{"actor": "the speaker", "action": "vote to require",
  "object": "background checks on all gun sales", "direction": "support",
  "testable": true, "confidence": 0.95,
  "quote_span": "I will vote to require background checks on all gun sales."}]
# Testable: names the exact mechanism (universal background checks). A roll
# call on a background-check bill maps to it directly.

Passage: Congress must act on climate change before it is too late.
[{"actor": "Congress", "action": "act on", "object": null,
  "direction": "unclear", "testable": false, "confidence": 0.7,
  "quote_span": "Congress must act on climate change"}]
# IMPLICIT ACTOR, reasoned through: the actor is "Congress", not the speaker.
# The speaker is describing what a body ought to do, not committing their own
# future vote, and recording them as the actor would attribute a promise they
# did not make — then score them for breaking it. When a candidate says
# "Congress must act", the honest reading is a stated priority, not a personal
# commitment. Not testable for the same reason as the rhetoric above: "act on"
# names no mechanism. Note the quote_span is a substring, not the whole
# sentence — exactly the words carrying the commitment, still verbatim.

Passage: I’ll vote to cap insulin co-pays at $35/month — no exceptions.
[{"actor": "the speaker", "action": "vote to cap",
  "object": "insulin co-pays at $35/month", "direction": "support",
  "testable": true, "confidence": 0.95,
  "quote_span": "I’ll vote to cap insulin co-pays at $35/month"}]
# QUOTE EXACTNESS: the span keeps the curly apostrophe in "I’ll" and the
# "$35/month" spelling exactly as the page wrote them. Returning "I'll vote
# to cap insulin copays at $35 per month" would be a paraphrase — it reads
# like a quote, is displayed as a quote, and is not one. Such a span is
# REJECTED by the pipeline, and the extraction fails.

Passage: Paid for by the committee. Donate today to join the fight.
[]
# No commitment. Donation furniture. An empty array is correct here.

CONTEXT (for reading only — never quote from this):
{context}

PASSAGE (quote only from this):
{passage}
"""


# ===========================================================================
# Segmentation
# ===========================================================================

#: Abbreviations whose trailing period is not a sentence boundary. Short list
#: on purpose: these are the ones that actually occur in political source text
#: ("Sen. Ossoff", "Rep. Collins", "H.R. 1", "U.S. Senate"), and a long
#: general-purpose list would be a dependency pretending to be a constant.
_ABBREVIATIONS = frozenset(
    {
        "sen",
        "rep",
        "gov",
        "sec",
        "dr",
        "mr",
        "mrs",
        "ms",
        "jr",
        "sr",
        "st",
        "vs",
        "no",
        "u.s",
        "h.r",
        "s.res",
        "h.res",
        "d.c",
        "inc",
        "etc",
        "approx",
    }
)

#: A sentence boundary: terminal punctuation (with any closing quote or
#: bracket) followed by whitespace, OR a newline. The newline half matters
#: more than it looks: `campaign_sites.extract_text` joins each HTML text node
#: with "\n", and those nodes are usually headings or list items with no
#: terminal punctuation at all — without it, a whole page is one "sentence".
_BOUNDARY = re.compile(r'(?<=[.!?])["\'’”)\]]*\s+|\n+')

#: A period that is part of a figure rather than a sentence end: `$35.50`,
#: `2.5 million`. Splitting here truncates a quote span mid-number, which is
#: precisely what the quote-span criterion is about.
_DECIMAL = re.compile(r"\d\.\d")


def segment_sentences(text: str) -> list[str]:
    """Split source text into candidate passages, each a verbatim substring.

    Sentence-level, because a commitment is atomic and one sentence is the
    unit of claim this story's output is about. Regex-based and
    dependency-free on purpose: `pyproject.toml` has no NLP library, and
    adding spacy or nltk (a model download and a large runtime dependency) to
    split on punctuation is the same trade `campaign_sites.py` already refused
    for HTML parsing and PDF rendering.

    **The invariant that matters: every returned string is findable in `text`
    exactly as returned.** Whitespace is trimmed from the ends only — never
    normalised inside — because every quote span is derived from one of these
    and a single re-punctuated character would stop it being a verbatim
    quote. `test_segment_sentences_returns_verbatim_substrings` holds this.

    Two things are deliberately NOT boundaries: a decimal point inside a
    figure (`$35.50`) and the period after a known abbreviation
    (`Sen. Ossoff`), both of which a naive `split(".")` gets wrong by
    truncating a quote mid-claim.
    """
    if not text or not text.strip():
        return []

    sentences: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(text):
        candidate = text[start : match.start()]
        if _is_false_boundary(candidate, text, match.start()):
            continue
        if candidate.strip():
            sentences.append(candidate.strip())
        start = match.end()

    tail = text[start:]
    if tail.strip():
        sentences.append(tail.strip())
    return sentences


def _is_false_boundary(candidate: str, text: str, position: int) -> bool:
    """Whether a matched boundary is inside a figure or after an abbreviation."""
    # `$35.50` — the period has a digit on both sides.
    if position >= 1 and _DECIMAL.search(text[max(0, position - 2) : position + 2]):
        return True
    # `Sen. Ossoff` — the token before the period is a known abbreviation.
    stripped = candidate.rstrip()
    if not stripped.endswith("."):
        return False
    last_token = stripped[:-1].rsplit(None, 1)[-1] if stripped[:-1].strip() else ""
    return last_token.lower().lstrip("(\"'") in _ABBREVIATIONS


def passages_with_context(
    sentences: Sequence[str], *, neighbours: int = DEFAULT_CONTEXT_SENTENCES
) -> list[tuple[str, str]]:
    """Pair each sentence with the text around it, as (passage, context).

    The extractor reads `context` to resolve pronouns and antecedents — real
    campaign copy routinely splits a commitment's object from its verb across
    a sentence boundary ("Insulin costs too much. I'll cap it at $35.") — and
    quotes only from `passage`. See `PromiseExtractor`.
    """
    paired: list[tuple[str, str]] = []
    for index, sentence in enumerate(sentences):
        low = max(0, index - neighbours)
        high = min(len(sentences), index + neighbours + 1)
        paired.append((sentence, " ".join(sentences[low:high])))
    return paired


# ===========================================================================
# Quote-span exactness
# ===========================================================================


def verify_quote_span(quote_span: str, *, source_text: str) -> None:
    """Refuse a quote that is not verbatim in the text it came from.

    Enforced, not hoped for, and a hard failure: see `QuoteSpanError`. This is
    a plain substring check on purpose — no normalisation of whitespace, case,
    quotes or dashes — because any tolerance here is exactly the tolerance
    that lets a tidied-up paraphrase be stored and displayed as a quote.

    Raises:
        QuoteSpanError: the span is empty, or is not a substring of
            `source_text`.
    """
    if not quote_span or not quote_span.strip():
        raise QuoteSpanError(
            "an extracted commitment has an empty quote_span; the verbatim quote is "
            "the citation the promise exists to carry"
        )
    if quote_span not in source_text:
        raise QuoteSpanError(
            f"quote_span {quote_span[:120]!r} is not a verbatim substring of the "
            "source text it was extracted from. The span must be the source's own "
            "characters — a paraphrase, a straightened apostrophe or a trimmed "
            "dash still displays as a quote while no longer being one, so the "
            "extraction is refused rather than stored."
        )


# ===========================================================================
# Identity and idempotency
# ===========================================================================


def dedupe_key(source_id: str, commitment: ExtractedCommitment) -> str:
    """The commitment's identity: what makes two extractions the same promise.

    `(source_id, quote_span, actor, action, object, direction)`. Deliberately
    excludes `confidence` and `testable` — a re-extraction that only revises a
    judgement has restated the same commitment and must update that row, not
    appear beside it. See `Promise`'s docstring.

    The separator is a character that cannot appear in any of the parts, so
    two different tuples cannot collide by concatenation ("ab" + "c" vs. "a" +
    "bc").
    """
    parts = (
        source_id,
        commitment.quote_span,
        commitment.actor or "",
        commitment.action or "",
        commitment.object or "",
        commitment.direction,
    )
    return "\x1f".join(parts)


def promise_id_for(source_id: str, commitment: ExtractedCommitment) -> str:
    """This commitment's deterministic `prm:` id.

    A truncated sha256 of `dedupe_key`, so re-extraction is an idempotent
    upsert rather than a duplicate — datamodel.md rule 1 applied to a record
    whose natural key is its own text. See `Promise`'s docstring for why this
    deviates from datamodel.md's literal `prm:{uuid}` and why that is the
    right call.
    """
    digest = hashlib.sha256(dedupe_key(source_id, commitment).encode("utf-8")).hexdigest()
    return f"prm:{digest[:_ID_HEX_WIDTH]}"


# ===========================================================================
# The stand-in extractor — NOT the real one
# ===========================================================================

#: Verbs that commit to a concrete legislative act. Matched as a phrase so
#: "vote to cap" is caught but a bare "cap" in prose is not.
_COMMITMENT_VERBS = (
    "vote to",
    "vote for",
    "vote against",
    "vote no on",
    "vote yes on",
    "voted to",
    "voted for",
    "voted against",
    "cosponsor",
    "co-sponsor",
    "sponsor",
    "introduce",
    "repeal",
    "pass",
)

#: Aspirational verbs: a commitment to a feeling or an effort, not an act.
#: Their presence does not by itself make a passage non-testable — "I will
#: fight for a $35 insulin cap by voting for H.R. 1" is testable — but with no
#: specific mechanism alongside, they are prd.md's "vague rhetoric".
_ASPIRATIONAL_VERBS = (
    "fight for",
    "fought for",
    "stand up for",
    "stood up for",
    "believe in",
    "believe that",
    "work tirelessly",
    "work hard",
    "work for",
    "committed to",
    "commit to",
    "be a voice for",
    "always stand",
    "always put",
    "support",
    "oppose",
    "protect",
    "defend",
    "deliver for",
    "care about",
)

#: Markers of a specific, checkable object: a figure, a named bill, or a
#: mechanism phrased concretely. This is the crude stand-in for the judgement
#: the real prompt asks a model to make.
_SPECIFICITY_MARKERS = re.compile(
    r"""
    \$\s?[\d,]+                      # a dollar figure: $35, $2,000
  | \b\d[\d,]*\s*(?:percent|%)       # a percentage
  | \b(?:H\.?R\.?|S\.?)\s?\d+\b      # a bill number: H.R. 1, S. 25
  | \b[A-Z][A-Za-z]+\s+(?:Act|Amendment|Resolution)\b   # a named Act
  | \brequire\w*\b | \bcap\w*\b | \brepeal\w*\b | \bban\b | \bbans\b
  | \beliminate\w*\b | \bexempt\w*\b | \bexpand\s+(?:eligibility|coverage)\b
    # A named program plus a concrete fiscal verb. "any bill that cuts
    # Medicaid funding" names no number and no bill id, but it IS checkable:
    # a roll call on a bill cutting Medicaid maps to it directly. Without
    # this, every commitment phrased as blocking a cut to a named program
    # reads as vague rhetoric -- and since "I will vote against cuts to X" is
    # one of the most common forms a real commitment takes, dropping it would
    # bias the stub against negative commitments specifically.
  | \b(?:cut|cuts|cutting|slash\w*|defund\w*|gut|gutting)\s+
    (?:\w+\s+){0,2}
    (?:medicare|medicaid|social\s+security|snap|medicaid\s+funding|funding|benefits)\b
    """,
    re.VERBOSE | re.IGNORECASE,
)

_OPPOSING_MARKERS = (
    "vote against",
    "voted against",
    "vote no",
    "oppose",
    "opposed",
    "block",
    "repeal",
    "against any bill",
)


class RuleBasedStubExtractor:
    """A STAND-IN extractor. NOT the real one. Never measured for precision.

    ===========================================================================
    READ THIS BEFORE TRUSTING ANY OUTPUT OF THIS CLASS
    ===========================================================================

    This is **scaffolding**, not the extractor backlog.md story 9.3's "at least
    90% precision on a 300-promise labeled set" criterion is about. It is a
    handful of regexes and verb lists. It exists for exactly one purpose: to
    drive the pipeline around it — the four-part schema, the
    testable/non-testable split, verbatim quote spans, idempotent upserts, the
    `promises` row shape story 10.1 reads — end to end, against realistic
    text, with no credential and no network, so that all of that is verified
    rather than assumed.

    It will be wrong about real text in ways no rule list can fix: it cannot
    resolve an implicit actor, cannot tell a candidate's own commitment from
    reported speech, cannot read a commitment split across two sentences, and
    will classify by keyword where the real judgement is about meaning. **Its
    precision is not this story's precision and must never be reported as
    such** — see this module's docstring, "THE 90% PRECISION ACCEPTANCE
    CRITERION".

    Every row it produces is stamped `extractor_version="stub-rule-based-v0"`
    so that nothing downstream — and no human reading the table — can mistake
    its output for a real extraction.

    Replace it by implementing `PromiseExtractor` against
    `EXTRACTION_PROMPT_TEMPLATE` once an LLM credential exists. The pipeline
    does not change.
    """

    def __call__(self, passage: str, *, context: str = "") -> list[ExtractedCommitment]:
        """Zero or one commitment per passage. See the class docstring."""
        lowered = passage.lower()
        has_commitment_verb = any(verb in lowered for verb in _COMMITMENT_VERBS)
        has_aspirational_verb = any(verb in lowered for verb in _ASPIRATIONAL_VERBS)
        if not (has_commitment_verb or has_aspirational_verb):
            return []

        specific = _SPECIFICITY_MARKERS.search(passage) is not None
        testable = has_commitment_verb and specific
        direction = self._direction(lowered, testable=testable)
        actor = self._actor(passage)
        action = self._action(lowered)
        obj = self._object(passage, action) if testable else None

        if testable and not (action and obj):
            # The crude splitter could not produce the two parts a testable
            # promise owes (`Promise` would refuse the row). Honest answer:
            # keep the quote, mark it non-testable, let a human or a real
            # extractor revisit it -- never fabricate an object to pass a
            # validator.
            testable = False
            obj = None
            direction = self._direction(lowered, testable=False)

        return [
            ExtractedCommitment(
                quote_span=passage,
                actor=actor,
                action=action,
                object=obj,
                direction=direction,
                testable=testable,
                # Flat, honest numbers rather than a fake score: a rule match
                # carries no real probability, and a precise-looking 0.87
                # would imply a calibration that does not exist.
                confidence=0.6 if testable else 0.3,
            )
        ]

    @staticmethod
    def _direction(lowered: str, *, testable: bool) -> str:
        if any(marker in lowered for marker in _OPPOSING_MARKERS):
            return "oppose"
        if not testable:
            # Rhetoric commits to something about the object without saying
            # which way; "unclear" is the honest reading. See
            # `PromiseDirection`.
            return "unclear"
        return "support"

    @staticmethod
    def _actor(passage: str) -> str | None:
        lowered = passage.lower()
        if re.search(r"\b(i|i'll|i’ll|my|we|we'll|we’ll)\b", lowered):
            return "the speaker"
        for body in ("congress", "the senate", "the house"):
            if body in lowered:
                return body
        return None

    @staticmethod
    def _action(lowered: str) -> str | None:
        for verb in (*_COMMITMENT_VERBS, *_ASPIRATIONAL_VERBS):
            if verb in lowered:
                return verb
        return None

    @staticmethod
    def _object(passage: str, action: str | None) -> str | None:
        """Whatever follows the action verb, as the passage's own words.

        Crude by design — see the class docstring. Returns None rather than a
        guess when the verb is at the end of the passage.
        """
        if not action:
            return None
        lowered = passage.lower()
        index = lowered.find(action)
        if index < 0:
            return None
        tail = passage[index + len(action) :].strip(" .,:;—-")
        return tail or None


# ===========================================================================
# The pipeline
# ===========================================================================


class PromiseExtractionPipeline:
    """Extracts, validates and stores one source's commitments, idempotently.

    `extractor` is the injected LLM call point with **no default** — see this
    module's docstring, "THE EXTRACTOR SEAM". `source_text` is a parameter of
    `extract_from_source` rather than something this class fetches, because
    the archived bytes cannot currently be read back out of ZeroDB Files (see
    the module docstring's second live finding); that also makes every test
    here run with no network at all.
    """

    def __init__(
        self,
        client: ZeroDBClient,
        *,
        extractor: PromiseExtractor | None,
        extractor_version: str,
        pace: Callable[[], None] | None = None,
        context_sentences: int = DEFAULT_CONTEXT_SENTENCES,
    ) -> None:
        """Build the pipeline for one extractor.

        Raises:
            ValueError: `extractor` is None, or `extractor_version` is blank.
                No default extractor is provided deliberately: the only
                candidates would be a real LLM (no credential exists in this
                environment) or `RuleBasedStubExtractor` (which would make
                stand-in output the silent default in production). The caller
                states which it wants.
        """
        if extractor is None:
            raise ValueError(
                "PromiseExtractionPipeline needs an explicit extractor; there is no "
                "default. A real LLM extractor requires a credential this "
                "environment does not have (see this module's docstring), and "
                "defaulting to RuleBasedStubExtractor would ship stand-in "
                "extractions as if they were real ones."
            )
        if not extractor_version or not extractor_version.strip():
            raise ValueError(
                "PromiseExtractionPipeline needs an extractor_version; it is the only "
                "thing that tells a reader of the promises table which extractor "
                "produced a row"
            )
        self._client = client
        self._extractor = extractor
        self._extractor_version = extractor_version.strip()
        self._pace = pace if pace is not None else (lambda: None)
        self._context_sentences = context_sentences
        # No ProvenanceContext: `Promise` declares no `source_url`, and the
        # repository would refuse one. A promise's citability flows through
        # `source_id` to the already-provenanced `promise_sources` row. See
        # `Promise`'s docstring.
        self._promises: Repository[Promise] = Repository(client=client, model=Promise)
        self._sources: Repository[PromiseSource] = Repository(client=client, model=PromiseSource)
        self._candidacies: Repository[Candidacy] = Repository(client=client, model=Candidacy)

    def extract_from_source(self, source_id: str, *, source_text: str) -> ExtractionReport:
        """Extract every commitment in one source's text and store them.

        `source_text` may be HTML or plain text; HTML is converted with story
        9.1's own `extract_text` rather than a second HTML parser.

        Raises:
            PromiseExtractionError: `source_id` does not resolve to a
                `promise_sources` row, or that row's candidacy does not
                resolve. Both mean the caller is extracting from something
                that is not on file, which must not produce an uncitable
                promise.
            QuoteSpanError: the extractor returned a span that is not verbatim
                in the passage it came from.
        """
        source = self._sources.get(source_id)
        if source is None:
            raise PromiseExtractionError(
                f"no promise_sources row {source_id!r}; a promise must cite an "
                "archived source that is actually on file (story 9.1 writes those)"
            )
        candidacy = self._candidacies.get(source.candidacy_id)
        if candidacy is None:
            raise PromiseExtractionError(
                f"promise source {source_id!r} references candidacy "
                f"{source.candidacy_id!r}, which does not resolve; the promise would "
                "have no person to attribute it to"
            )

        text = extract_text_if_html(source_text)
        sentences = segment_sentences(text)
        passages = passages_with_context(sentences, neighbours=self._context_sentences)

        report = ExtractionReport(
            source_id=source_id,
            candidacy_id=candidacy.id or source.candidacy_id,
            person_id=candidacy.person_id,
            extractor_version=self._extractor_version,
            passages_considered=len(passages),
        )

        seen: set[str] = set()
        for passage, context in passages:
            for commitment in self._extractor(passage, context=context):
                # The span must be verbatim in the PASSAGE, not merely
                # somewhere in the page: an extractor that quoted from
                # `context` would attribute a neighbouring sentence's words to
                # this commitment.
                verify_quote_span(commitment.quote_span, source_text=passage)
                promise_id = promise_id_for(source_id, commitment)
                if promise_id in seen:
                    # The same commitment reached twice within one run (a
                    # repeated sentence on the page). Already written; writing
                    # it again would be a wasted request, not a second row.
                    continue
                seen.add(promise_id)
                self._store(commitment, source=source, candidacy=candidacy, report=report)

        logger.info(
            "promise_extraction: %s -> %d promise(s) from %d passage(s) "
            "(%d new, %d updated, %d testable) via %s",
            source_id,
            report.total_promises,
            report.passages_considered,
            report.new_promises,
            report.updated_promises,
            report.testable_promises,
            self._extractor_version,
        )
        return report

    def _store(
        self,
        commitment: ExtractedCommitment,
        *,
        source: PromiseSource,
        candidacy: Candidacy,
        report: ExtractionReport,
    ) -> None:
        """Upsert one commitment as a `promises` row, counting new vs. updated."""
        promise_id = promise_id_for(report.source_id, commitment)
        already_stored = self._promises.get(promise_id) is not None

        self._pace()
        self._promises.upsert(
            {
                "id": promise_id,
                "candidacy_id": report.candidacy_id,
                "person_id": candidacy.person_id,
                "source_id": source.id,
                "quote_span": commitment.quote_span,
                "actor": commitment.actor,
                "action": commitment.action,
                "object": commitment.object,
                "direction": commitment.direction,
                # Every one of these belongs to a later story; see `Promise`'s
                # docstring. Extraction states what the source said and
                # whether it can be checked — nothing more.
                "topic_id": None,
                "stance_id": None,
                "testable": commitment.testable,
                "extractor_version": self._extractor_version,
                "confidence": commitment.confidence,
                "vector_id": None,  # story 9.4 embeds and fills this
                "mappings": [],  # story 10.1 maps and fills this
                "qa_pending": False,  # story 10.1 routes to QA, not extraction
            }
        )

        report.promise_ids.append(promise_id)
        if already_stored:
            report.updated_promises += 1
        else:
            report.new_promises += 1
        if commitment.testable:
            report.testable_promises += 1
        else:
            report.non_testable_promises += 1


def extract_text_if_html(source_text: str) -> str:
    """Plain text from `source_text`, parsing it as HTML only if it is HTML.

    Reuses story 9.1's `app.ingest.campaign_sites.extract_text` (the standard
    library's `html.parser`) rather than adding a second HTML-to-text path —
    there is no BeautifulSoup in this project and no reason to introduce one.
    Plain text is passed through untouched, so a caller with a transcript
    (story 9.2's source types) does not have to pretend it is markup.
    """
    from app.ingest.campaign_sites import extract_text

    looks_like_html = "<" in source_text and ">" in source_text
    return extract_text(source_text) if looks_like_html else source_text


__all__ = [
    "ADAPTER_VERSION",
    "DEFAULT_CONTEXT_SENTENCES",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "EXTRACTION_PROMPT_TEMPLATE",
    "STUB_EXTRACTOR_VERSION",
    "ExtractedCommitment",
    "ExtractionReport",
    "PromiseExtractionError",
    "PromiseExtractionPipeline",
    "PromiseExtractor",
    "QuoteSpanError",
    "RuleBasedStubExtractor",
    "dedupe_key",
    "extract_text_if_html",
    "passages_with_context",
    "promise_id_for",
    "segment_sentences",
    "verify_quote_span",
]
