Feature: Issue taxonomy v1 (story 3.1)
  As a voter
  I want a clear list of issue topics and subtopics
  So that I can find what I care about

  Background:
    Given a ZeroDB client backed by an in-memory fake

  # --- the acceptance criterion: ~20 topics, ~60 subtopics, en-US -----------

  Scenario: The taxonomy has about twenty top-level topics
    When I seed the issue taxonomy
    Then the taxonomy has 20 top-level topics

  Scenario: The taxonomy has about sixty subtopics
    When I seed the issue taxonomy
    Then the taxonomy has 60 subtopics

  Scenario: Every seeded row is in en-US at taxonomy version 1
    When I seed the issue taxonomy
    Then every seeded topic has locale "en-US"
    And every seeded topic has taxonomy version 1

  Scenario: The seed reports what it wrote
    When I seed the issue taxonomy
    Then the taxonomy seed reports 20 topics and 60 subtopics written

  # --- the parent/child tree ------------------------------------------------

  Scenario: Top-level topics have no parent
    When I seed the issue taxonomy
    Then every top-level topic has no parent

  Scenario: Every subtopic points at a stored top-level topic
    When I seed the issue taxonomy
    Then every subtopic's parent is a stored top-level topic

  Scenario: Every top-level topic has at least two subtopics
    When I seed the issue taxonomy
    Then every top-level topic has at least 2 subtopics

  Scenario: The tree is two levels deep, never three
    When I seed the issue taxonomy
    Then no subtopic is the parent of another topic

  Scenario: Healthcare carries the subtopic prd.md names
    When I seed the issue taxonomy
    Then the topic "en-US:healthcare:v1" exists with name "Healthcare"
    And the topic "en-US:prescription-drug-pricing:v1" exists with name "Prescription drug pricing"
    And the topic "en-US:prescription-drug-pricing:v1" has parent "en-US:healthcare:v1"

  Scenario: Ids follow datamodel.md's locale, slug and version pattern
    When I seed the issue taxonomy
    Then every seeded id is a locale, a slug and a version, like "en-US:healthcare:v1"

  # --- naming neutrality (prd.md: nonpartisan by construction) -------------

  Scenario: No topic name presupposes a political stance
    When I seed the issue taxonomy
    Then no topic name contains a stance-presupposing word

  Scenario: Topic names are unique within the locale and version
    When I seed the issue taxonomy
    Then no two seeded topics share a name
    And no two seeded topics share an id

  # --- idempotency ----------------------------------------------------------

  Scenario: Re-seeding leaves exactly one row per topic id
    Given the issue taxonomy has already been seeded once
    When I seed the issue taxonomy again
    Then exactly 1 row carries each seeded topic id

  Scenario: Re-seeding updates rather than appending
    Given the issue taxonomy has already been seeded once
    When I seed the issue taxonomy again
    Then the second run issued no row inserts
    And the second run updated 80 rows

  Scenario: A hand-edited topic name is restored on the next seed
    Given the issue taxonomy has already been seeded once
    And the topic "en-US:healthcare:v1" has been hand-edited to name "Something Else"
    When I seed the issue taxonomy again
    Then the topic "en-US:healthcare:v1" exists with name "Healthcare"
    And exactly 1 row carries each seeded topic id

  # --- reference integrity --------------------------------------------------

  Scenario: A parent is never written after a child that points at it
    When I seed the issue taxonomy
    Then the topic "en-US:healthcare:v1" was written before "en-US:prescription-drug-pricing:v1"

  Scenario: A subtopic whose parent does not exist is refused
    Given the issue topics table exists
    When I seed a subtopic whose parent is missing
    Then the write is refused for a missing parent
