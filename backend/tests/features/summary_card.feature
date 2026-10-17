# Story 14.6 — Shareable summary card
#
# "As a voter, I want to share a candidate summary without revealing my
# interests, so that I can discuss it safely."
#
# Acceptance: The card contains candidate data only; a test asserts no
# profile fields leak.
#
# datamodel.md's `candidate_cards` read model is specified as
# {person_name, party, district, photo_url, verdict_counts, say_do_gap,
# positions_summary, updated_at}, rebuilt from events (verdict.published,
# positions.recomputed) that no story has published yet (epics 6, 7, 11 are
# not built). This story builds a MINIMAL version from data that genuinely
# exists today — a candidacy's own person_name/party/district, from story
# 2.2's legislators and 2.6's candidacies — with the not-yet-computed fields
# honestly absent rather than fabricated.
#
# The endpoint takes NO profile_id/user_id parameter at all, by design: its
# response is identical regardless of who calls it, which is what makes "no
# profile field can leak" a structural guarantee rather than a policy one.

Feature: Shareable candidate summary card
  A voter can get a summary of one candidacy to share and discuss, built
  entirely from public candidate data. The endpoint accepts no voter
  identity of any kind, so there is nothing a profile could leak through.

  Background:
    Given a candidacy store seeded with real reference data

  Scenario: A summary card contains only candidate data
    Given a candidacy "election:district:person" for "Pat Rivera", party "IND", in a known district
    When a voter requests the summary card for that candidacy
    Then the card shows the candidate's name, party and district
    And the card's not-yet-computed fields are honestly absent, not fabricated
    And the card contains no field named profile_id, user_id or interests

  Scenario: A profile field cannot leak through the summary-card endpoint
    Given a voter profile exists with primary interests set
    And a candidacy "election:district:person" for "Pat Rivera", party "IND", in a known district
    When a voter requests the summary card for that candidacy, however they try to pass their profile
    Then the summary card response contains zero trace of the voter's interests
    And the summary-card endpoint has no request parameter that accepts a profile or user id

  Scenario: Requesting a summary card for an unknown candidacy is a clear 404
    When a voter requests the summary card for a candidacy that does not exist
    Then the response is a 404 naming the candidacy, not a 500

  Scenario: The summary card never recommends or scores
    Given a candidacy "election:district:person" for "Pat Rivera", party "IND", in a known district
    When a voter requests the summary card for that candidacy
    Then the card contains no alignment score and no recommendation
