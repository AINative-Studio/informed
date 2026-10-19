Feature: Senate roll-call ingest (story 8.1)
  As the system
  I want Senate roll calls ingested from the senate.gov LIS XML feed
  So that senators are scored too

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist
    And every senator in the sample crosswalk exists as a people row

  # --- the acceptance criterion: ids, source_url, content_hash, provenance --

  Scenario: Ingesting a roll call writes it under the documented id pattern
    When I ingest the sample roll call "item_cloture_motion_to_proceed"
    Then the roll call "us-senate:119-2:254" is stored

  Scenario: Ingesting a roll call records its source_url
    When I ingest the sample roll call "item_cloture_motion_to_proceed"
    Then the roll call "us-senate:119-2:254" has a source_url

  Scenario: Ingesting a roll call records a content_hash
    When I ingest the sample roll call "item_cloture_motion_to_proceed"
    Then the roll call "us-senate:119-2:254" has a content_hash

  Scenario: Ingesting a roll call writes a matching provenance_log entry
    When I ingest the sample roll call "item_cloture_motion_to_proceed"
    Then a provenance_log entry exists for roll_calls "us-senate:119-2:254"
    And that provenance_log entry has adapter_version "senate_rollcalls/v1"

  Scenario: This story's coverage reaches the 118th Congress, not just the 119th
    When I ingest the sample roll call "item_final_passage_118"
    Then the roll call "us-senate:118-1:77" is stored

  # --- field transform correctness -------------------------------------------

  Scenario: A bill final-passage vote is classified final_passage
    When I ingest the sample roll call "item_final_passage_118"
    Then the roll call "us-senate:118-1:77" has vote_type "final_passage"
    And the roll call "us-senate:118-1:77" has result "Bill Passed"
    And the roll call "us-senate:118-1:77" has tally yea 66 nay 30 present 0 not_voting 4

  Scenario: An amendment vote is classified amendment and carries the amendment reference verbatim
    When I ingest the sample roll call "item_amendment"
    Then the roll call "us-senate:119-2:100" has vote_type "amendment"
    And the roll call "us-senate:119-2:100" has amendment_ref "S.Amdt. 5235"

  Scenario: A motion to table is classified procedural
    When I ingest the sample roll call "item_procedural_motion_to_table"
    Then the roll call "us-senate:118-1:340" has vote_type "procedural"

  Scenario: A bare nomination with no cloture prefix is classified other, not final_passage
    When I ingest the sample roll call "item_nomination"
    Then the roll call "us-senate:119-2:1" has vote_type "other"
    And the roll call "us-senate:119-2:1" has result "Nomination Confirmed"

  # --- cloture: this story's highest-value classification case --------------

  Scenario: Cloture on a motion to proceed to a specific bill is classified cloture, not final_passage
    When I ingest the sample roll call "item_cloture_motion_to_proceed"
    Then the roll call "us-senate:119-2:254" has vote_type "cloture"

  Scenario: Cloture on a nomination is classified cloture, not other
    When I ingest the sample roll call "item_cloture_nomination"
    Then the roll call "us-senate:119-2:255" has vote_type "cloture"
    And the roll call "us-senate:119-2:255" has result "Cloture Motion Agreed to"

  # --- member_votes: this story's wider scope than 5.1's roll_calls-only ----

  Scenario: Ingesting a roll call also writes member_votes for every mapped senator
    When I ingest the sample roll call "item_cloture_motion_to_proceed" with member positions
    Then 6 member_votes rows exist for roll call "us-senate:119-2:254"

  Scenario: A member_vote row carries the roll call's denormalized fields
    When I ingest the sample roll call "item_cloture_motion_to_proceed" with member positions
    Then the member_vote for roll call "us-senate:119-2:254" and bioguide "person:B001261" has position "yea"
    And the member_vote for roll call "us-senate:119-2:254" and bioguide "person:B001261" has vote_type "cloture"

  Scenario: A senator with no bioguide mapping is skipped, not written with a guessed id
    When I ingest the sample roll call "item_amendment" with a partial crosswalk missing bioguide "S428"
    Then no member_votes row references lis id "S428"
    And exactly 5 member_votes rows exist for roll call "us-senate:119-2:100"

  # --- idempotency -------------------------------------------------------------

  Scenario: Re-ingesting an unchanged roll call does not duplicate the row
    Given I have already ingested the sample roll call "item_cloture_motion_to_proceed" once
    When I ingest the sample roll call "item_cloture_motion_to_proceed" again
    Then exactly 1 row carries the roll call id "us-senate:119-2:254"

  Scenario: Re-ingesting an unchanged roll call does not duplicate its provenance entry
    Given I have already ingested the sample roll call "item_cloture_motion_to_proceed" once
    When I ingest the sample roll call "item_cloture_motion_to_proceed" again
    Then exactly 1 provenance_log row exists for roll_calls "us-senate:119-2:254"

  Scenario: Re-ingesting the same roll call with member positions does not duplicate member_votes rows
    Given I have already ingested the sample roll call "item_cloture_motion_to_proceed" with member positions once
    When I ingest the sample roll call "item_cloture_motion_to_proceed" with member positions again
    Then 6 member_votes rows exist for roll call "us-senate:119-2:254"
