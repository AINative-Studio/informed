Feature: Quick onboarding (story 4.1)
  As a busy voter
  I want to pick 3 primary topics and my stance on each in under 2 minutes
  So that I get value fast

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the issue taxonomy has been seeded
    And the stance cards have been seeded

  # --- GET /v1/topics: the tree the quick picker is built from --------------

  Scenario: The topic tree lists every top-level topic with its subtopics
    When a voter requests the topic tree
    Then the response lists 20 top-level topics
    And every top-level topic carries its subtopics nested under it
    And the tree holds 60 subtopics in total

  Scenario: The topic tree carries no stance-presupposing framing
    When a voter requests the topic tree
    Then no topic or subtopic name in the tree reads as a suggestion

  # --- GET /v1/topics/{id}/stances: cards for one subtopic, randomized ------

  Scenario: Stance options for a subtopic arrive with nothing pre-selected
    When a voter requests stance options for "en-US:prescription-drug-pricing:v1"
    Then at least 3 stance options are returned
    And none of the returned stance options is marked as selected or default

  Scenario: Requesting stances for an unknown topic returns an empty list, not an error
    When a voter requests stance options for "en-US:does-not-exist:v1"
    Then an empty list of stance options is returned

  # --- POST /v1/profiles: the consent gate ----------------------------------

  Scenario: Saving a profile without consent is refused
    Given a completed quick-onboarding selection of 3 primary topics with one stance each
    When the voter submits the profile without the consent field
    Then the save is refused with a 422 naming the missing consent

  Scenario: Saving a profile with consent withheld is refused
    Given a completed quick-onboarding selection of 3 primary topics with one stance each
    When the voter submits the profile with consent explicitly false
    Then the save is refused with a 422 naming the missing consent

  Scenario: Saving a profile with consent given succeeds
    Given a completed quick-onboarding selection of 3 primary topics with one stance each
    When the voter submits the profile with consent given
    Then the profile is saved at version 1
    And no candidate score is shown until at least one primary interest exists

  Scenario: A quick-onboarding profile round-trips through the encrypted store
    Given a completed quick-onboarding selection of 3 primary topics with one stance each
    When the voter submits the profile with consent given
    Then loading the voter's profile returns the same 3 primary topics

  Scenario: An incomplete selection with no primary interest is rejected
    Given a quick-onboarding selection with no primary topics chosen
    When the voter submits the profile with consent given
    Then the save is refused with a 422 naming the missing primary interest

  Scenario: Saving in anonymous mode is refused by the server, as documented
    Given a completed quick-onboarding selection of 3 primary topics with one stance each
    When the voter submits the profile with consent given but no account
    Then the save is refused because anonymous mode writes no row
