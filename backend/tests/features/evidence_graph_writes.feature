Feature: Evidence graph writes (story 13.1)
  As the system
  I want ingest and verdict agents to write the evidence graph per the ontology
  So that the agent can traverse evidence

  Background:
    Given a bootstrapped ZeroDB project with the evidence-graph ontology registered

  Scenario: The registered ontology is exactly the one datamodel.md documents
    When the registered ontology is read back
    Then every entity type in datamodel.md's predicate table is registered
    And every predicate in datamodel.md's predicate table is registered with its documented source and target types
    And no predicate outside datamodel.md's predicate table is registered

  Scenario: A candidacy's ran_in edge is written from a real candidacy row
    Given a person row and a candidacy row for that person
    When the evidence graph writer writes the candidacy's evidence
    Then a "person" entity exists whose canonical name is the person's NoSQL id
    And a "candidacy" entity exists whose canonical name is the candidacy's NoSQL id
    And a "ran_in" edge connects the person to the candidacy
    And the edge carries the party and incumbency from the candidacy row

  Scenario: Canonical names are NoSQL ids, so a traversal resolves to a citable row
    Given a person row and a candidacy row for that person
    When the evidence graph writer writes the candidacy's evidence
    And the graph is traversed from the person entity
    Then every entity reached names a row id that can be read back from its NoSQL table

  Scenario: Re-writing the same evidence changes nothing
    Given a person row and a candidacy row for that person
    When the evidence graph writer writes the candidacy's evidence
    And the evidence graph writer writes the candidacy's evidence again
    Then the graph holds exactly one "ran_in" edge for that pair

  Scenario: A promise's evidence is written as made_promise, about and has_verdict edges
    Given a candidacy with one promise, a stance and a published verdict
    When the evidence graph writer writes the promise's evidence
    Then a "made_promise" edge connects the candidacy to the promise
    And an "about" edge connects the promise to the stance
    And a "has_verdict" edge connects the promise to the verdict
    And the verdict edge carries its methodology version and is_current flag

  Scenario: A voted edge is written only where a promise or bill-stance edge touches the roll call
    Given a roll call that one promise is tested by
    And a roll call that no promise and no bill-stance edge touches
    When the evidence graph writer writes the member's vote evidence
    Then a "voted" edge exists for the roll call a promise tests
    And no "voted" edge exists for the untouched roll call

  Scenario: An edge whose predicate is not in the ontology is refused before any write
    Given a person row and a candidacy row for that person
    When the evidence graph writer is asked to write an edge with the predicate "endorses"
    Then the write is refused with an unknown-predicate error
    And nothing was written to the graph

  Scenario: An edge whose endpoints do not match the ontology's types is refused
    Given a person row and a candidacy row for that person
    When the evidence graph writer is asked to write a "ran_in" edge from the candidacy to the person
    Then the write is refused with an endpoint-type error
    And nothing was written to the graph
