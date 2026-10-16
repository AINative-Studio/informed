Feature: Public corrections (story 12.4)
  As anyone
  I want to report an error on any claim
  So that mistakes get fixed

  # prd.md, "Trust, neutrality, privacy and safety": "Public correction form on
  # every claim, with an SLA (72 hours to triage)."
  #
  # backlog.md 12.4's acceptance criteria, which these scenarios are the
  # executable form of:
  #   * triaged within 72 hours        -> `sla_due_at` is set on creation and
  #                                       "past SLA" is queryable
  #   * status is visible to the reporter
  #   * contact email is optional and deleted 90 days after resolution
  #
  # Two things this story settles that datamodel.md leaves open, both argued in
  # `app/corrections/models.py`:
  #
  #   1. **The status vocabulary.** datamodel.md documents the `status` field
  #      but no values for it (unlike `verdict` and `qa_status`, which it
  #      enumerates). This story defines `received`, `triaged`, `resolved`,
  #      `rejected`.
  #   2. **How an anonymous reporter checks status.** There are no accounts for
  #      anonymous reporters and `contact_email` is optional, so submission
  #      returns a single-use, unguessable tracking token. The row id is
  #      `cor:{uuid4}` per datamodel.md and the token is a SEPARATE secret, so
  #      a leaked id does not grant the lookup.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the corrections table exists

  # --- anyone can report an error, with no account -------------------------
  # "As anyone" is the whole story. A correction form behind a login is a
  # correction form nobody uses, and the people most likely to spot an error
  # about a candidate are the least likely to have signed up.

  Scenario: Anyone can submit a correction with no authentication
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    Then the response is 201
    And the correction is stored with status "received"

  Scenario: A submitted correction is given an id matching the documented pattern
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    Then the correction id starts with "cor:"

  Scenario: The correction records what it is about and what is wrong
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    Then the stored correction targets table "stances" and id "en-US:housing:v1:build-more-homes"
    And the stored correction carries the reporter's description of the error

  Scenario: A correction about an unknown row is accepted, not rejected
    When anyone submits a correction about stance "en-US:housing:v1:no-such-stance" with no authentication
    Then the response is 201
    And the correction is stored with status "received"

  Scenario: A correction naming a table that is not in the schema is refused
    When anyone submits a correction against target table "not_a_real_table"
    Then the response is 422
    And no correction is stored

  Scenario: A correction with an empty body is refused
    When anyone submits a correction with an empty body
    Then the response is 422
    And no correction is stored

  # --- contact_email is genuinely optional ---------------------------------
  # datamodel.md's retention table calls it an "Optional field", and the
  # acceptance criterion says "contact email is optional". Optional means a
  # submission without one succeeds AND still gets a working status lookup —
  # otherwise the email is required in practice to use the feature.

  Scenario: A correction can be submitted with no contact email at all
    When anyone submits a correction with no contact email
    Then the response is 201
    And the stored correction has no contact email

  Scenario: A reporter who gave no email can still check their status
    When anyone submits a correction with no contact email
    And the reporter checks the status with their tracking token
    Then the response is 200
    And the reported status is "received"

  Scenario: A contact email is stored when the reporter chooses to give one
    When anyone submits a correction with contact email "reporter@example.invalid"
    Then the stored correction has contact email "reporter@example.invalid"

  Scenario: A malformed contact email is refused rather than stored unusable
    When anyone submits a correction with contact email "not-an-email"
    Then the response is 422
    And no correction is stored

  # --- the 72-hour triage SLA ----------------------------------------------
  # prd.md commits to triage within 72 hours. The commitment is only real if
  # the deadline is recorded per correction and the overdue ones can be found.

  Scenario: sla_due_at is set to 72 hours after the correction was created
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    Then the stored correction is due for triage 72 hours after it was created

  Scenario: A correction still untriaged after 72 hours is past its SLA
    Given a correction submitted 73 hours ago and never triaged
    When the ops team lists corrections past their triage SLA
    Then that correction is listed

  Scenario: A correction submitted an hour ago is not past its SLA
    Given a correction submitted 1 hour ago and never triaged
    When the ops team lists corrections past their triage SLA
    Then that correction is not listed

  Scenario: A correction triaged within the window is not past its SLA
    Given a correction submitted 73 hours ago and never triaged
    And the ops team triages that correction
    When the ops team lists corrections past their triage SLA
    Then that correction is not listed

  Scenario: A resolved correction is never reported as past its triage SLA
    Given a correction submitted 73 hours ago and never triaged
    And the ops team resolves that correction
    When the ops team lists corrections past their triage SLA
    Then that correction is not listed

  # --- status is visible to the reporter, without an account ---------------

  Scenario: A reporter can check their correction's status with no account
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    And the reporter checks the status with their tracking token
    Then the response is 200
    And the reported status is "received"

  Scenario: The reporter sees the status change once the correction is triaged
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    And the ops team triages that correction
    And the reporter checks the status with their tracking token
    Then the reported status is "triaged"

  Scenario: The reporter sees the status change once the correction is resolved
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    And the ops team resolves that correction
    And the reporter checks the status with their tracking token
    Then the reported status is "resolved"

  Scenario: The reporter is told the triage deadline their report is held to
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    And the reporter checks the status with their tracking token
    Then the status response states the 72-hour triage deadline

  Scenario: An unknown tracking token is a 404, not an error
    When someone checks the status with tracking token "cor-track-does-not-exist"
    Then the response is 404

  # --- one reporter's lookup cannot leak another's correction --------------
  # The lookup is unauthenticated by necessity, so the token is the only thing
  # standing between it and every correction in the table.

  Scenario: One correction's status lookup never returns another's contents
    Given two corrections submitted by two different reporters
    When the first reporter checks the status with their tracking token
    Then the response carries nothing from the second reporter's correction

  Scenario: A status lookup never discloses any reporter's contact email
    When anyone submits a correction with contact email "reporter@example.invalid"
    And the reporter checks the status with their tracking token
    Then the response does not contain the contact email

  Scenario: A status lookup never discloses the correction's own body
    When anyone submits a correction with contact email "reporter@example.invalid"
    And the reporter checks the status with their tracking token
    Then the response does not contain the reported description

  Scenario: The row id alone does not open the status lookup
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    And someone checks the status using the correction's row id instead of the token
    Then the response is 404

  Scenario: Tracking tokens are not sequential or guessable from one another
    Given two corrections submitted by two different reporters
    Then the two tracking tokens share no guessable relationship

  # --- contact_email is deleted 90 days after resolution -------------------
  # datamodel.md, "Privacy, retention and integrity": `corrections.contact_email`
  # is retained "90 days after resolution". The retention rule is a promise to
  # the reporter, so it is enforced by a tested function rather than a note in
  # a runbook.

  Scenario: A contact email is erased 90 days after the correction was resolved
    Given a correction resolved 91 days ago with contact email "reporter@example.invalid"
    When the retention sweep runs
    Then that correction has no contact email
    And the correction itself is still stored

  Scenario: A contact email is kept while the retention window is still open
    Given a correction resolved 10 days ago with contact email "reporter@example.invalid"
    When the retention sweep runs
    Then that correction still has its contact email

  Scenario: An unresolved correction's contact email is never swept
    Given a correction submitted 200 days ago and never triaged
    When the retention sweep runs
    Then that correction still has its contact email

  Scenario: A rejected correction's email is swept 90 days after it was closed
    Given a correction rejected 91 days ago with contact email "reporter@example.invalid"
    When the retention sweep runs
    Then that correction has no contact email

  Scenario: The retention sweep reports what it erased
    Given a correction resolved 91 days ago with contact email "reporter@example.invalid"
    And a correction resolved 10 days ago with contact email "keep@example.invalid"
    When the retention sweep runs
    Then the sweep reports erasing exactly 1 contact email

  Scenario: The retention sweep is safe to run twice
    Given a correction resolved 91 days ago with contact email "reporter@example.invalid"
    When the retention sweep runs
    And the retention sweep runs again
    Then the sweep reports erasing exactly 0 contact emails
    And that correction has no contact email

  Scenario: Erasing the email leaves the correction's status and history intact
    Given a correction resolved 91 days ago with contact email "reporter@example.invalid"
    When the retention sweep runs
    Then that correction is still status "resolved"
    And that correction still records when it was resolved
    And that correction still records what was reported

  Scenario: A reporter whose email was erased can still check the status
    Given a correction resolved 91 days ago with contact email "reporter@example.invalid"
    When the retention sweep runs
    And the reporter checks the status with their tracking token
    Then the response is 200
    And the reported status is "resolved"

  # --- triage and resolution ------------------------------------------------
  # The "so that mistakes get fixed" half. No admin UI in this story; what has
  # to exist is a path that moves a correction through the vocabulary and
  # records when it was closed.

  Scenario: Triaging a correction records that it was triaged
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    And the ops team triages that correction
    Then the correction is stored with status "triaged"
    And the correction has no resolution time yet

  Scenario: Resolving a correction records when it was resolved
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    And the ops team resolves that correction
    Then the correction is stored with status "resolved"
    And the correction records when it was resolved

  Scenario: Rejecting a correction records when it was closed
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    And the ops team rejects that correction
    Then the correction is stored with status "rejected"
    And the correction records when it was resolved

  Scenario: Reopening a resolved correction clears its resolution time
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    And the ops team resolves that correction
    And the ops team moves that correction back to triaged
    Then the correction is stored with status "triaged"
    And the correction has no resolution time yet

  Scenario: An unknown status value is refused
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    And the ops team tries to set the status to "mostly-fine"
    Then the status change is refused

  # --- nothing voter-derived rides along -----------------------------------
  # `POST /v1/corrections` is a public, uncredentialed write. prd.md's privacy
  # rule is absolute: no voter profile data may touch this table or this
  # endpoint's responses.

  Scenario: A correction submission response carries nothing voter-derived
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    Then the response mentions no voter, profile or interest data

  Scenario: A correction row carries no voter profile fields
    When anyone submits a correction about stance "en-US:housing:v1:build-more-homes" with no authentication
    Then the stored correction carries only the fields datamodel.md documents
