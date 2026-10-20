Feature: User-sortable ranking on the ballot
  As a voter
  I want to sort candidates by score if I choose
  So that I control how results are presented

  Story 7.6. Builds on story 7.1's whole-ballot view (`BallotView`), which
  renders every race's candidates in ballot order and never re-sorts them —
  prd.md's non-negotiable that the app never recommends who to vote for. This
  feature adds a sort control the voter can turn on themselves; the default
  stays ballot order until they act, and turning the control on re-orders by
  each candidate's score (`say_do_gap`) without ever calling anything
  "recommended" or a "best match" — this app does not rank candidates by its
  own judgment, it only lets a voter re-order what it already shows, on
  their own request.

  `/v1/ballot` and `/v1/ballot/cards` are stubbed at the network boundary,
  same posture as `ballot_view.feature`.

  Background:
    Given a voter opens the Informed app with a ballot that has multiple scored candidates in one race

  Scenario: Candidates render in ballot order by default
    When they submit their address for the whole-ballot view
    Then the candidates in that race appear in ballot order

  Scenario: A voter can turn on sorting by score
    When they submit their address for the whole-ballot view
    And they turn on sorting by score
    Then the candidates in that race appear in score order

  Scenario: Turning sorting back off restores ballot order
    When they submit their address for the whole-ballot view
    And they turn on sorting by score
    And they turn sorting off again
    Then the candidates in that race appear in ballot order

  Scenario: A candidate with no score yet does not break the sort
    Given a voter opens the Informed app with a ballot where one candidate in a race has no score yet
    When they submit their address for the whole-ballot view
    And they turn on sorting by score
    Then every candidate in that race is still shown
    And the candidate with no score does not crash or appear scored as zero

  Scenario: The sort control never uses recommending language
    When they submit their address for the whole-ballot view
    Then the sort control's label does not say recommended, best match, top pick or endorse
