Feature: Evidence drawer, opening the full source behind a score
  As a voter
  I want to open any vote behind a score and see the source
  So that I can verify it

  Story 7.5. Builds on story 7.3's candidate detail page, which already links
  each reason's citations (`AlignmentReason.citations`, from
  `GET /v1/candidates/{id}/alignment?profile_id=`) as a bare "Roll call ..."
  link. This feature proves that clicking a citation opens a drawer showing
  the full roll-call detail a voter needs to verify it: the roll-call number,
  its date, the kind of vote it was, how the candidate voted, a link to the
  bill, and the archived copy of that bill's text (story 5.4's
  `GET /v1/bills/{bill_id}/text`), not just the live external source.

  Both `/v1/candidates/{id}/alignment` and `/v1/bills/{bill_id}/text` are
  stubbed at the network boundary, same posture as every other feature here.

  Background:
    Given a voter opens the Informed app with a scored candidate whose citation has an archived bill

  Scenario: Opening a citation shows the full roll-call detail
    When they open that candidate's detail page
    And they open the evidence drawer for that citation
    Then the drawer shows the roll-call number, the date, the vote type and the candidate's position
    And the drawer shows a link to the bill
    And the drawer shows a link to the archived copy of the bill text

  Scenario: The drawer can be closed and returns focus to the control that opened it
    When they open that candidate's detail page
    And they open the evidence drawer for that citation
    And they close the drawer
    Then the drawer is no longer visible
    And focus returns to the link that opened it

  Scenario: The drawer can be closed with the Escape key
    When they open that candidate's detail page
    And they open the evidence drawer for that citation
    And they press Escape
    Then the drawer is no longer visible

  Scenario: A citation with no bill attached has no archived source to open
    Given a voter opens the Informed app with a scored candidate whose citation has no bill
    When they open that candidate's detail page
    Then no evidence drawer control is offered for that citation
