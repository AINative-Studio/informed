# Stories 14.2, 14.3 and 14.4 — the backend half of three privacy stories.
#
# 14.2 Consent at onboarding (3 pts, Must, Iteration 2)
#   "As a voter, I want to give explicit consent before my political interests
#   are saved, so that I control my data."
#   Acceptance: GDPR special-category consent recorded; no save without it;
#   anonymous mode is offered as the alternative.
#
#   Story 4.1 already built the *gate* (`consent: true` required on
#   `POST /v1/profiles`, 422 otherwise) and deliberately deferred the *record*
#   to this story — see `app.profiles.router`'s "Why `consent` is a required
#   field" section. What 14.2 adds is the auditable record: which consent
#   text, at which version, agreed at which time, append-only.
#
# 14.3 Anonymous mode (5 pts, Must, Iteration 8)
#   "As a privacy-conscious voter, I want to use the full app without an
#   account, so that nothing about me is stored."
#   Acceptance: The profile lives in the browser only; `/v1/alignment` accepts
#   it inline; no server rows are written.
#
#   `/v1/alignment` is Epic 6 and does not exist yet, so the inline-scoring
#   half of that criterion is NOT claimed here. What this file proves is the
#   part that is buildable today and is the actual privacy guarantee: an
#   anonymous voter writes NO row of any kind — not a profile, not a key, and
#   (new in 14.2) not a consent record either — and the public reference data
#   they need is readable with no identity at all. The browser-only half is
#   proven end to end in frontend/tests/features/anonymous_mode.feature.
#
# 14.4 Export and delete (3 pts, Must, Iteration 8)
#   "As a voter, I want to export or permanently delete my profile in one
#   action, so that I stay in control."
#   Acceptance: Export returns JSON; delete removes the row and shreds the
#   key; completes in under 1 minute.
#
# Every interest here is obviously synthetic ("test-topic-alpha", not a real
# political issue). CLAUDE.md: never put real voter data in fixtures, logs or
# test output.

