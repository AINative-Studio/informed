Feature: Archive bill text (story 5.4)
  As a voter
  I want to open the exact bill text a vote was on
  So that I can verify the evidence myself

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist
    And the bill "us-house:119:hr-3424" is already ingested

  # --- the acceptance criterion, first half: stored in Files with its sha256 ---

  Scenario: Archiving a bill's text stores the bytes in Files
    When I archive the text of bill "us-house:119:hr-3424"
    Then the bill "us-house:119:hr-3424" has a text_file_id
    And the stored file's bytes are byte-for-byte the fetched document
    And the stored file's metadata records the sha256 of those bytes
    And the stored file's metadata records the source url and captured_at

  Scenario: The reported sha256 is the hash of the archived bytes
    When I archive the text of bill "us-house:119:hr-3424"
    Then the report's sha256 is the sha256 of the fetched document
    And the file metadata sha256 matches the report's sha256

  Scenario: The archive records which text version it captured
    When I archive the text of bill "us-house:119:hr-3424"
    Then the report names version type "Referred in Senate"
    And the report names format "Formatted XML"
    And the report's source url is the congress.gov document url

  # --- version selection: the newest version, not whichever came first ------

  Scenario: The most recent text version is the one archived
    Given the bill text source lists four versions, newest dated "2025-09-09"
    When I archive the text of bill "us-house:119:hr-3424"
    Then the report names version date "2025-09-09T04:00:00Z"

  Scenario: The latest-dated version wins even though the source lists it last
    Given the bill "us-house:119:hr-1" is already ingested
    And the bill text source is the live H.R. 1 response, which lists an undated version first and its latest-dated version last
    When I archive the text of bill "us-house:119:hr-1"
    Then the report names version type "Public Law"
    And the report names version date "2025-07-05T03:59:59Z"

  # --- format preference ------------------------------------------------------

  Scenario: A preferred format is chosen over the others on offer
    Given the newest text version offers "Formatted Text", "PDF" and "Formatted XML"
    When I archive the text of bill "us-house:119:hr-3424"
    Then the report names format "Formatted XML"

  Scenario: A version offering only one format is archived in that format
    Given the newest text version offers only "PDF"
    When I archive the text of bill "us-house:119:hr-3424"
    Then the report names format "PDF"
    And the stored file's content type is "application/pdf"

  # --- graceful absence: a bill whose text is not published yet ---------------

  Scenario: A bill with no published text versions is skipped, not failed
    Given the bill text source reports no text versions
    When I archive the text of bill "us-house:119:hr-3424"
    Then no text was archived and the reason is recorded
    And the bill "us-house:119:hr-3424" still has no text_file_id
    And no error was raised

  Scenario: A version carrying no usable format is skipped, not failed
    Given the newest text version offers no formats at all
    When I archive the text of bill "us-house:119:hr-3424"
    Then no text was archived and the reason is recorded
    And no error was raised

  Scenario: A bill that is not ingested yet cannot have its text archived
    When I archive the text of bill "us-house:119:hr-9999"
    Then archiving fails because the bill is unknown

  # --- idempotency: re-archiving unchanged text writes nothing new ------------

  Scenario: Re-archiving unchanged text uploads no second copy
    Given the text of bill "us-house:119:hr-3424" is already archived
    When I archive the text of bill "us-house:119:hr-3424"
    Then the report says no new upload was made
    And the bill "us-house:119:hr-3424" has 1 stored text file

  Scenario: Changed text is archived as a new file and the bill points at it
    Given the text of bill "us-house:119:hr-3424" is already archived
    And the bill text document has since changed
    When I archive the text of bill "us-house:119:hr-3424"
    Then the report says a new upload was made
    And the bill "us-house:119:hr-3424" has 2 stored text files
    And the bill "us-house:119:hr-3424" points at the newest text file

  Scenario: An archive whose bytes were never stored is repaired on re-run
    Given the bill "us-house:119:hr-3424" names a text file with no stored bytes
    When I archive the text of bill "us-house:119:hr-3424"
    Then the report says a new upload was made
    And the stored file's bytes are byte-for-byte the fetched document

  # --- the acceptance criterion, second half: the UI opens it via a presigned URL ---

  Scenario: The UI gets a short-lived presigned URL for an archived bill text
    Given the text of bill "us-house:119:hr-3424" is already archived
    When the UI requests the text of bill "us-house:119:hr-3424"
    Then the response is 200
    And the response carries a presigned url
    And the response carries the sha256 of the archived bytes
    And the response's url expires within an hour

  Scenario: The presigned URL serves the archived bytes
    Given the text of bill "us-house:119:hr-3424" is already archived
    When the UI requests the text of bill "us-house:119:hr-3424"
    And I fetch the presigned url
    Then the fetched bytes hash to the sha256 in the response

  Scenario: The response names the primary source so a voter can check it themselves
    Given the text of bill "us-house:119:hr-3424" is already archived
    When the UI requests the text of bill "us-house:119:hr-3424"
    Then the response carries the congress.gov source url
    And the response names the captured text version

  Scenario: A bill with no archived text answers 404, not an empty url
    When the UI requests the text of bill "us-house:119:hr-3424"
    Then the response is 404

  Scenario: An unknown bill answers 404
    When the UI requests the text of bill "us-house:119:hr-0"
    Then the response is 404

  # --- provenance -------------------------------------------------------------

  Scenario: Archiving bill text writes a provenance entry naming the document
    When I archive the text of bill "us-house:119:hr-3424"
    Then a provenance entry exists for the bill text file
    And that provenance entry's source url is the congress.gov document url
    And that provenance entry's content hash is the archived sha256
