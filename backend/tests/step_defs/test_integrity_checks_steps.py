"""Step definitions for tests/features/integrity_checks.feature (16.2).

Runs against story 1.2's in-memory fake, with `break_server_side_filter()`
applied — the same posture `test_provenance_enforcement_steps.py` takes,
because the checks here page through tables with `ZeroDBClient.query_rows`
and `get_row`, both of which rest on the client's paging fallback working
under the live filter bug (datamodel.md "Open questions", story 1.4).

Several scenarios deliberately write rows that bypass the repository layer's
own validation (`client.insert_row(...)` directly, or a hand-built second
"current" row) — this is sabotage testing: the whole point of a nightly
integrity audit is to catch data that got into a bad state some OTHER way
(a torn write, a bug in a future adapter, a direct fix someone ran by hand),
so the fixtures for "catches X" scenarios must produce that bad state through
a path the write-time repository guards do NOT cover.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper
from app.integrity.checks import (
    CheckReport,
    CombinedReport,
    check_archive_retrievability,
    check_current_verdict_uniqueness,
    check_member_votes_drift,
    check_provenance_coverage,
    check_references,
    check_verdict_roll_call_mapping,
    run_all_checks,
)
from app.repositories import Repository
from app.repositories.models import (
    Candidacy,
    CandidatePosition,
    MemberVote,
    Person,
    PromiseSource,
    RollCall,
    Verdict,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/integrity_checks.feature")

ADAPTER = "test_adapter/v1"


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values (reports, ids) between Given/When and Then steps."""
    return {}


# --- background --------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake", target_fixture="integrity_client")
def integrity_client(zerodb: ZeroDBClient) -> ZeroDBClient:
    return zerodb


@given("the reference tables exist")
def reference_tables(zerodb: ZeroDBClient) -> None:
    Bootstrapper(zerodb, pace=lambda: None).run()


# --- fixture rows: roll_calls, people, member_votes, candidacies ------------


def _roll_call_row(roll_call_id: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": roll_call_id,
        "voted_at": "2026-03-11T14:22:00-04:00",
        "question": "On Motion to Suspend the Rules and Pass",
        "vote_type": "final_passage",
        "result": "Passed",
        "topic_ids": [],
        "is_bundled": False,
    }
    row.update(overrides)
    return row


def _ensure_bill(zerodb: ZeroDBClient, bill_id: str) -> None:
    """A minimal `bills` row so a `roll_calls.bill_id` reference resolves.

    `bills` ingestion is not this story's concern (no story has written a
    `Bill` model yet), so the row is written directly through the client
    rather than through a repository this feature has no model for — the
    same posture every other fixture helper here takes toward a table outside
    its own scope (see `_election_and_district`).
    """
    if zerodb.get_row("bills", bill_id) is None:
        zerodb.insert_row("bills", {"id": bill_id, "title": "Test Bill", "body_id": "us-house"})


@given(parsers.parse('a roll_calls row "{roll_call_id}"'))
def roll_call_row_plain(zerodb: ZeroDBClient, roll_call_id: str) -> None:
    Repository(client=zerodb, model=RollCall).upsert(_roll_call_row(roll_call_id))


@given(
    parsers.parse(
        'a roll_calls row "{roll_call_id}" with vote_type "{vote_type}" and bill_id "{bill_id}"'
    )
)
def roll_call_row_with_fields(
    zerodb: ZeroDBClient, roll_call_id: str, vote_type: str, bill_id: str
) -> None:
    _ensure_bill(zerodb, bill_id)
    Repository(client=zerodb, model=RollCall).upsert(
        _roll_call_row(roll_call_id, vote_type=vote_type, bill_id=bill_id)
    )


@given(parsers.parse('the roll_calls row "{roll_call_id}" is corrected to vote_type "{vote_type}"'))
def correct_roll_call_vote_type(zerodb: ZeroDBClient, roll_call_id: str, vote_type: str) -> None:
    zerodb.update_row(RollCall.table_name, roll_call_id, {"vote_type": vote_type})


