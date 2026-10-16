Feature: ZeroDB client resilience and error mapping
  As a backend developer
  I want transient failures retried and ZeroDB errors mapped to domain exceptions
  So that callers never see raw HTTP or httpx exceptions and a blip is not an outage

  Background:
    Given a ZeroDB client backed by an in-memory fake

  # --- Retries with backoff: only on transient failures ---

  Scenario: The client retries a 503 and then succeeds
    Given the next 2 requests fail with status 503
    When I create the table "jurisdictions" with fields "name,active"
    Then the call succeeds
    And the fake saw 3 request attempts

  Scenario: The client retries a connection error and then succeeds
    Given the next 1 requests fail with a connection error
    When I create the table "jurisdictions" with fields "name,active"
    Then the call succeeds
    And the fake saw 2 request attempts

  Scenario: The client retries a timeout and then succeeds
    Given the next 1 requests fail with a timeout
    When I create the table "jurisdictions" with fields "name,active"
    Then the call succeeds
    And the fake saw 2 request attempts

  Scenario: The client gives up after the retry budget is exhausted
    Given every request fails with status 503
    When I create the table "jurisdictions" with fields "name,active"
    Then the call raises "ZeroDBServerError"
    And the fake saw 4 request attempts

  Scenario: Backoff grows exponentially between attempts
    Given every request fails with status 503
    When I create the table "jurisdictions" with fields "name,active"
    Then the call raises "ZeroDBServerError"
    And the recorded sleeps grow exponentially

  Scenario Outline: A client error is never retried
    Given every request fails with status <status>
    When I create the table "jurisdictions" with fields "name,active"
    Then the call raises "<exception>"
    And the fake saw 1 request attempts

    Examples:
      | status | exception              |
      | 400    | ZeroDBValidationError  |
      | 401    | ZeroDBAuthError        |
      | 403    | ZeroDBAuthError        |
      | 404    | ZeroDBNotFoundError    |
      | 409    | ZeroDBConflictError    |
      | 422    | ZeroDBValidationError  |

  # --- Error mapping: domain exceptions, never raw httpx ---

  Scenario: A 404 becomes a not-found domain error carrying the ZeroDB detail
    Given every request fails with status 404
    When I create the table "jurisdictions" with fields "name,active"
    Then the call raises "ZeroDBNotFoundError"
    And the raised error message mentions the ZeroDB detail

  Scenario: A rate limit is mapped to its own error and is retried
    Given the next 1 requests fail with status 429
    When I create the table "jurisdictions" with fields "name,active"
    Then the call succeeds
    And the fake saw 2 request attempts

  Scenario: A persistent rate limit raises the rate-limit error
    Given every request fails with status 429
    When I create the table "jurisdictions" with fields "name,active"
    Then the call raises "ZeroDBRateLimitError"

  Scenario: A connection error that never clears becomes a connection domain error
    Given every request fails with a connection error
    When I create the table "jurisdictions" with fields "name,active"
    Then the call raises "ZeroDBConnectionError"

  Scenario: A malformed response body becomes a protocol error
    Given every request returns a body that is not JSON
    When I create the table "jurisdictions" with fields "name,active"
    Then the call raises "ZeroDBProtocolError"

  Scenario: Every domain error descends from one base error
    Then every ZeroDB domain error descends from "ZeroDBError"

  # --- Transport boundary: swappable to MCP later (tracked deviation) ---

  Scenario: Callers never see a raw httpx exception
    Given every request fails with a connection error
    When I create the table "jurisdictions" with fields "name,active"
    Then the raised error is not an httpx exception

  Scenario: The client sends its credentials as a bearer token
    When I create the table "jurisdictions" with fields "name,active"
    Then the request carried an Authorization bearer header
    And no request body or header is written to the log

  Scenario: The transport is injectable so MCP can replace REST without touching callers
    Then the client accepts any transport implementing the ZeroDB transport protocol
