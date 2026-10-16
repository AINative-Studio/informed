Feature: Free-text interest mapping (story 3.5)
  As a voter who does not know the taxonomy's names
  I want to describe a concern in my own words and get topic suggestions
  So that I can still use the app without having to browse the tree first

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the issue taxonomy has been seeded
    And every topic has been embedded into the topics vector namespace

  # --- the acceptance criterion: top 3 suggestions ---------------------------

  Scenario: Free text produces up to 3 topic suggestions
    When a voter describes their concern as "I worry about the price of my prescription drug medications"
    Then at most 3 topic suggestions are returned
    And each suggestion carries a topic_id, a name and a similarity score

  Scenario: A concern closely matching a topic's wording surfaces that topic first
    When a voter describes their concern as "I worry about the price of my prescription drug medications"
    Then "Prescription drug pricing" is the top-ranked suggestion

  Scenario: Blank free text is refused rather than silently returning nothing
    When a voter describes their concern as ""
    Then the request is refused with a 422 naming the missing text

  # --- "nothing is saved until the voter confirms" ----------------------------
  # This endpoint must never write anything. Confirming a suggestion is just
  # the voter picking its topic_id and going through the normal POST
  # /v1/profiles save flow (story 4.1/4.4) — a separate, existing endpoint.

  Scenario: Requesting suggestions writes no row to any table
    Given a snapshot of every table's row count
    When a voter describes their concern as "I can't afford my prescriptions"
    Then no table's row count has changed since the snapshot

  Scenario: Requesting suggestions creates no new vector
    Given a snapshot of the topics namespace's vector count
    When a voter describes their concern as "I can't afford my prescriptions"
    Then the topics namespace's vector count has not changed since the snapshot

  Scenario: Suggestions require no save or write side effect
    When a voter describes their concern as "I can't afford my prescriptions"
    Then the response confirms nothing was saved and a separate confirm step is required

  # --- "the text itself is not stored" ----------------------------------------

  Scenario: The free text is never written to any table
    When a voter describes their concern as "my secret unique concern about the price of chemotherapy drugs"
    Then no stored row in any table contains that free text

  Scenario: The free text never appears in application logs
    When a voter describes their concern as "my secret unique concern about the price of chemotherapy drugs"
    Then no log record contains that free text
