Feature: Candidate cards read model (story 7.2)
  As the system
  I want a per-candidacy read model rebuilt from events
  So that the ballot view needs one query

  # backlog.md 7.2: "Rebuilt on `verdict.published` and `positions.recomputed`;
  # never aggregates at request time."
  # datamodel.md line 87: `candidate_cards` (read model), id `{candidacy_id}`,
  # fields person_name, party, district, photo_url,
  # verdict_counts {kept, partial, broken, ...}, say_do_gap,
  # positions_summary, updated_at. Queried "By (election, district)".
  # datamodel.md rule 4: "`candidate_cards` is rebuilt from events, so the
  # ballot view never aggregates at request time."
  #
  # THE TWO TRIGGERS, AND WHICH ONE IS REAL TODAY
  # ==============================================
  # `positions.recomputed` EXISTS and fires for real: story 6.2's
  # `PositionComputer.recompute_person` emits it whenever a position actually
  # changes, and the live project holds real events of that topic. Its
  # consumer here is live code on a live input.
  #
  # `verdict.published` DOES NOT EXIST YET. Epic 11 (the verdict engine) is
  # entirely unbuilt, nothing in this codebase emits the topic, and the live
  # project holds zero events of it (measured 2026-10-19). So the consumer
  # below is exercised against a fixture event emitted by these scenarios,
  # NOT against a real published verdict. That is stated here rather than
  # buried in a comment because the distinction decides what this story may
  # claim: the wiring is proven, the production path is not, and story 11.6
  # ("verdict.published rebuilds candidate cards within 5 minutes") is where
  # the latter becomes provable.
  #
  # WHAT A CARD MAY AND MAY NOT CLAIM
  # ==================================
  # prd.md's "evidence over opinion" decides every absent field here. A
  # rebuild triggered by `positions.recomputed` knows about positions and
  # knows NOTHING about verdicts, so it must not write a verdict count — and
  # `say_do_gap` is defined in prd.md as "alignment computed from promises vs.
  # from votes", which needs verdicts on promises. So a positions-only rebuild
  # leaves `verdict_counts` and `say_do_gap` null rather than zero: `kept: 0`
  # would read to a voter as "this candidate kept none of their promises",
  # which is a claim about a real person that no evidence supports.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And a candidacy for a synthetic incumbent

  # --- the read model itself: one query, no aggregation ---------------------

  Scenario: A rebuilt card is one row addressed by its candidacy id
    When the card for that candidacy is rebuilt
    Then candidate_cards holds 1 row
    And the card's id is the candidacy id

  Scenario: Reading a card is a single-row fetch, not an aggregation
    Given the card for that candidacy has been rebuilt
    When the ballot reads that candidate's card
    Then exactly 1 table was read
    And no row was read from candidate_positions
    And no row was read from verdicts
    And no row was read from member_votes

  Scenario: A card carries the candidate's identity from the candidacy and person rows
    When the card for that candidacy is rebuilt
    Then the card names the candidate
    And the card names their party
    And the card names their district by its readable name
    And the card carries the person's photo url

  Scenario: Cards are queried by election and district
    Given a second candidacy in the same district
    And a third candidacy in a different district
    And every card has been rebuilt
    When the ballot reads the cards for that election and district
    Then 2 cards are returned
    And the card from the other district is not among them

  Scenario: Rebuilding the same candidacy twice leaves one row
    Given the card for that candidacy has been rebuilt
    When the card for that candidacy is rebuilt
    Then candidate_cards holds 1 row

  Scenario: A rebuild for a candidacy that does not exist writes nothing
    When the card for an unknown candidacy is rebuilt
    Then candidate_cards holds 0 rows
    And the rebuild reports that the candidacy was not found

  # --- rebuilt on positions.recomputed, 7.2's first live trigger ------------
  # datamodel.md's Events table: `positions.recomputed`, payload "person_id,
  # methodology_version", consumer "`candidate_cards` rebuild".

  Scenario: A positions.recomputed event rebuilds that person's cards
    Given the incumbent has a current position on a stance
    And a "positions.recomputed" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the rebuild reports 1 card rebuilt
    And the card summarises that position

  Scenario: A positions rebuild records the methodology it summarises
    Given the incumbent has a current position on a stance
    And a "positions.recomputed" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the card names the methodology version from the event

  Scenario: A positions rebuild leaves verdict fields absent rather than zero
    Given the incumbent has a current position on a stance
    And a "positions.recomputed" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the card's verdict counts are absent
    And the card's say-do gap is absent

  Scenario: One person's recompute rebuilds every candidacy they hold
    Given the incumbent also has a candidacy in another election
    And the incumbent has a current position on a stance
    And a "positions.recomputed" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the rebuild reports 2 cards rebuilt

  Scenario: A recompute for a person with no candidacy rebuilds nothing
    Given a "positions.recomputed" event was emitted for a person with no candidacy
    When the card rebuild consumer runs
    Then the rebuild reports 0 cards rebuilt
    And candidate_cards holds 0 rows

  Scenario: The same event is not processed twice
    Given the incumbent has a current position on a stance
    And a "positions.recomputed" event was emitted for the incumbent
    When the card rebuild consumer runs
    And the card rebuild consumer runs again
    Then the rebuild reports 0 cards rebuilt

  Scenario: Only current positions reach the summary
    Given the incumbent has a superseded position and a current one on the same stance
    And a "positions.recomputed" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the card summarises 1 position
    And the summarised position is the current one

  # --- rebuilt on verdict.published, 7.2's second trigger ------------------
  # NOT LIVE-VERIFIABLE TODAY. See the header: epic 11 is unbuilt and nothing
  # emits this topic, so these scenarios drive a fixture event. They prove the
  # consumer is wired and correct; they cannot prove it against a real
  # published verdict, and this story does not claim they do.

  Scenario: A verdict.published event rebuilds that person's cards
    Given the incumbent has a current verdict on a promise
    And a "verdict.published" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the rebuild reports 1 card rebuilt
    And the card counts that verdict

  Scenario: A verdict rebuild counts each verdict value separately
    Given the incumbent has current verdicts of kept, kept, partial and broken
    And a "verdict.published" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the card counts 2 kept, 1 partial and 1 broken

  Scenario: A verdict rebuild computes the say-do gap from the verdicts it counted
    Given the incumbent has current verdicts of kept, kept, partial and broken
    And a "verdict.published" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the card reports a say-do gap between 0 and 1

  Scenario: A candidate who kept every testable promise has no say-do gap
    Given the incumbent has current verdicts of kept, kept and kept
    And a "verdict.published" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the card reports a say-do gap of 0.0

  Scenario: A candidate who broke every testable promise has the widest say-do gap
    Given the incumbent has current verdicts of broken, broken and broken
    And a "verdict.published" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the card reports a say-do gap of 1.0

  Scenario: Verdicts that record no opportunity to act are not a say-do gap
    Given the incumbent has current verdicts of kept, no_opportunity and not_testable
    And a "verdict.published" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the card reports a say-do gap of 0.0
    And the card counts 1 kept, 1 no_opportunity and 1 not_testable

  Scenario: A candidate with no testable verdict has no say-do gap to report
    Given the incumbent has current verdicts of no_opportunity and not_testable
    And a "verdict.published" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the card's say-do gap is absent
    And the card counts 1 no_opportunity and 1 not_testable

  Scenario: Only current verdicts are counted
    Given the incumbent has a superseded verdict and a current one on the same promise
    And a "verdict.published" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the card counts 1 verdict in total
    And the counted verdict is the current one

  Scenario: A verdict rebuild preserves a positions summary a previous rebuild wrote
    Given the incumbent has a current position on a stance
    And the card for that candidacy has been rebuilt from a positions recompute
    And the incumbent has a current verdict on a promise
    And a "verdict.published" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the card still summarises that position
    And the card counts that verdict

  Scenario: A positions rebuild preserves verdict counts a previous rebuild wrote
    Given the incumbent has a current verdict on a promise
    And the card for that candidacy has been rebuilt from a verdict publication
    And the incumbent has a current position on a stance
    And a "positions.recomputed" event was emitted for the incumbent
    When the card rebuild consumer runs
    Then the card still counts that verdict
    And the card summarises that position

  # --- neutrality, which is non-negotiable ---------------------------------
  # prd.md: "One scoring codebase for all candidates; no party-specific rules."
  # A read model is as capable of smuggling in a party rule as a scorer is --
  # it is the row the ballot view actually renders.

  Scenario: Two candidates with mirrored records get mirrored cards
    Given a second synthetic incumbent of another party whose record mirrors the first
    And both have current verdicts that mirror each other
    And a "verdict.published" event was emitted for both
    When the card rebuild consumer runs
    Then their say-do gaps are exact opposites about the midpoint
    And their verdict counts are mirror images

  Scenario: A card carries no voter-derived field
    Given the card for that candidacy has been rebuilt
    Then the card model forbids any field that is not in datamodel.md's spec
    And no stored card field holds a voter interest, profile or alignment score
