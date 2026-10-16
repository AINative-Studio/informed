Feature: The ballot lookup endpoint returns races with incumbents flagged
  As a voter
  I want GET /v1/ballot?address= to return my districts, races and candidates
  So that the app knows what is on my ballot

  This is story 2.4. Stories 1.5 and 2.3 built the chain this endpoint walks
  (address -> Census Geocoder -> districts row -> people with a current term),
  and `walking_skeleton.feature` still owns the proof that the chain works at
  all. What story 2.4 adds, and what this file covers, is the two things the
  story's own acceptance criteria name and 1.5/2.3 did not deliver:

    1. the response lists **races**, each with its candidates and an explicit
       incumbent flag — 1.5/2.3 returned a flat `officeholders` list, which is
       who represents the voter but not what is on their ballot;
    2. the lookup stays inside a **p95 latency budget of 800 ms**.

  On what a "candidate" honestly is today. datamodel.md models a candidate as a
  `candidacies` row, whose id requires an `election_id`, and no `elections` or
  `candidacies` rows exist yet — story 2.6 ingests them. So every candidate in
  this response is a **sitting officeholder, flagged as an incumbent**, and the
  response says so in `data_state` rather than implying a complete ballot.
  Nothing here invents an election, a candidacy id or a challenger.

  Background:
    Given the district "ocd-division/country:us/state:ca/cd:19" exists in ZeroDB
    And its representative and California's two senators are stored in ZeroDB

  Scenario: The ballot lists one race per seat, with its candidates
    When the voter submits the address "1600 Pennsylvania Ave, Washington, DC"
    Then the ballot lists a House race for the voter's district
    And the ballot lists a Senate race for the voter's state
    And the House race lists exactly 1 candidate
    And the Senate race lists exactly 2 candidates
    And every race names the office being contested

  Scenario: Every sitting officeholder on the ballot is flagged as an incumbent
    When the voter submits the address "1600 Pennsylvania Ave, Washington, DC"
    Then every candidate in every race is flagged as an incumbent
    And every candidate names the person it refers to

  Scenario: The response says that only incumbents are listed so far
    When the voter submits the address "1600 Pennsylvania Ave, Washington, DC"
    Then the ballot declares that challengers are not loaded yet
    And the ballot makes no recommendation about who to vote for

  Scenario: A former representative is not listed as a candidate
    Given the district's previous representative is also stored in ZeroDB, with a term that has ended
    When the voter submits the address "1600 Pennsylvania Ave, Washington, DC"
    Then the House race lists exactly 1 candidate
    And the former representative is not among the candidates

  # The at-large bug story 2.3 fixed, asserted again through the new response
  # shape: in WY, VT, DE, AK, ND and SD the House seat is at-large, so the House
  # and Senate divisions are the *same* OCD-ID. Grouping races by division alone
  # would collapse the two chambers into one race and lose a seat.
  Scenario: An at-large state gets a House race and a Senate race, not one merged race
    Given the at-large state "ocd-division/country:us/state:wy" and its three members exist in ZeroDB
    When the voter submits an address in the at-large state
    Then the ballot lists a House race for the voter's district
    And the ballot lists a Senate race for the voter's state
    And the House race lists exactly 1 candidate
    And the Senate race lists exactly 2 candidates

  # DC, PR, GU, VI, AS and MP elect a non-voting delegate and no senators.
  Scenario: A delegate district gets one race and no empty Senate race
    Given the delegate district "ocd-division/country:us/state:dc" and its delegate exist in ZeroDB
    When the voter submits an address in the delegate district
    Then the ballot lists a House race for the voter's district
    And the ballot lists no Senate race

  Scenario: A district whose members are not loaded yet lists an empty race, not an error
    Given the district "ocd-division/country:us/state:wy" exists in ZeroDB
    When the voter submits an address in that empty district
    Then the ballot lists a House race for the voter's district
    And the House race lists exactly 0 candidates

  # The story's second acceptance criterion. Asserted here against the
  # in-memory fake so CI is fast and deterministic: what this proves is the
  # *shape* of the work the endpoint does per lookup — one geocode and one
  # bounded read of `people`, not one read per member and not a read per page.
  # The live p95 is measured by scripts/verify_ballot_live.py, which is where a
  # number in milliseconds can honestly be claimed.
  Scenario: One lookup costs one geocode and one bounded read of the people table
    When the voter submits the address "1600 Pennsylvania Ave, Washington, DC"
    Then the geocoder was called exactly 1 time
    And the people table was read at most 2 times

  Scenario: A second lookup in the same district does not re-read the people table
    When the voter submits the address "1600 Pennsylvania Ave, Washington, DC"
    And the voter submits the address "1600 Pennsylvania Ave, Washington, DC" again
    Then the people table was read at most 2 times
    And both lookups returned the same races

  Scenario: The latency budget is declared in the response schema
    When the API's OpenAPI schema is fetched
    Then the ballot endpoint documents its 800 ms p95 budget
    And every field of the ballot response carries a description
