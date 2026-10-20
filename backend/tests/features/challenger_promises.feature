Feature: Challenger promises, grouped by the voter's interests (story 9.6)
  As a voter
  I want to see a challenger's promises even though they have no voting record
  So that I can still compare them

  # backlog.md 9.6: "Promises are grouped by my interests; a prior record in
  # another office is linked if one exists."
  #
  # A challenger has promises (`promises`, story 9.3) but no `candidate_positions`
  # (those come only from a voting record — story 6.2) and so would be invisible
  # to `GET /v1/candidates/{id}/alignment` (stories 6.4-6.7), which reports
  # `insufficient` confidence and an empty `reasons` list for exactly that voter.
  # This is the promise-side counterpart for a candidate with no vote history:
  # `GET /v1/candidates/{id}/promises?profile_id=` groups a challenger's testable
  # promises under the voter's OWN stated interests (`stance_id`, the same key
  # `AlignmentReason` groups by), and separately names any prior elected office
  # the person's own `people.terms[]` already records.
  #
  # Grouping is keyed by `stance_id`, not an arbitrary category: a promise with
  # no `stance_id` (not yet classified by story 10.1's mapping) or whose
  # `stance_id` names no interest the voter chose is not thrown away -- it is
  # counted as "not about one of your interests" rather than silently dropped,
  # because a missing promise is a worse failure than an uncategorized one.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And a synthetic voter with an encrypted profile naming two interests

  Scenario: A challenger's promises are grouped under the voter's own interests
    Given a synthetic challenger with no voting record
    And the challenger has a testable promise about the voter's first interest
    And the challenger has a testable promise about the voter's second interest
    When the voter requests the challenger's promises
    Then the response groups the promises under each matching interest
    And each group names the real stance the voter chose

  Scenario: A promise about a different interest is not silently dropped
    Given a synthetic challenger with no voting record
    And the challenger has a testable promise about an interest the voter did not choose
    When the voter requests the challenger's promises
    Then the response reports it as not matching one of the voter's interests
    And no interest group names a promise the voter never asked about

  Scenario: A non-testable promise is shown but carries no scoring claim
    Given a synthetic challenger with no voting record
    And the challenger has a non-testable promise about the voter's first interest
    When the voter requests the challenger's promises
    Then the promise appears in its group
    And it is marked not testable

  Scenario: Every promise carries its verbatim quote and its source
    Given a synthetic challenger with no voting record
    And the challenger has a testable promise about the voter's first interest
    When the voter requests the challenger's promises
    Then the promise shows its verbatim quote
    And the promise links the archived campaign source and its capture date

  Scenario: A prior record in another office is linked when one exists
    Given a synthetic challenger who previously served in a different body
    And the challenger has a testable promise about the voter's first interest
    When the voter requests the challenger's promises
    Then the response names the challenger's prior office
    And the prior office names the body they served in

  Scenario: A challenger with no prior office gets no fabricated one
    Given a synthetic challenger with no voting record
    And the challenger has a testable promise about the voter's first interest
    When the voter requests the challenger's promises
    Then the response names no prior office

  Scenario: A promise whose archived source has gone missing is never shown uncited
    Given a synthetic challenger with no voting record
    And the challenger has a promise whose archived source row no longer exists
    When the voter requests the challenger's promises
    Then that promise is omitted rather than shown without a citation

  Scenario: A challenger with no promises yet gets an honest empty result
    Given a synthetic challenger with no voting record
    And the challenger has made no promises
    When the voter requests the challenger's promises
    Then the response carries no promise groups
    And the response does not fail

  Scenario: An incumbent's own voting record is irrelevant to this view
    Given a synthetic challenger with no voting record
    And the challenger has a testable promise about the voter's first interest
    When the voter requests the challenger's promises
    Then the response requires no voting record for the challenger

  Scenario: A voter with no stored profile gets no grouped view
    Given a synthetic challenger with no voting record
    And the voter has no stored profile
    When the voter requests the challenger's promises
    Then the request is refused with a 404

  Scenario: An unknown candidacy is refused
    When the voter requests promises for an unknown candidacy
    Then the request is refused with a 404

  Scenario: The response never recommends a candidate
    Given a synthetic challenger with no voting record
    And the challenger has a testable promise about the voter's first interest
    When the voter requests the challenger's promises
    Then the response carries no recommendation, endorsement or ranking
