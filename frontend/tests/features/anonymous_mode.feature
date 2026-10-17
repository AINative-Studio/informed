# Story 14.3 — Anonymous mode (5 pts, Must, Iteration 8), plus the UI half of
# story 14.4 (export and delete).
#
# 14.3: "As a privacy-conscious voter, I want to use the full app without an
# account, so that nothing about me is stored."
# Acceptance: The profile lives in the browser only; `/v1/alignment` accepts it
# inline; no server rows are written.
#
# WHAT THIS FILE PROVES, AND WHAT IT DOES NOT
# ===========================================
#
# `/v1/alignment` is Epic 6's endpoint and does not exist yet, so "accepts it
# inline" cannot be driven against a real scorer in this story — see
# `frontend/src/api/profiles.ts`'s `saveAnonymousProfile` docstring and the
# report for story 14.3's honest scoping. What IS provable today, and is the
# actual substance of the acceptance criterion's other two clauses, is that
# the WHOLE profile flow works in anonymous mode with zero writes leaving the
# browser:
#
#   pick topics -> pick stances -> set tiers -> mark a dealbreaker -> save
#
# ...completes, survives a reload, and issues NO POST, PUT or PATCH and no
# DELETE to any profile endpoint. The network assertion is the point of the
# scenarios below: a counter over `page.on('request')` filtered to
# non-GET requests, asserted to be empty, is a claim a future refactor cannot
# quietly break (adding a telemetry POST would fail these scenarios).
#
# Public reference data (topics, stances, the tier weights, the consent text)
# IS fetched, by GET, with no identity attached. That is not a contradiction:
# those rows describe the taxonomy, not the voter, and an anonymous voter who
# could not read them would have a degraded path rather than ui-prd.md's "real,
# equally-functional" one.

Feature: Anonymous mode, export and delete (stories 14.3, 14.4)
  As a privacy-conscious voter
  I want to use the full profile editor without an account, and to export or
  erase an account profile in one action
  So that nothing about me is stored unless I choose it, and I can undo that
  choice at any time

  Background:
    Given a voter opens the Informed app

  # --- Story 14.3: the full flow, browser-only -------------------------------

  Scenario: Anonymous mode is offered alongside consent, not buried
    When they switch to the full profile editor
    Then the editor offers anonymous mode as an alternative to saving to an account

  Scenario: The whole profile flow works anonymously with no writes to the server
    When they switch to the full profile editor
    And they choose anonymous mode
    And they assign "Prescription drug pricing" to primary with a stance
    And they mark it as a dealbreaker
    And they assign "Broadband access" to secondary with a stance
    And they save anonymously
    Then they see a confirmation that their profile was saved in this browser only
    And no request that could write to the server was made

  Scenario: An anonymously saved profile is readable from this browser after a reload
    When they switch to the full profile editor
    And they choose anonymous mode
    And they assign "Prescription drug pricing" to primary with a stance
    And they save anonymously
    And they reload the page and switch to the full profile editor
    And they choose anonymous mode
    Then "Prescription drug pricing" already shows as primary
    And no request that could write to the server was made

  # A complete, valid profile first: "no consent is required" is only a
  # meaningful claim about a save that is otherwise ready to go. Without a
  # primary interest the save button is disabled for a different reason
  # entirely, and the scenario would pass while proving nothing.
  Scenario: Anonymous mode never asks for consent to store, because nothing is stored
    When they switch to the full profile editor
    And they choose anonymous mode
    And they assign "Prescription drug pricing" to primary with a stance
    Then no account consent checkbox is required before saving

  Scenario: Clearing an anonymous profile leaves nothing in this browser
    When they switch to the full profile editor
    And they choose anonymous mode
    And they assign "Prescription drug pricing" to primary with a stance
    And they save anonymously
    And they clear their anonymous profile
    Then nothing of their profile remains in this browser
    And no request that could write to the server was made

  # --- Story 14.4: export and delete, from the account path ------------------

  Scenario: A voter can export their saved profile from the editor
    Given they have already saved a profile with "Medicare" as a primary dealbreaker
    When they switch to the full profile editor
    And they export their profile
    Then they are offered their profile as a JSON download

  Scenario: Deleting asks for confirmation first, because it cannot be undone
    Given they have already saved a profile with "Medicare" as a primary dealbreaker
    When they switch to the full profile editor
    And they ask to delete their profile
    Then they are warned the deletion is permanent and asked to confirm
    And no delete request has been sent yet

  Scenario: Confirming the deletion erases the profile
    Given they have already saved a profile with "Medicare" as a primary dealbreaker
    When they switch to the full profile editor
    And they ask to delete their profile
    And they confirm the deletion
    Then they are told their profile and its key were permanently deleted

  Scenario: Cancelling the deletion changes nothing
    Given they have already saved a profile with "Medicare" as a primary dealbreaker
    When they switch to the full profile editor
    And they ask to delete their profile
    And they cancel the deletion
    Then no delete request has been sent yet
    And "Medicare" already shows as primary with the dealbreaker checked
