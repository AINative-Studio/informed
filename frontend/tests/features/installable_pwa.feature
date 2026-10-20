Feature: Installable PWA with encrypted offline profile (story 7.7)
  As a mobile voter
  I want to install the app and view my profile offline
  So that I can use it at the polling place

  Two acceptance criteria. This file covers the one a browser-driven scenario
  can exercise: a voter's saved profile keeps showing once the device goes
  offline, read from a client-side cache that is encrypted at rest in this
  browser rather than left in the clear. The other half — Lighthouse's PWA
  checks passing against the built app, and the manifest/service worker that
  makes installation possible at all — has no Gherkin scenario of its own
  because there is no voter-observable "install" action to script in a
  Chromium page the OS never offers an install prompt to; that half is
  verified directly (manifest contents, service-worker registration, and a
  real Lighthouse PWA-category run against the built app) and the result is
  reported in this story's PR rather than asserted here as a scenario that
  would only be able to lie about what it checked.

  Background:
    Given a voter opens the Informed app
    And they switch to the full profile editor
    And they assign "Prescription drug pricing" to primary with a stance
    And they give consent and save

  Scenario: A freshly saved profile is cached offline, encrypted
    Then the profile is cached for offline use, and the raw cache entry contains no readable topic or stance text

  Scenario: Losing the network still shows the last-saved profile
    Given the browser goes offline
    When they switch to the full profile editor
    Then "Prescription drug pricing" already shows as primary
    And the editor shows that it is displaying a cached offline copy

  Scenario: Regaining the network returns to the live profile load
    Given the browser goes offline
    And they switch to the full profile editor
    When the browser comes back online
    And they switch to the full profile editor
    Then the editor no longer shows the offline-copy notice
