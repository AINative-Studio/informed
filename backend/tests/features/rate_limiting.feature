# Story 14.5 — Rate limiting and bot protection.
#
# prd.md, "Integrity and abuse": "Rate limits and bot protection on public
# endpoints to stop scraping of aggregate sentiment."
#
# backlog.md 14.5 acceptance: "Per-IP and per-account limits; challenge on
# abuse; load test shows no degradation for real users."
#
# Three of those four clauses are satisfied here. The fourth, per-account, is
# deliberately NOT faked — see the last scenario in this file and
# app/ratelimit/policy.py's docstring. There is no authentication anywhere in
# this codebase; every story so far takes a bare `user_id` from the caller, so
# an "account" limit keyed on it would be a limit any abuser lifts by
# incrementing a number. The hook is built and asserted; the key function is
# wired the day a real auth story lands.

Feature: Rate limiting and bot protection on public endpoints
  As an operator
  I want public endpoints rate-limited and protected from bots
  So that aggregate data isn't scraped

  Background:
    Given the API is running behind the rate limiter

  # --- "load test shows no degradation for real users" --------------------
  #
  # The honest reading of that criterion at this story's scope: a simulated
  # population of real users, each browsing at a human pace from its own
  # address, is never turned away and pays no measurable cost for the
  # limiter's presence. A production-scale load test against deployed
  # infrastructure is a separate, non-code concern.

  Scenario: A population of real users browsing normally is never rate-limited
    When 50 different voters each read the topics tree twice
    Then every one of those requests succeeds
    And no response carries a 429 status

  Scenario: One voter browsing at a human pace is never rate-limited
    When a voter makes 20 reads from the same address within a minute
    Then every one of those requests succeeds

  Scenario: The limiter adds no meaningful latency to a normal read
    When a voter makes 200 reads from the same address under a generous limit
    Then the limiter's own overhead stays under 1 millisecond per request

  # --- per-IP limits, and the 429 "challenge" -----------------------------

  Scenario: A scraper hammering a read endpoint from one address is cut off
    When one address requests the topics tree 61 times in a minute
    Then the last request is refused with 429
    And the refusal carries a Retry-After header in seconds
    And the refusal names no voter, profile or interest data

  Scenario: The refusal tells an honest client exactly when to come back
    When one address exhausts its read allowance
    Then the Retry-After header is at most the length of the window

  Scenario: A new window lets a cut-off client back in
    Given one address has exhausted its read allowance
    When the window passes
    Then that address can read again

  Scenario: One scraper's limit does not affect anybody else
    Given one address has exhausted its read allowance
    When a different address reads the topics tree
    Then that request succeeds

  # --- writes are stricter than reads -------------------------------------

  Scenario Outline: Write endpoints carry a stricter limit than read endpoints
    Then the limit for "<write_path>" is lower than the limit for "/v1/topics"

    Examples:
      | write_path         |
      | /v1/corrections    |
      | /v1/profiles       |
      | /v1/topics/suggest |

  Scenario: The uncredentialed correction write is limited
    When one address posts 6 corrections in an hour
    Then the last request is refused with 429

  Scenario: The uncredentialed interest-mapping write is limited
    When one address posts 31 free-text interest mappings in a minute
    Then the last request is refused with 429

  # --- the limiter must not change any endpoint's behaviour ---------------

  Scenario: Every public route is covered by a policy
    Then every public route resolves to a rate-limit policy

  Scenario: The health endpoint is never rate-limited
    When an uptime checker probes health 500 times in a minute
    Then every one of those requests succeeds

  Scenario: An endpoint under the limiter behaves exactly as it did without it
    When a voter reads the published methodology through the limiter
    Then the response is byte-for-byte what the unlimited app returns

  Scenario: A refused request never reaches the endpoint behind it
    Given one address has exhausted its correction allowance
    When that address posts another correction
    Then no correction row is written

  # --- per-account: honestly deferred, with the hook asserted -------------

  Scenario: Per-account limiting is deferred to a real authentication story
    Then the limiter exposes an account-identity hook
    And that hook identifies nobody today
    And a request carrying a bare user_id is still limited by address only
