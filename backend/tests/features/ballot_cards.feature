Feature: Ballot cards endpoint serves one race's candidates, scores and confidence
  As a voter
  I want GET /v1/ballot/cards?election_id=&district_id= to return every
  candidate's card for a race
  So that I can see my whole ballot at once

  This is story 7.1. Story 7.2 built `candidate_cards` — the read model — and
  its own reader (`app.ballot.cards.CandidateCardReader`), and deliberately
  shipped no HTTP endpoint: "Story 7.1 is the ballot view... this story is the
  read model they read." This feature covers the endpoint 7.1 adds on top of
  that reader, plus the two things backlog.md's acceptance criterion for 7.1
  names that the read model alone does not give a voter:

    1. **ballot order by default** — candidates come back in the same stable,
       never-ranked order `CandidateCardReader.cards_for_race` already
       returns (by name), because prd.md forbids a default order that reads as
       a recommendation;
    2. **incumbents labeled** — `candidate_cards` does not carry
       `is_incumbent` (it is a `candidacies` field, not a card field; 7.2's
       model docstring lists the card's fields and this is not one of them),
       so this endpoint reads `candidacies` for the race as well — a second
       bounded read of the same size class as the cards read itself (6-15
       rows), not the five-table aggregation over `candidate_positions` /
       `verdicts` / `member_votes` that 7.2's "never aggregates at request
       time" rule actually forbids.

  A `confidence_badge` is computed per card from `positions_summary`'s own
  `evidence_count` fields — already stored on the row — through
  `app.scoring.badges.badge_for`, the same pure function and the same
  methodology-sourced thresholds story 6.5 built for the personalized
  alignment endpoint. This is a pure function of data already on the card, so
  it is not request-time aggregation of a source table either.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And a candidacy for a synthetic incumbent

  Scenario: Cards for a race come back in stable name order, never ranked
    Given a second candidacy in the same race, with a lexically earlier name
    And every card has been rebuilt
    When the ballot cards endpoint is asked for that race
    Then the cards are returned in name order
    And nothing about the order implies a recommendation

  Scenario: Every card in the response is labeled with its incumbency
    Given every card has been rebuilt
    When the ballot cards endpoint is asked for that race
    Then every card in the response carries an explicit is_incumbent flag
    And the incumbent candidacy is flagged true

  Scenario: A challenger with no current term is labeled as a non-incumbent
    Given a challenger candidacy with no current term in the same race
    And every card has been rebuilt
    When the ballot cards endpoint is asked for that race
    Then the challenger's card is flagged is_incumbent false

  Scenario: A card with no computed positions shows an insufficient confidence badge
    Given every card has been rebuilt
    When the ballot cards endpoint is asked for that race
    Then the incumbent's card shows confidence badge "insufficient"

  Scenario: A card whose positions rest on plenty of evidence shows a high confidence badge
    Given the incumbent has a current position on a stance backed by 12 votes
    And every card has been rebuilt
    When the ballot cards endpoint is asked for that race
    Then the incumbent's card shows confidence badge "high"

  Scenario: A race with no cards yet returns an empty list, not an error
    When the ballot cards endpoint is asked for a race with no candidacies
    Then the response is 200 with an empty list of cards

  Scenario: A card the reader has never built is absent, not a placeholder row
    Given a candidacy exists with no card ever rebuilt for it
    When the ballot cards endpoint is asked for that race
    Then that candidacy's card is absent from the response rather than a fabricated zero row

  Scenario: Cards from another district never leak onto this ballot
    Given a candidacy in a different district, with its card rebuilt
    And every card has been rebuilt
    When the ballot cards endpoint is asked for that race
    Then the other district's candidate is not among the cards

  Scenario: Missing query parameters are rejected before any read
    When the ballot cards endpoint is asked with no district_id
    Then the response is 422

  Scenario: The response never carries a per-voter alignment score
    Given every card has been rebuilt
    When the ballot cards endpoint is asked for that race
    Then no card in the response carries a personalized score or a profile reference

  Scenario: The latency-relevant read shape is declared in the response schema
    When the API's OpenAPI schema is fetched
    Then the ballot cards endpoint documents that it never aggregates at request time
    And every field of the ballot cards response carries a description
