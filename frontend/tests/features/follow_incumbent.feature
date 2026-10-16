Feature: A voter can follow an incumbent from the ballot view
  As an engaged citizen
  I want to follow an incumbent from my ballot
  So that I can track them over their term

  Story 15.1, the UI half. The backend half is
  backend/tests/features/follow_incumbent.feature, which proves follows are
  stored inside the encrypted profile payload; this feature proves the
  browser calls the right endpoint and reflects the result.

  The API is stubbed at the network boundary, same as the walking-skeleton
  feature: this feature's job is to prove the browser calls the follow
  endpoint and renders what it gets back, not to re-prove the backend.

  Scenario: A voter follows an officeholder from the ballot
    Given a voter opens the Informed app
    When they enter their address and submit it
    And they follow "Rep. Synthetic Example"
    Then the button now offers to unfollow "Rep. Synthetic Example"

  Scenario: A voter unfollows someone they already followed
    Given a voter opens the Informed app
    When they enter their address and submit it
    And they follow "Rep. Synthetic Example"
    And they unfollow "Rep. Synthetic Example"
    Then the button now offers to follow "Rep. Synthetic Example"

  Scenario: A rejected follow shows a readable message and does not flip the button
    Given a voter opens the Informed app
    When they enter their address and submit it
    And they try to follow "Rep. Synthetic Example" but the server rejects it
    Then they see a readable error message
    And the button still offers to follow "Rep. Synthetic Example"
