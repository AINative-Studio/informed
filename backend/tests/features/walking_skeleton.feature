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

  This file is kept as the skeleton proof rather than renamed or folded into
  story 2.4's `ballot_lookup.feature`, and the split is deliberate. What these
  scenarios prove is that the React -> FastAPI -> ZeroDB path carries a
  delegation at all — the claim story 1.5 existed to make, and the one that
  should fail first and loudest if the repository layer, the client or the
  transport breaks. `ballot_lookup.feature` proves what story 2.4 added on top:
  the race grouping, the incumbent flag, and the latency budget. Merging them
  would mean a broken race grouping and a broken ZeroDB path reported as the
  same failure.

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

  Scenario: A former representative is not shown as a current one
    Given the district's previous representative is also stored in ZeroDB, with a term that has ended
    When the voter submits the address "1600 Pennsylvania Ave, Washington, DC"
    Then it lists exactly 1 House member
    And the former representative is not among them

  # Wyoming's single at-large seat, written as the state-only division with no
  # `/cd:` segment. Story 2.3 corrected this from `/cd:at-large`, a spelling
  # that appeared only here and matches no row in the live data: both the
  # Census Geocoder (which codes an at-large seat `00`) and story 2.2's
  # legislator loader produce the state-only form.
  Scenario: A district with no officeholders yet returns an empty race, not an error
    Given the district "ocd-division/country:us/state:wy" exists in ZeroDB
    When the voter submits an address in that district
    Then the ballot response names the voter's House district
    And it lists exactly 0 House members
