Feature: Embed topics and stances (story 3.4)
  As the system
  I want topics and stances embedded
  So that free text and promises can be matched to them

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the issue taxonomy has been seeded
    And the stance cards have been seeded

  # --- the acceptance criterion: both namespaces are populated --------------

  Scenario: Every topic gets an embedding
    When I run the embedding pipeline
    Then the topics namespace has one embedding per stored topic

  Scenario: Every stance gets an embedding
    When I run the embedding pipeline
    Then the stances namespace has one embedding per stored stance

  Scenario: Each topic embedding carries the documented metadata
    When I run the embedding pipeline
    Then every topic embedding's metadata has topic_id, locale and taxonomy_version

  Scenario: Each stance embedding carries the documented metadata
    When I run the embedding pipeline
    Then every stance embedding's metadata has stance_id, topic_id and wording_version

  Scenario: A subtopic's embedded text includes its parent for context
    When I run the embedding pipeline
    Then the embedded text for "en-US:prescription-drug-pricing:v1" includes its own name and its parent's name

  Scenario: A stance's embedded text includes both its label and description
    When I run the embedding pipeline
    Then the embedded text for stance "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs" includes its label and its description

  # --- idempotency: re-running with no version change does not duplicate ---

  Scenario: Re-running with no version change does not create duplicate topic embeddings
    Given I have run the embedding pipeline once
    When I run the embedding pipeline again
    Then the topics namespace still has one embedding per stored topic
    And the second run embedded no topics

  Scenario: Re-running with no version change does not create duplicate stance embeddings
    Given I have run the embedding pipeline once
    When I run the embedding pipeline again
    Then the stances namespace still has one embedding per stored stance
    And the second run embedded no stances

  # --- rebuild on version change --------------------------------------------

  Scenario: A taxonomy version bump triggers a rebuild, superseding the old embedding
    Given I have run the embedding pipeline once
    When a topic's taxonomy_version changes
    And I run the embedding pipeline again
    Then that topic's old embedding is gone
    And that topic has exactly one embedding, at the new version
    And every other topic's embedding is unchanged

  Scenario: A wording version bump triggers a rebuild, superseding the old embedding
    Given I have run the embedding pipeline once
    When a stance's wording_version changes
    And I run the embedding pipeline again
    Then that stance's old embedding is gone
    And that stance has exactly one embedding, at the new version
    And every other stance's embedding is unchanged

  # --- the bootstrap marker is left alone ------------------------------------

  Scenario: The namespace's bootstrap marker vector is untouched
    Given the topics namespace already holds a bootstrap marker vector
    When I run the embedding pipeline
    Then the bootstrap marker vector is still present and unchanged
