# Story 14.7 — Privacy-safe analytics
#
# "As the team, we want usage analytics only above k-anonymity thresholds, so
# that no individual can be singled out."
#
# Acceptance: Aggregates require k >= 50; no third-party trackers.
#
# Events are recorded through datamodel.md's existing `events` primitive
# (zerodb_create_event, story 1.2's ZeroDBClient.create_event) — coarse,
# non-identifying usage facts only: an event type, a timestamp, and an
# optional coarse bucket (e.g. a two-letter state code). No user_id, no IP,
# no address, nothing that could re-identify an individual from a small
# bucket.
#
# k-anonymity: an aggregate for a (event_type, bucket) slice is answered with
# the real count only when that slice has at least 50 recorded events.
# Fewer than 50 returns "insufficient data", never the real small number —
# a small real count is itself re-identifying information.

Feature: Privacy-safe usage analytics
  Usage events carry no identity. Aggregate counts are only ever revealed
  above a k-anonymity threshold of 50 events for the slice being asked
  about; below that, the answer is "insufficient data", not a small real
  number.

  Background:
    Given an analytics event store with no events recorded yet

  Scenario: Recording a usage event carries no identifying fields
    When the app records a "ballot_viewed" usage event in bucket "WA"
    Then the recorded event has no user_id, no ip address and no address field
    And the recorded event has only an event type, a timestamp and the coarse bucket

  Scenario: An aggregate below k=50 returns insufficient data, not the real count
    Given 12 "onboarding_completed" events have been recorded in bucket "WA"
    When the team requests the aggregate count for "onboarding_completed" in bucket "WA"
    Then the response says data is insufficient
    And the response never reveals the number 12

  Scenario: An aggregate at exactly k=50 reveals the real count
    Given 50 "onboarding_completed" events have been recorded in bucket "OR"
    When the team requests the aggregate count for "onboarding_completed" in bucket "OR"
    Then the response reveals a real count of 50

  Scenario: An aggregate just below k=50 still withholds the count
    Given 49 "onboarding_completed" events have been recorded in bucket "NV"
    When the team requests the aggregate count for "onboarding_completed" in bucket "NV"
    Then the response says data is insufficient

  Scenario: No third-party tracker dependency exists in the frontend
    When the frontend dependency manifest is checked against the disallowed analytics list
    Then no disallowed analytics package is present
    And no script tag pointing at a known third-party analytics domain exists in the frontend source
