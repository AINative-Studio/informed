Feature: Archive debates and questionnaires (story 9.2)
  As the system
  I want debate transcripts and candidate questionnaires ingested and archived
  So that promises made outside campaign websites count

  # Story 9.1 archives campaign websites under source_type "campaign_website".
  # This story adds the other source types prd.md's "Collect promises" step
  # names, so a promise made in a questionnaire answer or spoken on the record
  # is as citable as one published on a campaign site. The acceptance criterion
  # is "at least 2 source types beyond websites; each is archived with
  # provenance" — so every scenario below is written per source type, and a
  # scenario at the end asserts the count of distinct non-website types.

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist
    And a candidacy "us-2026-11-03-general:ocd-division/country:us/state:ga:person:bioguide-O000175" exists

  # --- source type 1: candidate questionnaires --------------------------------

  Scenario: Archiving a questionnaire stores the fetched document with its sha256
    When I archive the sample questionnaire for that candidacy
    Then a document file is stored with the questionnaire's own sha256
    And downloading the archived document returns the fetched bytes exactly
    And the downloaded document hashes to the sha256 recorded on its file

  Scenario: Archiving a questionnaire stores an extracted-text file for promise extraction
    When I archive the sample questionnaire for that candidacy
    Then a text file is stored alongside the document
    And the stored text contains the questionnaire's answer wording
    And the stored text carries no HTML markup

  Scenario: Archiving a questionnaire writes a promise_sources row keyed by its sha256
    When I archive the sample questionnaire for that candidacy
    Then a promise_sources row exists with id "src:{document_sha256}"
    And that promise_sources row references the candidacy
    And that promise_sources row has source_type "candidate_questionnaire"
    And that promise_sources row has the questionnaire's own url
    And that promise_sources row has a captured_at timestamp

  Scenario: Archiving a questionnaire records a provenance_log entry
    When I archive the sample questionnaire for that candidacy
    Then a provenance_log entry exists for promise_sources "src:{document_sha256}"
    And that provenance_log entry has source_url equal to the questionnaire's own url
    And that provenance_log entry has adapter_version "debates_questionnaires/v1"
    And that provenance_log entry has the questionnaire's own content hash

  # --- source type 2: debate transcripts --------------------------------------

  Scenario: Archiving a debate transcript stores the fetched document with its sha256
    When I archive the sample debate transcript for that candidacy
    Then a document file is stored with the transcript's own sha256
    And downloading the archived document returns the fetched bytes exactly

  Scenario: Archiving a debate transcript writes a promise_sources row with its own source type
    When I archive the sample debate transcript for that candidacy
    Then a promise_sources row exists with source_type "debate_transcript"
    And that promise_sources row references the candidacy
    And that promise_sources row has the transcript's own url

  Scenario: Archiving a debate transcript records a provenance_log entry
    When I archive the sample debate transcript for that candidacy
    Then a provenance_log entry exists for that transcript's promise_sources row
    And that provenance_log entry has source_url equal to the transcript's own url
    And that provenance_log entry has adapter_version "debates_questionnaires/v1"

  # --- the acceptance criterion, stated as a count ----------------------------

  Scenario: At least two source types beyond campaign websites are archived
    Given a campaign website has already been archived for that candidacy
    When I archive the sample questionnaire for that candidacy
    And I archive the sample debate transcript for that candidacy
    Then the promise_sources rows for that candidacy carry at least 2 source types other than "campaign_website"
    And every one of those rows has a provenance_log entry

  # --- what the archived text is FOR: story 9.3's extractor -------------------

  # prd.md step 2 reads promises out of an archived source. A capture whose text
  # cannot be read back is not usable evidence, so this is asserted end to end
  # through the same repository the extraction pipeline uses, rather than
  # trusted because the upload returned 200.

  Scenario: An archived questionnaire is readable as the text promise extraction consumes
    When I archive the sample questionnaire for that candidacy
    Then the archived source's text can be read back from its promise_sources row
    And that text is the same text that was stored

  # --- idempotency: re-archiving the same document ----------------------------

  Scenario: Archiving the same unchanged questionnaire twice writes exactly one promise_sources row
    Given I have already archived the sample questionnaire for that candidacy once
    When I archive the sample questionnaire for that candidacy again
    Then exactly 1 promise_sources row exists for that candidacy
    And no new file is uploaded by the second capture

  Scenario: Re-archiving a capture whose stored document is missing repairs it
    Given I have already archived the sample questionnaire for that candidacy once
    And that capture's archived document has gone missing from storage
    When I archive the sample questionnaire for that candidacy again
    Then the files are re-uploaded
    And downloading the archived document returns the fetched bytes exactly
    And exactly 1 promise_sources row exists for that candidacy

  Scenario: A genuinely changed questionnaire produces a second, distinct promise_sources row
    Given I have already archived the sample questionnaire for that candidacy once
    When I archive a changed version of the questionnaire for that candidacy
    Then 2 promise_sources rows exist for that candidacy

  # --- no invented sources, and no invented source types ----------------------

  # The same posture story 9.1 took about campaign-site discovery: there is no
  # automated discovery of a candidate's questionnaire page or debate
  # transcript, so a missing url is the caller's mistake and must never be
  # guessed at. An uncitable promise_sources row is worse than a missing one.

  Scenario: The loader requires an explicit url and never invents one
    When I try to archive a questionnaire for that candidacy with no url
    Then the archival is refused for having no url to capture

  Scenario: The loader refuses a source type it does not know
    When I try to archive a source for that candidacy with source type "vibes"
    Then the archival is refused for an unrecognised source type

  Scenario: The loader refuses a document that carries no extractable text
    When I try to archive a questionnaire whose page has no readable text
    Then the archival is refused for having no extractable text

  # A fetch that fails must fail the capture, not write a row claiming an
  # archive that does not exist.

  Scenario: A failed fetch writes no promise_sources row at all
    Given the questionnaire url cannot be fetched
    When I try to archive the sample questionnaire for that candidacy
    Then the archival is refused for being unfetchable
    And no promise_sources row exists for that candidacy

  # --- the reference check: a promise source must cite a real candidacy -------

  Scenario: A capture for a candidacy that does not exist is refused
    When I try to archive the sample questionnaire for a candidacy that does not exist
    Then the archival is refused for an unresolvable candidacy
