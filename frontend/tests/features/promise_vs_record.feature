Feature: Promise vs. Record
  Scenario: Kept promise with cited evidence
    Given incumbent B's 2024 campaign site committed to "vote to cap insulin costs"
    And B voted Yea on the final-passage roll call of a bill capping insulin costs
    When the voter opens B's Promise vs. Record
    Then the promise shows verdict "Kept"
    And it links the archived campaign page and the roll-call record

  Scenario: No recorded vote
    Given a promise maps only to a bill passed by voice vote
    Then the verdict is "No opportunity" and the UI says "no recorded vote"
