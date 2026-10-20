Feature: House incremental sync, roll-call events and full backfill (stories 5.6, 5.7, 5.8)
  As the system
  I want new House roll calls picked up hourly, announced as events, and the
  118th/119th Congress backfilled in full
  So that scoring rests on an incumbent's whole current-era record and
  downstream consumers update without polling

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist

  # --- story 5.6: hourly incremental sync -------------------------------------
  #
  # "A new roll call appears within 2 hours of publication; reruns are no-ops."
  #
  # The two-hour promise is a function of the registered cadence, not of the
  # ingest code: the job is registered hourly, so a roll call published just
  # after one run is picked up by the next, bounding visible lag at one
  # interval plus the run's own duration. Pinned here so a later change to the
  # cadence has to change a test that states the promise.

  Scenario: The House roll-call sync is registered to run hourly
    When I read the default job registry
    Then the job "load_house_rollcalls" is registered
    And the job "load_house_rollcalls" runs every 1 hour
    And the job "load_house_rollcalls" meets the 2-hour publication promise

  Scenario: The hourly sync is blocked rather than failed without an API key
    Given no CONGRESS_GOV_API_KEY is set in the environment
    When I read the default job registry
    Then the job "load_house_rollcalls" is blocked with a reason naming CONGRESS_GOV_API_KEY

  Scenario: A roll call published between runs is picked up by the next run
    Given the hourly sync last succeeded 61 minutes ago
    When the scheduled job runner runs
    Then the job "load_house_rollcalls" is due

  Scenario: Re-running the hourly sync over unchanged source data writes no new rows
    Given I have already synced the sample roll call "item_final_passage" once
    When I sync the sample roll call "item_final_passage" again
    Then exactly 1 row carries the roll call id "us-house:119-2:452"
    And exactly 1 provenance_log row exists for roll_calls "us-house:119-2:452"

  # --- story 5.7: rollcall.ingested event -------------------------------------
  #
  # "The event carries roll_call_id, bill_id and topic_ids; it is emitted
  # exactly once per roll call."
  #
  # Story 6.2 already built the consumer (PositionRecomputeConsumer) and left
  # the producer as a documented gap. These scenarios are that producer.

  Scenario: Ingesting a new roll call emits a rollcall.ingested event
    When I sync the sample roll call "item_final_passage"
    Then exactly 1 "rollcall.ingested" event was emitted
    And the "rollcall.ingested" event carries roll_call_id "us-house:119-2:452"
    And the "rollcall.ingested" event carries a bill_id field
    And the "rollcall.ingested" event carries a topic_ids field

  Scenario: Re-ingesting an unchanged roll call emits no second event
    Given I have already synced the sample roll call "item_final_passage" once
    When I sync the sample roll call "item_final_passage" again
    Then exactly 1 "rollcall.ingested" event was emitted

  Scenario: Re-ingesting a corrected roll call emits a second event
    Given I have already synced the sample roll call "item_final_passage" once
    When the source corrects roll call "us-house:119-2:452" and I sync it again
    Then exactly 2 "rollcall.ingested" events were emitted

  Scenario: Each distinct roll call in one batch gets its own event
    When I sync the sample roll calls "item_final_passage" and "item_amendment"
    Then exactly 2 "rollcall.ingested" events were emitted
    And a "rollcall.ingested" event was emitted for "us-house:119-2:452"
    And a "rollcall.ingested" event was emitted for "us-house:119-2:451"

  Scenario: The emitted event is the shape story 6.2's consumer reads
    When I sync the sample roll call "item_final_passage"
    Then the position recompute consumer handles the emitted event without error

  # --- story 5.8: backfill the 118th and 119th Congress -----------------------
  #
  # "Backfill completes; row counts match the source totals."
  #
  # The source total is `pagination.count` on the list endpoint, confirmed
  # live. A backfill that silently loads only the first page would "complete"
  # while missing two thirds of the record, so paging is what these pin.

  Scenario: The backfill plan covers both the 118th and 119th Congress
    When I read the backfill plan
    Then the backfill plan covers congress 118 sessions 1 and 2
    And the backfill plan covers congress 119 sessions 1 and 2

  Scenario: Listing roll calls pages past the source's 250-row limit
    Given the source lists 724 roll calls for congress 118 session 1 in pages of 250
    When I list every roll call number for congress 118 session 1
    Then 724 roll call numbers are returned
    And the numbers are unique

  Scenario: The source total is read from the list endpoint's pagination count
    Given the source reports a pagination count of 724 for congress 118 session 1
    When I read the source total for congress 118 session 1
    Then the source total is 724

  Scenario: A completed backfill reconciles stored rows against the source total
    Given the source reports a pagination count of 3 for congress 119 session 2
    And 3 roll calls are stored for congress 119 session 2
    When I reconcile congress 119 session 2
    Then the reconciliation matches
    And the reconciliation reports 3 stored and 3 expected

  Scenario: A short backfill is reported as a mismatch rather than a success
    Given the source reports a pagination count of 3 for congress 119 session 2
    And 2 roll calls are stored for congress 119 session 2
    When I reconcile congress 119 session 2
    Then the reconciliation does not match
    And the reconciliation reports 2 stored and 3 expected

  # --- live-shape regressions found during 5.8's live run ---------------------
  #
  # Story 5.1 built its fixture from the endpoint's DOCUMENTATION, which does
  # not match what the endpoint actually returns. Confirmed live 2026-10-19
  # across 12 real records: `votePartyTotal` is a bare list (not
  # `{"item": [...]}`) and no item carries a `url` field at all. Both defects
  # are silent — the first zeroes every tally, the second skips every record —
  # so a backfill could not have worked without these.

  Scenario: A live-shaped party total list is summed rather than zeroed
    When I transform the live-shaped sample roll call
    Then the transformed roll call has tally yea 208 nay 187 present 0 not_voting 37

  Scenario: A live record with no url field still ingests under a derived source_url
    When I transform the live-shaped sample roll call
    Then the transformed roll call has source_url "https://api.congress.gov/v3/house-vote/119/2/74"

  Scenario: A live-shaped record is not skipped
    When I transform the live-shaped batch
    Then no roll call is skipped
