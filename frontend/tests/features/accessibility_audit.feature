Feature: Accessibility, meeting WCAG 2.2 AA
  As a voter using assistive technology
  I want the app to meet WCAG 2.2 AA
  So that I can use it fully

  Story 7.8. Two acceptance criteria: automated axe checks are clean, and a
  manual screen-reader pass covers the main journey.

  This feature covers the automated half for real, against the actual
  rendered app (not a shallow unit render): axe-core's WCAG 2.0/2.1 A and AA
  rule sets, run via @axe-core/playwright against the same browser/page every
  other BDD feature here already drives, on each major view this epic built —
  the ballot list, the candidate detail page, the compare view, and the
  evidence drawer open. A clean scan here means zero violations with no rule
  disabled to force the count to zero; any violation this suite finds is
  fixed in the component, not suppressed in the scan.

  It also covers what this sandboxed environment CAN verify of the
  screen-reader-facing half without an actual screen reader driving it:
  Playwright's own accessibility-tree snapshot, to catch an interactive
  control with no real accessible name and a reading order that does not
  match the visual order. That is not the same thing as a human running
  VoiceOver or NVDA through the main journey — see this story's PR for the
  honest statement of what still needs a real human pass before this ships.

  Background:
    Given a voter opens the Informed app with a scored candidate whose citation has an archived bill

  Scenario: The ballot list has no automated accessibility violations
    Then the ballot list has no automated accessibility violations

  Scenario: The candidate detail page has no automated accessibility violations
    When they open that candidate's detail page
    Then the candidate detail page has no automated accessibility violations

  Scenario: The evidence drawer has no automated accessibility violations while open
    When they open that candidate's detail page
    And they open the evidence drawer for that citation
    Then the open page has no automated accessibility violations

  Scenario: The compare view has no automated accessibility violations
    Given a voter opens the Informed app with a ballot that has four scored candidates in one race
    And they submit their address for the whole-ballot view
    When they select two candidates in that race to compare
    And they open the compare view
    Then the compare view has no automated accessibility violations

  Scenario: The quick onboarding flow has no automated accessibility violations
    Given a voter opens the Informed app
    When they switch to the quick-setup screen
    Then the onboarding screen has no automated accessibility violations

  Scenario: Every interactive control on the ballot list has a real accessible name
    Then every interactive control on the page has a real accessible name

  Scenario: The evidence drawer's reading order matches its visual order
    When they open that candidate's detail page
    And they open the evidence drawer for that citation
    Then the drawer's accessibility-tree reading order matches its visual order
