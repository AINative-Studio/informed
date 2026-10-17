# Story 16.5 — Monitoring and tracing
#
# "As an operator, I want logs, metrics and traces across API, agents and
# ZeroDB calls, so that I can find problems fast."
#
# Acceptance: Dashboards for ingest lag, error rate and p95 latency; alerts on
# ingest lag over 6 hours.
#
# This codebase has no deployed server and no staging environment (see story
# 1.5/1.6's findings, and docs/environments.md's "deployment targets: none
# provisioned yet"). So this story's scope is the honest subset of the
# acceptance criterion that application code alone can deliver:
#
#   * request-level metrics (latency, status code) exposed in a format a real
#     dashboard tool could scrape, in Prometheus text exposition format;
#   * an "ingest lag" gauge per scheduled job, computed from the same
#     `scheduled_jobs` bookkeeping table `app.scheduling.registry` already
#     maintains — the metric an alert on "ingest lag over 6 hours" would fire
#     on, not a paging pipeline to a service that does not exist;
#   * a repo-wide, enforced guarantee that no log line anywhere leaks
#     plaintext voter data, free-text input, or key/secret material — the
#     single most important thing an operator-facing logging story can get
#     wrong for this app (prd.md: "Privacy first").
#
# docs/observability.md names the dashboard panels an operator should build
# once real infra exists; building that infra is out of scope here, the same
# honest gap every other Epic 16 story already documents for its own
# paging/ticketing/deployment dependency.

Feature: Monitoring and tracing
  The API exposes request metrics and per-job ingest lag in a format an
  external monitoring tool can scrape, and no log line anywhere leaks
  plaintext voter data or secret material.

  Scenario: The metrics endpoint exposes request latency and status counts
    Given a running instance of the API
    When a request is made to "GET /health"
    And a second request is made to "GET /metrics"
    Then the metrics response is valid Prometheus text exposition format
    And it contains a counter for http requests by method, route and status
    And it contains a histogram for http request duration in seconds

  Scenario: The metrics endpoint itself is never rate limited
    Given a running instance of the API
    Then "GET /metrics" is not subject to any rate-limit policy

  Scenario: Ingest lag is computed per job from the last successful run
    Given a scheduled job "load_house_rollcalls" last succeeded 2 hours ago
    When the ingest lag is computed for every registered job
    Then "load_house_rollcalls" reports a lag of approximately 2 hours
    And a job that has never run reports no last-success timestamp

  Scenario: Ingest lag over six hours is the threshold an alert would fire on
    Given a scheduled job "load_house_rollcalls" last succeeded 7 hours ago
    When the ingest lag is computed for every registered job
    Then "load_house_rollcalls" is flagged as over the 6 hour ingest-lag alert threshold

  Scenario: No plaintext voter data appears in any log line across a full request cycle
    Given a synthetic voter profile with a primary interest "test-topic-alpha"
    When the voter saves their profile over HTTP while logs are captured at every level
    Then no captured log line contains any interest topic id
    And no captured log line contains any stance id
    And no captured log line contains any key material
    And no captured log line contains the raw request body

  Scenario: The repo-wide plaintext-in-logs guard rejects a line that would have leaked
    Given a log record carrying a synthetic voter interest topic id in its message
    When the plaintext-in-logs guard inspects that record
    Then the guard reports it as a violation