@given(parsers.parse('the roll_calls row "{roll_call_id}" is corrected to bill_id "{bill_id}"'))
def correct_roll_call_bill_id(zerodb: ZeroDBClient, roll_call_id: str, bill_id: str) -> None:
    _ensure_bill(zerodb, bill_id)
    zerodb.update_row(RollCall.table_name, roll_call_id, {"bill_id": bill_id})


@given(
    parsers.parse(
        'the roll_calls row "{roll_call_id}" is corrected to vote_type "{vote_type}" '
        'and bill_id "{bill_id}"'
    )
)
def correct_roll_call_both(
    zerodb: ZeroDBClient, roll_call_id: str, vote_type: str, bill_id: str
) -> None:
    _ensure_bill(zerodb, bill_id)
    zerodb.update_row(
        RollCall.table_name, roll_call_id, {"vote_type": vote_type, "bill_id": bill_id}
    )


@given(parsers.parse('a people row "{person_id}"'))
def people_row(zerodb: ZeroDBClient, person_id: str) -> None:
    Repository(client=zerodb, model=Person).upsert(
        {"id": person_id, "canonical_name": "Test Person"}
    )


@given(parsers.parse('a member_votes row for roll call "{roll_call_id}" and person "{person_id}"'))
def member_votes_row_plain(zerodb: ZeroDBClient, roll_call_id: str, person_id: str) -> None:
    Repository(client=zerodb, model=MemberVote).upsert(
        {
            "roll_call_id": roll_call_id,
            "person_id": person_id,
            "position": "yea",
            "voted_at": "2026-03-11T14:22:00-04:00",
            "vote_type": "final_passage",
        }
    )


@given(
    parsers.parse(
        'a member_votes row for roll call "{roll_call_id}" and person "{person_id}" '
        "copying its denormalized fields"
    )
)
def member_votes_row_copying(zerodb: ZeroDBClient, roll_call_id: str, person_id: str) -> None:
    roll_call = zerodb.get_row(RollCall.table_name, roll_call_id)
    assert roll_call is not None, f"no roll_calls row {roll_call_id!r} to copy from"
    row_data = roll_call.row_data
    Repository(client=zerodb, model=MemberVote).upsert(
        {
            "roll_call_id": roll_call_id,
            "person_id": person_id,
            "position": "yea",
            "voted_at": row_data["voted_at"],
            "vote_type": row_data["vote_type"],
            "bill_id": row_data.get("bill_id"),
            "topic_ids": row_data.get("topic_ids", []),
            "is_bundled": row_data.get("is_bundled", False),
        }
    )


@given(
    parsers.parse(
        'a member_votes row is written directly with a dangling roll_call_id "{roll_call_id}"'
    )
)
def dangling_member_vote(zerodb: ZeroDBClient, roll_call_id: str) -> None:
    """Bypasses the repository entirely: `Repository.upsert` would refuse
    this (datamodel.md rule 5), so to prove the AUDIT catches a dangling
    reference that reached storage some other way, the row is written
    straight through the client.
    """
    zerodb.insert_row(
        MemberVote.table_name,
        {
            "id": f"{roll_call_id}:B000944",
            "roll_call_id": roll_call_id,
            "person_id": "person:B000944",
            "position": "yea",
            "voted_at": "2026-03-11T14:22:00-04:00",
            "vote_type": "final_passage",
            "bill_id": None,
            "topic_ids": [],
            "is_bundled": False,
        },
    )


