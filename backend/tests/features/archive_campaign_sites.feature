Feature: Archive campaign sites (story 9.1)
  As the system
  I want each candidate's campaign site captured once per election cycle and archived
  So that promises stay citable after sites change

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist
    And a candidacy "us-2026-11-03-general:ocd-division/country:us/state:pa/cd:2:person:fec-H6PA02205" exists

  # --- the acceptance criterion: stored as HTML and PDF, with sha256, and a promise_sources row ---

  Scenario: Archiving a campaign site stores the page as an HTML file with its sha256
    When I archive the sample campaign site for that candidacy
    Then an HTML file is stored with the campaign site's own sha256

  Scenario: Archiving a campaign site stores a PDF rendering of the captured content
    When I archive the sample campaign site for that candidacy
    Then a PDF file is stored with its own sha256

  # Story 9.1 shipped with the metadata half of this criterion only: the file
  # rows existed and carried the right sha256, but the bytes were never stored,
  # so every presigned download 404ed. Archiving is only citable if the archived
  # content can be read back.

  Scenario: The archived HTML can be read back byte-for-byte
    When I archive the sample campaign site for that candidacy
    Then downloading the archived HTML returns the captured page's exact bytes
    And the downloaded HTML hashes to the sha256 recorded on its file

  Scenario: The archived PDF can be read back byte-for-byte
    When I archive the sample campaign site for that candidacy
    Then downloading the archived PDF returns a real PDF
    And the downloaded PDF hashes to the sha256 recorded on its file

  Scenario: Archiving a campaign site writes a promise_sources row keyed by its sha256
    When I archive the sample campaign site for that candidacy
    Then a promise_sources row exists with id "src:{html_sha256}"
    And that promise_sources row references the candidacy
    And that promise_sources row has source_type "campaign_website"
    And that promise_sources row has the campaign site's own url
    And that promise_sources row has a captured_at timestamp

  Scenario: Archiving a campaign site records a provenance_log entry
    When I archive the sample campaign site for that candidacy
    Then a provenance_log entry exists for promise_sources "src:{html_sha256}"
    And that provenance_log entry has source_url equal to the campaign site's own url
    And that provenance_log entry has adapter_version "campaign_sites/v1"

  # --- "once per election cycle": idempotency ---------------------------------

  Scenario: Archiving the same unchanged site twice writes exactly one promise_sources row
    Given I have already archived the sample campaign site for that candidacy once
    When I archive the sample campaign site for that candidacy again
    Then exactly 1 promise_sources row exists for that candidacy

  Scenario: Archiving the same unchanged site twice does not re-upload the files
    Given I have already archived the sample campaign site for that candidacy once
    When I archive the sample campaign site for that candidacy again
    Then no new file is uploaded by the second capture

  # A re-run must MEND a capture whose content was never stored, not treat the
  # surviving row as proof the archive is intact. Every capture made before the
  # file-upload retrievability fix is in exactly this state.

  Scenario: Re-archiving a capture whose stored content is missing repairs it
    Given I have already archived the sample campaign site for that candidacy once
    And that capture's archived content has gone missing from storage
    When I archive the sample campaign site for that candidacy again
    Then the files are re-uploaded
    And downloading the archived HTML returns the captured page's exact bytes
    And exactly 1 promise_sources row exists for that candidacy

  Scenario: A genuinely changed site produces a second, distinct promise_sources row
    Given I have already archived the sample campaign site for that candidacy once
    When I archive a changed version of the campaign site for that candidacy
    Then 2 promise_sources rows exist for that candidacy

  # --- Internet Archive Save Page Now: best-effort, never required ------------

  Scenario: Archiving succeeds even when Internet Archive's Save Page Now is unavailable
    Given Internet Archive's Save Page Now API is unreachable
    When I archive the sample campaign site for that candidacy
    Then an HTML file is stored with the campaign site's own sha256
    And the capture report notes the Wayback submission failed

  Scenario: A successful Save Page Now submission is recorded on the capture report
    Given Internet Archive's Save Page Now API accepts the submission
    When I archive the sample campaign site for that candidacy
    Then the capture report carries the archive.org snapshot url

  # --- candidate website discovery: no automated discovery is claimed ---------

  Scenario: The loader requires an explicit url and never invents one
    When I try to archive a campaign site for that candidacy with no url
    Then the archival is refused for having no url to capture
