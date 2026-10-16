Feature: Tier limits, dealbreaker and profile load (stories 4.2, 4.3, 4.4)

  # Story 4.2 — "As a voter, I want to sort interests into primary, secondary
  # and nice-to-have, so that scores reflect what matters most to me."
  # Acceptance: 1-5 primary and up to 8 secondary; weights load from
  # methodology_versions; violations show inline errors (the 422's detail
  # names the violated rule, which is what a UI turns into an inline error —
  # see frontend/tests/features/profile_tiers_and_dealbreaker.feature for the
  # UI-rendered half of that acceptance criterion).
  #
  # Story 4.3 — "As a voter, I want to mark a primary interest as a
  # dealbreaker, so that a candidate who acts against it is flagged regardless
  # of their average." Acceptance: allowed only on primary; shown in the
  # profile summary (the loaded profile's own shape, read back in full below).
  #
  # Story 4.4 — "As a signed-in voter, I want my profile saved, so that I can
  # come back to it." Acceptance: uses the encrypted store (14.1); a round
  # trip returns an identical profile.

  Background:
    Given a ZeroDB client backed by an in-memory fake

  # --- GET /v1/profiles/methodology: where the tier weights come from -------

  Scenario: The tier weights are read from methodology_versions, not hard-coded
    When a voter requests the current tier weights
    Then the weights are primary 3.0, secondary 1.5 and nice_to_have 0.5
    And the response names which methodology version they came from

  # --- Story 4.2: tier limits enforced on save, violations named -----------

  Scenario: A profile with 1-5 primary and up to 8 secondary interests saves
    Given a tiered selection with 5 primary, 8 secondary and 2 nice-to-have interests
    When the voter submits the profile with consent given
    Then the profile is saved at version 1

  Scenario: More than 5 primary interests is refused, naming the primary limit
    Given a tiered selection with 6 primary interests
    When the voter submits the profile with consent given
    Then the save is refused with a 422 naming the primary limit

  Scenario: More than 8 secondary interests is refused, naming the secondary limit
    Given a tiered selection with 1 primary and 9 secondary interests
    When the voter submits the profile with consent given
    Then the save is refused with a 422 naming the secondary limit

  Scenario: Nice-to-have interests are unlimited
    Given a tiered selection with 1 primary and 20 nice-to-have interests
    When the voter submits the profile with consent given
    Then the profile is saved at version 1

  # --- Story 4.3: dealbreaker allowed only on primary -----------------------

  Scenario: A dealbreaker on a primary interest is saved and shown in the profile
    Given a tiered selection with a dealbreaker on a primary interest
    When the voter submits the profile with consent given
    And the voter loads their profile
    Then the loaded profile's summary shows that interest as a dealbreaker

  Scenario: A dealbreaker on a secondary interest is refused, naming dealbreaker
    Given a tiered selection with a dealbreaker on a secondary interest
    When the voter submits the profile with consent given
    Then the save is refused with a 422 naming dealbreaker

  # --- Story 4.4: load returns the saved profile, round-tripped exactly ----

  Scenario: Loading a never-saved voter's profile returns nothing, not an error
    When a voter with no saved profile loads their profile
    Then the load returns no profile, with a 200 rather than an error

  Scenario: A saved profile round-trips exactly through load
    Given a tiered selection with 2 primary, 3 secondary and 1 nice-to-have interests
    When the voter submits the profile with consent given
    And the voter loads their profile
    Then the loaded profile has the same interests, tiers and stances as saved

  Scenario: Loading a profile never exposes it to a different voter
    Given a tiered selection with 1 primary interest
    When the voter submits the profile with consent given
    And a different voter loads their profile
    Then the different voter's load returns no profile
