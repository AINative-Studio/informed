Feature: A voter's address resolves to their congressional district
  As a voter
  I want my address resolved to my congressional district
  So that I see the right races

  This is story 2.3. It replaces story 1.5's deliberate stub — which returned
  one fixed district for every address — with the real US Census Geocoder
  (geocoding.geo.census.gov, free and keyless, the source prd.md names).

  The district id is an OCD-ID, built from the geocoder's own answer:
  `States[].STUSAB` for the state and the congressional-district layer's
  `CD1xx` for the number, which the geocoder zero-pads ("07") and which this
  scheme does not ("/cd:7"), because `people.terms[].district_id` from story
  2.2 is unpadded and the ballot query matches those two ids as strings.

  Every scenario below drives a captured Census response rather than the live
  service: CI must not depend on census.gov being reachable (CLAUDE.md), and a
  captured response is the only way to assert on a shape — six matches in four
  states, say — that a live call cannot be relied on to reproduce on demand.

  Scenario: A valid address resolves to a congressional district OCD-ID
    Given the Census Geocoder matches an address in Washington's 7th district
    When the voter's address is resolved
    Then the resolved district id is "ocd-division/country:us/state:wa/cd:7"
    And the resolved senate district id is "ocd-division/country:us/state:wa"
    And the resolved state is "WA"

  Scenario: The geocoder's zero-padded district number is not carried into the id
    Given the Census Geocoder matches an address in Washington's 7th district
    When the voter's address is resolved
    Then the resolved district id does not contain "/cd:07"

  Scenario Outline: An at-large or delegate seat resolves to the statewide division
    Given the Census Geocoder returns congressional district "<cd>" in "<state>"
    When the voter's address is resolved
    Then the resolved district id is "<district_id>"
    And the resolved district id does not contain "/cd:"

    Examples: the two codes the geocoder uses for a seat with no district number
      | state | cd | district_id                             |
      | WY    | 00 | ocd-division/country:us/state:wy        |
      | VT    | 00 | ocd-division/country:us/state:vt        |
      | DC    | 98 | ocd-division/country:us/state:dc        |
      | PR    | 98 | ocd-division/country:us/state:pr        |

  Scenario: An address the geocoder cannot find gives a helpful error
    Given the Census Geocoder finds no match for the address
    When the voter's address is resolved and fails
    Then the error tells the voter their address could not be found
    And the error message does not contain the address

  Scenario: An empty address is refused without calling the geocoder
    When an empty address is resolved and fails
    Then the error tells the voter an address is required
    And the Census Geocoder was never called

  Scenario: An ambiguous address asks the voter for more detail rather than guessing
    Given the Census Geocoder returns matches in several different districts
    When the voter's address is resolved and fails
    Then the error asks the voter for a more complete address
    And the error message does not contain the address

  Scenario: Matches that agree on one district resolve despite being several
    Given the Census Geocoder returns two matches in the same district
    When the voter's address is resolved
    Then the resolved district id is "ocd-division/country:us/state:ma/cd:1"

  Scenario: The geocoder being unreachable is reported as a service problem
    Given the Census Geocoder cannot be reached
    When the voter's address is resolved and fails
    Then the error tells the voter the lookup is temporarily unavailable
    And the error message does not contain the address

  Scenario: The address itself is never persisted
    Given the Census Geocoder matches an address in Washington's 7th district
    And the district "ocd-division/country:us/state:wa/cd:7" exists in ZeroDB
    When the voter submits that address to the ballot endpoint
    Then no row written to ZeroDB contains the address
    And the response does not echo the address back
    And nothing the resolver returns carries the address

  Scenario: The address is never written to the log
    Given the Census Geocoder finds no match for the address
    When the voter's address is resolved and fails
    Then no log line records the address

  Scenario: A real address joins to a real legislator's district
    Given the Census Geocoder matches an address in Washington's 7th district
    And a sitting representative whose term district id came from story 2.2
    When the voter's address is resolved
    Then the resolved district id equals that representative's term district id
