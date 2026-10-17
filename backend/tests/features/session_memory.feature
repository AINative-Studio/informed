Feature: Short-lived Q&A session memory (story 13.5)
  As a voter
  I want my Q&A conversation forgotten after a day
  So that my questions aren't kept

  Scenario: A turn written in identified mode carries a one-day retention
    Given an identified Q&A session
    When the voter's turn is remembered
    Then the stored turn's retention is 1 day
    And the stored turn expires exactly 1 day after it was written

  Scenario: Nothing is written in anonymous mode
    Given an anonymous Q&A session
    When the voter's turn is remembered
    Then nothing was written to ZeroDB
    And the service reports the turn as not stored because the session is anonymous

  Scenario: Anonymous mode is refused before the request is built, not filtered afterwards
    Given an anonymous Q&A session
    When the voter's turn is remembered
    Then no ZeroDB request was made at all

  Scenario: Reading a session back returns only its own unexpired turns
    Given an identified Q&A session with two remembered turns
    And a second identified Q&A session with one remembered turn
    When the first session's memory is read
    Then only the first session's two turns come back

  Scenario: A turn older than one day is never returned, even if the store still holds it
    Given an identified Q&A session with a turn remembered 25 hours ago
    When the session's memory is read
    Then no turns come back

  Scenario: The retention sweep deletes every turn past its one-day window
    Given an identified Q&A session with a turn remembered 25 hours ago
    And an identified Q&A session with a turn remembered 1 hour ago
    When the session-memory retention sweep runs
    Then the turn from 25 hours ago is deleted
    And the turn from 1 hour ago is kept

  Scenario: The retention sweep is safe to run twice
    Given an identified Q&A session with a turn remembered 25 hours ago
    When the session-memory retention sweep runs
    And the session-memory retention sweep runs again
    Then the second run deletes nothing

  Scenario: The retention sweep is a registered scheduled job
    When the scheduled job registry is listed
    Then it contains a job named "sweep_qa_session_memory"
    And that job's interval is at most 1 day

  Scenario: A remembered turn never carries the voter's profile
    Given an identified Q&A session
    When the voter's turn is remembered
    Then the stored payload holds no interest, stance, tier or dealbreaker field
