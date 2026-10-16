Feature: Provenance on every source (story 16.3)
  As a journalist
  I want every record with a source URL traceable to when and how it was fetched
  So that evidence is auditable

  # datamodel.md, "Integrity checks": "Every NoSQL row with a `source_url` has a
  # matching `provenance_log` entry, so no evidence goes untraced."
  #
  # Story 5.1 implemented that rule correctly for one source. This story makes it
  # STRUCTURAL: the repository layer itself writes the provenance entry, and
  # refuses the write when it has no provenance to write — so a future ingest
  # adapter cannot forget. The CI check is the last three scenarios.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the provenance reference tables exist

  # --- the rule, enforced at the write ---------------------------------------

  Scenario: Writing a row that carries a source_url also writes its provenance entry
    When I write a roll_calls row with source_url "https://api.congress.gov/v3/house-vote/119/2/452" through a provenance-aware repository
    Then the roll_calls row "us-house:119-2:452" is stored
    And a provenance_log entry exists for roll_calls "us-house:119-2:452"
    And that provenance entry has source_url "https://api.congress.gov/v3/house-vote/119/2/452"
    And that provenance entry has adapter_version "test_adapter/v1"
    And that provenance entry records when it was fetched

  Scenario: The provenance entry is content-addressed, so re-writing unchanged data adds nothing
    Given I have already written that roll_calls row once
    When I write the identical roll_calls row again
    Then exactly 1 row carries the roll_calls id "us-house:119-2:452"
    And exactly 1 provenance_log row exists for roll_calls "us-house:119-2:452"

  Scenario: Changed source content writes a second provenance entry, keeping the first
    Given I have already written that roll_calls row once
    When I write the same roll_calls row with changed source content
    Then exactly 1 row carries the roll_calls id "us-house:119-2:452"
    And exactly 2 provenance_log rows exist for roll_calls "us-house:119-2:452"

  Scenario: A row with no source_url needs no provenance entry
    When I write a people row through a repository with no provenance context
    Then the people row "person:B000944" is stored
    And no provenance_log rows exist at all

  # --- the structural refusal: forgetting is impossible, not merely discouraged

  Scenario: Writing a source_url row through a repository with no provenance context is refused
    When I try to write a roll_calls row with a source_url through a repository with no provenance context
    Then the write is refused as untraceable evidence
    And no roll_calls row "us-house:119-2:452" is stored
    And no provenance_log rows exist at all

  Scenario: A source_url row written in a batch gets a provenance entry per row
    When I write 3 roll_calls rows with distinct source_urls in one batch through a provenance-aware repository
    Then 3 roll_calls rows are stored
    And 3 provenance_log rows exist

  # --- the CI integrity check ------------------------------------------------

  Scenario: The integrity check passes for the codebase as it stands
    When I run the provenance integrity check over the repository models and ingest modules
    Then the integrity check reports no violations

  Scenario: The integrity check names every model that carries a source_url
    When I run the provenance integrity check over the repository models and ingest modules
    Then the integrity check covers the "roll_calls" table

  Scenario: The integrity check fails when an ingest path writes a source_url row without provenance
    Given an ingest module that writes a source_url row through a repository with no provenance context
    When I run the provenance integrity check over that module
    Then the integrity check reports a violation naming that module
