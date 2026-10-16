Feature: Typed ZeroDB client covers every primitive
  As a backend developer
  I want a typed Python wrapper around ZeroDB
  So that repositories never call MCP tools or raw HTTP directly

  Background:
    Given a ZeroDB client backed by an in-memory fake

  # --- NoSQL tables (datamodel.md: system of record) ---

  Scenario: The client creates a table with a field-type schema
    When I create the table "jurisdictions" with fields "name,country_code,level,active"
    Then the fake recorded a POST to "/projects/{project}/database/tables"
    And the created table is named "jurisdictions"

  Scenario: The client inserts a row and reads it back by deterministic id
    Given the table "jurisdictions" exists
    When I insert the row "us-federal" into "jurisdictions"
    Then the row "us-federal" is readable from "jurisdictions"

  Scenario: Re-inserting the same natural-key id is an idempotent upsert
    Given the table "jurisdictions" exists
    And the row "us-federal" was already inserted into "jurisdictions"
    When I insert the row "us-federal" into "jurisdictions" again
    Then "jurisdictions" holds exactly 1 row with id "us-federal"

  Scenario: The client queries rows with a filter
    Given the table "bodies" exists
    And the rows "us-house,us-senate" were inserted into "bodies"
    When I query "bodies" filtered by id "us-house"
    Then the query returns 1 row
    And the returned row has id "us-house"

  Scenario: The client updates a row by id
    Given the table "jurisdictions" exists
    And the row "us-federal" was already inserted into "jurisdictions"
    When I update the row "us-federal" in "jurisdictions" setting "active" to "false"
    Then the row "us-federal" in "jurisdictions" has "active" equal to "false"

  Scenario: The client deletes a row by id
    Given the table "jurisdictions" exists
    And the row "us-federal" was already inserted into "jurisdictions"
    When I delete the row "us-federal" from "jurisdictions"
    Then "jurisdictions" holds exactly 0 rows with id "us-federal"

  # --- Vectors (datamodel.md: four namespaces) ---

  Scenario: The client embeds and stores text in a namespace
    When I embed and store "lower out-of-pocket drug costs" in namespace "promises"
    Then 1 vector is stored in namespace "promises"

  Scenario: The client runs a semantic search in a namespace
    Given the text "lower out-of-pocket drug costs" is stored in namespace "promises"
    When I semantically search "drug prices" in namespace "promises"
    Then the search returns at least 1 hit
    And each hit carries the metadata it was stored with

  Scenario: The client creates an HNSW index for a namespace
    When I create an HNSW index for namespace "promises"
    Then namespace "promises" has an index

  Scenario: The client lists every namespace that has an index
    Given an HNSW index exists for namespace "topics"
    When I list vector indexes
    Then the index list includes namespace "topics"

  # --- Knowledge graph (datamodel.md: evidence graph) ---

  Scenario: The client upserts the graph ontology
    When I upsert an ontology with entity types "person,promise,roll_call"
    Then the ontology records the entity type "promise"

  Scenario: The client upserts an entity whose canonical name is its NoSQL id
    When I upsert the graph entity "person:B000944" of type "person"
    Then the graph holds the entity "person:B000944"

  Scenario: The client creates an edge between two entities
    Given the graph entities "prm:1,us-house:119-2:452" exist
    When I create the edge "tested_by" from "prm:1" to "us-house:119-2:452"
    Then the graph holds an edge "tested_by" from "prm:1" to "us-house:119-2:452"

  Scenario: The client runs a grounded RAG search over the graph
    Given the graph entities "prm:1,us-house:119-2:452" exist
    When I run a graph RAG search for "drug pricing votes"
    Then the RAG search returns a result set

  Scenario: The client traverses the graph from an entity
    Given the graph entities "prm:1,us-house:119-2:452" exist
    And an edge "tested_by" exists from "prm:1" to "us-house:119-2:452"
    When I traverse the graph from "prm:1"
    Then the traversal reaches "us-house:119-2:452"

  # --- Files (datamodel.md: immutable snapshots behind citations) ---

  Scenario: The client registers an uploaded file with its provenance metadata
    When I upload the file "archive/campaign-page.html" with sha256 "abc123"
    Then the stored file carries sha256 "abc123"

  Scenario: The client generates a short-lived presigned URL for a file
    Given the file "archive/campaign-page.html" was uploaded
    When I generate a presigned URL for that file expiring in 300 seconds
    Then a presigned URL is returned

  # --- Events (datamodel.md: keep read models in sync) ---

  Scenario: The client publishes an event
    When I publish the event "rollcall.ingested" with payload key "roll_call_id"
    Then 1 event with topic "rollcall.ingested" was published

  Scenario: The client lists events filtered by topic
    Given the events "rollcall.ingested,verdict.published" were published
    When I list events with topic "verdict.published"
    Then the event list contains only topic "verdict.published"

  Scenario: The client subscribes to event types
    When I subscribe to the event types "verdict.published,positions.recomputed"
    Then a subscription for "verdict.published" is registered
