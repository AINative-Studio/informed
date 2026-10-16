Feature: A voter's address returns their federal representatives
  As a voter
  I want to enter my address and see my federal representatives
  So that the team proves the React -> FastAPI -> ZeroDB path works

  This is story 1.5, the walking skeleton. It proves the whole path end to end
  with the minimum real data: one House district, its representative, and the
  two senators for that state, all read from ZeroDB through the story 1.4
  repository layer.

  Deliberately NOT in scope, and each covered by its own later story:
    * real address -> district geocoding (story 2.3 — the Census Geocoder);
    * ingesting the actual sitting Congress (story 2.2);
    * races, candidacies and alignment scores (epics 2 and 4).

  Background:
    Given the district "ocd-division/country:us/state:ca/cd:19" exists in ZeroDB
    And its representative and California's two senators are stored in ZeroDB

  Scenario: A voter's address returns one House member and two senators
    When the voter submits the address "1600 Pennsylvania Ave, Washington, DC"
    Then the ballot response names the voter's House district
    And it lists exactly 1 House member
    And it lists exactly 2 senators
    And every officeholder carries the body they serve in

  Scenario: An address is required
    When the voter submits an empty address
    Then the request is rejected with a helpful message

  Scenario: The voter's address is never stored
    When the voter submits the address "1600 Pennsylvania Ave, Washington, DC"
    Then the response does not echo the address back
    And no row written to ZeroDB contains the address

  Scenario: A district with no officeholders yet returns an empty race, not an error
    Given the district "ocd-division/country:us/state:wy/cd:at-large" exists in ZeroDB
    When the voter submits an address in that district
    Then the ballot response names the voter's House district
    And it lists exactly 0 House members
