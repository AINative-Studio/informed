Feature: Panel review workflow (story 3.3)
  As an advisory panel member
  I want to review and approve stance wording before release
  So that the wording stays neutral

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the issue taxonomy has been seeded
    And the stance cards have been seeded

  # --- the review ledger ----------------------------------------------------
  # Review state is a row in its own append-only `panel_reviews` table, never a
  # field on the reviewed row. datamodel.md fixes `issue_topics` and `stances`
  # at their documented fields, their seeders restate every row on each run
  # (so a status field would be wiped), and a decision needs to be history
  # rather than a flag that the next edit overwrites.

  Scenario: A review decision is recorded against the row it judges
    When reviewer "panelist:synthetic-a" approves the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs" at wording version 1
    Then the review ledger holds 1 decision for that stance
    And that stance's review status is "approved"
    And the decision records the reviewer and a timestamp

  Scenario: A rejection is recorded with its reason and leaves the row unapproved
    When reviewer "panelist:synthetic-a" rejects the stance "en-US:prescription-drug-pricing:v1:let-the-market-set-prices" at wording version 1 because "frames competition as the neutral baseline"
    Then that stance's review status is "rejected"
    And the rejection reason is visible in the review history

  Scenario: Review decisions are append-only, so a later decision does not erase the first
    Given reviewer "panelist:synthetic-a" rejected the stance "en-US:health-insurance-coverage:v1:public-option-alongside-private-plans" at wording version 1
    When reviewer "panelist:synthetic-b" approves the stance "en-US:health-insurance-coverage:v1:public-option-alongside-private-plans" at wording version 1
    Then the review ledger holds 2 decisions for that stance
    And that stance's review status is "approved"
    And the earlier rejection is still in the review history

  Scenario: A review of a row that does not exist is refused
    When reviewer "panelist:synthetic-a" approves the stance "en-US:prescription-drug-pricing:v1:no-such-card" at wording version 1
    Then the review write is refused for a missing stance

  Scenario: A review naming a wording version the row is not at is refused
    When reviewer "panelist:synthetic-a" approves the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs" at wording version 7
    Then the review is refused as judging wording the row does not carry

  Scenario: A reviewer cannot approve their own wording proposal
    Given reviewer "panelist:synthetic-a" proposed the label "Cap monthly out-of-pocket drug costs" for the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    When reviewer "panelist:synthetic-a" approves their own proposal
    Then the approval is refused as self-review

  # --- unapproved wording cannot be published -------------------------------
  # "Published" means served by the app's read paths. The gate is opt-in per
  # call so the existing corpus keeps serving (see the grandfathering
  # scenarios below) while any NEW or EDITED card must earn its approval.

  Scenario: A newly written stance is not served while it is unreviewed
    Given a new stance "en-US:prescription-drug-pricing:v1:index-prices-to-inflation" has been written but never reviewed
    When I read the approved stances for "en-US:prescription-drug-pricing:v1"
    Then the new stance is not among them
    And the previously approved stances are still among them

  Scenario: Approving a new stance publishes it
    Given a new stance "en-US:prescription-drug-pricing:v1:index-prices-to-inflation" has been written but never reviewed
    And reviewer "panelist:synthetic-a" approved that new stance
    When I read the approved stances for "en-US:prescription-drug-pricing:v1"
    Then the new stance is among them

  Scenario: A rejected stance is not served
    Given a new stance "en-US:prescription-drug-pricing:v1:index-prices-to-inflation" has been written but never reviewed
    And reviewer "panelist:synthetic-a" rejected that new stance
    When I read the approved stances for "en-US:prescription-drug-pricing:v1"
    Then the new stance is not among them

  Scenario: A newly written topic is not served while it is unreviewed
    Given a new topic "en-US:synthetic-review-topic:v1" has been written but never reviewed
    When I read the approved topic tree
    Then the new topic is not among its topics
    And the grandfathered topics are still among its topics

  Scenario: The ungated read path still serves everything, approved or not
    Given a new stance "en-US:prescription-drug-pricing:v1:index-prices-to-inflation" has been written but never reviewed
    When I read the stances for "en-US:prescription-drug-pricing:v1"
    Then the new stance is among them

  # --- the existing corpus is grandfathered, honestly -----------------------
  # Stories 3.1 and 3.2 seeded 80 topics and 201 stances into the live project
  # before this workflow existed. Marking them "approved" would claim a panel
  # review that never happened; blocking them would take the whole app down.
  # They are marked `grandfathered` — explicitly NOT approved, explicitly still
  # served, and explicitly still owed a real review.

  Scenario: Grandfathering the existing corpus records a status that is not approval
    When the existing corpus is grandfathered by "informed:story-3.3-backfill"
    Then every seeded stance's review status is "grandfathered"
    And every seeded topic's review status is "grandfathered"
    And no grandfathered row is recorded as approved
    And every grandfathered decision names story 3.3's backfill rather than a panel member

  Scenario: Grandfathered rows keep serving, so the app does not go dark
    When the existing corpus is grandfathered by "informed:story-3.3-backfill"
    And I read the approved stances for "en-US:prescription-drug-pricing:v1"
    Then every seeded stance for that subtopic is among them

  Scenario: Grandfathered rows are reported as still owing a panel review
    When the existing corpus is grandfathered by "informed:story-3.3-backfill"
    Then the review queue reports every grandfathered row as awaiting a real review
    And the backfill reports how many rows it marked

  Scenario: Grandfathering twice leaves one decision per row
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    When the existing corpus is grandfathered by "informed:story-3.3-backfill" again
    Then exactly 1 grandfathering decision is stored per seeded row

  Scenario: A real panel approval supersedes a row's grandfathered status
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    When reviewer "panelist:synthetic-a" approves the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs" at wording version 1
    Then that stance's review status is "approved"
    And the review queue no longer reports that stance as awaiting a real review

  # --- every edit bumps wording_version -------------------------------------
  # Editing is a two-step proposal, not a write: the proposal holds the new
  # wording while the live row keeps the approved wording, so nothing
  # unreviewed is ever served.

  Scenario: Proposing a wording change does not alter the live row
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    When reviewer "panelist:synthetic-a" proposes the label "Cap monthly out-of-pocket drug costs" for the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    Then the stored stance still carries its original label
    And the stored stance is still at wording version 1
    And the proposal is awaiting review

  Scenario: Approving a proposal applies the new wording and bumps the wording version
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    And reviewer "panelist:synthetic-a" proposed the label "Cap monthly out-of-pocket drug costs" for the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    When reviewer "panelist:synthetic-b" approves that proposal
    Then the stored stance carries the proposed label
    And the stored stance is at wording version 2
    And that stance's review status is "approved"
    And the approved stance is served by the gated read path

  Scenario: The stance id does not change when its wording version is bumped
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    And reviewer "panelist:synthetic-a" proposed the label "Cap monthly out-of-pocket drug costs" for the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    When reviewer "panelist:synthetic-b" approves that proposal
    Then the stance keeps the id "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    And exactly 1 row carries that stance id

  Scenario: Rejecting a proposal leaves the live wording and version untouched
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    And reviewer "panelist:synthetic-a" proposed the label "Cap monthly out-of-pocket drug costs" for the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    When reviewer "panelist:synthetic-b" rejects that proposal because "loses the insulin example voters recognize"
    Then the stored stance still carries its original label
    And the stored stance is still at wording version 1
    And the rejected proposal is visible in the review history

  Scenario: Two successive approved edits bump the version twice
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    When two wording changes to "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs" are proposed and approved in turn
    Then the stored stance is at wording version 3
    And the review history for that stance lists both approved edits in order

  Scenario: A proposal that changes nothing is refused
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    When reviewer "panelist:synthetic-a" proposes the stance's existing label unchanged
    Then the proposal is refused as changing nothing

  Scenario: A proposed stance label naming a political side is refused before review
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    When reviewer "panelist:synthetic-a" proposes the label "End the Democrats' price controls" for the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    Then the proposal is refused as naming a political side

  Scenario: A proposed topic name presupposing a stance is refused before review
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    When reviewer "panelist:synthetic-a" proposes the name "Commonsense gun safety reform" for the topic "en-US:firearms-policy:v1"
    Then the proposal is refused as presupposing a stance

  Scenario: Approving a topic wording change bumps the taxonomy version on that row
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    And reviewer "panelist:synthetic-a" proposed the name "Prescription drug prices" for the topic "en-US:prescription-drug-pricing:v1"
    When reviewer "panelist:synthetic-b" approves that proposal
    Then the stored topic carries the proposed name
    And the stored topic keeps the id "en-US:prescription-drug-pricing:v1"

  # --- the review history is visible ----------------------------------------

  Scenario: The review history for a stance reads back in order, newest first
    Given reviewer "panelist:synthetic-a" rejected the stance "en-US:health-insurance-coverage:v1:public-option-alongside-private-plans" at wording version 1
    And reviewer "panelist:synthetic-b" approved the stance "en-US:health-insurance-coverage:v1:public-option-alongside-private-plans" at wording version 1
    When I read the review history for "en-US:health-insurance-coverage:v1:public-option-alongside-private-plans"
    Then the history lists 2 decisions newest first
    And every decision names its reviewer, decision, wording version and timestamp

  Scenario: The review history of an unreviewed row is empty rather than an error
    When I read the review history for "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    Then the history is empty

  Scenario: An approved wording change shows what changed
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    And reviewer "panelist:synthetic-a" proposed the label "Cap monthly out-of-pocket drug costs" for the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    When reviewer "panelist:synthetic-b" approves that proposal
    And I read the review history for "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    Then the history shows the label before and after the change

  Scenario: The review history is served over HTTP
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    And reviewer "panelist:synthetic-a" rejected the stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs" at wording version 1
    When the API is asked for that stance's review history
    Then the response lists the rejection with its reviewer and reason

  Scenario: The review queue is served over HTTP
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    When the API is asked for the review queue
    Then the response reports every grandfathered row as awaiting a real review

  Scenario: A wording change is proposed and approved over HTTP
    Given the existing corpus has been grandfathered by "informed:story-3.3-backfill"
    When the API is used to propose and then approve a new label for "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs"
    Then the API reports the stance at wording version 2 with the new label

  Scenario: The API refuses a review that names no reviewer
    When the API is asked to approve a stance with no reviewer id
    Then the API refuses it as a bad request
