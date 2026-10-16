Feature: A voter enters an address and sees their federal representatives
  As a voter
  I want to type my address into the app and see who represents me
  So that the team proves the React -> FastAPI -> ZeroDB path works in a browser

  Story 1.5, the UI half of the walking skeleton. The backend half is
  backend/tests/features/walking_skeleton.feature.

  The API is stubbed at the network boundary here on purpose: this feature's
  job is to prove the browser renders what the endpoint returns, and the
  backend feature already proves the endpoint returns it from ZeroDB. The
  polished design in ui-prd.md is a later story.

  Scenario: A voter submits an address and sees one House member and two senators
    Given a voter opens the Informed app
    When they enter their address and submit it
    Then they see their House district
    And they see 1 House member
    And they see 2 senators

  Scenario: Submitting an empty address asks for one instead of calling the API
    Given a voter opens the Informed app
    When they submit the address form without entering an address
    Then they are asked to enter an address

  Scenario: The app still does not tell the voter who to vote for
    Given a voter opens the Informed app
    When they enter their address and submit it
    Then the page never recommends a candidate
