Feature: Anyone can read how Informed scores candidates (story 12.2)
  As a voter or journalist
  I want the methodology and its changelog published on a page I can open
  So that I can check how scores work without taking anyone's word for it

  Story 12.2, the UI half. The backend half is
  backend/tests/features/public_methodology.feature.

  The API is stubbed at the network boundary here on purpose, exactly as
  walking_skeleton.steps.ts stubs /v1/ballot: this feature's job is to prove the
  browser renders the published methodology, and the backend feature already
  proves the endpoint serves it out of ZeroDB.

  The page is reachable without signing in and without an interest profile,
  because a journalist checking the method is not a voter using the product.
  The polished design in ui-prd.md is a later story; what is asserted here is
  that a skeptical reader actually gets the numbers and a way to check them.

  Scenario: A reader opens the methodology page and sees the live tier weights
    Given a reader opens the Informed methodology page
    Then they see the methodology version in force
    And they see the weight for each interest tier

  Scenario: The page links the changelog so a reader can check what changed
    Given a reader opens the Informed methodology page
    Then they see a link to the methodology changelog

  Scenario: The page says plainly what is not scored yet
    Given a reader opens the Informed methodology page
    Then they are told which parts of the methodology are not published yet

  Scenario: Reading the methodology needs no interest profile
    Given a reader opens the Informed methodology page
    Then they were never asked to sign in or enter an address

  Scenario: The methodology page still does not tell anyone who to vote for
    Given a reader opens the Informed methodology page
    Then the methodology page never recommends a candidate
