Feature: Tiers, dealbreaker and profile load (stories 4.2, 4.3, 4.4)
  As a voter
  I want to sort interests into primary, secondary and nice-to-have, mark a
  primary interest as a dealbreaker, and come back to a saved profile
  So that scores reflect what matters most to me and I never have to start over

  Background:
    Given a voter opens the Informed app

  # --- Story 4.2: tiers and limits ------------------------------------------

  Scenario: Every topic starts with no tier assigned
    When they switch to the full profile editor
    Then none of the tier choices is pre-selected

  Scenario: The tier weights shown come from the live methodology, not a guess
    When they switch to the full profile editor
    Then the editor states the primary weight as 3 and the secondary weight as 1.5

  Scenario: Assigning a topic to primary and giving it a stance requires consent before saving
    When they switch to the full profile editor
    And they assign "Prescription drug pricing" to primary with a stance
    Then the save button is disabled until consent is given

  # --- Story 4.3: dealbreaker, primary only ---------------------------------

  Scenario: A dealbreaker checkbox is offered only once a topic is primary
    When they switch to the full profile editor
    And they assign "Prescription drug pricing" to primary with a stance
    Then a dealbreaker checkbox is shown for that topic

  Scenario: A secondary topic offers no dealbreaker checkbox
    When they switch to the full profile editor
    And they assign "Broadband access" to secondary
    Then no dealbreaker checkbox is shown for that topic

  Scenario: A marked dealbreaker appears in the profile summary
    When they switch to the full profile editor
    And they assign "Prescription drug pricing" to primary with a stance
    And they mark it as a dealbreaker
    Then the profile summary shows that topic as a dealbreaker

  # --- Story 4.4: save and load ---------------------------------------------

  Scenario: Saving with consent given stores the profile
    When they switch to the full profile editor
    And they assign "Prescription drug pricing" to primary with a stance
    And they give consent and save
    Then they see a confirmation that their profile was saved

  Scenario: Returning to the editor loads the previously-saved profile
    Given they have already saved a profile with "Medicare" as a primary dealbreaker
    When they switch to the full profile editor
    Then "Medicare" already shows as primary with the dealbreaker checked
