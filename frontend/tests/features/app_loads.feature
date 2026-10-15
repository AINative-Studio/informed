Feature: The Informed web app loads
  As a voter
  I want the app to load in my browser
  So that the team can prove the React build and the BDD runner work end to end

  Scenario: A voter opens the app
    Given a voter opens the Informed app
    Then the page shows the Informed name
    And the page explains that Informed is nonpartisan
