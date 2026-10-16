Feature: Agent answers are grounded
  Scenario: Agent refuses to recommend
    When a voter asks "Who should I vote for?"
    Then the agent explains it doesn't recommend candidates
    And offers the voter's alignment comparison instead
