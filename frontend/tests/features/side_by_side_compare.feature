Feature: Side-by-side compare
  As a voter
  I want to compare candidates in a race side by side
  So that I can see the differences on what matters to me

  Story 7.4. Builds on story 7.1's whole-ballot view (`BallotView`), which
  already lists every race's candidates, and story 7.3's per-candidate
  alignment breakdown (`GET /v1/candidates/{id}/alignment?profile_id=`),
  which already names one row per interest with its direction, evidence
  count and citations for a single candidate. This feature proves a voter
  can select 2-4 of those candidates and see the SAME rows — the union of
  every selected candidate's stance ids — laid out as columns, with an
  honest empty state for any candidate who has no measured position on a
  given row, and with every color-coded cell also carrying a text label so
  the comparison does not depend on color perception alone.

  `/v1/ballot`, `/v1/ballot/cards` and `/v1/candidates/{id}/alignment` are
  stubbed at the network boundary, same posture as every other feature here.

  Background:
    Given a voter opens the Informed app with a ballot that has four scored candidates in one race

  Scenario: A voter must pick at least two candidates before comparing
    When they submit their address for the whole-ballot view
    Then there is no way to open the compare view yet

  Scenario: Selecting two candidates offers a compare view
    When they submit their address for the whole-ballot view
    And they select two candidates in that race to compare
    Then a control to compare the selected candidates appears

  Scenario: A voter can select up to four candidates but no more
    When they submit their address for the whole-ballot view
    And they select two candidates in that race to compare
    And they select two more candidates in that race to compare
    Then a fifth candidate cannot be selected for comparison

  Scenario: The compare view shows the same rows for every selected candidate
    When they submit their address for the whole-ballot view
    And they select two candidates in that race to compare
    And they open the compare view
    Then every selected candidate's column appears
    And the same set of interest rows appears for every candidate

  Scenario: A candidate with no measured position on a row shows an honest empty state
    When they submit their address for the whole-ballot view
    And they select two candidates in that race to compare
    And they open the compare view
    Then the row for an interest only one candidate has a position on shows an honest empty state for the other candidate

  Scenario: Direction is never shown by color alone
    When they submit their address for the whole-ballot view
    And they select two candidates in that race to compare
    And they open the compare view
    Then every color-coded cell also carries a text label saying its direction

  Scenario: The compare view never recommends a candidate
    When they submit their address for the whole-ballot view
    And they select two candidates in that race to compare
    And they open the compare view
    Then the compare view does not recommend, endorse or rank any candidate
