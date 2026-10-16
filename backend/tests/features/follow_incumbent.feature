# Story 15.1 — Follow an incumbent
#
# "As an engaged citizen, I want to follow an incumbent, so that I can track
# them over their term."
#
# Acceptance: Follows are stored inside the encrypted profile payload.
#
# Every person followed in this file is either a deliberately-synthetic test
# id ("person:zzz-synthetic-...") or a reference to a row this scenario itself
# seeds into the fake `people` table — never a real legislator's bioguide id.
# CLAUDE.md: never put real voter data in fixtures, logs or test output; the
# live verification script (scripts/verify_follow_incumbent_live.py) is the
# one place a real legislator is used, and only to confirm existence, never as
# test voter data.

Feature: Follow an incumbent
  A voter can follow a sitting member of Congress to track them over their
  term. The follow list lives inside the same encrypted profile payload as
  every other interest — never a separate, unencrypted table — and only a
  person who actually exists in the live `people` table can be followed.

  Background:
    Given a profile store backed by a per-user envelope encryption scheme
    And a real legislator "person:zzz-synthetic-B000001" exists in the people table

  Scenario: A voter can follow a real incumbent
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    And the voter has saved their profile
    When the voter follows "person:zzz-synthetic-B000001"
    Then "person:zzz-synthetic-B000001" is in the voter's followed persons

  Scenario: Following a nonexistent person_id is rejected
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    And the voter has saved their profile
    When the voter tries to follow "person:zzz-does-not-exist"
    Then the follow is rejected because the person does not exist
    And "person:zzz-does-not-exist" is not in the voter's followed persons

  Scenario: A voter can unfollow someone they followed
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    And the voter has saved their profile
    And the voter follows "person:zzz-synthetic-B000001"
    When the voter unfollows "person:zzz-synthetic-B000001"
    Then "person:zzz-synthetic-B000001" is not in the voter's followed persons

  Scenario: Follows persist across save and load
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    And the voter has saved their profile
    And the voter follows "person:zzz-synthetic-B000001"
    When the voter's profile is reloaded fresh from storage
    Then "person:zzz-synthetic-B000001" is in the voter's followed persons

  Scenario: Follows live inside the encrypted payload, not a separate table
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    And the voter has saved their profile
    And the voter follows "person:zzz-synthetic-B000001"
    Then the stored row's payload is not readable as plaintext
    And the stored row contains no followed person id anywhere in it
    And the stored row's fields are exactly id, version, locale, jurisdiction_ids, updated_at, key_ref and payload
