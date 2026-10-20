Feature: Alignment score, confidence, dealbreakers and reasons (stories 6.4, 6.5, 6.6, 6.7)
  As a voter
  I want a 0-100 alignment score for each candidate against my profile, with the
  evidence it rests on, a warning when a candidate crossed a dealbreaker, and the
  top reasons behind the number
  So that I can compare candidates quickly without over-trusting thin data

  # prd.md, Feature 3's formula, which this feature file is the executable
  # statement of:
  #
  #     Alignment = 50 * (1 + (sum_i w_i*k_i*c_i*s_i) / (sum_i w_i*k_i*c_i))
  #
  # w_i = tier weight (methodology_versions.weights.tiers)
  # k_i = intensity, 1-3 (the voter's own, per interest)
  # c_i = evidence confidence, 0-1 (candidate_positions.confidence)
  # s_i = candidate position, -1..+1 (candidate_positions.s_value)
  #
  # backlog.md 6.4: "Implements the PRD formula; computed per request and never
  # persisted; the same input always gives the same output."
  # backlog.md 6.5: "Badges are High, Medium, Low or Insufficient, with
  # thresholds from the methodology."
  # backlog.md 6.6: "Matches the PRD Gherkin scenario 'Dealbreaker overrides
  # the average'."
  # backlog.md 6.7: "Each reason cites a roll call or bill; no reason lacks a
  # citation."

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And a published methodology with real tier weights and badge thresholds
    And a synthetic candidacy for a synthetic incumbent
    And a synthetic voter with an encrypted profile

  # --- story 6.4: the formula ----------------------------------------------

  Scenario: A candidate who matches every interest perfectly scores 100
    Given the candidate's position is +1.0 on every interest in the profile
    When the voter requests the candidate's alignment
    Then the alignment score is 100.0

  Scenario: A candidate who opposes every interest scores 0
    Given the candidate's position is -1.0 on every interest in the profile
    When the voter requests the candidate's alignment
    Then the alignment score is 0.0

  Scenario: A candidate with no measured position either way scores 50
    Given the candidate's position is 0.0 on every interest in the profile
    When the voter requests the candidate's alignment
    Then the alignment score is 50.0

  Scenario: Tier weight makes a primary interest count more than a nice-to-have
    Given a profile with one primary interest and one nice-to-have interest
    And the candidate matches the primary interest and opposes the nice-to-have
    When the voter requests the candidate's alignment
    Then the alignment score is above 50.0

  Scenario: Thin evidence moves the score less than strong evidence
    Given a profile with two equally weighted interests
    And the candidate matches one with confidence 1.0 and opposes the other with confidence 0.1
    When the voter requests the candidate's alignment
    Then the alignment score is above 50.0

  Scenario: Intensity breaks ties inside a tier
    Given a profile with two primary interests at intensity 3 and 1
    And the candidate matches the intense one and opposes the other
    When the voter requests the candidate's alignment
    Then the alignment score is above 50.0

  # datamodel.md: "Per-user alignment scores are computed per request and never
  # stored." A stored score would be a row linking a voter's priorities to a
  # candidate -- exactly the join datamodel.md says no table may make.

  Scenario: Requesting an alignment stores nothing
    Given the candidate's position is +1.0 on every interest in the profile
    When the voter requests the candidate's alignment
    Then no table holds any row linking the voter to the candidate
    And no new rows were written by the request

  Scenario: The same profile and the same positions always give the same score
    Given the candidate's position is +1.0 on every interest in the profile
    When the voter requests the candidate's alignment 5 times
    Then every response carries an identical score

  # prd.md's own acceptance criterion, previously pending in
  # tests/features/interest_profile.feature: "no candidate score is shown until
  # at least one primary interest exists".

  Scenario: A voter with no stored profile gets no score
    Given the voter has no stored profile
    When the voter requests the candidate's alignment
    Then the request is refused with a 404

  # --- story 6.5: confidence badges ----------------------------------------
  # prd.md: "Confidence badge: High (>=10 relevant votes or clear
  # commitments), Medium, Low, Insufficient data." The thresholds are read
  # from the methodology, never hard-coded in the scorer -- the same rule
  # datamodel.md states for tier weights.

  Scenario: Ten or more relevant votes earn a High badge
    Given the candidate's positions rest on 10 relevant votes in total
    When the voter requests the candidate's alignment
    Then the confidence badge is "high"

  Scenario: A handful of votes earns a Medium badge
    Given the candidate's positions rest on 5 relevant votes in total
    When the voter requests the candidate's alignment
    Then the confidence badge is "medium"

  Scenario: One or two votes earn a Low badge
    Given the candidate's positions rest on 2 relevant votes in total
    When the voter requests the candidate's alignment
    Then the confidence badge is "low"

  Scenario: No evidence at all earns an Insufficient badge
    Given the candidate has no measured position on any of the voter's interests
    When the voter requests the candidate's alignment
    Then the confidence badge is "insufficient"
    And no alignment score is reported

  Scenario: The badge thresholds come from the published methodology
    When the methodology publishes different badge thresholds
    And the candidate's positions rest on 5 relevant votes in total
    And the voter requests the candidate's alignment
    Then the confidence badge reflects the new thresholds

  # --- story 6.6: dealbreaker detection ------------------------------------
  # This is prd.md's own BDD scenario, reproduced verbatim below under its
  # original name, plus the structural rules that make it true.

  Scenario: Dealbreaker overrides the average
    Given a voter marks "Prescription drug pricing" as a dealbreaker
    And candidate A scores 82 overall but acted against that stance on 3 final-passage votes
    When the voter views the ballot
    Then candidate A shows a dealbreaker warning above the score

  Scenario: The dealbreaker warning is a separate field, not a score adjustment
    Given a voter with a dealbreaker interest the candidate acted against
    When the voter requests the candidate's alignment
    Then the response carries a dealbreaker warning
    And the alignment score is unchanged by the warning

  Scenario: A dealbreaker the candidate supports raises no warning
    Given a voter with a dealbreaker interest the candidate acted for
    When the voter requests the candidate's alignment
    Then the response carries no dealbreaker warning

  Scenario: A dealbreaker with no measured position raises no warning
    Given a voter with a dealbreaker interest the candidate has never voted on
    When the voter requests the candidate's alignment
    Then the response carries no dealbreaker warning

  Scenario: A dealbreaker warning cites the votes behind it
    Given a voter with a dealbreaker interest the candidate acted against
    When the voter requests the candidate's alignment
    Then the dealbreaker warning cites at least one roll call

  Scenario: A non-dealbreaker interest the candidate opposed raises no warning
    Given a voter whose non-dealbreaker primary interest the candidate acted against
    When the voter requests the candidate's alignment
    Then the response carries no dealbreaker warning

  # --- story 6.7: top reasons with citations -------------------------------
  # backlog.md 6.7: "Each reason cites a roll call or bill; no reason lacks a
  # citation." Made structural rather than asserted: a reason cannot be
  # constructed without at least one citation (see
  # `app.scoring.models.AlignmentReason`).

  Scenario: The response carries the top three reasons behind the score
    Given the candidate has measured positions on 5 of the voter's interests
    When the voter requests the candidate's alignment
    Then the response carries 3 reasons

  Scenario: Every reason cites a roll call
    Given the candidate has measured positions on 5 of the voter's interests
    When the voter requests the candidate's alignment
    Then every reason cites at least one roll call
    And every citation names a real stored roll call

  Scenario: A reason cannot be built without a citation
    When a reason is constructed with no citations
    Then the construction is refused

  Scenario: The reasons are the largest contributors to the score
    Given the candidate has measured positions on 5 of the voter's interests
    When the voter requests the candidate's alignment
    Then the reasons are ordered by how much they moved the score
    And the first reason contributed more than the last

  Scenario: A candidate with fewer than three measured positions gets fewer reasons
    Given the candidate has a measured position on 1 of the voter's interests
    When the voter requests the candidate's alignment
    Then the response carries 1 reason

  Scenario: A reason says which way it moved the score
    Given the candidate has measured positions on 5 of the voter's interests
    When the voter requests the candidate's alignment
    Then each reason says whether it raised or lowered the score

  # --- privacy, which applies to every response above ----------------------
  # prd.md: voter profiles are political-opinion data. The alignment response
  # is derived from one, so it must carry no more of it than the voter already
  # knows, and the request must leave no trace of it.

  Scenario: The alignment response holds no raw profile payload
    Given the candidate's position is +1.0 on every interest in the profile
    When the voter requests the candidate's alignment
    Then the response holds no encrypted payload, key reference or profile row id

  Scenario: An alignment request writes nothing to the events log
    Given the candidate's position is +1.0 on every interest in the profile
    When the voter requests the candidate's alignment
    Then no event was emitted

  # --- neutrality ----------------------------------------------------------
  # prd.md: "a nightly symmetry audit runs the same synthetic profiles against
  # all candidates and flags skew". The formula half of that is checkable here.

  Scenario: Mirrored profiles score mirrored candidates identically
    Given two synthetic candidates with mirrored positions
    And two synthetic voters with mirrored profiles
    When each voter's alignment with their aligned candidate is computed
    Then the two scores are identical within 0.5 points
