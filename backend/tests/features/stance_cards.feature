Feature: Balanced stance cards (story 3.2)
  As a voter
  I want stance options written neutrally from across the spectrum in random order
  So that the app does not steer me

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the issue taxonomy has been seeded

  # --- the acceptance criterion: at least 3 stances per subtopic ------------

  Scenario: Every subtopic has at least three stances
    When I seed the stance cards
    Then every subtopic has at least 3 stored stances

  Scenario: Every stance hangs off a subtopic, never off a top-level topic
    When I seed the stance cards
    Then no stored stance references a top-level topic

  Scenario: Every stance's topic_id resolves to a stored subtopic
    When I seed the stance cards
    Then every stored stance's topic_id names a stored subtopic

  Scenario: Every seeded stance is at wording version 1
    When I seed the stance cards
    Then every stored stance has wording version 1

  Scenario: The seed reports what it wrote
    When I seed the stance cards
    Then the stance seed reports one row per declared stance

  Scenario: Ids follow datamodel.md's topic-and-slug pattern
    When I seed the stance cards
    Then every stored stance id is its topic id followed by a slug

  # --- spanning the spectrum -----------------------------------------------
  # The neutrality concern for a stance card is not that it avoids taking a
  # position — stating a position is its whole job — but that the SET of
  # options for a subtopic covers the real disagreement rather than one side.

  Scenario: A subtopic's stances do not all point the same way
    When I seed the stance cards
    Then no subtopic's stances all share one direction code

  Scenario: Every subtopic offers an option on each side of its axis
    When I seed the stance cards
    Then every subtopic offers at least one negative and one positive direction code

  Scenario: Direction codes are signed positions on the subtopic's own axis
    When I seed the stance cards
    Then every stored direction code is a signed integer between -2 and 2

  Scenario: No two stances under one subtopic duplicate a direction code
    When I seed the stance cards
    Then no two stances under one subtopic share a direction code

  Scenario: Stance wording states an outcome rather than naming a side
    When I seed the stance cards
    Then no stance label contains a partisan-identity word

  # --- no stance is preselected --------------------------------------------

  Scenario: The stored row has no default-selection field
    When I seed the stance cards
    Then no stored stance row carries a default or preselected field

  Scenario: The stance model cannot be given a default-selection field
    When I try to store a stance flagged as the default
    Then the write is refused as an unknown field

  # --- display order --------------------------------------------------------
  # Randomization per session is the client's job (it needs session state the
  # backend deliberately does not keep). What the backend owes is that the set
  # it hands over carries no implied ranking.

  Scenario: Reading a subtopic's stances does not return them in direction order
    Given the stance cards have been seeded
    When I read the stances for "en-US:background-checks-and-purchase-rules:v1" twice
    Then the two reads return the same set of stances
    And the returned order differs between the two reads

  Scenario: A shuffled read is still the complete set
    Given the stance cards have been seeded
    When I read the stances for "en-US:gestational-limits-and-exceptions:v1" twice
    Then each read returns every stance stored for that subtopic

  Scenario: The service never marks one stance as the one to pick
    Given the stance cards have been seeded
    When I read the stances for "en-US:prescription-drug-pricing:v1" twice
    Then no returned stance is flagged as selected, default or recommended

  # --- idempotency ----------------------------------------------------------

  Scenario: Re-seeding leaves exactly one row per stance id
    Given the stance cards have been seeded
    When I seed the stance cards again
    Then exactly 1 row carries each seeded stance id

  Scenario: Re-seeding updates rather than appending
    Given the stance cards have been seeded
    When I seed the stance cards again
    Then the second stance run issued no row inserts
    And the second stance run updated every stance row

  Scenario: A hand-edited stance label is restored on the next seed
    Given the stance cards have been seeded
    And the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs" has been hand-edited to label "Something Else"
    When I seed the stance cards again
    Then the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs" has its seeded label back
    And exactly 1 row carries each seeded stance id

  # --- reference integrity --------------------------------------------------

  Scenario: A stance whose topic does not exist is refused
    When I seed a stance whose topic is missing
    Then the stance write is refused for a missing topic
