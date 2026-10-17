Feature: Nightly data integrity checks (story 16.2)

  As an operator
  I want the five integrity checks from the data model run nightly
  So that broken references are caught

  # datamodel.md, "Integrity checks" — the five checks this feature covers,
  # in order:
  #
  #   1. Every member_votes.roll_call_id resolves to a roll_calls row, and
  #      every candidacies.person_id resolves to a people row.
  #   2. Every kept/partial/broken verdict has at least one approved mapping
  #      to a recorded roll call.
  #   3. Exactly one is_current = true row exists per (promise) in verdicts
  #      and per (person, stance) in candidate_positions.
  #   4. The denormalized fields on member_votes match their roll_calls row;
  #      any drift triggers a re-copy. (Detection here; repair is 16.4, see
  #      drift_repair.feature.)
  #   5. Every NoSQL row with a source_url has a matching provenance_log
  #      entry, so no evidence goes untraced.
  #
  # Checks 2 and 3 depend on verdicts/candidate_positions, which hold ZERO
  # real rows today (Epic 6/11 have not built real scoring yet). They must
  # still be correctly built: a check over an empty table finds no violation
  # because there is nothing to violate (vacuously true), proven here with
  # synthetic data that simulates a violation so the check logic itself is
  # verified sound, not merely untested.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist

  # --- check 1: dangling references ------------------------------------------

  Scenario: Check 1 passes when every member_votes and candidacies reference resolves
    Given a roll_calls row "us-house:119-2:452"
    And a people row "person:B000944"
    And a member_votes row for roll call "us-house:119-2:452" and person "person:B000944"
    And a candidacies row for person "person:B000944"
    When I run the reference integrity check
    Then the reference integrity check reports no violations

  Scenario: Check 1 catches a member_votes row whose roll_call_id resolves to nothing
    Given a people row "person:B000944"
    And a member_votes row is written directly with a dangling roll_call_id "us-house:119-2:999"
    When I run the reference integrity check
    Then the reference integrity check reports a violation for member_votes row referencing roll_call_id "us-house:119-2:999"

  Scenario: Check 1 catches a candidacies row whose person_id resolves to nothing
    Given a candidacies row is written directly with a dangling person_id "person:ghost"
    When I run the reference integrity check
    Then the reference integrity check reports a violation for candidacies row referencing person_id "person:ghost"

  Scenario: Check 1 passes vacuously when member_votes and candidacies are both empty
    When I run the reference integrity check
    Then the reference integrity check reports no violations

  # --- check 2: verdict -> approved roll-call mapping ------------------------

  Scenario: Check 2 passes vacuously when verdicts is empty
    When I run the verdict roll-call mapping check
    Then the verdict roll-call mapping check reports no violations

  Scenario: Check 2 passes when a kept verdict's promise has an approved roll-call mapping
    Given a roll_calls row "us-house:119-2:452"
    And a promise "prm:0001" with an approved mapping to roll call "us-house:119-2:452"
    And a verdict for promise "prm:0001" with value "kept"
    When I run the verdict roll-call mapping check
    Then the verdict roll-call mapping check reports no violations

  Scenario: Check 2 catches a kept verdict whose promise has no mapping at all
    Given a promise "prm:0001" with no mappings
    And a verdict for promise "prm:0001" with value "kept"
    When I run the verdict roll-call mapping check
    Then the verdict roll-call mapping check reports a violation for verdict referencing promise "prm:0001"

  Scenario: Check 2 catches a broken verdict whose promise's only mapping is not approved
    Given a roll_calls row "us-house:119-2:452"
    And a promise "prm:0002" with an unapproved mapping to roll call "us-house:119-2:452"
    And a verdict for promise "prm:0002" with value "broken"
    When I run the verdict roll-call mapping check
    Then the verdict roll-call mapping check reports a violation for verdict referencing promise "prm:0002"

  Scenario: Check 2 catches an approved mapping pointing at a roll call that does not exist
    Given a promise "prm:0004" with an approved mapping to roll call "us-house:119-2:nonexistent"
    And a verdict for promise "prm:0004" with value "partial"
    When I run the verdict roll-call mapping check
    Then the verdict roll-call mapping check reports a violation for verdict referencing promise "prm:0004"

  Scenario: Check 2 catches a verdict whose promise_id does not resolve to any promise
    Given a verdict for promise "prm:ghost" with value "kept"
    When I run the verdict roll-call mapping check
    Then the verdict roll-call mapping check reports a violation for verdict referencing promise "prm:ghost"

  Scenario: Check 2 does not require a mapping for a not_testable verdict
    Given a promise "prm:0003" with no mappings
    And a verdict for promise "prm:0003" with value "not_testable"
    When I run the verdict roll-call mapping check
    Then the verdict roll-call mapping check reports no violations

  # --- check 3: exactly one is_current row per key ---------------------------

  Scenario: Check 3 passes vacuously when verdicts and candidate_positions are both empty
    When I run the is_current uniqueness check
    Then the is_current uniqueness check reports no violations

  Scenario: Check 3 passes when a promise has exactly one current verdict
    Given a verdict "prm:0001" is appended with value "kept"
    When I run the is_current uniqueness check
    Then the is_current uniqueness check reports no violations

  Scenario: Check 3 catches a promise with two current verdicts
    Given a verdict "prm:0001" is appended with value "kept"
    And a second current verdict row is force-written for promise "prm:0001", bypassing append_version
    When I run the is_current uniqueness check
    Then the is_current uniqueness check reports a violation for verdicts promise "prm:0001" with 2 current rows

  Scenario: Check 3 catches a (person, stance) with two current candidate_positions
    Given a candidate_position for person "person:B000944" and stance "healthcare:lower-drug-costs" is appended
    And a second current candidate_position row is force-written for person "person:B000944" and stance "healthcare:lower-drug-costs", bypassing append_version
    When I run the is_current uniqueness check
    Then the is_current uniqueness check reports a violation for candidate_positions key "person:B000944"/"healthcare:lower-drug-costs" with 2 current rows

  Scenario: Check 3 catches a promise with zero current verdicts despite stored history
    Given a verdict "prm:0001" is appended with value "kept"
    And every is_current flag for promise "prm:0001" is force-cleared, bypassing append_version
    When I run the is_current uniqueness check
    Then the is_current uniqueness check reports a violation for verdicts promise "prm:0001" with 0 current rows

  # --- check 4: member_votes denormalized-field drift (detection) ------------

  Scenario: Check 4 passes vacuously when member_votes is empty
    When I run the member_votes drift check
    Then the member_votes drift check reports no violations

  Scenario: Check 4 passes when a member_votes row's denormalized fields match its roll call
    Given a roll_calls row "us-house:119-2:452" with vote_type "final_passage" and bill_id "bill:hr-1"
    And a people row "person:B000944"
    And a member_votes row for roll call "us-house:119-2:452" and person "person:B000944" copying its denormalized fields
    When I run the member_votes drift check
    Then the member_votes drift check reports no violations

  Scenario: Check 4 catches a member_votes row whose vote_type has drifted from its roll call
    Given a roll_calls row "us-house:119-2:452" with vote_type "final_passage" and bill_id "bill:hr-1"
    And a people row "person:B000944"
    And a member_votes row for roll call "us-house:119-2:452" and person "person:B000944" copying its denormalized fields
    And the roll_calls row "us-house:119-2:452" is corrected to vote_type "cloture"
    When I run the member_votes drift check
    Then the member_votes drift check reports a violation for member_votes row "us-house:119-2:452:B000944" field "vote_type"

  Scenario: Check 4 catches a member_votes row whose bill_id has drifted from its roll call
    Given a roll_calls row "us-house:119-2:452" with vote_type "final_passage" and bill_id "bill:hr-1"
    And a people row "person:B000944"
    And a member_votes row for roll call "us-house:119-2:452" and person "person:B000944" copying its denormalized fields
    And the roll_calls row "us-house:119-2:452" is corrected to bill_id "bill:hr-2"
    When I run the member_votes drift check
    Then the member_votes drift check reports a violation for member_votes row "us-house:119-2:452:B000944" field "bill_id"

  # --- check 5: provenance coverage (reuses story 16.3's mechanism) ----------

  Scenario: Check 5 passes for the codebase as it stands
    When I run the provenance coverage check
    Then the provenance coverage check reports no violations

  Scenario: Check 5 catches a sourced row with no provenance entry written outside the repository layer
    Given a roll_calls row with a source_url is written directly through the client, bypassing the repository
    When I run the provenance coverage check
    Then the provenance coverage check reports a violation naming that row

  # --- the combined nightly run -----------------------------------------------

  Scenario: Running all five checks together reports each check's name
    When I run every integrity check
    Then the combined report covers checks "references", "verdict_roll_call_mapping", "is_current_uniqueness", "member_votes_drift", "provenance_coverage"
