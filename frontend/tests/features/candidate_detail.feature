Feature: Candidate detail, broken down interest by interest
  As a voter
  I want a candidate page that breaks the score down interest by interest
  So that I see where we agree and disagree

  Story 7.3. The backend half is already merged: `GET
  /v1/candidates/{id}/alignment?profile_id=` (stories 6.4-6.7) returns
  `reasons`, each an `AlignmentReason` carrying a `stance_id`, a `direction`
  ("raised" or "lowered"), an `s_value` (the candidate's measured position,
  -1..+1), an `evidence_count`, and `citations` that are structurally
  guaranteed non-empty. This feature proves the browser renders that payload
  as one row per interest with its direction, its evidence count, and at
  least one real clickable link per row — never a fabricated or inert one.

  `/v1/candidates/{id}/alignment` is stubbed at the network boundary, same
  posture as every other feature here: this feature's job is to prove the
  browser renders what it gets back, not to re-prove the scoring endpoint.

  Background:
    Given a voter opens the Informed app with a scored candidate

  Scenario: A voter sees one row per interest with its direction and evidence
    When they open that candidate's detail page
    Then the detail page shows a row for each reason behind the score
    And each row shows whether the candidate's record agrees or disagrees with that interest
    And each row shows how many pieces of evidence it rests on

  Scenario: Every row's evidence links to a real, clickable source
    When they open that candidate's detail page
    Then every row has at least one link a voter can actually open

  Scenario: A candidate with no measured positions shows an honest empty state
    Given a voter opens the Informed app with a candidate who has no measured positions
    When they open that candidate's detail page
    Then the detail page says there is nothing measured yet
    And no interest row is shown

  Scenario: The candidate detail page never recommends a vote
    When they open that candidate's detail page
    Then the detail page does not recommend, endorse or rank the candidate
