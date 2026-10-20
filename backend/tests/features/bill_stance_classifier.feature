Feature: Bill-to-stance classifier v0 (story 5.5)
  As the system
  I want bills tagged with topics and with whether they advance or oppose each stance
  So that votes translate into stance positions

  # backlog.md 5.5: "As the system, I want bills tagged with topics and with
  # whether they advance or oppose each stance, so that votes translate into
  # stance positions."
  # Acceptance: "At least 85% precision on a 200-bill labeled set;
  # low-confidence tags go to QA."
  # ZeroDB data: bills, graph: advances/opposes
  #
  # The labeled set is tests/fixtures/bill_topic_labeled.json: real House-voted
  # measures captured live from api.congress.gov, labeled from their own CRS
  # policy area and legislative subject terms. See that fixture's `_comment`
  # for how it was drawn and how each label was decided.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist
    And the issue taxonomy and stance cards are seeded

  # === The acceptance criterion: precision on the labeled set ==============

  Scenario: Topic classification is at least 85% precise on the 200-bill labeled set
    When I classify every bill in the labeled set
    Then the labeled set holds at least 200 real bills
    And topic precision on the labeled set is at least 85%
    And every precision figure is measured over accepted tags only

  Scenario: The labeled set is real data, not synthetic
    Then every bill in the labeled set carries a real congress.gov source
    And every bill in the labeled set carries its own CRS policy area or subject terms
    And the labeled set covers at least 12 of the taxonomy's top-level topics

  # === Topic tagging from CRS signal ========================================

  Scenario: A bill's CRS policy area maps to a taxonomy topic
    Given a bill whose CRS policy area is "Health"
    When I classify that bill
    Then its topic_ids include the topic for slug "healthcare"

  Scenario: A CRS legislative subject term maps to a subtopic, not just its policy area
    Given a bill whose CRS policy area is "Health"
    And whose CRS subject terms include "Prescription drug costs"
    When I classify that bill
    Then its topic_ids include the topic for slug "prescription-drug-pricing"
    And its topic_ids include the topic for slug "healthcare"

  Scenario: A bill with no CRS classification at all is tagged with nothing
    Given a bill with no CRS policy area and no CRS subject terms
    When I classify that bill
    Then its topic_ids are empty
    And no tag is routed to QA for it

  Scenario: An unrecognised CRS term contributes no topic rather than a guess
    Given a bill whose CRS subject terms include "Alaska"
    And whose CRS policy area is absent
    When I classify that bill
    Then its topic_ids are empty

  # === The omnibus problem: a bill about everything is evidence about nothing

  Scenario: An omnibus bill touching dozens of topics has its tags routed to QA
    Given a bill carrying 200 CRS subject terms spanning the whole taxonomy
    When I classify that bill
    Then every tag for that bill is routed to QA
    And no tag for that bill is accepted
    And the QA reason for that bill is "breadth"

  # === Low-confidence tags go to QA (the second acceptance criterion) =======

  Scenario: A tag below the acceptance threshold is routed to QA rather than accepted
    Given a bill whose only CRS signal is a single weakly-matching subject term
    When I classify that bill
    Then that tag is routed to QA
    And that tag is not written to bills.topic_ids

  Scenario: A tag at or above the acceptance threshold is accepted
    Given a bill whose CRS policy area and subject terms agree on one topic
    When I classify that bill
    Then that tag is accepted
    And that tag is written to bills.topic_ids

  Scenario: Every QA-routed tag records why it was held
    Given a bill carrying 200 CRS subject terms spanning the whole taxonomy
    When I classify that bill
    Then every QA-routed tag names a reason from the documented QA reason list
    And every QA-routed tag carries its confidence and its matched CRS term

  # === Writing the result ===================================================

  Scenario: Accepted topic tags are written to the bill row
    Given a stored bill whose CRS policy area is "Health"
    When I run the classifier over the stored bills
    Then the stored bill's topic_ids include the topic for slug "healthcare"
    And the bill row's other fields are unchanged

  Scenario: Re-running the classifier over an unchanged bill writes nothing new
    Given a stored bill whose CRS policy area is "Health"
    When I run the classifier over the stored bills twice
    Then the stored bill has exactly one set of topic_ids
    And the second run reports zero bills changed

  Scenario: Classifying a bill does not invent a topic id the taxonomy does not hold
    Given a stored bill whose CRS policy area is "Health"
    When I run the classifier over the stored bills
    Then every topic id written to that bill resolves to a stored issue_topics row

  # === The graph edges: advances / opposes ==================================

  Scenario: An accepted tag writes a bill-to-stance edge per stance under the topic
    Given a stored bill whose CRS policy area is "Health"
    And the taxonomy holds stance cards under the healthcare topic
    When I run the classifier over the stored bills
    Then a graph edge is written from that bill to each stance under its topics
    And each such edge's predicate is "advances" or "opposes"
    And each such edge carries its confidence and its method

  Scenario: A bill that moves a stance's axis the stance's own way advances it
    Given a bill tagged to a topic whose stance has direction_code "+1"
    And the bill's CRS signal says it expands federal action on that topic
    When I write that bill's stance edges
    Then the edge to that stance has predicate "advances"

  Scenario: A bill that moves a stance's axis the other way opposes it
    Given a bill tagged to a topic whose stance has direction_code "-1"
    And the bill's CRS signal says it expands federal action on that topic
    When I write that bill's stance edges
    Then the edge to that stance has predicate "opposes"

  Scenario: A stance that does not move along the axis gets no edge
    Given a bill tagged to a topic whose stance has direction_code "0"
    When I write that bill's stance edges
    Then no edge is written to that stance

  Scenario: A QA-routed tag writes no graph edge until it is approved
    Given a bill carrying 200 CRS subject terms spanning the whole taxonomy
    When I run the classifier over the stored bills
    Then no bill-to-stance edge is written for that bill

  Scenario: Both directions are available to every stance, with no party-specific rule
    Then the classifier's direction rule reads only the stance's own direction_code
    And the classifier holds no rule naming a party or a candidate

  # === What this unblocks: story 6.2's scoring =============================

  Scenario: A classified roll call carries its bill's topics to member_votes
    Given a stored bill whose CRS policy area is "Health"
    And a stored roll call on that bill with a member vote
    When I run the classifier over the stored bills
    And I propagate the bill's topics to its roll calls
    Then the roll call's topic_ids include the topic for slug "healthcare"
    And that member's vote carries the same topic_ids

  Scenario: Story 6.2 finds a position where it previously found none
    Given a stored bill whose CRS policy area is "Health"
    And a stored roll call on that bill with a member vote
    And story 6.2 finds no position for that member before classification
    When I run the classifier over the stored bills
    And I propagate the bill's topics to its roll calls
    Then story 6.2 finds a position for that member on a healthcare stance

  # === Provenance and events ================================================

  Scenario: A classification run records how it decided
    Given a stored bill whose CRS policy area is "Health"
    When I run the classifier over the stored bills
    Then each accepted tag records the classifier version that produced it
    And each accepted tag records the CRS term it matched
