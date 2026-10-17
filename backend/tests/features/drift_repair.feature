Feature: Drift repair for denormalized member_votes fields (story 16.4)

  As the system
  I want denormalized member_votes fields repaired when their roll call changes
  So that copies never disagree

  # datamodel.md: the italicised fields on member_votes (voted_at, vote_type,
  # bill_id, topic_ids, is_bundled) are denormalized copies of their roll_calls
  # row's own fields, kept in sync by this repair rather than by the write path
  # (re-reading the roll call on every one of ~600k member_votes writes per
  # Congress would be too costly — see MemberVote's own docstring). This is
  # check 4 from integrity_checks.feature, built out into an actual REPAIR: it
  # finds the mismatches and re-copies the correct values, idempotently.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist
    And a synced roll call "us-house:119-2:452" with vote_type "final_passage" and bill_id "bill:hr-1"

  Scenario: Repair is a safe no-op when nothing has drifted
    Given a synced member vote on "us-house:119-2:452" for person "person:B000944"
    When I run the drift repair
    Then the repair reports 0 row(s) repaired
    And the member_votes row "us-house:119-2:452:B000944" still has vote_type "final_passage"

  Scenario: Repair re-copies a single drifted field
    Given a synced member vote on "us-house:119-2:452" for person "person:B000944"
    And the synced roll call is corrected to vote_type "cloture"
    When I run the drift repair
    Then the repair reports 1 row(s) repaired
    And the member_votes row "us-house:119-2:452:B000944" now has vote_type "cloture"

  Scenario: Repair re-copies every drifted field on one row in a single pass
    Given a synced member vote on "us-house:119-2:452" for person "person:B000944"
    And the synced roll call is corrected to vote_type "cloture" and bill_id "bill:hr-2"
    When I run the drift repair
    Then the repair reports 1 row(s) repaired
    And the member_votes row "us-house:119-2:452:B000944" now has vote_type "cloture"
    And the member_votes row "us-house:119-2:452:B000944" now has bill_id "bill:hr-2"

  Scenario: Repair leaves undrifted fields and the vote's own position untouched
    Given a synced member vote on "us-house:119-2:452" for person "person:B000944"
    And the synced roll call is corrected to vote_type "cloture"
    When I run the drift repair
    Then the member_votes row "us-house:119-2:452:B000944" still has position "yea"
    And the member_votes row "us-house:119-2:452:B000944" still has bill_id "bill:hr-1"

  Scenario: Repairing twice in a row is idempotent
    Given a synced member vote on "us-house:119-2:452" for person "person:B000944"
    And the synced roll call is corrected to vote_type "cloture"
    When I run the drift repair
    And I run the drift repair again
    Then the second repair reports 0 row(s) repaired

  Scenario: Repair finds and fixes drift across multiple member_votes rows for the same roll call
    Given a synced member vote on "us-house:119-2:452" for person "person:B000944"
    And a synced member vote on "us-house:119-2:452" for person "person:A000001"
    And the synced roll call is corrected to vote_type "cloture"
    When I run the drift repair
    Then the repair reports 2 row(s) repaired
    And the member_votes row "us-house:119-2:452:B000944" now has vote_type "cloture"
    And the member_votes row "us-house:119-2:452:A000001" now has vote_type "cloture"

  Scenario: Repair is vacuously a no-op when member_votes is empty
    When I run the drift repair
    Then the repair reports 0 row(s) repaired
