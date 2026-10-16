Feature: Public methodology page (story 12.2)
  As a voter or journalist
  I want the methodology and its changelog published
  So that I can check how scores work

  # prd.md, "Trust, neutrality, privacy and safety": "Methodology page and
  # changelog; every verdict links to the methodology version that produced
  # it." Story 4.2 already seeds and reads the `methodology_versions` row for
  # its own internal use (tier weights for the profile editor). What this story
  # adds is the PUBLIC half: a stable, unauthenticated URL per version, so that
  # a verdict or score can cite one and anyone can follow the citation.
  #
  # Scope line, stated honestly: neither verdicts (epic 11) nor computed
  # scores (epic 6) exist in this codebase yet, so "every verdict links to its
  # methodology version" cannot be demonstrated end to end today. What is
  # demonstrable — and is what these scenarios pin — is that the thing a
  # verdict will cite is citable: it has a stable id, a stable public URL, it
  # stays fetchable after a newer version supersedes it, and it names its own
  # changelog. See `app/methodology/router.py` for the forward-looking note.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the methodology_versions table exists

  # --- the current version is public ---------------------------------------

  Scenario: The current methodology is fetchable with no credentials
    Given the current methodology version has been published
    When anyone requests the methodology with no authentication
    Then the response is 200
    And the response names methodology version "2026.1"

  Scenario: The current methodology publishes the live tier weights
    Given the current methodology version has been published
    When anyone requests the methodology with no authentication
    Then the published tier weights are primary 3.0, secondary 1.5 and nice_to_have 0.5

  Scenario: The current methodology publishes its changelog URL
    Given the current methodology version has been published
    When anyone requests the methodology with no authentication
    Then the response includes a changelog_url

  Scenario: The current methodology publishes when it was published
    Given the current methodology version has been published
    When anyone requests the methodology with no authentication
    Then the response includes the date it was published

  Scenario: The current methodology says which weights are not yet in force
    Given the current methodology version has been published
    When anyone requests the methodology with no authentication
    Then the unpopulated weight groups are reported as not yet published
    And no weight group is given an invented value

  # --- a version is citable by id ------------------------------------------
  # This is the half of "every verdict links to its methodology version" that
  # is demonstrable before epic 11 exists: the link target resolves.

  Scenario: A methodology version is fetchable by its own version id
    Given the current methodology version has been published
    When anyone requests methodology version "2026.1"
    Then the response is 200
    And the response names methodology version "2026.1"

  Scenario: The current methodology states its own canonical public URL
    Given the current methodology version has been published
    When anyone requests the methodology with no authentication
    Then the response carries the canonical public path for version "2026.1"

  Scenario: An unknown methodology version is a 404, not an empty answer
    Given the current methodology version has been published
    When anyone requests methodology version "1999.9"
    Then the response is 404

  # --- history stays readable (the audit trail) -----------------------------
  # datamodel.md marks the live row with `is_current` and never updates
  # history in place. A verdict published under 2026.1 must still be able to
  # show the voter the weights it was actually computed with, years later.

  Scenario: A superseded version stays fetchable by its id
    Given methodology version "2026.1" has been superseded by "2027.1"
    When anyone requests methodology version "2026.1"
    Then the response is 200
    And the published tier weights are primary 3.0, secondary 1.5 and nice_to_have 0.5

  Scenario: A superseded version is marked as no longer current
    Given methodology version "2026.1" has been superseded by "2027.1"
    When anyone requests methodology version "2026.1"
    Then the response says this version is not current

  Scenario: The current endpoint follows the is_current flag
    Given methodology version "2026.1" has been superseded by "2027.1"
    When anyone requests the methodology with no authentication
    Then the response names methodology version "2027.1"
    And the response says this version is current

  # --- the changelog list ---------------------------------------------------

  Scenario: Every published version is listed, newest first
    Given methodology version "2026.1" has been superseded by "2027.1"
    When anyone requests the list of methodology versions
    Then the response is 200
    And the listed versions are "2027.1" then "2026.1"

  Scenario: Each listed version carries its own public path and changelog URL
    Given methodology version "2026.1" has been superseded by "2027.1"
    When anyone requests the list of methodology versions
    Then every listed version carries a public path and a changelog_url

  Scenario: Exactly one listed version is marked current
    Given methodology version "2026.1" has been superseded by "2027.1"
    When anyone requests the list of methodology versions
    Then exactly one listed version is marked current

  # --- nothing voter-specific leaks ----------------------------------------
  # This is the one public, uncredentialed read in the API so far, and
  # prd.md's privacy rule is absolute. The methodology is published reference
  # data; a voter's profile must never ride along with it.

  Scenario: The published methodology carries nothing about any voter
    Given the current methodology version has been published
    When anyone requests the methodology with no authentication
    Then the response body mentions no voter, profile or interest data
