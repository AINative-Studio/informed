Feature: House bills with sponsors, cosponsors and CRS subjects (story 5.3)
  As the system
  I want bills ingested with sponsors, cosponsors and subjects
  So that sponsorship counts as evidence

  Background:
    Given a ZeroDB client backed by an in-memory fake
    And the reference tables exist
    And every sponsor in the sample bill responses exists as a people row

  # --- the acceptance criterion: sponsors[] is populated -------------------

  Scenario: Ingesting a bill writes it under the documented id pattern
    When I ingest the sample bill "bill_with_cosponsors"
    Then the bill "us-house:119:hr-3424" is stored
    And the bill "us-house:119:hr-3424" has title "SPACE Act of 2025"
    And the bill "us-house:119:hr-3424" has introduced_on "2025-05-15"

  Scenario: The bill's own sponsor is in sponsors[] with role sponsor
    When I ingest the sample bill "bill_with_cosponsors"
    Then the bill "us-house:119:hr-3424" has 3 sponsors
    And the bill "us-house:119:hr-3424" has a sponsor "person:O000177" with role "sponsor"
    And the sponsor "person:O000177" on bill "us-house:119:hr-3424" has joined_on "2025-05-15"

  Scenario: Cosponsors are in the same sponsors[] array with role cosponsor
    When I ingest the sample bill "bill_with_cosponsors"
    Then the bill "us-house:119:hr-3424" has a sponsor "person:P000614" with role "cosponsor"
    And the sponsor "person:P000614" on bill "us-house:119:hr-3424" has joined_on "2025-06-05"
    And the bill "us-house:119:hr-3424" has a sponsor "person:L000599" with role "cosponsor"

  Scenario: An original cosponsor is distinguished from a later one
    When I ingest the sample bill "bill_original_cosponsor"
    Then the bill "us-house:119:hr-9999" has a sponsor "person:P000614" with role "original_cosponsor"

  Scenario: Sponsorship evidence is readable by sponsor, for story 6.3's blend
    When I ingest the sample bill "bill_with_cosponsors"
    Then 1 bill lists "person:O000177" as a sponsor
    And 1 bill lists "person:P000614" as a cosponsor

  # --- CRS subjects ----------------------------------------------------------

  Scenario: The CRS policy area is stored as a subject
    When I ingest the sample bill "bill_with_cosponsors"
    Then the bill "us-house:119:hr-3424" has subject "Government Operations and Politics"
    And the bill "us-house:119:hr-3424" has policy_area "Government Operations and Politics"

  Scenario: CRS legislative subject terms are stored as subjects
    When I ingest the sample bill "bill_with_cosponsors"
    Then the bill "us-house:119:hr-3424" has subject "Congressional oversight"
    And the bill "us-house:119:hr-3424" has subject "Government buildings, facilities, and property"

  Scenario: A bill with no cosponsors still ingests with its sponsor alone
    When I ingest the sample bill "bill_no_cosponsors"
    Then the bill "us-house:119:hr-1" is stored
    And the bill "us-house:119:hr-1" has 1 sponsors
    And the bill "us-house:119:hr-1" has policy_area "Economics and Public Finance"

  Scenario: A sponsor with no people row is skipped, not written with a guessed id
    When I ingest the sample bill "bill_with_cosponsors" with bioguide "P000614" missing from people
    Then the bill "us-house:119:hr-3424" has 2 sponsors
    And the bill "us-house:119:hr-3424" has no sponsor "person:P000614"

  # --- topic_ids and summary are later stories, left honestly empty --------

  Scenario: topic_ids is left empty because subject-to-topic mapping is a later story
    When I ingest the sample bill "bill_with_cosponsors"
    Then the bill "us-house:119:hr-3424" has no topic_ids

  # --- provenance ------------------------------------------------------------

  Scenario: Ingesting a bill writes a matching provenance_log entry
    When I ingest the sample bill "bill_with_cosponsors"
    Then a provenance_log entry exists for bills "us-house:119:hr-3424"
    And that provenance_log entry has adapter_version "house_bills/v1"

  # --- idempotency: the stated acceptance criterion -------------------------

  Scenario: Re-ingesting the same bill is an upsert, not a duplicate
    Given I have ingested the sample bill "bill_with_cosponsors"
    When I ingest the sample bill "bill_with_cosponsors"
    Then exactly 1 row carries the bill id "us-house:119:hr-3424"
    And the bill "us-house:119:hr-3424" has 3 sponsors

  Scenario: Re-ingesting an unchanged bill does not duplicate its provenance entry
    Given I have ingested the sample bill "bill_with_cosponsors"
    When I ingest the sample bill "bill_with_cosponsors"
    Then exactly 1 provenance_log row exists for bills "us-house:119:hr-3424"

  Scenario: A bill that gains a cosponsor upserts in place and records new provenance
    Given I have ingested the sample bill "bill_with_cosponsors"
    When the source adds a cosponsor to bill "us-house:119:hr-3424" and I ingest it again
    Then exactly 1 row carries the bill id "us-house:119:hr-3424"
    And the bill "us-house:119:hr-3424" has 4 sponsors
    And exactly 2 provenance_log rows exist for bills "us-house:119:hr-3424"

  # --- roll calls can now carry a verified bill_id (5.1 left it null) ------

  Scenario: A roll call on a bill that has been ingested carries its bill_id
    Given I have ingested the sample bill "bill_with_cosponsors"
    When I ingest the live-shaped House roll call "live_item_119_1_240" linked to ingested bills
    Then the roll call "us-house:119-1:240" has bill_id "us-house:119:hr-3424"

  Scenario: A roll call on a bill that has not been ingested leaves bill_id null
    When I ingest the live-shaped House roll call "live_item_119_1_240" linked to ingested bills
    Then the roll call "us-house:119-1:240" has no bill_id
