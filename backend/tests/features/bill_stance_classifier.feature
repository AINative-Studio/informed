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
  # THE FIRST CRITERION IS NOT MET. Measured against the real 200-bill labeled
  # set at tests/fixtures/bill_topic_labeled.json, this classifier scores
  # 54.2% precision, not 85%. The scenarios below therefore assert the
  # measurement and its consequence -- that NOTHING is accepted automatically
  # and every tag goes to the QA queue -- rather than a bar the code does not
  # clear. See app/ingest/bill_topic_classifier.py's docstring for the full
  # finding, the nine rule variants that also fail it, and why the fix is a
  # better input (the bill's summary text) rather than a better threshold.
  #
  # The precision arithmetic itself lives in
  # tests/test_bill_topic_classifier_unit.py, which reads the labeled fixture
  # directly; these scenarios cover the behaviour a reader of the feature file
  # needs to understand.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist
    And a synthetic topic tree with two mirrored stance cards

  # === The acceptance criterion, and the honest result =====================

  Scenario: The labeled set is 200 real measures with real CRS classification
    Then the labeled set holds at least 200 real bills
    And every bill in the labeled set carries a real congress.gov measure id
    And every bill in the labeled set carries its own CRS policy area or subject terms
    And the labeled set covers at least 12 of the taxonomy's top-level topics

  Scenario: Measured precision on the labeled set is below story 5.5's bar
    When I classify every bill in the labeled set
    Then the measured precision is 54.2 percent
    And the measured precision is below the required 85 percent
    And the module records that same precision figure

  Scenario: Because the bar is unmet, no tag is accepted automatically
    When I classify every bill in the labeled set
    Then no tag is accepted
    And every tag is routed to QA

  Scenario: Tightening the confidence rule does not reach the bar either
    When I classify the labeled set requiring every corroboration
    Then the measured precision is still below the required 85 percent
    And far fewer tags are proposed than before

  # === Topic tagging from CRS signal ========================================
  # These exercise what the classifier PROPOSES. Production accepts none of it
  # (see above), so each scenario asks for the proposal, not a stored write.

  Scenario: A bill's CRS policy area proposes its taxonomy topic
    Given a bill whose CRS policy area is "Health"
    When I classify that bill
    Then it proposes a tag for slug "healthcare"

  Scenario: A CRS legislative subject term proposes a subtopic, not just its policy area
    Given a bill whose CRS policy area is "Health"
    And whose CRS subject terms include "Prescription drugs"
    When I classify that bill
    Then it proposes a tag for slug "prescription-drug-pricing"
    And it proposes a tag for slug "healthcare"

  Scenario: A bill with no CRS classification at all proposes nothing
    Given a bill with no CRS policy area and no CRS subject terms
    When I classify that bill
    Then it proposes no tags
    And nothing is routed to QA for it

  Scenario: An unrecognised CRS term proposes no topic rather than a guess
    Given a bill with no CRS policy area and no CRS subject terms
    And whose CRS subject terms include "Alaska"
    When I classify that bill
    Then it proposes no tags

  Scenario: The policy area is not double-counted as one of its own subject terms
    Given a bill whose CRS policy area is "Health"
    And whose CRS subject terms repeat the policy area
    When I classify that bill
    Then it proposes a tag for slug "healthcare"
    And that tag's confidence is one corroboration

  # === The omnibus rule: a bill about everything is evidence about nothing ===
  # This is the one rule that demonstrably works on real data: it held every
  # one of the 63 omnibus-shaped measures in the live population, including the
  # NDAA (201 CRS terms) and eight continuing resolutions (249 terms each).

  Scenario: An omnibus bill touching many topics has every tag held for breadth
    Given a bill carrying CRS subject terms spanning the whole taxonomy
    When I classify that bill
    Then every tag for that bill is routed to QA
    And the QA reason for every tag on that bill is "breadth"

  Scenario: Breadth overrides a doubly corroborated tag
    Given a bill carrying CRS subject terms spanning the whole taxonomy
    And two of those terms agree on one subtopic
    When I classify that bill
    Then that subtopic's tag is fully corroborated
    And the QA reason for that tag is "breadth"

  Scenario: A bill exactly at the breadth limit is not held for breadth
    Given a bill whose CRS subject terms reach exactly the breadth limit
    When I classify that bill
    Then no tag for that bill is held for breadth

  # === Low-confidence tags go to QA (the second acceptance criterion) =======
  # This criterion IS met, and is met for every tag rather than some.

  Scenario: Every QA-routed tag records why it was held and what it matched
    Given a bill whose CRS policy area is "Health"
    When I classify that bill
    Then every QA-routed tag names a reason from the documented QA reason list
    And every QA-routed tag carries its confidence and its matched CRS term

  Scenario: A held tag is returned rather than silently dropped
    Given a bill whose CRS policy area is "Health"
    When I classify that bill
    Then the classification report lists that bill's held tags

  Scenario: A classification run emits one QA event per bill with held tags
    Given a stored bill whose CRS policy area is "Health"
    When I run the classifier over the stored bills
    Then one bill.tags_pending_qa event is emitted for that bill
    And that event names the classifier version and the mapping version

  # === Writing the result ===================================================

  Scenario: No topic_ids are written to a bill while nothing is accepted
    Given a stored bill whose CRS policy area is "Health"
    When I run the classifier over the stored bills
    Then the stored bill's topic_ids are still empty
    And the bill row's other fields are unchanged

  Scenario: Accepted tags would be written to the bill row
    Given a stored bill whose CRS policy area is "Health"
    When I run the classifier over the stored bills accepting one corroboration
    Then the stored bill's topic_ids include the topic for slug "healthcare"
    And the bill row's other fields are unchanged

  Scenario: Re-running over an unchanged bill writes nothing new
    Given a stored bill whose CRS policy area is "Health"
    When I run the classifier over the stored bills accepting one corroboration twice
    Then the second run reports zero bills changed

  Scenario: A tag naming an unseeded topic is refused rather than written
    Given a stored bill whose CRS policy area is "Health"
    And the healthcare topic is not seeded
    When I run the classifier over the stored bills accepting one corroboration
    Then the stored bill's topic_ids are still empty

  # === The graph edges: advances / opposes ==================================
  # Story 5.5's title says "whether they advance or oppose each stance", and
  # this half is ENTIRELY undone on real data: CRS subject terms carry no
  # direction at all, so every tag v0 produces has direction UNKNOWN and writes
  # no edge. The code path below is complete and tested; it has nothing real to
  # feed it.

  Scenario: A tag whose direction is unknown writes no stance edge
    Given a tag on a topic whose stances are seeded
    And that tag's direction is unknown
    When I write that tag's stance edges
    Then no bill-to-stance edge is written

  Scenario: A bill that moves a stance's axis the stance's own way advances it
    Given a tag on a topic whose stances are seeded
    And that tag's direction is "expands"
    When I write that tag's stance edges
    Then the edge to the stance with direction_code "+1" has predicate "advances"

  Scenario: A bill that moves a stance's axis the other way opposes it
    Given a tag on a topic whose stances are seeded
    And that tag's direction is "expands"
    When I write that tag's stance edges
    Then the edge to the stance with direction_code "-1" has predicate "opposes"

  Scenario: Reversing the bill's direction reverses every effect
    Given a tag on a topic whose stances are seeded
    When I write that tag's stance edges for both bill directions
    Then every stance's effect is reversed between the two runs

  Scenario: A stance that does not move along the axis gets no edge
    Given a tag on a topic whose stances are seeded
    And a third stance with direction_code "0"
    And that tag's direction is "expands"
    When I write that tag's stance edges
    Then no edge is written to the stance with direction_code "0"

  Scenario: A QA-routed tag writes no graph edge even with a known direction
    Given a tag on a topic whose stances are seeded
    And that tag's direction is "expands"
    And that tag is routed to QA
    When I write that tag's stance edges
    Then no bill-to-stance edge is written

  Scenario: Each written edge carries its confidence and its method
    Given a tag on a topic whose stances are seeded
    And that tag's direction is "expands"
    When I write that tag's stance edges
    Then each written edge carries its confidence and its method
    And each method names the classifier version and the mapping version

  Scenario: No classification rule names a party or a candidate
    Then the mapping table holds no rule naming a party
    And the direction rule reads only the stance's own direction_code

  # === What this unblocks: story 6.2's scoring =============================
  # The point of the whole story. 6.2 matches member_votes.topic_ids against
  # stances.topic_id and has found nothing on live data since it merged.

  Scenario: Classified topics propagate from a bill to its roll calls and member votes
    Given a stored bill whose CRS policy area is "Health"
    And a stored roll call on that bill with a member vote
    When I propagate that bill's topics to its roll calls
    Then the roll call's topic_ids include the topic for slug "healthcare"
    And that member's vote carries the same topic_ids

  Scenario: Story 6.2 finds a position where it previously found none
    Given a stored bill whose CRS policy area is "Health"
    And a stored roll call on that bill with a member vote
    And story 6.2 finds no position for that member before classification
    When I propagate that bill's topics to its roll calls
    Then story 6.2 finds a position for that member on a healthcare stance

  Scenario: Propagation keeps the roll call and its member votes in agreement
    Given a stored bill whose CRS policy area is "Health"
    And a stored roll call on that bill with a member vote
    When I propagate that bill's topics to its roll calls
    Then the member vote's topic_ids equal the roll call's topic_ids
