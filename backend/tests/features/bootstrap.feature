Feature: Idempotent ZeroDB bootstrap (story 1.3)
  As a developer
  I want one command that creates all NoSQL tables, vector namespaces, HNSW
  indexes and the graph ontology
  So that any environment can be built from code

  Background:
    Given a ZeroDB client backed by an in-memory fake

  # --- creates everything -----------------------------------------------

  Scenario: Running bootstrap once creates all 22 datamodel.md tables plus the infrastructure tables
    When I run the bootstrap
    Then exactly 25 tables exist
    And the table "jurisdictions" exists
    And the table "people" exists
    And the table "roll_calls" exists
    And the table "member_votes" exists
    And the table "promises" exists
    And the table "verdicts" exists
    And the table "candidate_positions" exists
    And the table "voter_profiles" exists
    # Story 14.1: wrapped per-user data keys, kept out of voter_profiles so
    # that shredding a key is independent of the profile row's lifecycle.
    And the table "voter_profile_keys" exists
    # Story 3.3: the advisory panel's wording review ledger, kept out of
    # issue_topics and stances so the seeders cannot wipe a review decision.
    And the table "panel_reviews" exists
    And the table "schema_versions" exists

  Scenario: Running bootstrap once creates all 4 vector namespaces with HNSW indexes
    When I run the bootstrap
    Then exactly 4 vector namespaces have an index
    And the namespace "promises" has an HNSW index at 1024 dimensions
    And the namespace "bill_summaries" has an HNSW index at 1024 dimensions
    And the namespace "topics" has an HNSW index at 1024 dimensions
    And the namespace "stances" has an HNSW index at 1024 dimensions

  Scenario: Running bootstrap once registers the knowledge graph ontology
    When I run the bootstrap
    Then the ontology is registered
    And the ontology includes the predicate "voted" from "person" to "roll_call"
    And the ontology includes the predicate "ran_in" from "person" to "candidacy"
    And the ontology includes the predicate "made_promise" from "candidacy" to "promise"

  Scenario: Running bootstrap once records a schema version
    When I run the bootstrap
    Then a schema version row is recorded
    And the schema version is queryable afterwards

  # Story 2.1's acceptance criterion is "after bootstrap", so it is asserted
  # from here too. The seed's own behaviour — field values, idempotency,
  # ordering, reference refusal — is covered in seed_reference_data.feature.
  Scenario: Running bootstrap once seeds the US federal reference data
    When I run the bootstrap
    Then the bootstrap report says 3 reference rows were seeded

  # --- idempotency: the hard part ----------------------------------------

  Scenario: Running bootstrap twice creates no duplicate tables
    Given the bootstrap has already run once
    When I run the bootstrap again
    Then exactly 25 tables exist

  Scenario: Running bootstrap twice creates no duplicate vector namespaces
    Given the bootstrap has already run once
    When I run the bootstrap again
    Then exactly 4 vector namespaces have an index

  Scenario: Running bootstrap twice re-affirms the same ontology, not a second one
    Given the bootstrap has already run once
    When I run the bootstrap again
    Then the ontology is registered
    And the ontology was already current on the second run
    And the ontology includes the predicate "voted" from "person" to "roll_call"

  Scenario: Running bootstrap twice leaves the schema version unchanged
    Given the bootstrap has already run once
    And the recorded schema version is noted
    When I run the bootstrap again
    Then the schema version is unchanged from what was noted

  Scenario: The second run skips table creation calls entirely
    Given the bootstrap has already run once
    When I run the bootstrap again
    Then no "POST" calls were made to create a table

  # --- pacing --------------------------------------------------------------

  Scenario: Bootstrap paces its calls to respect the rate limit
    When I run the bootstrap with a call pacer
    Then the pacer was invoked before every ZeroDB write call
