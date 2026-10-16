Feature: Tiered interest profile
  Scenario: Voter sets primary, secondary and nice-to-have interests
    Given a new voter on the onboarding screen
    When they choose "Prescription drug pricing" as a primary interest with stance "lower out-of-pocket costs"
    And they choose "Broadband access" as a secondary interest
    And they choose "Public lands" as a nice-to-have interest
    Then their profile is saved with weights 3.0, 1.5 and 0.5
    And no candidate score is shown until at least one primary interest exists

  Scenario: Dealbreaker overrides the average
    Given a voter marks "Prescription drug pricing" as a dealbreaker
    And candidate A scores 82 overall but acted against that stance on 3 final-passage votes
    When the voter views the ballot
    Then candidate A shows a dealbreaker warning above the score
