Feature: Embed each promise and announce it (stories 9.4 and 9.5)
  As the system
  I want every promise embedded into the `promises` vector namespace
    and a `promise.extracted` event emitted for it
  So that the mapping agent can match it to bills, and starts automatically

  # Two stories, one feature file, because 9.5's event is the completion
  # signal of 9.4's embedding step: a `promise.extracted` event tells the
  # mapping agent (story 10.1) that a promise is ready to be matched, and a
  # promise is not ready to be matched until it has a vector to match WITH.
  # Emitting before the embedding exists would wake a consumer that then
  # finds nothing searchable. So the two are one transition, verified here as
  # one.
  #
  # Every scenario runs against story 1.2's in-memory fake transport. The
  # embedding itself is ZeroDB's real `bge-m3` (1024 dimensions) in
  # production and the fake's stand-in here; what these scenarios pin is the
  # contract around it -- the metadata datamodel.md requires, the `vector_id`
  # written back, the once-per-promise event, and what happens when either
  # call fails.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist
    And a candidacy and an archived promise source exist
    And promises have been extracted from that source

  # --- story 9.4: every promise has a vector_id ----------------------------

  Scenario: Every promise ends up with a vector_id
    When I embed the promises for that source
    Then every promise row has a vector_id
    And the vector_id names a vector that really exists in the "promises" namespace

  Scenario: The embedded text is the promise's own verbatim quote
    When I embed the promises for that source
    Then every stored vector's document is the quote span of the promise it belongs to

  # --- story 9.4: the metadata the acceptance criterion names ---------------

  Scenario: Each vector's metadata carries the promise_id and the person_id
    When I embed the promises for that source
    Then every stored vector's metadata carries the promise_id it was built from
    And every stored vector's metadata carries that promise's person_id

  Scenario: The vector metadata carries the rest of datamodel.md's filter fields
    When I embed the promises for that source
    Then every stored vector's metadata carries topic_id, cycle and testable

  Scenario: A vector resolves back to the promise row it was built from
    When I embed the promises for that source
    Then the promise_id in each vector's metadata resolves to a stored promise row

  # --- story 9.4: non-testable promises are embedded too -------------------

  Scenario: A non-testable promise is embedded as well, and marked so
    Given the source text contains one testable and one non-testable commitment
    When I embed the promises for that source
    Then exactly 2 vectors exist in the "promises" namespace
    And 1 of those vectors is marked testable in its metadata
    And 1 of those vectors is marked not testable in its metadata

  # --- story 9.4: idempotency -----------------------------------------------

  Scenario: Re-running the embedding does not store a second vector per promise
    Given I have already embedded the promises for that source
    When I embed the promises for that source again
    Then exactly 1 vector exists in the "promises" namespace per promise row

  Scenario: A re-run reports that it embedded nothing new
    Given I have already embedded the promises for that source
    When I embed the promises for that source again
    Then the embedding report says 0 promises were embedded
    And the embedding report says every promise was already current

  Scenario: A promise whose quote changes is re-embedded, not left stale
    Given I have already embedded the promises for that source
    And a stored promise's vector was built from different text
    When I embed the promises for that source again
    Then that promise is re-embedded
    And exactly 1 vector exists in the "promises" namespace per promise row

  # --- story 9.4: a promise is never reported as embedded unless it is -----

  Scenario: A promise whose embedding call fails keeps no vector_id
    Given the vector store refuses to store embeddings
    When I try to embed the promises for that source
    Then no promise row has a vector_id
    And the embedding report says 0 promises were embedded
    And the embedding report records the failure

  Scenario: A promise whose vector_id cannot be read back is not reported as embedded
    Given the vector store stores embeddings but will not list them back
    When I try to embed the promises for that source
    Then the embedding report says 0 promises were embedded
    And the embedding report records the failure

  # --- story 9.5: a promise.extracted event per promise --------------------

  Scenario: Embedding a promise emits a promise.extracted event
    When I embed the promises for that source
    Then a "promise.extracted" event exists for every promise row

  Scenario: The event payload carries the promise_id and the person_id
    When I embed the promises for that source
    Then every "promise.extracted" event payload carries a promise_id and a person_id
    And each event's promise_id resolves to a stored promise row

  Scenario: The event is emitted exactly once per promise
    When I embed the promises for that source
    Then exactly 1 "promise.extracted" event exists per promise row

  Scenario: Re-running emits no second event for an already-embedded promise
    Given I have already embedded the promises for that source
    When I embed the promises for that source again
    Then exactly 1 "promise.extracted" event exists per promise row
    And the embedding report says 0 events were emitted

  # --- story 9.5: an event is never counted unless it was really sent ------

  Scenario: An event that fails to publish is not reported as emitted
    Given the event log refuses to publish events
    When I embed the promises for that source
    Then the embedding report says 0 events were emitted
    And every promise row still has a vector_id
    And the embedding report records the failure

  Scenario: A promise is never announced before it can actually be matched
    Given the vector store refuses to store embeddings
    When I try to embed the promises for that source
    Then no "promise.extracted" event exists
