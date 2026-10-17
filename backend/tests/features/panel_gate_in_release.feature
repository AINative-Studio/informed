Feature: Panel gate in release (story 12.5)
  As the advisory panel
  I want unapproved taxonomy or stance changes blocked from release
  So that neutrality reviews can't be skipped

  # prd.md, "Neutrality": "Stance cards and issue taxonomy reviewed by a
  # cross-partisan advisory panel before each release; wording changes are
  # versioned and public." Story 3.3 built the ledger and the gated READ path
  # that withholds unapproved wording from a voter. This story builds the
  # release-time CHECK: a structural guarantee that CI fails when any current
  # issue_topics/stances row has no corresponding approved-or-grandfathered
  # decision in panel_reviews for the exact version it is at.
  #
  # "The CI check fails on unapproved wording versions" (backlog.md's
  # acceptance criterion) is exercised here against the in-memory fake, which
  # is what makes this check genuinely CI-runnable today with no ZeroDB
  # credentials — see app.panel_review.gate's module docstring for why
  # grandfathered content passes this gate (tolerated, not endorsed) while a
  # row with no ledger entry at all fails it.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the issue taxonomy has been seeded
    And the stance cards have been seeded

  Scenario: The gate passes once the whole corpus is grandfathered
    Given the existing corpus has been grandfathered
    When I run the panel gate check
    Then the panel gate check passes
    And the panel gate report counts every topic and stance as grandfathered

  Scenario: The gate fails on a brand new stance with no panel review at all
    Given the existing corpus has been grandfathered
    And a new stance "en-US:prescription-drug-pricing:v1:panel-gate-sabotage" has been written but never reviewed
    When I run the panel gate check
    Then the panel gate check fails
    And the panel gate report flags "en-US:prescription-drug-pricing:v1:panel-gate-sabotage" as never reviewed

  Scenario: The gate fails on a brand new topic with no panel review at all
    Given the existing corpus has been grandfathered
    And a new issue topic "en-US:panel-gate-sabotage-topic:v1" has been written but never reviewed
    When I run the panel gate check
    Then the panel gate check fails
    And the panel gate report flags "en-US:panel-gate-sabotage-topic:v1" as never reviewed

  Scenario: The gate fails on a stance a reviewer rejected and nobody rewrote
    Given the existing corpus has been grandfathered
    And reviewer "panelist:synthetic-a" rejected the stance "en-US:prescription-drug-pricing:v1:let-the-market-set-prices" at wording version 1 because "frames competition as the neutral baseline"
    When I run the panel gate check
    Then the panel gate check fails
    And the panel gate report flags "en-US:prescription-drug-pricing:v1:let-the-market-set-prices" as rejected

  Scenario: An approved edit that bumps the version passes the gate at its new version
    Given the existing corpus has been grandfathered
    And reviewer "panelist:synthetic-a" proposed the label "Cap monthly out-of-pocket drug costs" for the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    And reviewer "panelist:synthetic-b" approves that proposal
    When I run the panel gate check
    Then the panel gate check passes

  Scenario: A row with no ledger entry fails the gate on a completely empty ledger
    Given a brand new stance "en-US:prescription-drug-pricing:v1:panel-gate-empty-ledger" exists and the ledger is otherwise empty for it
    When I run the panel gate check
    Then the panel gate check fails
    And the panel gate report flags "en-US:prescription-drug-pricing:v1:panel-gate-empty-ledger" as never reviewed
