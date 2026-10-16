Feature: Repository layer with natural-key ids (story 1.4)
  As a backend developer
  I want a base repository that upserts by deterministic id and checks
  references before writing
  So that NoSQL data stays consistent without foreign keys

  Background:
    Given a bootstrapped ZeroDB backed by an in-memory fake
    And a repository for each of people, districts, roll_calls, member_votes and verdicts

  # --- reference validation: the first acceptance criterion ---------------
  #
  # datamodel.md rule 5: "Integrity lives in code — a FastAPI repository layer
  # validates references with Pydantic before every write."

  Scenario: A row referencing a missing id is rejected
    Given a roll call "us-house:119-2:452" exists
    When I save a member vote referencing roll call "us-house:119-2:452" and person "person:NOBODY"
    Then the write is rejected as a missing reference
    And the missing reference names the table "people" and the id "person:NOBODY"
    And no row was written to "member_votes"

  Scenario: A row referencing a missing roll call is rejected
    Given a person "person:B000944" exists
    When I save a member vote referencing roll call "us-house:119-2:999" and person "person:B000944"
    Then the write is rejected as a missing reference
    And the missing reference names the table "roll_calls" and the id "us-house:119-2:999"

  Scenario: A row whose references all resolve is written
    Given a person "person:B000944" exists
    And a roll call "us-house:119-2:452" exists
    When I save a member vote referencing roll call "us-house:119-2:452" and person "person:B000944"
    Then the member vote "us-house:119-2:452:B000944" is stored
    And exactly 1 row exists in "member_votes"

  # `jurisdictions` is the reference-free table this is asserted on. It was
  # `districts` until story 2.3, which declared `districts.body_id` a reference
  # to `bodies` — the call story 1.4 explicitly left to it, because 2.3 is the
  # story that writes real district rows. So the example moved to a table that
  # genuinely declares nothing, and the district now exercises the check.
  Scenario: Reference checks are skipped for a table that references nothing
    When I save a jurisdiction "us-federal"
    Then the jurisdiction "us-federal" is stored
    And no reference lookup was made against "people"

  Scenario: A district referencing a chamber that was never seeded is rejected
    When I save a district "ocd-division/country:us/state:ca/cd:19"
    Then the write is rejected as a missing reference
    And the missing reference names the table "bodies" and the id "us-house"

  Scenario: A district whose chamber exists is written
    Given the chamber "us-house" exists
    When I save a district "ocd-division/country:us/state:ca/cd:19"
    Then the district "ocd-division/country:us/state:ca/cd:19" is stored

  # --- idempotent upsert: the second acceptance criterion -----------------
  #
  # datamodel.md rule 1: "Re-ingesting the same record is an idempotent
  # upsert, never a duplicate." ZeroDB has no upsert-by-natural-key; POST
  # always appends, so the repository must find-then-write.

  Scenario: Re-ingesting the same record produces no duplicate
    Given a person "person:B000944" exists
    When I save the person "person:B000944" again
    Then exactly 1 row exists in "people"

  Scenario: Re-ingesting a changed record updates the one row in place
    Given a person "person:B000944" exists
    When I save the person "person:B000944" with canonical name "Updated Name"
    Then exactly 1 row exists in "people"
    And the person "person:B000944" has canonical name "Updated Name"

  Scenario: An upsert addresses ZeroDB's internal row UUID, never the natural key
    Given a person "person:B000944" exists
    When I save the person "person:B000944" with canonical name "Updated Name"
    Then the update was addressed by ZeroDB's internal row UUID
    And no request path contained the natural-key id "person:B000944"

  Scenario: An upsert of an existing row preserves the natural-key id field
    Given a person "person:B000944" exists
    When I save the person "person:B000944" with canonical name "Updated Name"
    Then the person row still carries the id field "person:B000944"

  Scenario: Re-ingesting many records leaves one row per id
    When I save 3 distinct people twice each
    Then exactly 3 rows exist in "people"

  # --- append-only versioning --------------------------------------------
  #
  # datamodel.md rule 6: verdicts, positions and the provenance log are never
  # updated in place; an `is_current` flag marks the live row.

  Scenario: Append-only versioning marks exactly one row current
    Given a person "person:B000944" exists
    And a promise "prm:0001" exists
    When I append a verdict "kept" for promise "prm:0001"
    And I append a verdict "broken" for promise "prm:0001"
    Then exactly 2 rows exist in "verdicts"
    And exactly 1 verdict row for promise "prm:0001" is current
    And the current verdict for promise "prm:0001" is "broken"
    And the superseded verdict "kept" is still stored

  Scenario: Append-only versioning never issues an in-place update of history
    Given a person "person:B000944" exists
    And a promise "prm:0001" exists
    When I append a verdict "kept" for promise "prm:0001"
    And I append a verdict "broken" for promise "prm:0001"
    Then the only row rewritten was to clear a superseded is_current flag

  Scenario: Appending a version checks references too
    Given a promise "prm:0001" exists
    When I append a verdict "kept" for promise "prm:0001" by person "person:NOBODY"
    Then the write is rejected as a missing reference
    And no row was written to "verdicts"

  Scenario: An append-only table rejects a plain upsert
    Given a person "person:B000944" exists
    And a promise "prm:0001" exists
    When I upsert a verdict instead of appending a version
    Then the write is rejected as an append-only violation

  Scenario: A plain table rejects append_version
    When I append a version of a person instead of upserting
    Then the write is rejected as an append-only violation

  # --- schema validation --------------------------------------------------

  Scenario: A row that does not match its Pydantic model is rejected
    When I save a member vote with position "maybe"
    Then the write is rejected as invalid
    And no row was written to "member_votes"

  Scenario: A row whose id does not match its natural key is rejected
    Given a person "person:B000944" exists
    And a roll call "us-house:119-2:452" exists
    When I save a member vote whose id disagrees with its natural key
    Then the write is rejected as invalid
