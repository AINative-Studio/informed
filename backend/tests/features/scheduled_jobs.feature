Feature: Scheduled job runner (infrastructure for stories 2.6, 5.1/5.6, 12.4)

  As an operator with no scheduler, worker, cron or queue available yet
  I want one command that knows every periodic job this codebase needs and
  runs only the ones that are due
  So that an external trigger (system cron, a Railway cron service, a GitHub
  Actions schedule) can invoke it blindly, on any cadence, with no per-job
  awareness, and still get correct behaviour

  Background:
    Given a ZeroDB client backed by an in-memory fake

  # --- a due job runs and records it --------------------------------------

  Scenario: A job that has never run is due and runs
    Given a registered job "demo-job" that runs every 1 day(s)
    And the job has never recorded a last-run
    When I run the scheduled job runner
    Then the job "demo-job" ran
    And the job "demo-job" has a recorded last-run timestamp

  Scenario: A job becomes due again once its interval has elapsed
    Given a registered job "demo-job" that runs every 1 day(s)
    And the job "demo-job" last ran 2 day(s) ago
    When I run the scheduled job runner
    Then the job "demo-job" ran

  # --- a job not yet due is skipped, cleanly --------------------------------

  Scenario: A job not yet due is skipped, not re-run
    Given a registered job "demo-job" that runs every 7 day(s)
    And the job "demo-job" last ran 1 day(s) ago
    When I run the scheduled job runner
    Then the job "demo-job" did not run
    And the runner reports the job "demo-job" as skipped, not due yet

  # --- one job's failure doesn't block the others ---------------------------

  Scenario: One job's failure does not block the others from running
    Given a registered job "broken-job" that runs every 1 day(s) and always fails
    And a registered job "healthy-job" that runs every 1 day(s)
    And neither job has ever run
    When I run the scheduled job runner
    Then the job "healthy-job" ran
    And the runner reports the job "broken-job" as failed
    And the runner's overall exit code is nonzero

  Scenario: A job's failure does not update its last-run timestamp
    Given a registered job "broken-job" that runs every 1 day(s) and always fails
    And the job has never recorded a last-run
    When I run the scheduled job runner
    Then the job "broken-job" has no recorded last-run timestamp

  # --- the real registry: sweep_correction_emails is in it, daily ----------

  Scenario: sweep_correction_emails is registered to run daily
    Then the real job registry includes a job named "sweep_correction_emails"
    And that job's interval is 1 day(s)

  Scenario: load_candidacies is registered to run weekly
    Then the real job registry includes a job named "load_candidacies"
    And that job's interval is 7 day(s)

  Scenario: load_house_rollcalls is registered to run hourly, ready for story 5.6
    Then the real job registry includes a job named "load_house_rollcalls"
    And that job's interval is 1 hour(s)

  Scenario: run_nightly_integrity_checks is registered to run nightly
    Then the real job registry includes a job named "run_nightly_integrity_checks"
    And that job's interval is 1 day(s)

  Scenario: repair_member_votes_denormalization is registered to run nightly
    Then the real job registry includes a job named "repair_member_votes_denormalization"
    And that job's interval is 1 day(s)

  Scenario: A job missing its required credentials is skipped as blocked, not crashed
    Given a registered job "needs-a-key" that runs every 1 day(s) and requires an unset environment variable
    And the job has never recorded a last-run
    When I run the scheduled job runner
    Then the job "needs-a-key" did not run
    And the runner reports the job "needs-a-key" as blocked, missing credentials
    And the job "needs-a-key" has no recorded last-run timestamp
