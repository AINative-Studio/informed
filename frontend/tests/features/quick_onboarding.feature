Feature: Quick onboarding (story 4.1)
  As a busy voter
  I want to pick 3 primary topics and my stance on each in under 2 minutes
  So that I get value fast

  Background:
    Given a voter opens the Informed app
    And they switch to the quick-setup screen

  Scenario: Picking topics shows nothing pre-selected
    Then none of the topic choices is pre-selected

  Scenario: The picker shows how many of 3 have been chosen
    When they pick 2 of the 3 required topics
    Then the picker shows "2 of 3 picked"

  Scenario: A 4th topic is blocked with an explanation, not silently swapped
    When they pick 3 primary topics
    Then a 4th topic choice is disabled with an explanation shown

  Scenario: Stance options arrive with nothing pre-selected
    When they pick 3 primary topics and move to the stance step
    Then none of the stance choices for any topic is pre-selected

  Scenario: The consent screen offers the account and anonymous paths side by side
    When they pick 3 primary topics, a stance for each, and move to the consent step
    Then the consent screen offers saving to an account
    And the consent screen offers staying anonymous, as an equal choice

  Scenario: No score is shown anywhere before or after saving
    When they pick 3 primary topics, a stance for each, and move to the consent step
    Then the onboarding screen shows no candidate score anywhere

  Scenario: Choosing anonymous mode saves nothing to the network
    When they pick 3 primary topics, a stance for each, and choose anonymous mode
    Then they see a confirmation that their profile is saved in this browser only
