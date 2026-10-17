Feature: Methodology versions (story 6.1)
  As a methodology owner
  I want weights for tiers, evidence blend and vote types versioned
  So that every score is reproducible

  # backlog.md 6.1: "Exactly one current version exists; publishing a version
  # emits `methodology.published`."
  #
  # What already existed before this story, and is NOT re-litigated here:
  #   * story 4.2 (`app.profiles.methodology`) owns the stored row shape and
  #     seeds the first one, with prd.md's tier weights;
  #   * story 12.2 (`app.methodology`) publishes it — current, by id, and the
  #     whole changelog — and already pins "exactly one listed version is
  #     marked current" as a READ.
  #
  # What this story closes is the remaining gap in three parts:
  #   1. the two weight groups datamodel.md documents and the live row did not
  #      carry at all: `evidence_blend` and `vote_types`;
  #   2. "exactly one current version exists" as an INVARIANT over the stored
  #      table, not just a property of one response — so a torn write or a
  #      hand-edit is caught, the same way story 16.2's check 3 catches it for
  #      `verdicts` and `candidate_positions`;
  #   3. a publish PATH that actually emits datamodel.md's
  #      `methodology.published` event. Before this story no code anywhere
  #      created that event, and the live project had none.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the methodology_versions table exists

  # --- the weights a version must carry -------------------------------------
  # datamodel.md: `weights {tiers, evidence_blend, vote_types}`. Story 4.2
  # seeded `tiers` only and said so out loud; 12.2 published the other two as
  # `weights_not_yet_published`. This story gives them real numbers.

  Scenario: A published version carries all three documented weight groups
    When the methodology owner publishes version "2027.1"
    Then the published version carries weight groups "tiers, evidence_blend, vote_types"
    And no weight group is reported as not yet published

  Scenario: The evidence blend is prd.md's documented default
    When the methodology owner publishes version "2027.1"
    Then the evidence blend weights votes 0.7, sponsorships 0.15 and statements 0.15

  Scenario: The evidence blend is a complete split of the evidence
    When the methodology owner publishes version "2027.1"
    Then the evidence blend weights sum to 1.0

  Scenario: Final passage outweighs every other vote type
    When the methodology owner publishes version "2027.1"
    Then the vote type weights cover "final_passage, amendment, cloture, motion_to_recommit, procedural, other"
    And final passage carries weight 1.0
    And every other vote type carries less weight than final passage

  Scenario: Procedural and cloture votes are weighted lower than final passage
    When the methodology owner publishes version "2027.1"
    Then "cloture" is weighted lower than "amendment"
    And "procedural" is weighted lower than "cloture"

  Scenario: Every vote type weight is a usable multiplier
    When the methodology owner publishes version "2027.1"
    Then every vote type weight is greater than 0 and at most 1.0

  Scenario: The tier weights are unchanged by this story
    When the methodology owner publishes version "2027.1"
    Then the published tier weights are primary 3.0, secondary 1.5 and nice_to_have 0.5

  # --- publishing emits the event -------------------------------------------
  # datamodel.md's Events table: `methodology.published`, payload `version`,
  # consumers "Full recompute + symmetry audit". A recompute that never fires
  # means every stored score silently keeps citing a methodology that is no
  # longer in force, which is the exact failure this story's acceptance
  # criterion exists to prevent.

  Scenario: Publishing a version emits methodology.published
    When the methodology owner publishes version "2027.1"
    Then exactly one "methodology.published" event has been emitted
    And the event names version "2027.1"

  Scenario: The event carries nothing about any voter
    When the methodology owner publishes version "2027.1"
    Then the emitted event mentions no voter, profile or interest data

  Scenario: Publishing the first-ever version also emits the event
    Given the methodology_versions table is empty
    When the methodology owner publishes version "2026.1"
    Then exactly one "methodology.published" event has been emitted
    And the event names version "2026.1"

  Scenario: Seeding a default version is not a publication
    Given the methodology_versions table is empty
    When something reads the current methodology and the default is seeded
    Then no "methodology.published" event has been emitted

  # --- exactly one current version ------------------------------------------
  # Append-only per datamodel.md: the new row is inserted and the old row's
  # `is_current` is cleared in place of being overwritten. Never an update of
  # the old row's weights — a superseded version has to keep showing the
  # weights it actually scored with.

  Scenario: Publishing a second version leaves exactly one current
    Given the current methodology version has been published
    When the methodology owner publishes version "2027.1"
    Then exactly one stored version is marked current
    And the current version is "2027.1"

  Scenario: The superseded version keeps its own weights
    Given the current methodology version has been published
    When the methodology owner publishes version "2027.1"
    Then version "2026.1" is still stored and is not current
    And version "2026.1" still reports tier weights primary 3.0, secondary 1.5 and nice_to_have 0.5

  Scenario: Publishing three versions still leaves exactly one current
    Given the current methodology version has been published
    When the methodology owner publishes version "2027.1"
    And the methodology owner publishes version "2028.1"
    Then exactly one stored version is marked current
    And the current version is "2028.1"

  Scenario: Republishing the version already in force is refused
    Given the current methodology version has been published
    When the methodology owner tries to publish version "2026.1" again
    Then the publication is refused
    And no new "methodology.published" event has been emitted

  Scenario: A version id that does not match the documented pattern is refused
    When the methodology owner tries to publish version "v2"
    Then the publication is refused

  # --- the invariant, checked over the stored table -------------------------
  # Story 16.2's integrity check 3 pins "exactly one is_current row per key"
  # for `verdicts` and `candidate_positions`. `methodology_versions` was not
  # covered: its key is the whole table (there is one methodology in force,
  # globally), so it needs its own grouping rather than a key field.

  Scenario: The integrity check passes on a correctly published table
    Given the current methodology version has been published
    When the integrity checks run
    Then the is_current uniqueness check passes

  Scenario: The integrity check catches a second current version
    Given two methodology versions are both marked current
    When the integrity checks run
    Then the is_current uniqueness check reports a methodology_versions violation

  Scenario: The integrity check catches a table with no current version
    Given a methodology version is stored with no current version marked
    When the integrity checks run
    Then the is_current uniqueness check reports a methodology_versions violation

  Scenario: An empty methodology table is not reported as a violation
    Given the methodology_versions table is empty
    When the integrity checks run
    Then the is_current uniqueness check passes

  # --- the public page serves the new version -------------------------------
  # Story 12.2's endpoints are the only way a voter or a journalist sees any of
  # this, so a publish that the public page does not pick up has not happened.

  Scenario: The public page serves the newly published version
    Given the current methodology version has been published
    When the methodology owner publishes version "2027.1"
    And anyone requests the methodology with no authentication
    Then the response names methodology version "2027.1"
    And the response publishes all three weight groups

  Scenario: The superseded version stays citable with its own weights
    Given the current methodology version has been published
    When the methodology owner publishes version "2027.1"
    And anyone requests methodology version "2026.1"
    Then the response is 200
    And the response says this version is not current
