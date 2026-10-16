Feature: A district lookup answers for a date, not just for today
  As the system
  I want districts to carry valid_from and valid_to
  So that votes and ballots map to the right map after redistricting

  This is story 2.7. Story 2.3 already writes both dates on every `districts`
  row it creates — `valid_from` is the day the current Congress convened and
  `valid_to` is null, meaning "these boundaries are still current" — so the
  fields are not new. What is new is that they MEAN something to a reader: a
  lookup can name a date and get the district that was valid on it.

  Why there is no historical boundary data here, and what is tested instead
  ========================================================================

  Informed has one source of district boundaries, the Census Geocoder, and it
  answers for one vintage: the current one. There is no free, keyless source of
  *historical* congressional boundaries this story could pull, and inventing
  pre-2025 rows from nothing would be fabricated evidence — exactly what
  prd.md's evidence-over-opinion rule forbids. So today every district has
  exactly one version, and the honest scope of this story is that the storage
  and the query are correct *when* a second version arrives:

    * a lookup with no date answers with the current version, exactly as story
      2.3's ballot endpoint already does (no regression);
    * a lookup with a date before any version existed is a miss, not a guess;
    * a lookup with a date inside a particular version's range returns THAT
      version — tested against a synthetic two-version district, because no
      real one exists yet.

  The synthetic district below is clearly labelled as test data and uses a
  state (`zz`) that is not a real USPS code, so it can never be confused with
  a real row.

  Scenario: A lookup with no date returns the current district
    Given a current district "ocd-division/country:us/state:wa/cd:7" valid from "2025-01-03"
    When the district "ocd-division/country:us/state:wa/cd:7" is looked up with no date
    Then the lookup returns the district valid from "2025-01-03"
    And the returned district is open-ended

  Scenario: A lookup with today's date returns the current district
    Given a current district "ocd-division/country:us/state:wa/cd:7" valid from "2025-01-03"
    When the district "ocd-division/country:us/state:wa/cd:7" is looked up as of "2026-11-03"
    Then the lookup returns the district valid from "2025-01-03"

  Scenario: A lookup for a date before the district existed finds nothing
    Given a current district "ocd-division/country:us/state:wa/cd:7" valid from "2025-01-03"
    When the district "ocd-division/country:us/state:wa/cd:7" is looked up as of "2024-06-01"
    Then the lookup finds no district for that date

  Scenario: A lookup on the first day a district is valid returns it
    Given a current district "ocd-division/country:us/state:wa/cd:7" valid from "2025-01-03"
    When the district "ocd-division/country:us/state:wa/cd:7" is looked up as of "2025-01-03"
    Then the lookup returns the district valid from "2025-01-03"

  # The acceptance criterion, as a real capability rather than a coincidence of
  # there being only one version today. Both rows are synthetic.
  Scenario: Two versions of one district each answer for their own era
    Given a redistricted district "ocd-division/country:us/state:zz/cd:1" with these versions
      | valid_from | valid_to   | name                     |
      | 2013-01-03 | 2022-12-31 | Old Congressional District 1 |
      | 2023-01-03 |            | Congressional District 1 |
    When the district "ocd-division/country:us/state:zz/cd:1" is looked up as of "2016-11-08"
    Then the lookup returns the district named "Old Congressional District 1"
    When the district "ocd-division/country:us/state:zz/cd:1" is looked up as of "2026-11-03"
    Then the lookup returns the district named "Congressional District 1"

  Scenario: An election date between two versions falls in the one that contains it
    Given a redistricted district "ocd-division/country:us/state:zz/cd:1" with these versions
      | valid_from | valid_to   | name                     |
      | 2013-01-03 | 2022-12-31 | Old Congressional District 1 |
      | 2023-01-03 |            | Congressional District 1 |
    When the district "ocd-division/country:us/state:zz/cd:1" is looked up as of "2022-12-31"
    Then the lookup returns the district named "Old Congressional District 1"
    When the district "ocd-division/country:us/state:zz/cd:1" is looked up as of "2023-01-03"
    Then the lookup returns the district named "Congressional District 1"

  Scenario: The gap between two versions is a miss, not the nearest version
    Given a redistricted district "ocd-division/country:us/state:zz/cd:1" with these versions
      | valid_from | valid_to   | name                     |
      | 2013-01-03 | 2022-12-31 | Old Congressional District 1 |
      | 2023-01-03 |            | Congressional District 1 |
    When the district "ocd-division/country:us/state:zz/cd:1" is looked up as of "2023-01-01"
    Then the lookup finds no district for that date

  # A redistricting arrives as a new current version; the superseded one keeps
  # its own row and its own id, so history is never overwritten.
  Scenario: Recording a redistricting keeps the superseded version readable
    Given a current district "ocd-division/country:us/state:zz/cd:2" valid from "2023-01-03"
    When that district is redistricted on "2027-01-03" as "Congressional District 2 (new map)"
    Then the district "ocd-division/country:us/state:zz/cd:2" looked up as of "2026-11-03" is named "Congressional District 2"
    And the district "ocd-division/country:us/state:zz/cd:2" looked up as of "2027-06-01" is named "Congressional District 2 (new map)"
    And the superseded version's valid_to is "2027-01-02"
    And the current version is still stored under the plain OCD-ID

  # Overlapping ranges would make "which district was valid on date X" have two
  # answers, so the write refuses rather than storing an ambiguity for a reader
  # to resolve arbitrarily. datamodel.md rule 5: integrity lives in code.
  Scenario: Overlapping validity ranges are refused
    Given a current district "ocd-division/country:us/state:zz/cd:3" valid from "2023-01-03"
    When a version of that district valid from "2024-01-01" to "2025-01-01" is written and fails
    Then the write is refused as an overlap

  Scenario: A version whose valid_to precedes its valid_from is refused
    When a district version valid from "2025-01-03" to "2024-01-01" is validated and fails
    Then the validation names valid_to as before valid_from

  # No regression: story 2.4's ballot endpoint reads the current district by
  # its plain OCD-ID and must keep working untouched.
  Scenario: The ballot endpoint still answers for the current district
    Given the district "ocd-division/country:us/state:ca/cd:19" exists in ZeroDB
    When the voter submits an address to the ballot endpoint
    Then the ballot names the district "ocd-division/country:us/state:ca/cd:19"
