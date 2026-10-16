"""Unit tests for the repository layer (story 1.4).

The feature file covers the two acceptance criteria end to end. These tests
cover the generic machinery around them: deterministic id derivation, the
reference-check cache, paging, append-only version numbering, and the places
where the repository has to work around ZeroDB behaviour that the typed client
exposes rather than hides.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.repositories import (
    AppendOnlyViolationError,
    MissingReferenceError,
    Repository,
    RowValidationError,
)
from app.repositories.models import (
    CandidatePosition,
    District,
    MemberVote,
    Person,
    RollCall,
    Verdict,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

TABLES = (
    "people",
    "districts",
    "roll_calls",
    "member_votes",
    "verdicts",
    "candidate_positions",
    "promises",
    "stances",
    "bills",
)


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    for table in TABLES:
        client.create_table(table, {"id": "string"})
    return client


@pytest.fixture
def people(client: ZeroDBClient) -> Repository[Person]:
    return Repository(client=client, model=Person)


@pytest.fixture
def roll_calls(client: ZeroDBClient) -> Repository[RollCall]:
    return Repository(client=client, model=RollCall)


@pytest.fixture
def member_votes(client: ZeroDBClient) -> Repository[MemberVote]:
    return Repository(client=client, model=MemberVote)


@pytest.fixture
def verdicts(client: ZeroDBClient) -> Repository[Verdict]:
    return Repository(client=client, model=Verdict)


def person_row(person_id: str = "person:B000944", name: str = "Synthetic Member") -> dict:
    return {"id": person_id, "canonical_name": name, "external_ids": {}, "terms": []}


def roll_call_row(roll_call_id: str = "us-house:119-2:452") -> dict:
    return {
        "id": roll_call_id,
        "voted_at": "2026-03-04T15:00:00Z",
        "question": "On Passage",
        "vote_type": "final_passage",
        "result": "Passed",
        "tally": {"yea": 1, "nay": 0, "present": 0, "not_voting": 0},
        "topic_ids": [],
    }


def member_vote_row(
    roll_call_id: str = "us-house:119-2:452",
    person_id: str = "person:B000944",
    **overrides: object,
) -> dict:
    bioguide = person_id.removeprefix("person:")
    row = {
        "id": f"{roll_call_id}:{bioguide}",
        "roll_call_id": roll_call_id,
        "person_id": person_id,
        "position": "yea",
        "voted_at": "2026-03-04T15:00:00Z",
        "vote_type": "final_passage",
        "topic_ids": [],
    }
    row.update(overrides)
    return row


def verdict_row(promise_id: str = "prm:0001", verdict: str = "kept", **overrides: object) -> dict:
    row = {
        "promise_id": promise_id,
        "person_id": "person:B000944",
        "verdict": verdict,
        "confidence": 0.9,
        "methodology_version": "2026.1",
        "qa_status": "approved",
    }
    row.update(overrides)
    return row


# --- table binding --------------------------------------------------------


def test_the_repository_takes_its_table_name_from_the_model(
    client: ZeroDBClient,
) -> None:
    """One model, one table: nothing above the repository names a table twice."""
    assert Repository(client=client, model=Person).table_name == "people"
    assert Repository(client=client, model=District).table_name == "districts"
    assert Repository(client=client, model=MemberVote).table_name == "member_votes"


# --- deterministic ids ----------------------------------------------------


def test_the_id_is_derived_from_the_natural_key_when_absent(
    client: ZeroDBClient, people: Repository[Person], roll_calls: Repository[RollCall]
) -> None:
    """datamodel.md rule 1: ids are built from natural keys, not supplied.

    A caller that omits `id` must still get the canonical one, so two ingest
    adapters cannot disagree about a record's identity.
    """
    people.upsert(person_row())
    roll_calls.upsert(roll_call_row())
    repo: Repository[MemberVote] = Repository(client=client, model=MemberVote)

    row = member_vote_row()
    del row["id"]
    stored = repo.upsert(row)

    assert stored.id == "us-house:119-2:452:B000944"


def test_an_id_that_contradicts_the_natural_key_is_rejected(
    people: Repository[Person], roll_calls: Repository[RollCall], member_votes: Repository
) -> None:
    """A mismatch means the caller's id scheme has drifted; fail loudly."""
    people.upsert(person_row())
    roll_calls.upsert(roll_call_row())

    with pytest.raises(RowValidationError) as caught:
        member_votes.upsert(member_vote_row(id="us-house:119-2:452:WRONG"))

    assert "natural key" in str(caught.value).lower()