Feature: Consent, anonymous mode, export and delete (stories 14.2, 14.3, 14.4)
  A voter's political interests are special-category data. Three guarantees
  have to hold at the HTTP boundary: nothing is stored without a recorded,
  auditable consent; an anonymous voter leaves no row anywhere; and a voter
  who wants their data out, or gone, gets it in one call.

  Background:
    Given a ZeroDB client backed by an in-memory fake

  # --- Story 14.2: consent is recorded, not just asserted -------------------

  Scenario: The consent language on offer is published before anything is saved
    When a voter asks what they are being asked to consent to
    Then the response names the current consent version
    And the consent text says the interests are political-opinion data
    And the consent text offers anonymous mode as the alternative

  Scenario: Saving with consent writes an auditable consent record
    Given a synthetic tiered selection with 1 primary interest
    When the voter submits the profile with consent given
    Then the profile is saved
    And a consent record exists for that voter
    And the consent record names the consent version the voter was shown
    And the consent record carries the time consent was given
    And the consent record names the lawful basis as explicit consent

  Scenario: A consent record never contains the voter's interests or user id
    Given a synthetic tiered selection with 1 primary interest
    When the voter submits the profile with consent given
    Then the consent record contains no interest topic id
    And the consent record does not contain the voter's user id

  Scenario: No save without consent, and no consent record either
    Given a synthetic tiered selection with 1 primary interest
    When the voter submits the profile with consent refused
    Then the save is refused with a 422 naming consent
    And no profile row is written for that voter
    And no consent record exists for that voter

  Scenario: Consent for a stale version of the language is refused
    Given a synthetic tiered selection with 1 primary interest
    When the voter submits the profile consenting to an unknown consent version
    Then the save is refused with a 422 naming the consent version
    And no profile row is written for that voter

  Scenario: Re-consenting on a later day appends rather than overwriting
    Given a synthetic tiered selection with 1 primary interest
    When the voter submits the profile with consent given
    And the voter submits the profile with consent given again a day later
    Then two consent records exist for that voter
    And both consent records are still readable
    And the earlier consent record was not overwritten

  Scenario: Two saves in the same second record one consent event, not two
    Given a synthetic tiered selection with 1 primary interest
    When the voter submits the profile with consent given
    And the voter submits the profile with consent given again
    Then exactly 1 consent record exists for that voter
    And the profile is saved at version 2

  Scenario: Withdrawing consent by deleting the profile is itself recorded
    Given a synthetic tiered selection with 1 primary interest
    And the voter has submitted the profile with consent given
    When the voter deletes their profile
    Then a withdrawal record exists for that voter
    And the withdrawal record carries the time consent was withdrawn

  # --- Story 14.3: an anonymous voter writes nothing anywhere ---------------

  Scenario: An anonymous voter can read the consent language with no identity
    When an anonymous voter asks what they are being asked to consent to
    Then the response names the current consent version
    And no row of any kind was written

  Scenario: An anonymous save attempt is refused and writes nothing
    Given a synthetic tiered selection with 1 primary interest
    When an anonymous voter submits the profile with consent given
    Then the save is refused with a 422 naming anonymous mode
    And no row of any kind was written

  Scenario: Anonymous mode is held to the same tier limits as a saved profile
    Given a synthetic tiered selection with 6 primary interests
    Then validating that selection without storing it is refused, naming the primary limit

  Scenario: A valid anonymous selection validates without storing anything
    Given a synthetic tiered selection with 1 primary interest
    Then validating that selection without storing it is accepted
    And no row of any kind was written

  # --- Story 14.4: export ----------------------------------------------------

  Scenario: Export returns the voter's own profile as JSON
    Given a synthetic tiered selection with 2 primary, 1 secondary and 1 nice-to-have interests
    And the voter has submitted the profile with consent given
    When the voter exports their profile
    Then the export is JSON with a 200
    And the exported profile has the same interests, tiers and stances as saved
    And the export states which version was exported
    And the export states when it was produced

  Scenario: An export includes the consent record, so the audit trail travels with the data
    Given a synthetic tiered selection with 1 primary interest
    And the voter has submitted the profile with consent given
    When the voter exports their profile
    Then the export includes the consent history
    And the consent history in the export names the consent version

  Scenario: Exporting a never-saved voter's profile returns nothing, not an error
    When a voter with no saved profile exports their profile
    Then the export returns no profile, with a 404

  Scenario: One voter can never export another voter's profile
    Given a synthetic tiered selection with 1 primary interest
    And the voter has submitted the profile with consent given
    When a different voter exports their profile
    Then the export returns no profile, with a 404

  # --- Story 14.4: delete ----------------------------------------------------

  Scenario: Delete removes the row, shreds the key and is reported as done
    Given a synthetic tiered selection with 1 primary interest
    And the voter has submitted the profile with consent given
    When the voter deletes their profile
    Then the delete succeeds
    And the profile row is gone
    And the wrapped data key for that voter is gone
    And decrypting a retained copy of the ciphertext now fails

  Scenario: Delete completes well inside one minute
    Given a synthetic tiered selection with 1 primary interest
    And the voter has submitted the profile with consent given
    When the voter deletes their profile
    Then the delete took less than 60 seconds

  Scenario: Deleting twice is not an error
    Given a synthetic tiered selection with 1 primary interest
    And the voter has submitted the profile with consent given
    When the voter deletes their profile
    And the voter deletes their profile again
    Then the delete succeeds

  Scenario: Deleting a never-saved profile is not an error
    When a voter with no saved profile deletes their profile
    Then the delete succeeds

  Scenario: After deleting, loading and exporting both find nothing
    Given a synthetic tiered selection with 1 primary interest
    And the voter has submitted the profile with consent given
    When the voter deletes their profile
    And the voter loads their profile
    Then the load returns no profile, with a 200 rather than an error
    When the voter exports their profile
    Then the export returns no profile, with a 404

  Scenario: Deleting one voter's profile leaves another voter's intact
    Given a synthetic tiered selection with 1 primary interest
    And the voter has submitted the profile with consent given
    And a different voter has also submitted a profile with consent given
    When the voter deletes their profile
    Then the profile row is gone
    And the different voter's profile is still loadable
