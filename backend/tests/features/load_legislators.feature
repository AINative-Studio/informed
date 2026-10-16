Feature: Load legislators from congress-legislators (story 2.2)
  As the system
  I want every current and recent member of Congress loaded from congress-legislators with their terms
  So that votes resolve to people

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables and seeded bodies exist

  # --- the acceptance criterion: all sitting members load with bioguide ids -

  Scenario: Loading the fixture roster populates people with bioguide ids
    When I load legislators from the fixture roster
    Then every loaded person has an id of the form "person:{bioguide}"
    And the loader reports as many people written as the fixture roster holds

  Scenario: A loaded representative carries their external ids
    When I load legislators from the fixture roster
    Then the person "person:C000127" has external id "govtrack" "300018"
    And the person "person:C000127" has external id "bioguide" "C000127"

  Scenario: A loaded legislator with no photo convention available has a null photo_url
    When I load legislators from the fixture roster
    Then the person "person:C000127" has no photo_url

  # --- terms[] covers the 118th and 119th Congress --------------------------

  Scenario: A senator's terms include a term covering the 119th Congress
    When I load legislators from the fixture roster
    Then the person "person:C000127" has a term in body "us-senate" starting "2019-01-03" and ending "2025-01-03"
    And the person "person:C000127" has a term in body "us-senate" starting "2025-01-03" and ending "2031-01-03"

  Scenario: A representative's terms include a term covering the 118th Congress
    When I load legislators from the fixture roster
    Then the person "person:N000147" has a term in body "us-house" starting "2023-01-03" and ending "2025-01-03"
    And the person "person:N000147" has a term in body "us-house" starting "2025-01-03" and ending "2027-01-03"

  Scenario: Every loaded term carries a body id, district id, party, start and end
    When I load legislators from the fixture roster
    Then every term of every loaded person has a body id of "us-house" or "us-senate"
    And every term of every loaded person has a start date
    And every term's party is a known party or is absent

  # --- idempotency ------------------------------------------------------------

  Scenario: Loading the same roster twice leaves exactly one row per bioguide
    Given I have already loaded legislators from the fixture roster once
    When I load legislators from the fixture roster again
    Then exactly 1 row carries the person id "person:C000127"
    And exactly 1 row carries the person id "person:N000147"

  Scenario: Loading a refreshed roster updates the existing person instead of duplicating it
    Given I have already loaded legislators from the fixture roster once
    When I load a refreshed roster where "person:C000127" was redistricted to a new party label
    Then exactly 1 row carries the person id "person:C000127"
    And the person "person:C000127" has a term in body "us-senate" starting "2025-01-03" and ending "2031-01-03" with party "Democrat-Farmer-Labor"