def test_a_verdict_id_numbers_versions_in_sequence(
    client: ZeroDBClient, people: Repository[Person], verdicts: Repository[Verdict]
) -> None:
    """`{promise_id}:v{n}`, per datamodel.md's id pattern for verdicts."""
    people.upsert(person_row())
    client.upsert_row("promises", "prm:0001", {"candidacy_id": "synthetic"})

    first = verdicts.append_version(verdict_row())
    second = verdicts.append_version(verdict_row(verdict="broken"))

    assert first.id == "prm:0001:v1"
    assert second.id == "prm:0001:v2"


# --- reference validation -------------------------------------------------


def test_a_missing_reference_is_rejected_before_any_write(
    fake: FakeZeroDBTransport, roll_calls: Repository[RollCall], member_votes: Repository
) -> None:
    roll_calls.upsert(roll_call_row())
    fake.calls.clear()

    with pytest.raises(MissingReferenceError):
        member_votes.upsert(member_vote_row(person_id="person:NOBODY"))

    assert not any(
        call.method in {"POST", "PUT"} and call.path.endswith("/rows")
        for call in fake.calls
        if "member_votes" in call.path
    )


def test_the_error_names_the_table_field_and_id_that_did_not_resolve(
    roll_calls: Repository[RollCall], member_votes: Repository
) -> None:
    roll_calls.upsert(roll_call_row())

    with pytest.raises(MissingReferenceError) as caught:
        member_votes.upsert(member_vote_row(person_id="person:NOBODY"))

    error = caught.value
    assert error.table == "member_votes"
    assert error.field == "person_id"
    assert error.referenced_table == "people"
    assert error.referenced_id == "person:NOBODY"


def test_every_declared_reference_is_checked_not_just_the_first(
    member_votes: Repository,
) -> None:
    """Neither reference resolves; the error must name one of them, not pass."""
    with pytest.raises(MissingReferenceError):
        member_votes.upsert(member_vote_row())


def test_an_optional_reference_that_is_absent_is_not_checked(
    fake: FakeZeroDBTransport, people: Repository[Person], roll_calls: Repository[RollCall]
) -> None:
    """`roll_calls.bill_id` is optional: a roll call on no bill is legitimate."""
    fake.calls.clear()
    roll_calls.upsert(roll_call_row())

    assert not any("/tables/bills/" in call.path for call in fake.calls)


def test_an_optional_reference_that_is_present_is_checked(
    roll_calls: Repository[RollCall],
) -> None:
    with pytest.raises(MissingReferenceError) as caught:
        roll_calls.upsert({**roll_call_row(), "bill_id": "us-house:119:hr-9999"})

    assert caught.value.referenced_table == "bills"


def test_reference_checks_are_cached_within_one_batch(
    fake: FakeZeroDBTransport,
    people: Repository[Person],
    roll_calls: Repository[RollCall],
    member_votes: Repository,
) -> None:
    """600k member votes per Congress share a few thousand roll calls.

    Re-reading the same roll call once per vote would multiply an ingest run's
    request count by the number of votes on each roll call, so a batch resolves
    each referenced id once.
    """
    people.upsert(person_row())
    roll_calls.upsert(roll_call_row())
    rows = [
        member_vote_row(person_id="person:B000944"),
        member_vote_row(person_id="person:B000944", position="nay"),
    ]
    fake.calls.clear()

    member_votes.upsert_many(rows)

    roll_call_reads = [call for call in fake.calls if "/tables/roll_calls/" in call.path]
    assert len(roll_call_reads) == 1, "a batch must resolve each referenced id once"


