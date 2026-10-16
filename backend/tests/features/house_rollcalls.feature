Feature: House roll-call ingest (story 5.1)
  As the system
  I want House roll calls ingested from the Congress.gov house-vote API
  So that votes are available to score

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist

  # --- the acceptance criterion: ids, source_url, content_hash, provenance --

  Scenario: Ingesting a roll call writes it under the documented id pattern
    When I ingest the sample roll call "item_final_passage"
    Then the roll call "us-house:119-2:452" is stored

  Scenario: Ingesting a roll call records its source_url
    When I ingest the sample roll call "item_final_passage"
    Then the roll call "us-house:119-2:452" has source_url "https://api.congress.gov/v3/house-vote/119/2/452"

  Scenario: Ingesting a roll call records a content_hash
    When I ingest the sample roll call "item_final_passage"
    Then the roll call "us-house:119-2:452" has a content_hash

  Scenario: Ingesting a roll call writes a matching provenance_log entry
    When I ingest the sample roll call "item_final_passage"
    Then a provenance_log entry exists for roll_calls "us-house:119-2:452"
    And that provenance_log entry has source_url "https://api.congress.gov/v3/house-vote/119/2/452"
    And that provenance_log entry has adapter_version "house_rollcalls/v1"

  # --- field transform correctness -------------------------------------------

  Scenario: A final-passage suspension vote is classified final_passage
    When I ingest the sample roll call "item_final_passage"
    Then the roll call "us-house:119-2:452" has vote_type "final_passage"
    And the roll call "us-house:119-2:452" has result "Passed"
    And the roll call "us-house:119-2:452" has tally yea 408 nay 15 present 1 not_voting 5

  Scenario: An amendment vote is classified amendment and carries the amendment reference
    When I ingest the sample roll call "item_amendment"
    Then the roll call "us-house:119-2:451" has vote_type "amendment"
    And the roll call "us-house:119-2:451" has amendment_ref "HAMDT 6"

  Scenario: A motion to recommit is classified motion_to_recommit
    When I ingest the sample roll call "item_motion_to_recommit"
    Then the roll call "us-house:119-2:453" has vote_type "motion_to_recommit"

  Scenario: A previous-question vote is classified procedural
    When I ingest the sample roll call "item_cloture_like_procedural"
    Then the roll call "us-house:119-2:454" has vote_type "procedural"

  Scenario: A vote question this loader does not recognize falls back to other
    When I ingest the sample roll call "item_unrecognized_question"
    Then the roll call "us-house:119-2:455" has vote_type "other"

  Scenario: A roll call with no party totals still ingests with a zeroed tally
    When I ingest the sample roll call "item_missing_totals"
    Then the roll call "us-house:119-2:450" has tally yea 0 nay 0 present 0 not_voting 0

  # --- idempotency -------------------------------------------------------------

  Scenario: Re-ingesting an unchanged roll call does not duplicate the row
    Given I have already ingested the sample roll call "item_final_passage" once
    When I ingest the sample roll call "item_final_passage" again
    Then exactly 1 row carries the roll call id "us-house:119-2:452"

  Scenario: Re-ingesting an unchanged roll call does not duplicate its provenance entry
    Given I have already ingested the sample roll call "item_final_passage" once
    When I ingest the sample roll call "item_final_passage" again
    Then exactly 1 provenance_log row exists for roll_calls "us-house:119-2:452"

  Scenario: Re-ingesting a changed roll call writes a new provenance entry
    Given I have already ingested the sample roll call "item_final_passage" once
    When the source updates roll call "us-house:119-2:452" with a corrected result and I ingest it again
    Then exactly 1 row carries the roll call id "us-house:119-2:452"
    And the roll call "us-house:119-2:452" has result "Agreed to"
    And exactly 2 provenance_log rows exist for roll_calls "us-house:119-2:452"
