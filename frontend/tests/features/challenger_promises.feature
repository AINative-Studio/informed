Feature: Challenger promises view (story 9.6)
  As a voter
  I want to see a challenger's promises even though they have no voting record
  So that I can still compare them

  # The backend half is story 9.6's own: `GET
  # /v1/candidates/{id}/promises?profile_id=` groups a challenger's testable
  # promises under the voter's own chosen interests, and separately names a
  # prior elected office when `people.terms[]` records one. This feature
  # proves the browser renders that payload honestly: one group per matching
  # interest, the promise's verbatim quote and source, a prior-office link
  # when present, and a plain "no prior office" when it is not -- never a
  # fabricated one.
  #
  # `/v1/candidates/{id}/promises` is stubbed at the network boundary, same
  # posture as `candidate_detail.feature`: this feature proves the browser
  # renders what it gets back, not the grouping logic itself.

  Background:
    Given a voter opens the Informed app with a challenger who has promises

  Scenario: A voter sees the challenger's promises grouped by their own interests
    When they open that challenger's promises view
    Then the view shows a group for each of the voter's matching interests
    And each promise in a group shows its verbatim quote

  Scenario: A promise outside the voter's interests is shown, not hidden
    Given that challenger has a promise outside the voter's interests
    When they open that challenger's promises view
    Then the view says that promise does not match one of the voter's interests

  Scenario: A non-testable promise is labeled, not scored
    Given that challenger has a non-testable promise
    When they open that challenger's promises view
    Then that promise is labeled not testable

  Scenario: A prior office is linked when the challenger has one
    Given that challenger previously served in another office
    When they open that challenger's promises view
    Then the view shows a link to the challenger's prior office

  Scenario: No prior office is shown when the challenger has never held one
    Given that challenger has never held elected office
    When they open that challenger's promises view
    Then the view shows no prior-office link

  Scenario: A challenger with no promises yet shows an honest empty state
    Given a voter opens the Informed app with a challenger who has no promises yet
    When they open that challenger's promises view
    Then the view says there are no promises on file yet
    And no promise group is shown

  Scenario: The challenger promises view never recommends a vote
    When they open that challenger's promises view
    Then the view does not recommend, endorse or rank the candidate
