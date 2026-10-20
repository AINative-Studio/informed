Feature: Senate vote typing, bundling and no-recorded-vote handling (stories 8.3, 8.4, 8.5)
  As the system
  I want each Senate roll call typed, bundled measures flagged with a reason, and
  unrecorded outcomes kept out of the record entirely
  So that prd.md's weights apply correctly and silence is never read as support

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist
    And every senator in the labeled crosswalk exists as a people row

  # === 8.3 Vote-type classifier ============================================
  # Acceptance: "At least 95% accuracy on a labeled sample; matches the PRD
  # 'procedural vote' scenario."

  Scenario: The classifier is at least 95% accurate on the labeled sample of real Senate roll calls
    When I classify every roll call in the labeled sample
    Then at least 95% of the labeled sample is classified correctly
    And the labeled sample covers every vote_type in the published vocabulary
    And the labeled sample holds at least 50 real roll calls

  Scenario Outline: Each documented question shape classifies to its vote_type
    When I classify the labeled roll call "<label_id>"
    Then its vote_type is "<vote_type>"

    Examples: final passage — a measure's own final disposition
      | label_id                          | vote_type     |
      | passage-of-the-bill               | final_passage |
      | joint-resolution                  | final_passage |
      | concurrent-resolution             | final_passage |
      | resolution                        | final_passage |
      | conference-report                 | final_passage |
      | overriding-the-veto               | final_passage |
      | resolution-of-ratification        | final_passage |

    Examples: cloture — Rule XXII, in both of its live question spellings
      | label_id                          | vote_type     |
      | cloture-motion-on-a-nomination    | cloture       |
      | cloture-on-the-motion-to-proceed  | cloture       |

    Examples: amendment — the structural signal under a question about adopting it
      | label_id                          | vote_type     |
      | amendment                         | amendment     |

    Examples: motion to recommit
      | label_id                          | vote_type        |
      | motion-to-recommit                | motion_to_recommit |

    Examples: procedural — parliamentary process, not the measure's substance
      | label_id                          | vote_type     |
      | motion-to-table                   | procedural    |
      | motion-to-table-an-amendment      | procedural    |
      | motion-to-proceed                 | procedural    |
      | motion-to-discharge               | procedural    |
      | motion-to-reconsider              | procedural    |
      | motion-to-refer                   | procedural    |
      | motion-to-adjourn                 | procedural    |
      | point-of-order                    | procedural    |
      | decision-of-the-chair             | procedural    |
      | motion-for-attendance             | procedural    |
      | motion-to-close-the-doors         | procedural    |
      | motion-to-waive-budgetary-discipline | procedural |
      | motion-on-an-amendment            | procedural    |

    Examples: other — honestly unclassified, never forced into a wrong fit
      | label_id                          | vote_type     |
      | nomination                        | other         |
      | motion-to-concur                  | other         |

  Scenario: Cloture outranks the amendment signal on a cloture vote over an amendment
    When I classify the labeled roll call "cloture-on-an-amendment"
    Then its vote_type is "cloture"

  Scenario: A motion to table an amendment is procedural, not an amendment vote
    When I classify the labeled roll call "motion-to-table-an-amendment"
    Then its vote_type is "procedural"
    And the labeled roll call "motion-to-table-an-amendment" carries an amendment reference

  # prd.md's "procedural vote" scenario, at the layer this story owns: the
  # classifier is what makes the cloture vote distinguishable from the final
  # passage vote on the same bill, which is what lets the verdict say "Kept"
  # with a "procedural vote" note instead of "Broken".
  Scenario: A Nay on cloture and a Yea on final passage of the same bill type differently
    When I classify the labeled roll call "cloture-on-the-motion-to-proceed"
    And I classify the labeled roll call "passage-of-the-bill"
    Then the cloture vote and the final-passage vote carry different vote_types
    And the cloture vote weighs less than the final-passage vote under the published weights

  Scenario: An empty question falls back to other rather than raising
    When I classify a roll call whose question is empty
    Then its vote_type is "other"

  Scenario: Every classification is explainable
    When I classify the labeled roll call "motion-to-discharge"
    Then the classification names the rule that decided it

  # === 8.4 Bundled (omnibus) flag ==========================================
  # Acceptance: "Bills over a configurable size or with omnibus markers get
  # `is_bundled`; the UI explains it."

  Scenario Outline: A measure carrying a real omnibus marker is flagged bundled with a reason
    When I classify the labeled roll call "<label_id>"
    Then it is flagged bundled
    And its bundled reason is "<reason>"

    Examples: live omnibus markers captured from senate.gov
      | label_id                              | reason          |
      | further-consolidated-appropriations   | omnibus_marker  |
      | continuing-appropriations             | omnibus_marker  |
      | appropriations-and-related-agencies   | omnibus_marker  |
      | reconciliation                        | omnibus_marker  |
      | budget-resolution                     | omnibus_marker  |
      | en-bloc-nominations                   | omnibus_marker  |

  Scenario: A vote on many measures at once is flagged bundled by size, not by title
    When I classify the labeled roll call "en-bloc-48-nominations"
    Then it is flagged bundled
    And its bundled reason is "measure_count_exceeds_threshold"

  Scenario: A single-subject bill is not flagged bundled
    When I classify the labeled roll call "passage-of-the-bill"
    Then it is not flagged bundled
    And its bundled reason is empty

  Scenario: A nomination is not flagged bundled
    When I classify the labeled roll call "nomination"
    Then it is not flagged bundled

  Scenario: The bundling threshold is configurable
    Given a bundling threshold of 2 measures
    When I classify the labeled roll call "en-bloc-nominations"
    Then it is flagged bundled

  Scenario: Raising the threshold above the measure count drops the size reason
    Given a bundling threshold of 500 measures
    When I classify the labeled roll call "en-bloc-48-nominations"
    Then its bundled reason is not "measure_count_exceeds_threshold"

  Scenario: Every bundled reason carries plain-language UI text
    Then every bundled reason has a plain-language explanation

  Scenario: Ingesting a bundled roll call stores the flag and the reason
    When I ingest the labeled roll call "further-consolidated-appropriations"
    Then the stored roll call is flagged bundled
    And the stored roll call's bundled reason is "omnibus_marker"

  Scenario: A bundled roll call's member_votes inherit the flag
    When I ingest the labeled roll call "further-consolidated-appropriations" with member positions
    Then every stored member_vote for that roll call is flagged bundled

  # === 8.5 No-recorded-vote handling =======================================
  # Acceptance: "Matches the PRD Gherkin scenario 'No recorded vote'."

  Scenario: A voice vote leaves no recorded position for anyone
    Given a bill disposed of by voice vote
    When I ask whether the Senate's position on it is recorded
    Then the answer is that there is no recorded vote
    And the reason given is "voice_vote"

  Scenario: A unanimous-consent outcome leaves no recorded position for anyone
    Given a bill agreed to by unanimous consent
    When I ask whether the Senate's position on it is recorded
    Then the answer is that there is no recorded vote
    And the reason given is "unanimous_consent"

  Scenario: An unrecorded outcome is never written as a roll call
    Given a bill disposed of by voice vote
    When I try to ingest it as a roll call
    Then nothing is stored
    And the skip reason says there is no recorded vote

  Scenario: An unrecorded outcome yields no member_votes at all
    Given a bill disposed of by voice vote
    When I try to ingest it as a roll call
    Then no member_votes row is stored

  Scenario: A real recorded roll call is not mistaken for an unrecorded outcome
    When I classify the labeled roll call "passage-of-the-bill"
    Then it is a recorded vote

  # prd.md's "No recorded vote" scenario, verbatim, at the layer this story
  # owns: the ingest refuses to mint a roll call, so no position exists for a
  # verdict to read, which is what makes the verdict "No opportunity".
  Scenario: No recorded vote
    Given a promise maps only to a bill passed by voice vote
    Then the promise has no recorded position to score
    And the UI message for it is "no recorded vote"
    And the verdict it implies is "no_opportunity"

  Scenario: A senator absent from a real roll call is not an unrecorded outcome
    When I classify the labeled roll call "nomination"
    Then it is a recorded vote
    And a senator who did not vote on it is recorded as not_voting, not as unrecorded
