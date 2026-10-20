Feature: Nightly symmetry audit (story 12.1)
  As a methodology owner
  I want mirrored synthetic candidates and profiles scored every night
  So that any partisan skew is caught

  # prd.md, "Neutrality": "One scoring codebase for all candidates; no
  # party-specific rules. A nightly symmetry audit runs the same synthetic
  # profiles against all candidates and flags skew."
  #
  # What makes this story DIFFERENT from the antisymmetry unit test that
  # already exists (tests/test_alignment_formula_unit.py's
  # test_the_score_is_antisymmetric_under_flipping_every_position): that test
  # proves the FORMULA — four numbers in, one number out — is antisymmetric.
  # It cannot see candidate_positions being computed from votes, the stance
  # direction being applied, the tier weights being read, the badge, the
  # dealbreaker check or the reasons. A skew introduced in ANY of those is
  # invisible to it, and every one of them is code a voter's number passes
  # through. So this audit drives the REAL pipeline end to end over a
  # deliberately mirrored synthetic fixture and compares the real results.
  #
  # Acceptance criteria (backlog.md 12.1): "Fails if mirrored scores differ by
  # more than 0.5; results are written to audit_runs; a failure blocks the
  # next release."

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And a published methodology version

  # --- the passing path: the real pipeline is symmetric ---------------------

  Scenario: The audit passes on the real pipeline and reports a zero delta
    When I run the nightly symmetry audit
    Then the symmetry audit passes
    And the audit's maximum delta is at most 0.5
    And the audit scored at least one mirrored pair

  Scenario: A mirrored candidate's score is the complement of the original's
    When I run the nightly symmetry audit
    Then every mirrored candidate pair's two scores sum to 100
    And the two mirrored candidates' scores were computed by the same code path

  Scenario: A mirrored voter scoring a mirrored candidate gets the identical score
    When I run the nightly symmetry audit
    Then every mirrored voter and candidate pair scores identically

  Scenario: Confidence badges are identical across a mirrored pair
    When I run the nightly symmetry audit
    Then both candidates in every mirrored pair carry the same confidence badge
    And both candidates in every mirrored pair rest on the same amount of evidence

  Scenario: The reasons are mirror images, not asymmetric in count or direction
    When I run the nightly symmetry audit
    Then every mirrored pair names the same interests as its top reasons
    And each reason that raised one candidate's score lowered the mirror's

  Scenario: Dealbreaker warnings fire on exactly one side of a mirrored pair
    When I run the nightly symmetry audit
    Then exactly one candidate in each mirrored dealbreaker pair carries a warning

  # --- the result is recorded ----------------------------------------------

  Scenario: Results are written to audit_runs
    When I run the nightly symmetry audit
    Then an audit_runs row is written for today
    And the audit_runs row records the methodology version that scored it
    And the audit_runs row records the maximum delta and that it passed
    And the audit_runs row contains no voter data

  Scenario: Re-running the audit on the same day replaces that day's row
    When I run the nightly symmetry audit
    And I run the nightly symmetry audit again
    Then exactly one audit_runs row exists for today

  # --- the failing path: a real skew is caught ------------------------------

  Scenario: The audit fails when the scorer is skewed toward one direction
    Given the scorer has been sabotaged to favour positive positions
    When I run the nightly symmetry audit
    Then the symmetry audit fails
    And the audit's maximum delta is greater than 0.5
    And the failing audit_runs row records that it did not pass

  Scenario: A skew smaller than the tolerance still passes
    Given the scorer has been sabotaged by 0.2 points
    When I run the nightly symmetry audit
    Then the symmetry audit passes

  Scenario: A skew just over the tolerance fails
    Given the scorer has been sabotaged by 0.6 points
    When I run the nightly symmetry audit
    Then the symmetry audit fails

  # --- the release gate ----------------------------------------------------

  Scenario: A failure blocks the next release
    Given the scorer has been sabotaged to favour positive positions
    When I run the symmetry release gate
    Then the release gate blocks the release
    And the release gate explains which mirrored pair was asymmetric

  Scenario: A passing audit does not block the release
    When I run the symmetry release gate
    Then the release gate allows the release

  Scenario: The release gate blocks when no audit has ever run
    Given no audit_runs row exists
    When I run the symmetry release gate against the recorded result only
    Then the release gate blocks the release

  Scenario: The release gate blocks on a stale audit result
    Given the only audit_runs row is 3 days old and passed
    When I run the symmetry release gate against the recorded result only
    Then the release gate blocks the release

  # --- it runs every night -------------------------------------------------

  Scenario: The audit is a registered nightly job
    Then the job registry contains a nightly symmetry audit job
    And that job fails loudly when the audit finds skew
