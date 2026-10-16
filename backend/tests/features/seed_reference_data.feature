Feature: Seed the US federal jurisdiction and its chambers (story 2.1)
  As the system
  I want the US federal jurisdiction and its two chambers seeded
  So that ingest has a home

  Background:
    Given a ZeroDB client backed by an in-memory fake

  # --- the acceptance criterion: rows exist after bootstrap ----------------

  Scenario: Bootstrap seeds us-federal, us-house and us-senate
    When I run the bootstrap
    Then the jurisdiction "us-federal" exists
    And the body "us-house" exists
    And the body "us-senate" exists

  Scenario: The seeded jurisdiction carries the fields datamodel.md specifies
    When I run the bootstrap
    Then the jurisdiction "us-federal" has name "United States"
    And the jurisdiction "us-federal" has country code "US"
    And the jurisdiction "us-federal" has level "federal"
    And the jurisdiction "us-federal" has adapter name "us-federal"
    And the jurisdiction "us-federal" is active

  Scenario: The seeded chambers carry the fields datamodel.md specifies
    When I run the bootstrap
    Then the body "us-house" belongs to jurisdiction "us-federal"
    And the body "us-house" has name "U.S. House of Representatives"
    And the body "us-house" has chamber "lower"
    And the body "us-house" has 435 seats
    And the body "us-senate" belongs to jurisdiction "us-federal"
    And the body "us-senate" has name "U.S. Senate"
    And the body "us-senate" has chamber "upper"
    And the body "us-senate" has 100 seats

  Scenario: The seed reports what it wrote
    When I run the bootstrap
    Then the seed reports 1 jurisdiction and 2 bodies written

  # --- idempotency: re-running must not duplicate ---------------------------

  Scenario: Seeding twice leaves exactly one row per seeded id
    Given the bootstrap has already run once
    When I run the bootstrap again
    Then exactly 1 row carries the jurisdiction id "us-federal"
    And exactly 1 row carries the body id "us-house"
    And exactly 1 row carries the body id "us-senate"

  Scenario: Seeding twice leaves the seeded values unchanged
    Given the bootstrap has already run once
    When I run the bootstrap again
    Then the jurisdiction "us-federal" has name "United States"
    And the body "us-house" has 435 seats

  Scenario: A seeded row edited by hand is restored to its seeded values
    Given the bootstrap has already run once
    And the jurisdiction "us-federal" has been hand-edited to name "Something Else"
    When I run the bootstrap again
    Then the jurisdiction "us-federal" has name "United States"
    And exactly 1 row carries the jurisdiction id "us-federal"

  # --- reference integrity -------------------------------------------------

  Scenario: A chamber is never written before the jurisdiction it belongs to
    When I run the bootstrap
    Then the jurisdiction "us-federal" was written before the body "us-house"

  Scenario: A chamber referencing a jurisdiction that does not exist is refused
    Given the reference tables exist
    When I seed a chamber whose jurisdiction is missing
    Then the write is refused for a missing reference
