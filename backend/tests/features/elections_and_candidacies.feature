Feature: Load elections and candidacies from OpenFEC (story 2.6)
  As a voter
  I want challengers listed alongside incumbents
  So that I can compare everyone on my ballot

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables and seeded bodies exist

  # --- step 1: the election itself ------------------------------------------
  #
  # A candidacy id is `{election_id}:{district_id}:{person_id}` and
  # `election_id` is a declared reference, so the election row has to exist
  # before any candidacy can be written at all. Seeded as permanent reference
  # data, exactly like story 2.1's jurisdiction and chambers.

  Scenario: Seeding the upcoming election writes the real 2026 general election
    When I seed the upcoming federal elections
    Then exactly 1 row carries the election id "us-2026-11-03-general"
    And the election "us-2026-11-03-general" is held on "2026-11-03"
    And the election "us-2026-11-03-general" is of kind "general"
    And the election "us-2026-11-03-general" belongs to jurisdiction "us-federal"

  Scenario: Seeding the election twice leaves exactly one row
    Given I have already seeded the upcoming federal elections once
    When I seed the upcoming federal elections again
    Then exactly 1 row carries the election id "us-2026-11-03-general"

  # --- the acceptance criterion: candidacies load from the chosen source ----

  Scenario: Loading the fixture filings writes a candidacy per filed candidate
    Given the upcoming federal elections are seeded
    When I load candidacies from the fixture filings
    Then the loader reports as many candidacies written as the fixture holds
    And every loaded candidacy has an id of the form "{election_id}:{district_id}:{person_id}"
    And every loaded candidacy references the election "us-2026-11-03-general"

  Scenario: A House challenger's candidacy carries their district, party and name
    Given the upcoming federal elections are seeded
    When I load candidacies from the fixture filings
    Then the candidacy for FEC candidate "H6PA02205" is in district "ocd-division/country:us/state:pa/cd:2"
    And the candidacy for FEC candidate "H6PA02205" has party "REP"
    And the candidacy for FEC candidate "H6PA02205" has person name "ARRIAGA, JESSICA"

  Scenario: A Senate candidacy maps to the statewide division, not a numbered district
    Given the upcoming federal elections are seeded
    When I load candidacies from the fixture filings
    Then the candidacy for FEC candidate "S6DE00263" is in district "ocd-division/country:us/state:de"

  Scenario: An at-large House candidacy maps to the statewide division
    Given the upcoming federal elections are seeded
    When I load candidacies from the fixture filings
    Then the candidacy for FEC candidate "H6DE01121" is in district "ocd-division/country:us/state:de"

  # --- step 2: challengers are new people ----------------------------------
  #
  # Story 2.2 loaded only sitting members. A challenger has no bioguide id, so
  # they get datamodel.md's `person:{uuid}` form — made DETERMINISTIC from
  # FEC's own candidate id so a re-ingest finds the same person rather than
  # minting a second one. See app/ingest/candidacies.py's docstring.

  Scenario: A challenger with no bioguide id gets a deterministic FEC-derived person row
    Given the upcoming federal elections are seeded
    When I load candidacies from the fixture filings
    Then a person row exists with id "person:fec-H6PA02205"
    And the person "person:fec-H6PA02205" has canonical name "ARRIAGA, JESSICA"
    And the person "person:fec-H6PA02205" has external id "fec" "H6PA02205"
    And the person "person:fec-H6PA02205" has no terms

  Scenario: Re-loading the same filings does not mint a second person for a challenger
    Given the upcoming federal elections are seeded
    And I have already loaded candidacies from the fixture filings once
    When I load candidacies from the fixture filings again
    Then exactly 1 row carries the person id "person:fec-H6PA02205"

  Scenario: A sitting member already in people is reused, not duplicated under a FEC id
    Given the upcoming federal elections are seeded
    And the sitting member "person:M001234" is already loaded with FEC id "H4DE00045"
    When I load candidacies from the fixture filings
    Then the candidacy for FEC candidate "H4DE00045" has person id "person:M001234"
    And no person row exists with id "person:fec-H4DE00045"

  # --- step 3: incumbent flagging ------------------------------------------
  #
  # Cross-referenced against the real legislators story 2.2 loaded, not taken
  # on faith from FEC's own `incumbent_challenge` letter.

  Scenario: A candidacy for a sitting member of that same seat is flagged incumbent
    Given the upcoming federal elections are seeded
    And the sitting member "person:M001234" is already loaded with FEC id "H4DE00045"
    When I load candidacies from the fixture filings
    Then the candidacy for FEC candidate "H4DE00045" is flagged incumbent

  Scenario: A challenger is not flagged incumbent
    Given the upcoming federal elections are seeded
    When I load candidacies from the fixture filings
    Then the candidacy for FEC candidate "H6PA02205" is not flagged incumbent

  Scenario: A candidate FEC calls an incumbent but who holds no matching seat is not flagged
    Given the upcoming federal elections are seeded
    When I load candidacies from the fixture filings
    Then the candidacy for FEC candidate "H6XX99001" is not flagged incumbent

  # --- status mapping -------------------------------------------------------

  Scenario Outline: FEC candidate_status maps onto the candidacy status vocabulary
    Given the upcoming federal elections are seeded
    When I load candidacies from the fixture filings
    Then the candidacy for FEC candidate "<fec_id>" has status "<status>"

    Examples:
      | fec_id    | status    |
      | H6DE01121 | filed     |
      | H6PA02205 | statement |
      | H6XX99002 | withdrawn |

  # --- idempotency: the whole point of "refreshed weekly" -------------------

  Scenario: Loading the same filings twice leaves exactly one row per candidacy
    Given the upcoming federal elections are seeded
    And I have already loaded candidacies from the fixture filings once
    When I load candidacies from the fixture filings again
    Then exactly 1 row carries the candidacy id "us-2026-11-03-general:ocd-division/country:us/state:pa/cd:2:person:fec-H6PA02205"

  Scenario: A refreshed weekly pull updates a changed candidacy instead of duplicating it
    Given the upcoming federal elections are seeded
    And I have already loaded candidacies from the fixture filings once
    When I load a refreshed pull where FEC candidate "H6PA02205" has withdrawn
    Then exactly 1 row carries the candidacy id "us-2026-11-03-general:ocd-division/country:us/state:pa/cd:2:person:fec-H6PA02205"
    And the candidacy for FEC candidate "H6PA02205" has status "withdrawn"

  # --- records this loader must refuse to write -----------------------------

  Scenario: A filing for a different election year is skipped
    Given the upcoming federal elections are seeded
    When I load candidacies from the fixture filings
    Then no candidacy was written for FEC candidate "H4OLD00001"
    And the loader reports a skip for FEC candidate "H4OLD00001"

  Scenario: A presidential filing is skipped, having no district to place it in
    Given the upcoming federal elections are seeded
    When I load candidacies from the fixture filings
    Then no candidacy was written for FEC candidate "P60099001"
    And the loader reports a skip for FEC candidate "P60099001"

  # --- pacing against the live rate limit -----------------------------------

  Scenario: Every candidacy write is paced
    Given the upcoming federal elections are seeded
    When I load candidacies from the fixture filings with a recording pacer
    Then the pacer was called at least once per candidacy written