def test_a_cached_reference_check_does_not_leak_between_calls(
    fake: FakeZeroDBTransport,
    people: Repository[Person],
    roll_calls: Repository[RollCall],
    member_votes: Repository,
) -> None:
    """The cache lives for one call, so a later run sees a deleted reference.

    A long-lived cache would let an ingest run keep writing votes against a
    roll call that had since been removed.
    """
    people.upsert(person_row())
    roll_calls.upsert(roll_call_row())
    member_votes.upsert(member_vote_row())
    fake.calls.clear()

    member_votes.upsert(member_vote_row(position="nay"))

    assert any("/tables/roll_calls/" in call.path for call in fake.calls)


def test_a_batch_aborts_at_the_first_missing_reference_keeping_earlier_rows(
    fake: FakeZeroDBTransport,
    people: Repository[Person],
    roll_calls: Repository[RollCall],
    member_votes: Repository,
) -> None:
    """upsert_many is not a transaction, and the partial write must be usable.

    Pins the documented behaviour so nobody later "fixes" it into a silent
    skip: the rows before the bad one stay written, which is the right outcome
    for an idempotent ingest (fix the data, re-run, and those become no-ops).
    Caching the negative result is safe here because a batch writes one table
    and its references point at others, so a batch can never create the parent
    it is about to be rejected for.
    """
    people.upsert(person_row())
    roll_calls.upsert(roll_call_row())

    with pytest.raises(MissingReferenceError):
        member_votes.upsert_many(
            [
                member_vote_row(),
                member_vote_row(person_id="person:NOBODY"),
                member_vote_row(position="nay"),
            ]
        )

    written = [row["row_data"] for row in fake.rows["member_votes"].values()]
    assert len(written) == 1
    assert written[0]["id"] == "us-house:119-2:452:B000944"


def test_reference_checking_can_be_disabled_for_a_trusted_bulk_load(
    fake: FakeZeroDBTransport, member_votes: Repository
) -> None:
    """A full re-ingest inserts parents and children in one pass.

    The escape hatch is explicit per call and never the default, so skipping
    the check is always a visible decision in the calling code.
    """
    fake.calls.clear()
    member_votes.upsert(member_vote_row(person_id="person:NOBODY"), check_references=False)

    assert not any("/tables/people/" in call.path for call in fake.calls)
    assert member_votes.get("us-house:119-2:452:NOBODY") is not None


# --- idempotent upsert ----------------------------------------------------


def test_upserting_the_same_record_twice_writes_one_row(
    fake: FakeZeroDBTransport, people: Repository[Person]
) -> None:
    people.upsert(person_row())
    people.upsert(person_row())

    assert len(fake.rows["people"]) == 1


def test_upserting_a_changed_record_replaces_the_row_contents(
    people: Repository[Person],
) -> None:
    people.upsert(person_row(name="Before"))
    people.upsert(person_row(name="After"))

    stored = people.get("person:B000944")
    assert stored is not None
    assert stored.canonical_name == "After"


def test_upsert_returns_the_validated_model_not_a_raw_dict(
    people: Repository[Person],
) -> None:
    stored = people.upsert(person_row())

    assert isinstance(stored, Person)
    assert stored.id == "person:B000944"


def test_upsert_many_is_idempotent_across_runs(
    fake: FakeZeroDBTransport, people: Repository[Person]
) -> None:
    rows = [person_row(f"person:SYNTH{index}") for index in range(3)]

    people.upsert_many(rows)
    people.upsert_many(rows)

    assert len(fake.rows["people"]) == 3


# --- schema validation ----------------------------------------------------


def test_a_row_failing_its_model_is_rejected_with_the_field_named(
    people: Repository[Person], roll_calls: Repository[RollCall], member_votes: Repository
) -> None:
    people.upsert(person_row())
    roll_calls.upsert(roll_call_row())

    with pytest.raises(RowValidationError) as caught:
        member_votes.upsert(member_vote_row(position="maybe"))

    assert "position" in str(caught.value)


def test_validation_runs_before_any_reference_lookup(
    fake: FakeZeroDBTransport, member_votes: Repository
) -> None:
    """A malformed row should not cost a round trip to check its references."""
    fake.calls.clear()

    with pytest.raises(RowValidationError):
        member_votes.upsert(member_vote_row(position="maybe"))

    assert fake.calls == []


