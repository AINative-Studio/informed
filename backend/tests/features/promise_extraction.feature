Feature: Extract atomic commitments from promise sources (story 9.3)
  As the system
  I want text turned into atomic commitments (actor, action, object, direction)
    flagged as testable or not
  So that promises can be checked against a legislative record

  # Every scenario below runs through the explicitly-labeled
  # RuleBasedStubExtractor, NOT a real LLM extractor -- no LLM credential
  # exists in this environment (see app/ingest/promise_extraction.py's module
  # docstring, "The 90% precision acceptance criterion"). These scenarios
  # therefore prove the PIPELINE is correct -- schema, quote-span exactness,
  # idempotency, the testable/non-testable split, the row shape story 10.1
  # will read -- and deliberately claim nothing about extraction precision.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist
    And a candidacy "us-2026-11-03-general:ocd-division/country:us/state:ga:person:bioguide-O000175" exists
    And an archived promise source for that candidacy

  # --- the acceptance criterion: atomic commitments with all four parts ------

  Scenario: A specific commitment is extracted as an atomic four-part record
    Given the source text contains "I will vote to cap insulin co-pays at $35 per month."
    When I extract promises from that source
    Then exactly 1 promise row exists for that source
    And that promise has an actor, an action and an object
    And that promise has direction "support"
    And that promise is marked testable

  Scenario: An opposing commitment is extracted with direction "oppose"
    Given the source text contains "I will vote against any bill that cuts Medicaid funding."
    When I extract promises from that source
    Then exactly 1 promise row exists for that source
    And that promise has direction "oppose"
    And that promise is marked testable

  Scenario: An extracted promise carries the candidacy and the person it belongs to
    Given the source text contains "I will vote to cap insulin co-pays at $35 per month."
    When I extract promises from that source
    Then that promise references the candidacy
    And that promise references the same person as the candidacy
    And that promise references the promise source it came from

  Scenario: An extracted promise records which extractor version produced it
    Given the source text contains "I will vote to cap insulin co-pays at $35 per month."
    When I extract promises from that source
    Then that promise has extractor_version "stub-rule-based-v0"
    And that promise has a confidence between 0 and 1

  Scenario: An extracted promise is not pre-assigned to a topic, a stance or a mapping
    Given the source text contains "I will vote to cap insulin co-pays at $35 per month."
    When I extract promises from that source
    Then that promise has no topic_id and no stance_id
    And that promise has no mappings
    And that promise has no vector_id
    And that promise is not flagged qa_pending

  # --- the acceptance criterion: vague rhetoric is tagged not testable ------

  Scenario: Vague rhetoric is extracted but tagged not testable
    Given the source text contains "I'll fight for working families every single day."
    When I extract promises from that source
    Then exactly 1 promise row exists for that source
    And that promise is marked not testable

  Scenario: A value statement with no checkable mechanism is tagged not testable
    Given the source text contains "I believe in standing up for Georgia."
    When I extract promises from that source
    Then that promise is marked not testable

  Scenario: A stated position with no specific mechanism is tagged not testable
    Given the source text contains "I support stronger gun safety laws."
    When I extract promises from that source
    Then that promise is marked not testable

  Scenario: A named, checkable mechanism on the same topic IS testable
    Given the source text contains "I will vote to require background checks on all gun sales."
    When I extract promises from that source
    Then that promise is marked testable

  Scenario: A non-testable promise is never flagged for mapping QA
    Given the source text contains "I'll fight for working families every single day."
    When I extract promises from that source
    Then that promise is not flagged qa_pending

  Scenario: Testable and non-testable commitments from one source are both kept
    Given the source text contains "I will vote to cap insulin co-pays at $35 per month. I'll fight for working families every single day."
    When I extract promises from that source
    Then exactly 2 promise rows exist for that source
    And 1 of those promises is testable
    And 1 of those promises is not testable

  # --- the acceptance criterion: the quote span is kept ---------------------

  Scenario: The quote span is a verbatim substring of the source text
    Given the source text contains "I will vote to cap insulin co-pays at $35 per month."
    When I extract promises from that source
    Then every promise quote_span is a verbatim substring of the source text

  Scenario: The quote span is kept character-for-character, not cleaned up
    Given the source text contains "I’ll vote to cap insulin co-pays at $35/month — no exceptions."
    When I extract promises from that source
    Then every promise quote_span is a verbatim substring of the source text
    And that promise quote_span still contains "$35/month"

  Scenario: An extractor that paraphrases rather than quoting is refused
    Given an extractor that returns a paraphrase instead of a verbatim quote
    And the source text contains "I will vote to cap insulin co-pays at $35 per month."
    When I try to extract promises from that source
    Then the extraction is refused for a quote span that is not in the source

  # --- idempotency -----------------------------------------------------------

  Scenario: Re-extracting the same source writes no duplicate promise rows
    Given the source text contains "I will vote to cap insulin co-pays at $35 per month. I'll fight for working families every single day."
    And I have already extracted promises from that source once
    When I extract promises from that source again
    Then exactly 2 promise rows exist for that source

  Scenario: A re-run reports that it added no new promises
    Given the source text contains "I will vote to cap insulin co-pays at $35 per month."
    And I have already extracted promises from that source once
    When I extract promises from that source again
    Then the extraction report says 0 promises were new

  Scenario: A genuinely different commitment in a changed source is added
    Given the source text contains "I will vote to cap insulin co-pays at $35 per month."
    And I have already extracted promises from that source once
    When I extract promises from a source text with one more commitment
    Then exactly 2 promise rows exist for that source

  # --- the LLM seam: no real extractor is configured in this environment ----

  Scenario: The pipeline refuses to run with no extractor configured
    When I try to build an extraction pipeline with no extractor
    Then the pipeline is refused for having no extractor configured
