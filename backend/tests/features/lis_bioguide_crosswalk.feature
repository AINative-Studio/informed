Feature: LIS-to-bioguide crosswalk (story 8.2)
  As the system
  I want Senate LIS member ids mapped to bioguide ids
  So that Senate votes join to people

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist

  # --- the crosswalk is data, written into people.external_ids --------------

  Scenario: Refreshing the crosswalk writes each senator's LIS id onto their people row
    Given the sitting senators from the sample roster
    When I refresh the LIS crosswalk from the sample roster
    Then the people row "person:B001261" carries external id "lis" "S317"
    And the crosswalk report says 6 of 6 sitting senators mapped

  Scenario: Refreshing the crosswalk preserves the other external ids already on the row
    Given the sitting senators from the sample roster
    And the people row "person:B001261" already carries external id "govtrack" "412251"
    When I refresh the LIS crosswalk from the sample roster
    Then the people row "person:B001261" carries external id "lis" "S317"
    And the people row "person:B001261" carries external id "govtrack" "412251"

  Scenario: Refreshing the crosswalk twice does not duplicate the people row
    Given the sitting senators from the sample roster
    And I have already refreshed the LIS crosswalk from the sample roster once
    When I refresh the LIS crosswalk from the sample roster again
    Then exactly 1 row carries the people id "person:B001261"

  Scenario: A roster record carrying no LIS id at all is reported, never written as a blank mapping
    Given the sitting senators from the sample roster
    When I refresh the LIS crosswalk from a roster whose senator "person:S000033" has no LIS id
    Then the people row "person:S000033" carries no external id "lis"
    And the crosswalk report names "person:S000033" as a senator with no LIS id

  # --- reading the crosswalk back: what 8.3/8.4/8.5 build on ---------------

  Scenario: The crosswalk resolves a LIS id to its bioguide id
    Given the crosswalk has been refreshed from the sample roster
    When I load the crosswalk from ZeroDB
    Then resolving LIS id "S317" returns bioguide id "B001261"

  Scenario: The crosswalk resolves every LIS id in the roster
    Given the crosswalk has been refreshed from the sample roster
    When I load the crosswalk from ZeroDB
    Then the loaded crosswalk holds 6 mappings

  Scenario: A LIS id the crosswalk has never seen resolves to nothing rather than a guess
    Given the crosswalk has been refreshed from the sample roster
    When I load the crosswalk from ZeroDB
    Then resolving LIS id "S999" returns no bioguide id

  # --- the acceptance criterion: 100% of sitting senators map -------------

  Scenario: A complete crosswalk reports full coverage against the sitting Senate
    Given the crosswalk has been refreshed from the sample roster
    When I check the crosswalk covers the 6 sitting senators in the sample roster
    Then the coverage check passes with 6 of 6 mapped

  Scenario: A crosswalk missing one sitting senator fails the 100% coverage check
    Given the crosswalk has been refreshed from a roster missing senator "person:S000033"
    When I check the crosswalk covers the 6 sitting senators in the sample roster
    Then the coverage check fails naming "person:S000033" as unmapped
    And the coverage check reports 5 of 6 mapped

  # --- an unmapped id raises an alert, never a silent drop ----------------

  Scenario: Resolving an unmapped LIS id strictly raises rather than returning nothing
    Given the crosswalk has been refreshed from the sample roster
    When I load the crosswalk from ZeroDB
    Then resolving LIS id "S999" strictly raises an unmapped-id alert

  Scenario: An unmapped LIS id is logged as an alert with the story tag
    Given the crosswalk has been refreshed from the sample roster
    When I load the crosswalk from ZeroDB and resolve LIS id "S999" with alerting
    Then an alert was logged naming LIS id "S999"
    And that alert carries the story tag "8.2"

  Scenario: An unmapped LIS id is counted so a metrics scrape can see it
    Given the crosswalk has been refreshed from the sample roster
    When I load the crosswalk from ZeroDB and resolve LIS id "S999" with alerting
    Then the unmapped-id alert count is 1

  Scenario: Repeated unmapped ids each count, and the same id is not double-alerted into noise
    Given the crosswalk has been refreshed from the sample roster
    When I load the crosswalk from ZeroDB and resolve LIS ids "S999,S998,S999" with alerting
    Then the unmapped-id alert count is 3
    And 2 distinct unmapped LIS ids were alerted on

  # --- metrics rendering: the gauge an alert rule keys on -----------------

  Scenario: Incomplete coverage renders a metrics gauge an alert rule can fire on
    Given the crosswalk has been refreshed from a roster missing senator "person:S000033"
    When I render the crosswalk coverage metrics for the 6 sitting senators in the sample roster
    Then the rendered metrics include "lis_crosswalk_unmapped_senators 1"
    And the rendered metrics include "lis_crosswalk_coverage_complete 0"

  Scenario: Complete coverage renders the gauge at zero
    Given the crosswalk has been refreshed from the sample roster
    When I render the crosswalk coverage metrics for the 6 sitting senators in the sample roster
    Then the rendered metrics include "lis_crosswalk_unmapped_senators 0"
    And the rendered metrics include "lis_crosswalk_coverage_complete 1"

  # --- resolving 8.1's real member_votes shape ----------------------------

  Scenario: The crosswalk resolves a LIS member id into the person_id member_votes uses
    Given the crosswalk has been refreshed from the sample roster
    When I load the crosswalk from ZeroDB
    Then resolving LIS id "S317" returns person id "person:B001261"
