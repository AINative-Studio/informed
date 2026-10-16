Feature: API health check
  As a developer
  I want the API to report its own health
  So that CI and the deploy pipeline can prove the service starts and serves requests

  Scenario: The health endpoint reports the service is up
    Given the Informed API is running
    When I request the health endpoint
    Then the response status is 200
    And the response says the service is healthy

  Scenario: An unknown path is not found
    Given the Informed API is running
    When I request an unknown path
    Then the response status is 404