def test_the_model_rejects_a_position_outside_the_datamodel_vocabulary() -> None:
    """datamodel.md: position takes yea, nay, present or not_voting."""
    with pytest.raises(ValidationError):
        MemberVote.model_validate(member_vote_row(position="abstain"))


def test_the_model_rejects_a_verdict_outside_the_datamodel_vocabulary() -> None:
    with pytest.raises(ValidationError):
        Verdict.model_validate({**verdict_row(verdict="mostly"), "id": "prm:0001:v1"})


def test_an_unknown_field_is_rejected_rather_than_silently_stored(
    people: Repository[Person],
) -> None:
    """A typo'd field name would otherwise become a column nothing reads.

    ZeroDB's schema is not a validator, so a misspelled `cannonical_name` would
    be accepted by the API and then be invisible to every query. The model is
    the only thing that can catch it.
    """
    with pytest.raises(RowValidationError):
        people.upsert({**person_row(), "cannonical_name": "typo"})


# --- append-only versioning ----------------------------------------------


def test_append_version_keeps_history_and_moves_the_current_flag(
    client: ZeroDBClient,
    fake: FakeZeroDBTransport,
    people: Repository[Person],
    verdicts: Repository[Verdict],
) -> None:
    people.upsert(person_row())
    client.upsert_row("promises", "prm:0001", {"candidacy_id": "synthetic"})

    verdicts.append_version(verdict_row(verdict="kept"))
    verdicts.append_version(verdict_row(verdict="broken"))

    rows = [row["row_data"] for row in fake.rows["verdicts"].values()]
    assert len(rows) == 2
    assert [row["verdict"] for row in rows if row["is_current"]] == ["broken"]
    assert [row["verdict"] for row in rows if not row["is_current"]] == ["kept"]


def test_the_current_row_is_readable_by_its_version_key(
    client: ZeroDBClient, people: Repository[Person], verdicts: Repository[Verdict]
) -> None:
    people.upsert(person_row())
    client.upsert_row("promises", "prm:0001", {"candidacy_id": "synthetic"})
    verdicts.append_version(verdict_row(verdict="kept"))
    verdicts.append_version(verdict_row(verdict="broken"))

    current = verdicts.current(promise_id="prm:0001")

    assert current is not None
    assert current.verdict == "broken"
    assert current.is_current is True


def test_two_version_keys_keep_independent_current_rows(
    client: ZeroDBClient, people: Repository[Person], verdicts: Repository[Verdict]
) -> None:
    """datamodel.md integrity check: exactly one current row per promise."""
    people.upsert(person_row())
    for promise_id in ("prm:0001", "prm:0002"):
        client.upsert_row("promises", promise_id, {"candidacy_id": "synthetic"})

    verdicts.append_version(verdict_row("prm:0001", "kept"))
    verdicts.append_version(verdict_row("prm:0002", "broken"))
    verdicts.append_version(verdict_row("prm:0001", "partial"))

    assert verdicts.current(promise_id="prm:0001").verdict == "partial"
    assert verdicts.current(promise_id="prm:0002").verdict == "broken"


def test_a_composite_version_key_is_supported(
    client: ZeroDBClient,
    fake: FakeZeroDBTransport,
    people: Repository[Person],
) -> None:
    """`candidate_positions` is current per (person, stance), not per person."""
    people.upsert(person_row())
    client.upsert_row("stances", "en-US:rx:v1:lower", {"topic_id": "en-US:rx:v1"})
    positions: Repository[CandidatePosition] = Repository(client=client, model=CandidatePosition)
    row = {
        "person_id": "person:B000944",
        "stance_id": "en-US:rx:v1:lower",
        "s_value": 0.5,
        "confidence": 0.8,
        "evidence_count": 3,
        "basis": "votes",
        "methodology_version": "2026.1",
    }

    positions.append_version(row)
    positions.append_version({**row, "s_value": -0.25})

    rows = [r["row_data"] for r in fake.rows["candidate_positions"].values()]
    assert len(rows) == 2
    assert [r["s_value"] for r in rows if r["is_current"]] == [-0.25]


