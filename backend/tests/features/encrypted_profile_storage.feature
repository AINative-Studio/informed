# Story 14.1 — Encrypted profile storage
#
# "As a voter, I want my interests encrypted so that even the database operator
# can't read them, so that my political views stay private."
#
# Acceptance: AES-256-GCM per-user data keys via KMS; HMAC row ids; plaintext
# never logged.
#
# Every interest in this file is deliberately, obviously synthetic
# ("test-topic-alpha", not a real political issue). CLAUDE.md: never put real
# voter data in fixtures, logs or test output — and a profile shaped like a real
# voter's opinions IS the data this story exists to protect.

Feature: Encrypted voter profile storage
  A voter profile is special-category political-opinion data. The store must be
  unreadable to whoever holds the database, keyed so a dump cannot be joined
  back to accounts, and silent: no interest and no key material ever reaches a
  log line.

  Background:
    Given a profile store backed by a per-user envelope encryption scheme

  Scenario: A voter's profile is stored encrypted
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    When the voter saves their profile
    Then the stored row's payload is not readable as plaintext
    And the stored row contains no interest topic id anywhere in it
    And the stored payload decrypts back to the same profile

  Scenario: The stored row carries only locator and key metadata
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    When the voter saves their profile
    Then the stored row's fields are exactly id, version, locale, jurisdiction_ids, updated_at, key_ref and payload

  Scenario: The same user id always produces the same row id
    When a row id is derived for user "synthetic-user-1"
    And a row id is derived again for user "synthetic-user-1"
    Then both row ids are identical
    And the row id starts with "vp:"

  Scenario: A different user id produces a different row id
    When a row id is derived for user "synthetic-user-1"
    And a row id is derived for user "synthetic-user-2"
    Then the two row ids differ

  Scenario: A row id does not reveal the user id it was built from
    When a row id is derived for user "synthetic-user-1"
    Then the row id does not contain the user id
    And the row id cannot be derived without the id key

  Scenario: Each save bumps the version
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    When the voter saves their profile
    Then the stored version is 1
    When the voter saves their profile again
    Then the stored version is 2
    And the profile history records the previous version without its contents

  Scenario: Deleting a profile shreds the key and makes the data unrecoverable
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    And the voter has saved their profile
    When the voter deletes their profile
    Then the profile row is gone
    And the wrapped data key for that voter is gone
    And decrypting a retained copy of the ciphertext now fails

  Scenario: Plaintext is never logged
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    When the voter saves their profile and reads it back while logs are captured
    Then no captured log line contains any interest topic id
    And no captured log line contains any key material

  Scenario: A profile needs at least one primary interest
    Given a synthetic voter profile with no primary interests
    Then the profile is rejected as invalid

  Scenario: At most five primary interests are allowed
    Given a synthetic voter profile with 6 primary interests
    Then the profile is rejected as invalid

  Scenario: Five primary interests are allowed
    Given a synthetic voter profile with 5 primary interests
    Then the profile is accepted

  Scenario: At most eight secondary interests are allowed
    Given a synthetic voter profile with 9 secondary interests
    Then the profile is rejected as invalid

  Scenario: Nice-to-have interests are unlimited
    Given a synthetic voter profile with 20 nice-to-have interests
    Then the profile is accepted

  Scenario: A dealbreaker is allowed only on a primary interest
    Given a synthetic voter profile with a dealbreaker on a secondary interest
    Then the profile is rejected as invalid

  Scenario: A dealbreaker on a primary interest is allowed
    Given a synthetic voter profile with a dealbreaker on a primary interest
    Then the profile is accepted

  Scenario: Tier weights are not hard-coded in the profile
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    Then no interest carries a weight of its own

  Scenario: Anonymous mode writes no row
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    When the voter uses the app in anonymous mode
    Then no profile row is written
    And no wrapped data key is written
