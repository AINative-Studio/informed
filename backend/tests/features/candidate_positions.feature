Feature: Candidate positions from votes (story 6.2)
  As the system
  I want each incumbent's position (-1 to +1) per stance computed from their votes,
  weighted by vote type
  So that alignment has a basis

  # backlog.md 6.2: "Final passage outweighs procedural and bundled votes;
  # results are append-only with `is_current`; recomputed on
  # `rollcall.ingested`."
  #
  # WHAT THIS STORY COMPUTES FROM, AND WHY (the scoping decision)
  # =============================================================
  # The full pipeline prd.md describes routes a vote to a stance through a
  # bill-to-stance classifier (story 5.5) fed by `bills.subjects[]` (story
  # 5.3), and through promise-to-vote mapping (story 10.1). None of the three
  # is built. So this story computes from the association datamodel.md already
  # gives it: `member_votes.topic_ids[]` (denormalised from the roll call) and
  # `stances.topic_id`. A vote is evidence about a stance when the vote's
  # topic_ids contain that stance's topic_id.
  #
  # That is a FIRST-PASS association, deliberately narrower than the finished
  # product, and the engine is written so that replacing it changes one
  # function (`app.scoring.positions.votes_for_stance`) and nothing else. It
  # is also honest about today's data: story 5.1's ingest writes
  # `topic_ids: []` on every roll call it loads, because the bill subjects
  # that would populate it do not exist yet. So on live data this engine
  # currently finds zero votes per stance and writes no positions -- which is
  # the correct answer, not a bug, and the scenario "A stance with no matching
  # votes yields no position" pins it.
  #
  # HOW A VOTE BECOMES A SIGNED POSITION
  # ====================================
  # `stances.direction_code` is a signed position on the subtopic's own axis
  # (-2..+2, see `app.seed_stances`). A Yea on a measure that moves the axis
  # in the stance's direction is +1 for that stance; a Nay is -1. Because this
  # story has no classifier telling it which way a given bill moves the axis,
  # it reads that from the roll call's own `question` only to the extent the
  # vote's position says: see `app.scoring.positions` for the full derivation
  # and the explicit statement of what it does NOT claim to know yet.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And a published methodology with real vote type weights
    And a synthetic incumbent with a stance to be scored

  # --- the vote-type weighting, which is 6.2's first acceptance criterion ---

  Scenario: A final-passage vote outweighs a procedural vote on the same stance
    Given the incumbent voted for the stance on a final-passage roll call
    And the incumbent voted against the stance on a procedural roll call
    When positions are computed for the incumbent
    Then the stance position is positive
    And the stance position is closer to +1 than to 0

  Scenario: A procedural vote alone yields a weaker position than final passage alone
    Given the incumbent voted for the stance on a procedural roll call
    When positions are computed for the incumbent
    Then the stance confidence is lower than it would be for a final-passage vote

  Scenario: A bundled vote counts for less than an unbundled one
    Given the incumbent voted for the stance on a bundled final-passage roll call
    When positions are computed for the incumbent
    Then the stance confidence is lower than it would be for an unbundled final-passage vote

  Scenario: Consistent final-passage support reaches the top of the scale
    Given the incumbent voted for the stance on 5 final-passage roll calls
    When positions are computed for the incumbent
    Then the stance position is +1.0

  Scenario: Consistent final-passage opposition reaches the bottom of the scale
    Given the incumbent voted against the stance on 5 final-passage roll calls
    When positions are computed for the incumbent
    Then the stance position is -1.0

  Scenario: A split record lands between the extremes
    Given the incumbent voted for the stance on 2 final-passage roll calls
    And the incumbent voted against the stance on 2 final-passage roll calls
    When positions are computed for the incumbent
    Then the stance position is 0.0

  # --- votes that are not evidence of a position ----------------------------
  # prd.md: "Only recorded (roll-call) votes are scored. Voice votes and
  # unanimous consent are shown as 'no recorded vote', never as support."
  # `not_voting` and `present` are recorded non-positions: the member was
  # counted but took no side.

  Scenario: A not-voting position is not evidence either way
    Given the incumbent was recorded not voting on a final-passage roll call
    When positions are computed for the incumbent
    Then no position is written for the stance

  Scenario: A present position is not evidence either way
    Given the incumbent was recorded present on a final-passage roll call
    When positions are computed for the incumbent
    Then no position is written for the stance

  Scenario: A stance with no matching votes yields no position
    Given the incumbent has no votes on this stance's topic
    When positions are computed for the incumbent
    Then no position is written for the stance
    And nothing was written to candidate_positions

  # --- append-only with is_current, 6.2's second acceptance criterion -------
  # datamodel.md: "Verdicts, positions and the provenance log are append-only.
  # Mark the live row with `is_current`; never update history in place."

  Scenario: A recompute appends a new version rather than editing the old one
    Given the incumbent voted for the stance on a final-passage roll call
    And positions have been computed once
    When the incumbent votes against the stance on another final-passage roll call
    And positions are computed for the incumbent
    Then candidate_positions holds 2 versions for that person and stance
    And exactly one of them is marked current
    And the current version is the newer one

  Scenario: The superseded position keeps its own value
    Given the incumbent voted for the stance on a final-passage roll call
    And positions have been computed once
    When the incumbent votes against the stance on another final-passage roll call
    And positions are computed for the incumbent
    Then the superseded version still reports the value it was computed with

  Scenario: Recomputing with no new evidence does not append a duplicate version
    Given the incumbent voted for the stance on a final-passage roll call
    And positions have been computed once
    When positions are computed for the incumbent
    Then candidate_positions holds 1 version for that person and stance

  Scenario: Every written position cites the methodology it was computed under
    Given the incumbent voted for the stance on a final-passage roll call
    When positions are computed for the incumbent
    Then the stored position names the current methodology version

  Scenario: Every written position records how many votes it rests on
    Given the incumbent voted for the stance on 3 final-passage roll calls
    When positions are computed for the incumbent
    Then the stored position reports an evidence count of 3

  Scenario: A position computed from votes says so in its basis
    Given the incumbent voted for the stance on a final-passage roll call
    When positions are computed for the incumbent
    Then the stored position's basis names votes

  # --- recomputed on rollcall.ingested, 6.2's third acceptance criterion ----
  # datamodel.md's Events table: `rollcall.ingested`, payload
  # "roll_call_id, bill_id, topic_ids", consumers "Position recompute; mapping
  # agent; follower alerts".
  #
  # HONEST GAP, stated here rather than in a comment nobody reads: nothing in
  # this codebase emits `rollcall.ingested` yet -- story 5.1's loader does not,
  # and the live project holds none. Story 6.1 found the mirror-image gap for
  # `methodology.published` (it emitted the event with no consumer); this story
  # builds the consumer. So 6.2 ships BOTH halves of the wiring and the
  # recompute runs end-to-end over a fake-emitted event, which is exactly what
  # these scenarios exercise.

  Scenario: A rollcall.ingested event recomputes the affected person's positions
    Given the incumbent voted for the stance on a final-passage roll call
    And a "rollcall.ingested" event was emitted for that roll call
    When the position recompute consumer runs
    Then the stance position is +1.0
    And the recompute reports 1 person recomputed

  Scenario: Recomputing announces positions.recomputed
    Given the incumbent voted for the stance on a final-passage roll call
    And a "rollcall.ingested" event was emitted for that roll call
    When the position recompute consumer runs
    Then exactly one "positions.recomputed" event has been emitted
    And the event names the person and the methodology version

  Scenario: An event for a roll call nobody voted on recomputes nobody
    Given a "rollcall.ingested" event was emitted for a roll call with no votes
    When the position recompute consumer runs
    Then the recompute reports 0 people recomputed
    And nothing was written to candidate_positions

  Scenario: The same event is not processed twice
    Given the incumbent voted for the stance on a final-passage roll call
    And a "rollcall.ingested" event was emitted for that roll call
    When the position recompute consumer runs
    And the position recompute consumer runs again
    Then candidate_positions holds 1 version for that person and stance

  # --- neutrality, which is non-negotiable ---------------------------------
  # prd.md: "One scoring codebase for all candidates; no party-specific rules."

  Scenario: Two incumbents with mirrored records get mirrored positions
    Given a second synthetic incumbent whose record mirrors the first
    When positions are computed for both incumbents
    Then their stance positions are exact opposites
    And their stance confidences are equal