def test_append_version_on_a_plain_table_is_refused(
    people: Repository[Person],
) -> None:
    """Two different semantics, two different methods: never silently one."""
    with pytest.raises(AppendOnlyViolationError):
        people.append_version(person_row())


def test_upsert_on_an_append_only_table_is_refused(
    verdicts: Repository[Verdict],
) -> None:
    with pytest.raises(AppendOnlyViolationError) as caught:
        verdicts.upsert({**verdict_row(), "id": "prm:0001:v1"})

    assert "append_version" in str(caught.value)


def test_upsert_many_on_an_append_only_table_is_refused(
    verdicts: Repository[Verdict],
) -> None:
    with pytest.raises(AppendOnlyViolationError):
        verdicts.upsert_many([{**verdict_row(), "id": "prm:0001:v1"}])


def test_append_version_validates_references_too(
    client: ZeroDBClient, verdicts: Repository[Verdict]
) -> None:
    client.upsert_row("promises", "prm:0001", {"candidacy_id": "synthetic"})

    with pytest.raises(MissingReferenceError) as caught:
        verdicts.append_version(verdict_row(person_id="person:NOBODY"))

    assert caught.value.referenced_table == "people"


def test_a_superseded_rows_other_fields_survive_the_flag_flip(
    client: ZeroDBClient,
    fake: FakeZeroDBTransport,
    people: Repository[Person],
    verdicts: Repository[Verdict],
) -> None:
    """ZeroDB's PUT replaces a row wholesale, so the flip must read-modify-write.

    A naive `PUT {"is_current": false}` would destroy the superseded verdict —
    the historical record this table exists to keep.
    """
    people.upsert(person_row())
    client.upsert_row("promises", "prm:0001", {"candidacy_id": "synthetic"})
    verdicts.append_version(verdict_row(verdict="kept", confidence=0.77))

    verdicts.append_version(verdict_row(verdict="broken"))

    superseded = next(
        row["row_data"]
        for row in fake.rows["verdicts"].values()
        if row["row_data"]["is_current"] is False
    )
    assert superseded["verdict"] == "kept"
    assert superseded["confidence"] == 0.77
    assert superseded["id"] == "prm:0001:v1"
    assert superseded["methodology_version"] == "2026.1"


# --- reads ----------------------------------------------------------------


def test_get_returns_none_for_an_absent_id(people: Repository[Person]) -> None:
    """Absent is an ordinary answer, not an exception, for a reference check."""
    assert people.get("person:NOBODY") is None


def test_query_returns_validated_models(people: Repository[Person]) -> None:
    people.upsert(person_row("person:A"))
    people.upsert(person_row("person:B"))

    found = people.query(limit=10)

    assert {person.id for person in found} == {"person:A", "person:B"}
    assert all(isinstance(person, Person) for person in found)


def test_query_passes_its_filter_through(people: Repository[Person]) -> None:
    people.upsert(person_row("person:A", name="Wanted"))
    people.upsert(person_row("person:B", name="Other"))

    found = people.query(filter={"canonical_name": "Wanted"})

    assert [person.id for person in found] == ["person:A"]


def test_a_stored_row_that_no_longer_fits_its_model_is_raised_not_hidden(
    client: ZeroDBClient, people: Repository[Person]
) -> None:
    """A row written before a model change must not be silently dropped.

    Returning a short list would make an ingest look complete while quietly
    skipping records; the repository raises so the mismatch is visible.
    """
    client.insert_row("people", {"id": "person:LEGACY", "canonical_name": None})

    with pytest.raises(RowValidationError):
        people.query(limit=10)


def test_delete_removes_the_row(people: Repository[Person]) -> None:
    people.upsert(person_row())

    people.delete("person:B000944")

    assert people.get("person:B000944") is None


def test_delete_on_an_append_only_table_is_refused(
    verdicts: Repository[Verdict],
) -> None:
    """Append-only means indefinite retention (datamodel.md privacy table)."""
    with pytest.raises(AppendOnlyViolationError):
        verdicts.delete("prm:0001:v1")