def _election_and_district(zerodb: ZeroDBClient) -> tuple[str, str]:
    """A minimal elections+districts pair so a candidacies row's OTHER
    references resolve, leaving person_id as the only dangling one under
    test. Written directly since neither model is this feature's concern.
    """
    election_id = "us-2026-11-03-general"
    district_id = "ocd-division/country:us/state:zz/cd:1"
    if zerodb.get_row("jurisdictions", "us-federal") is None:
        zerodb.insert_row(
            "jurisdictions",
            {
                "id": "us-federal",
                "name": "United States",
                "country_code": "US",
                "level": "federal",
                "adapter_name": "us_federal",
                "active": True,
            },
        )
    if zerodb.get_row("elections", election_id) is None:
        zerodb.insert_row(
            "elections",
            {
                "id": election_id,
                "jurisdiction_id": "us-federal",
                "name": "2026 General",
                "election_date": "2026-11-03",
                "kind": "general",
            },
        )
    if zerodb.get_row("bodies", "us-house") is None:
        zerodb.insert_row(
            "bodies",
            {
                "id": "us-house",
                "jurisdiction_id": "us-federal",
                "name": "House",
                "chamber": "lower",
                "seats": 435,
            },
        )
    if zerodb.get_row("districts", district_id) is None:
        zerodb.insert_row(
            "districts",
            {
                "id": district_id,
                "body_id": "us-house",
                "name": "Test District",
                "state": "ZZ",
                "valid_from": "2025-01-03",
                "valid_to": None,
            },
        )
    return election_id, district_id


@given(parsers.parse('a candidacies row for person "{person_id}"'))
def candidacies_row(zerodb: ZeroDBClient, person_id: str) -> None:
    election_id, district_id = _election_and_district(zerodb)
    Repository(client=zerodb, model=Candidacy).upsert(
        {
            "election_id": election_id,
            "district_id": district_id,
            "person_id": person_id,
            "person_name": "Test Candidate",
            "status": "filed",
        }
    )


@given(
    parsers.parse('a candidacies row is written directly with a dangling person_id "{person_id}"')
)
def dangling_candidacy(zerodb: ZeroDBClient, person_id: str) -> None:
    election_id, district_id = _election_and_district(zerodb)
    zerodb.insert_row(
        Candidacy.table_name,
        {
            "id": f"{election_id}:{district_id}:{person_id}",
            "election_id": election_id,
            "district_id": district_id,
            "person_id": person_id,
            "person_name": "Ghost Candidate",
            "party": None,
            "is_incumbent": False,
            "status": "filed",
        },
    )


# --- fixture rows: promises, verdicts, candidate_positions ------------------


@given(
    parsers.parse('a promise "{promise_id}" with an approved mapping to roll call "{roll_call_id}"')
)
def promise_with_approved_mapping(zerodb: ZeroDBClient, promise_id: str, roll_call_id: str) -> None:
    zerodb.insert_row(
        "promises",
        {
            "id": promise_id,
            "person_id": "person:B000944",
            "mappings": [
                {
                    "bill_id": "bill:hr-1",
                    "roll_call_id": roll_call_id,
                    "effect": "supports",
                    "confidence": 0.9,
                    "qa_status": "approved",
                }
            ],
        },
    )


@given(
    parsers.parse(
        'a promise "{promise_id}" with an unapproved mapping to roll call "{roll_call_id}"'
    )
)
def promise_with_unapproved_mapping(
    zerodb: ZeroDBClient, promise_id: str, roll_call_id: str
) -> None:
    zerodb.insert_row(
        "promises",
        {
            "id": promise_id,
            "person_id": "person:B000944",
            "mappings": [
                {
                    "bill_id": "bill:hr-1",
                    "roll_call_id": roll_call_id,
                    "effect": "supports",
                    "confidence": 0.9,
                    "qa_status": "pending",
                }
            ],
        },
    )


@given(parsers.parse('a promise "{promise_id}" with no mappings'))
def promise_with_no_mappings(zerodb: ZeroDBClient, promise_id: str) -> None:
    zerodb.insert_row(
        "promises",
        {"id": promise_id, "person_id": "person:B000944", "mappings": []},
    )


@given(parsers.parse('a verdict for promise "{promise_id}" with value "{value}"'))
def verdict_for_promise(zerodb: ZeroDBClient, promise_id: str, value: str) -> None:
    """Written directly (not through `Repository.append_version`) because a
    verdict whose promise does not exist — one of this check's own sabotage
    scenarios — would be refused by the repository's reference check, and
    this step needs to express exactly that case too.
    """
    zerodb.insert_row(
        Verdict.table_name,
        {
            "id": f"{promise_id}:v1",
            "promise_id": promise_id,
            "person_id": "person:B000944",
            "verdict": value,
            "confidence": 0.8,
            "methodology_version": "v1",
            "qa_status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "is_current": True,
            "created_at": "2026-01-01T00:00:00Z",
        },
    )


