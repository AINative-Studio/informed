Feature: The whole ballot, with every race's candidates, scores and confidence
  As a voter
  I want each race with its candidates, alignment scores and confidence
  So that I can see my whole ballot at once

  Story 7.1. The backend half is backend/tests/features/ballot_cards.feature,
  which proves `/v1/ballot/cards` reads story 7.2's candidate_cards read model
  without aggregating at request time; this feature proves the browser calls
  that endpoint per race and renders what it gets back, never re-sorting by
  score (prd.md: the app never recommends who to vote for).

  Both `/v1/ballot` and `/v1/ballot/cards` are stubbed at the network
  boundary, same as the walking-skeleton and follow-incumbent features: this
  feature's job is to prove the browser renders the payload it gets, not to
  re-prove either endpoint.

  Background:
    Given a voter opens the Informed app with a ballot that has races and cards

  Scenario: A voter sees every race on their ballot with its candidates
    When they submit their address for the whole-ballot view
    Then they see the whole ballot section
    And the House race and the Senate race both appear
    And every candidate in the ballot section is labeled as incumbent or challenger

  Scenario: A candidate's card shows its confidence badge once computed
    When they submit their address for the whole-ballot view
    Then the incumbent's card shows "High confidence"

  Scenario: A candidate with nothing computed yet shows an honest empty state
    When they submit their address for the whole-ballot view
    Then a senator's card says scores are not available yet
    And no error is shown for that candidate

  Scenario: The ballot section never recommends a candidate
    When they submit their address for the whole-ballot view
    Then the ballot section does not recommend, endorse or rank any candidate
