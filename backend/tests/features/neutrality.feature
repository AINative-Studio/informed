Feature: Neutrality
  Scenario: Symmetry audit
    Given two synthetic candidates with mirrored voting records
    And two synthetic voters with mirrored profiles
    When scores are computed
    Then each voter's score for their aligned candidate is identical within 0.5 points