@given(parsers.parse('a verdict "{promise_id}" is appended with value "{value}"'))
def verdict_appended(zerodb: ZeroDBClient, promise_id: str, value: str) -> None:
    zerodb.insert_row("promises", {"id": promise_id, "person_id": "person:B000944", "mappings": []})
    if zerodb.get_row(Person.table_name, "person:B000944") is None:
        Repository(client=zerodb, model=Person).upsert(
            {"id": "person:B000944", "canonical_name": "Test Person"}
        )
    Repository(client=zerodb, model=Verdict).append_version(
        {
            "promise_id": promise_id,
            "person_id": "person:B000944",
            "verdict": value,
            "confidence": 0.8,
            "methodology_version": "v1",
        }
    )


@given(
    parsers.parse(
        'a second current verdict row is force-written for promise "{promise_id}", '
        "bypassing append_version"
    )
)
def force_second_current_verdict(zerodb: ZeroDBClient, promise_id: str) -> None:
    zerodb.insert_row(
        Verdict.table_name,
        {
            "id": f"{promise_id}:v2",
            "promise_id": promise_id,
            "person_id": "person:B000944",
            "verdict": "broken",
            "confidence": 0.5,
            "methodology_version": "v1",
            "qa_status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "is_current": True,
            "created_at": "2026-02-01T00:00:00Z",
        },
    )


@given(
    parsers.parse(
        'every is_current flag for promise "{promise_id}" is force-cleared, '
        "bypassing append_version"
    )
)
def force_clear_is_current(zerodb: ZeroDBClient, promise_id: str) -> None:
    for row in zerodb.query_rows(Verdict.table_name, limit=500).rows:
        if row.row_data.get("promise_id") == promise_id:
            zerodb.update_row(Verdict.table_name, str(row.row_data["id"]), {"is_current": False})


@given(
    parsers.parse(
        'a candidate_position for person "{person_id}" and stance "{stance_id}" is appended'
    )
)
def candidate_position_appended(zerodb: ZeroDBClient, person_id: str, stance_id: str) -> None:
    Repository(client=zerodb, model=CandidatePosition).append_version(
        {
            "person_id": person_id,
            "stance_id": stance_id,
            "s_value": 0.5,
            "confidence": 0.8,
            "evidence_count": 3,
            "basis": "voting record",
            "methodology_version": "v1",
        },
        check_references=False,
    )


@given(
    parsers.parse(
        'a second current candidate_position row is force-written for person "{person_id}" and '
        'stance "{stance_id}", bypassing append_version'
    )
)
def force_second_current_position(zerodb: ZeroDBClient, person_id: str, stance_id: str) -> None:
    zerodb.insert_row(
        CandidatePosition.table_name,
        {
            "id": f"{person_id}:{stance_id}:v2",
            "person_id": person_id,
            "stance_id": stance_id,
            "s_value": -0.5,
            "confidence": 0.6,
            "evidence_count": 2,
            "basis": "voting record",
            "methodology_version": "v1",
            "is_current": True,
            "computed_at": "2026-02-01T00:00:00Z",
        },
    )


# --- fixture rows: provenance ------------------------------------------------


@given(
    "a roll_calls row with a source_url is written directly through the client, "
    "bypassing the repository"
)
def sourced_row_bypassing_repo(zerodb: ZeroDBClient, result: dict[str, Any]) -> None:
    row_id = "us-house:119-2:zzz-bypass"
    zerodb.insert_row(
        RollCall.table_name,
        {
            **_roll_call_row(row_id),
            "source_url": "https://api.congress.gov/v3/house-vote/119/2/zzz-bypass",
            "content_hash": "c" * 64,
        },
    )
    result["bypass_row_id"] = row_id


