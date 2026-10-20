Feature: House member positions with denormalized roll-call fields (story 5.2)
  As the system
  I want each member's position stored with denormalized roll-call fields
  So that scoring reads one table

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist
    And every representative in the sample members response exists as a people row

  # --- the acceptance criterion: the five denormalized fields --------------

  Scenario: Ingesting a roll call writes a member_vote per voting member
    When I ingest the sample House roll call "item_final_passage" with member positions "members_yea_nay"
    Then 5 member_votes rows exist for roll call "us-house:119-2:452"

  Scenario: A member_vote row is keyed by roll call and bare bioguide
    When I ingest the sample House roll call "item_final_passage" with member positions "members_yea_nay"
    Then the member_vote "us-house:119-2:452:A000055" is stored

  Scenario: A member_vote row carries every denormalized roll-call field
    When I ingest the sample House roll call "item_final_passage" with member positions "members_yea_nay"
    Then the member_vote "us-house:119-2:452:A000055" has voted_at matching roll call "us-house:119-2:452"
    And the member_vote "us-house:119-2:452:A000055" has vote_type matching roll call "us-house:119-2:452"
    And the member_vote "us-house:119-2:452:A000055" has bill_id matching roll call "us-house:119-2:452"
    And the member_vote "us-house:119-2:452:A000055" has topic_ids matching roll call "us-house:119-2:452"
    And the member_vote "us-house:119-2:452:A000055" has is_bundled matching roll call "us-house:119-2:452"

  Scenario: A member_vote row records the member's own position
    When I ingest the sample House roll call "item_final_passage" with member positions "members_yea_nay"
    Then the member_vote "us-house:119-2:452:A000055" has position "yea"
    And the member_vote "us-house:119-2:452:B001292" has position "nay"

  # --- voteCast vocabularies: the live source publishes two ------------------

  Scenario: Yea and Nay map to the documented position vocabulary
    When I transform the sample member positions "members_yea_nay"
    Then the transformed positions include "yea"
    And the transformed positions include "nay"
    And the transformed positions include "not_voting"

  Scenario: Committee-of-the-Whole Aye and No map onto yea and nay
    When I transform the sample member positions "members_aye_no"
    Then the transformed positions include "yea"
    And the transformed positions include "nay"
    And no transformed position is "aye"
    And no transformed position is "no"

  Scenario: Present is its own position, not a yea and not a nay
    When I transform the sample member positions "members_present"
    Then the transformed positions include "present"

  Scenario: A voteCast value this loader has never seen is skipped, not guessed
    When I transform the sample member positions "members_yea_nay" with one voteCast replaced by "Sleeping"
    Then exactly 1 member position is skipped
    And no transformed position is "sleeping"

  Scenario: A member with no bioguideID is skipped, not written with a guessed id
    When I transform the sample member positions "members_yea_nay" with one bioguideID removed
    Then exactly 1 member position is skipped

  Scenario: A member whose bioguide has no people row is skipped
    When I ingest the sample House roll call "item_final_passage" with member positions "members_yea_nay" and bioguide "A000055" missing from people
    Then 4 member_votes rows exist for roll call "us-house:119-2:452"
    And the member_vote "us-house:119-2:452:A000055" is not stored

  # --- the drift check -------------------------------------------------------

  Scenario: Freshly ingested member_votes pass the denormalization drift check
    When I ingest the sample House roll call "item_final_passage" with member positions "members_yea_nay"
    Then the member_votes drift check passes

  Scenario: The drift check catches a member_vote whose denormalized field was tampered with
    Given I have ingested the sample House roll call "item_final_passage" with member positions "members_yea_nay"
    When the vote_type on member_vote "us-house:119-2:452:A000055" is changed to "procedural" behind the loader's back
    Then the member_votes drift check fails for member_vote "us-house:119-2:452:A000055"

  Scenario: Re-ingesting repairs drift without waiting for the nightly repair job
    Given I have ingested the sample House roll call "item_final_passage" with member positions "members_yea_nay"
    And the vote_type on member_vote "us-house:119-2:452:A000055" is changed to "procedural" behind the loader's back
    When I ingest the sample House roll call "item_final_passage" with member positions "members_yea_nay"
    Then the member_votes drift check passes

  Scenario: A roll call whose own fields change re-stamps every member_vote
    Given I have ingested the sample House roll call "item_final_passage" with member positions "members_yea_nay"
    When the source reclassifies roll call "us-house:119-2:452" as a procedural vote and I ingest it again with member positions "members_yea_nay"
    Then the member_vote "us-house:119-2:452:A000055" has vote_type "procedural"
    And the member_votes drift check passes

  # --- idempotency -----------------------------------------------------------

  Scenario: Re-ingesting the same roll call does not duplicate member_votes rows
    Given I have ingested the sample House roll call "item_final_passage" with member positions "members_yea_nay"
    When I ingest the sample House roll call "item_final_passage" with member positions "members_yea_nay"
    Then 5 member_votes rows exist for roll call "us-house:119-2:452"
    And exactly 1 row carries the member_vote id "us-house:119-2:452:A000055"

  # --- the live-source tally bug story 5.1 could not see --------------------

  Scenario: A live-shaped votePartyTotal list is summed into the roll call tally
    When I ingest the live-shaped House roll call "live_item_119_1_240"
    Then the roll call "us-house:119-1:240" has tally yea 397 nay 1 present 0 not_voting 32