# --- running the checks -------------------------------------------------------


@when("I run the reference integrity check", target_fixture="check_result")
def run_references(zerodb: ZeroDBClient) -> CheckReport:
    return check_references(zerodb)


@when("I run the verdict roll-call mapping check", target_fixture="check_result")
def run_verdict_mapping(zerodb: ZeroDBClient) -> CheckReport:
    return check_verdict_roll_call_mapping(zerodb)


@when("I run the is_current uniqueness check", target_fixture="check_result")
def run_is_current(zerodb: ZeroDBClient) -> CheckReport:
    return check_current_verdict_uniqueness(zerodb)


@when("I run the member_votes drift check", target_fixture="check_result")
def run_drift_check(zerodb: ZeroDBClient) -> CheckReport:
    return check_member_votes_drift(zerodb)


@when("I run the provenance coverage check", target_fixture="check_result")
def run_provenance_check(zerodb: ZeroDBClient) -> CheckReport:
    return check_provenance_coverage(zerodb)


@when("I run the archive retrievability check", target_fixture="check_result")
def run_archive_check(zerodb: ZeroDBClient) -> CheckReport:
    return check_archive_retrievability(zerodb)


@when("I run every integrity check", target_fixture="combined_result")
def run_combined(zerodb: ZeroDBClient) -> CombinedReport:
    return run_all_checks(zerodb)


# --- check 6: archive retrievability ----------------------------------------
#
# A `promise_sources` row's id IS its capture's sha256, so these fixtures write
# the row through the repository and then manipulate the fake's file store to
# reproduce each broken state. That is sabotage testing in the same sense as
# the scenarios above: the bad states below are what a half-working upload
# produces, which no write-time guard caught.

_ARCHIVED_HTML = b"<html><body><p>I will cap insulin at $35.</p></body></html>"


def _promise_source_with_archive(
    zerodb: ZeroDBClient,
    fake: FakeZeroDBTransport,
    result: dict[str, Any],
    *,
    store_bytes: bytes | None,
    file_id: str | None = None,
) -> str:
    """Write a `promise_sources` row, then set its archive's stored state.

    `store_bytes=None` leaves the file id with no stored object — the exact
    state the pre-fix `upload_file` produced.
    """
    _seed_candidacy(zerodb)
    archived_file_id = file_id if file_id is not None else str(uuid.uuid4())
    source_id = f"src:{hashlib.sha256(_ARCHIVED_HTML).hexdigest()}"
    if archived_file_id:
        fake.files[archived_file_id] = {
            "file_id": archived_file_id,
            "file_key": f"promise-sources/{source_id}.html",
            "file_name": f"{source_id}.html",
            "content_type": "text/html; charset=utf-8",
            "file_metadata": {"sha256": hashlib.sha256(_ARCHIVED_HTML).hexdigest()},
        }
        if store_bytes is not None:
            fake.file_content[archived_file_id] = store_bytes
    Repository(client=zerodb, model=PromiseSource).upsert(
        {
            "id": source_id,
            "candidacy_id": ARCHIVE_CANDIDACY_ID,
            "source_type": "campaign_website",
            "url": "https://arriaga-for-congress.example/",
            "archived_file_id": archived_file_id,
            "captured_at": "2026-10-16T22:00:00+00:00",
        }
    )
    result["archive_row_id"] = source_id
    return source_id


ARCHIVE_CANDIDACY_ID = (
    "us-2026-11-03-general:ocd-division/country:us/state:pa/cd:2:person:fec-H6PA02205"
)


def _seed_candidacy(zerodb: ZeroDBClient) -> None:
    """The reference `PromiseSource.candidacy_id` must resolve to."""
    from app.repositories.models import District, Election

    Repository(client=zerodb, model=Election).upsert(
        {
            "id": "us-2026-11-03-general",
            "jurisdiction_id": "us-federal",
            "name": "2026 federal general election",
            "election_date": "2026-11-03",
            "kind": "general",
        }
    )
    Repository(client=zerodb, model=District).upsert(
        {
            "id": "ocd-division/country:us/state:pa/cd:2",
            "body_id": "us-house",
            "name": "PA congressional district 2",
            "state": "PA",
            "valid_from": "2025-01-03",
        }
    )
    Repository(client=zerodb, model=Person).upsert(
        {
            "id": "person:fec-H6PA02205",
            "canonical_name": "ARRIAGA, JESSICA",
            "external_ids": {"fec": "H6PA02205"},
            "photo_url": None,
            "terms": [],
        }
    )
    Repository(client=zerodb, model=Candidacy).upsert(
        {
            "id": ARCHIVE_CANDIDACY_ID,
            "election_id": "us-2026-11-03-general",
            "district_id": "ocd-division/country:us/state:pa/cd:2",
            "person_id": "person:fec-H6PA02205",
            "person_name": "ARRIAGA, JESSICA",
            "party": "REP",
            "is_incumbent": False,
            "status": "filed",
        }
    )


@given("a promise_sources row whose archived file was stored with its bytes")
def archive_healthy(
    zerodb: ZeroDBClient, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    _promise_source_with_archive(zerodb, fake, result, store_bytes=_ARCHIVED_HTML)


@given("a promise_sources row whose archived file was registered with metadata only")
def archive_metadata_only(
    zerodb: ZeroDBClient, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    _promise_source_with_archive(zerodb, fake, result, store_bytes=None)


@given("a promise_sources row whose archived file holds bytes that do not match its sha256")
def archive_wrong_bytes(
    zerodb: ZeroDBClient, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    _promise_source_with_archive(
        zerodb, fake, result, store_bytes=b"<html>a completely different page</html>"
    )


@given("a promise_sources row with an empty archived_file_id")
def archive_missing_id(
    zerodb: ZeroDBClient, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    _promise_source_with_archive(zerodb, fake, result, store_bytes=None, file_id="")


@given("a row naming an archived file that reads back with no recorded sha256")
def archive_unverifiable(
    zerodb: ZeroDBClient, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    """A row whose id is NOT content-addressed, and whose file records no hash.

    `promise_sources` ids are always `src:<sha256>`, so the hash is in the id
    and this state cannot arise there. It is the realistic shape of the OTHER
    archive-bearing tables datamodel.md names (bill text, debate transcripts),
    whose ids will not be content hashes — so the row is written directly
    through the client, bypassing the repository that would reject a
    non-conforming `promise_sources` id. The check must report such a row as
    unverifiable rather than passing it for merely being reachable.
    """
    _seed_candidacy(zerodb)
    stored = zerodb.upload_file(
        file_key="promise-sources/transcript.txt", content=b"a debate transcript, unhashed"
    )
    row_id = "transcript:2026-03-11-debate"
    zerodb.insert_row(
        PromiseSource.table_name,
        {
            "id": row_id,
            "candidacy_id": ARCHIVE_CANDIDACY_ID,
            "source_type": "transcript",
            "url": "https://example.test/debate",
            "archived_file_id": stored.file_id,
            "captured_at": "2026-10-16T22:00:00+00:00",
        },
    )
    result["archive_row_id"] = row_id


# --- assertions ----------------------------------------------------------


def _check_report(check_result: CheckReport) -> CheckReport:
    return check_result


@then(parsers.re(r"the .+ check reports no violations"))
def report_has_no_violations(check_result: CheckReport) -> None:
    assert check_result.ok, "\n".join(str(v) for v in check_result.violations)


@then(
    parsers.parse(
        "the reference integrity check reports a violation for member_votes row referencing "
        'roll_call_id "{roll_call_id}"'
    )
)
def references_violation_member_votes(check_result: CheckReport, roll_call_id: str) -> None:
    assert any(
        v.table == MemberVote.table_name and roll_call_id in v.detail
        for v in check_result.violations
    ), check_result.violations


@then(
    parsers.parse(
        "the reference integrity check reports a violation for candidacies row referencing "
        'person_id "{person_id}"'
    )
)
def references_violation_candidacies(check_result: CheckReport, person_id: str) -> None:
    assert any(
        v.table == Candidacy.table_name and person_id in v.detail for v in check_result.violations
    ), check_result.violations


@then(
    parsers.parse(
        'the verdict roll-call mapping check reports a violation for verdict "{verdict_id}"'
    )
)
def mapping_violation_for_verdict_id(check_result: CheckReport, verdict_id: str) -> None:
    assert any(v.row_id == verdict_id for v in check_result.violations), check_result.violations


@then(
    parsers.parse(
        "the verdict roll-call mapping check reports a violation for verdict "
        'referencing promise "{promise_id}"'
    )
)
def mapping_violation_for_promise(check_result: CheckReport, promise_id: str) -> None:
    assert any(promise_id in v.detail for v in check_result.violations), check_result.violations


@then(
    parsers.parse(
        "the is_current uniqueness check reports a violation for verdicts promise "
        '"{promise_id}" with {count:d} current rows'
    )
)
def is_current_violation_verdicts(check_result: CheckReport, promise_id: str, count: int) -> None:
    matches = [
        v
        for v in check_result.violations
        if v.table == Verdict.table_name and promise_id in v.row_id
    ]
    assert matches, check_result.violations
    assert any(f"{count} row" in v.detail for v in matches), matches


@then(
    parsers.parse(
        "the is_current uniqueness check reports a violation for candidate_positions key "
        '"{person_id}"/"{stance_id}" with {count:d} current rows'
    )
)
def is_current_violation_positions(
    check_result: CheckReport, person_id: str, stance_id: str, count: int
) -> None:
    matches = [
        v
        for v in check_result.violations
        if v.table == CandidatePosition.table_name
        and person_id in v.row_id
        and stance_id in v.row_id
    ]
    assert matches, check_result.violations
    assert any(f"{count} row" in v.detail for v in matches), matches


@then(
    parsers.parse(
        "the member_votes drift check reports a violation for member_votes row "
        '"{row_id}" field "{field}"'
    )
)
def drift_violation(check_result: CheckReport, row_id: str, field: str) -> None:
    assert any(v.row_id == row_id and field in v.detail for v in check_result.violations), (
        check_result.violations
    )


@then("the provenance coverage check reports a violation naming that row")
def provenance_violation_names_row(check_result: CheckReport, result: dict[str, Any]) -> None:
    row_id = result["bypass_row_id"]
    assert any(v.row_id == row_id for v in check_result.violations), check_result.violations


@then("the archive retrievability check reports a violation naming that unreadable archive")
def archive_violation_unreadable(check_result: CheckReport, result: dict[str, Any]) -> None:
    row_id = result["archive_row_id"]
    assert any(
        v.row_id == row_id and "cannot be read back" in v.detail for v in check_result.violations
    ), check_result.violations


@then("the archive retrievability check reports a violation naming that mismatched archive")
def archive_violation_mismatch(check_result: CheckReport, result: dict[str, Any]) -> None:
    row_id = result["archive_row_id"]
    assert any(
        v.row_id == row_id and "not the recorded" in v.detail for v in check_result.violations
    ), check_result.violations


@then("the archive retrievability check reports a violation naming that unverifiable archive")
def archive_violation_unverifiable(check_result: CheckReport, result: dict[str, Any]) -> None:
    row_id = result["archive_row_id"]
    assert any(
        v.row_id == row_id and "records no sha256" in v.detail for v in check_result.violations
    ), check_result.violations


@then("the archive retrievability check reports a violation naming that row")
def archive_violation_names_row(check_result: CheckReport, result: dict[str, Any]) -> None:
    row_id = result["archive_row_id"]
    assert any(
        v.row_id == row_id and "names no archived_file_id" in v.detail
        for v in check_result.violations
    ), check_result.violations


@then(parsers.parse('the combined report covers checks "{names}"'))
def combined_report_covers(combined_result: CombinedReport, names: str) -> None:
    expected = {name.strip().strip('"') for name in names.split(",")}
    actual = {report.name for report in combined_result.reports}
    assert actual == expected, f"expected {expected}, got {actual}"
