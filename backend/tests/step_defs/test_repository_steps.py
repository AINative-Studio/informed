"""Step definitions for repository.feature (story 1.4).

Runs entirely against the story 1.2 in-memory fake, so CI needs no ZeroDB
credentials. The claims that depend on real ZeroDB behaviour rather than on the
fake's modelling of it — "re-ingesting the same record produces no duplicate"
and the reference check — are additionally verified against the live "informed"
project by `tests/test_repository_live_shapes.py` and the live verification
script in `backend/scripts/verify_repository_live.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.repositories import (
    AppendOnlyViolationError,
    MissingReferenceError,
    Repository,
    RowValidationError,
)
from app.repositories.models import (
    District,
    Jurisdiction,
    MemberVote,
    Person,
    RollCall,
    Verdict,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/repository.feature")


# --- fixtures -------------------------------------------------------------

#: Only the tables these scenarios touch. The live project has all 23
#: (story 1.3); creating five here keeps the fake's state readable.
TABLES = (
    "people",
    "jurisdictions",
    "bodies",
    "districts",
    "roll_calls",
    "member_votes",
    "verdicts",
    "promises",
)


@dataclass
class RepoContext:
    """State passed between the Given, When and Then of one scenario."""

    fake: FakeZeroDBTransport
    client: ZeroDBClient
    repos: dict[str, Repository[Any]] = field(default_factory=dict)
    error: Exception | None = None
    #: Index into `fake.calls` taken just before the When, so a Then can assert
    #: on the calls that the action itself made rather than the setup's.
    call_mark: int = 0

    def repo(self, table: str) -> Repository[Any]:
        return self.repos[table]

    def mark(self) -> None:
        self.call_mark = len(self.fake.calls)

    @property
    def calls_since_mark(self) -> list[Any]:
        return self.fake.calls[self.call_mark :]

    def rows(self, table: str) -> list[dict[str, Any]]:
        return [row["row_data"] for row in self.fake.rows.get(table, {}).values()]


@pytest.fixture
def repo_context() -> RepoContext:
    fake = FakeZeroDBTransport()
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    return RepoContext(fake=fake, client=client)


# --- Background -----------------------------------------------------------


@given("a bootstrapped ZeroDB backed by an in-memory fake")
def bootstrapped_fake(repo_context: RepoContext) -> None:
    for table in TABLES:
        repo_context.client.create_table(table, {"id": "string"})


@given("a repository for each of people, districts, roll_calls, member_votes and verdicts")
def repositories_for_each_table(repo_context: RepoContext) -> None:
    for model in (Person, Jurisdiction, District, RollCall, MemberVote, Verdict):
        repo = Repository(client=repo_context.client, model=model)
        repo_context.repos[repo.table_name] = repo


# --- seeding --------------------------------------------------------------


def _person(person_id: str, canonical_name: str = "Synthetic Member") -> dict[str, Any]:
    return {
        "id": person_id,
        "canonical_name": canonical_name,
        "external_ids": {"bioguide": person_id.removeprefix("person:")},
        "terms": [],
    }


def _roll_call(roll_call_id: str) -> dict[str, Any]:
    return {
        "id": roll_call_id,
        "voted_at": "2026-03-04T15:00:00Z",
        "question": "On Passage",
        "vote_type": "final_passage",
        "result": "Passed",
        "tally": {"yea": 220, "nay": 210, "present": 0, "not_voting": 5},
        "is_bundled": False,
        "topic_ids": [],
        "source_url": "https://clerk.house.gov/synthetic/roll-call",
    }


def _member_vote(roll_call_id: str, person_id: str, position: str = "yea") -> dict[str, Any]:
    bioguide = person_id.removeprefix("person:")
    return {
        "id": f"{roll_call_id}:{bioguide}",
        "roll_call_id": roll_call_id,
        "person_id": person_id,
        "position": position,
        "voted_at": "2026-03-04T15:00:00Z",
        "vote_type": "final_passage",
        "topic_ids": [],
        "is_bundled": False,
    }


def _verdict(promise_id: str, verdict: str, person_id: str = "person:B000944") -> dict[str, Any]:
    return {
        "promise_id": promise_id,
        "person_id": person_id,
        "verdict": verdict,
        "confidence": 0.9,
        "methodology_version": "2026.1",
        "qa_status": "approved",
    }


@given(parsers.parse('a person "{person_id}" exists'))
def seed_person(repo_context: RepoContext, person_id: str) -> None:
    repo_context.repo("people").upsert(_person(person_id))


@given(parsers.parse('a roll call "{roll_call_id}" exists'))
def seed_roll_call(repo_context: RepoContext, roll_call_id: str) -> None:
    repo_context.repo("roll_calls").upsert(_roll_call(roll_call_id))


@given(parsers.parse('the chamber "{body_id}" exists'))
def seed_chamber(repo_context: RepoContext, body_id: str) -> None:
    """A `bodies` row for a `districts` reference to resolve against.

    Written through the client rather than a `Body` repository, because `Body`
    itself references `jurisdictions` and this story's scenarios are about the
    district's check, not the chamber's. The row exists only to be found.
    """
    repo_context.client.upsert_row(
        "bodies",
        body_id,
        {"jurisdiction_id": "us-federal", "name": body_id, "chamber": "lower", "seats": 435},
    )


@given(parsers.parse('a promise "{promise_id}" exists'))
def seed_promise(repo_context: RepoContext, promise_id: str) -> None:
    # `promises` has no model in this story, so the row is written through the
    # client directly: it exists only to be the target of a reference check.
    repo_context.client.upsert_row("promises", promise_id, {"candidacy_id": "synthetic"})


# --- When -----------------------------------------------------------------


@when(
    parsers.parse(
        'I save a member vote referencing roll call "{roll_call_id}" and person "{person_id}"'
    )
)
def save_member_vote(repo_context: RepoContext, roll_call_id: str, person_id: str) -> None:
    repo_context.mark()
    _attempt(repo_context, "member_votes", _member_vote(roll_call_id, person_id))


@when(parsers.parse('I save a district "{district_id}"'))
def save_district(repo_context: RepoContext, district_id: str) -> None:
    repo_context.mark()
    _attempt(
        repo_context,
        "districts",
        {
            "id": district_id,
            "body_id": "us-house",
            "name": "California 19th",
            "state": "CA",
            "valid_from": "2023-01-03",
        },
    )


@when(parsers.parse('I save a jurisdiction "{jurisdiction_id}"'))
def save_jurisdiction(repo_context: RepoContext, jurisdiction_id: str) -> None:
    """The reference-free write: `Jurisdiction` declares no references at all.

    Took over this role from `districts` in story 2.3, which gave that table a
    `body_id` reference. A table that declares nothing must make no reference
    lookup, which is what the Then asserts.
    """
    repo_context.mark()
    _attempt(
        repo_context,
        "jurisdictions",
        {
            "id": jurisdiction_id,
            "name": "United States",
            "country_code": "US",
            "level": "federal",
            "adapter_name": jurisdiction_id,
            "active": True,
        },
    )


@when(parsers.parse('I save the person "{person_id}" again'))
def save_person_again(repo_context: RepoContext, person_id: str) -> None:
    repo_context.mark()
    _attempt(repo_context, "people", _person(person_id))


@when(parsers.parse('I save the person "{person_id}" with canonical name "{name}"'))
def save_person_renamed(repo_context: RepoContext, person_id: str, name: str) -> None:
    repo_context.mark()
    _attempt(repo_context, "people", _person(person_id, canonical_name=name))


@when(parsers.parse("I save {count:d} distinct people twice each"))
def save_distinct_people_twice(repo_context: RepoContext, count: int) -> None:
    repo_context.mark()
    for index in range(count):
        row = _person(f"person:SYNTH{index:04d}")
        repo_context.repo("people").upsert(row)
        repo_context.repo("people").upsert(row)


@when(parsers.parse('I append a verdict "{verdict}" for promise "{promise_id}"'))
def append_verdict(repo_context: RepoContext, verdict: str, promise_id: str) -> None:
    repo_context.mark()
    _attempt_append(repo_context, "verdicts", _verdict(promise_id, verdict))


@when(
    parsers.parse(
        'I append a verdict "{verdict}" for promise "{promise_id}" by person "{person_id}"'
    )
)
def append_verdict_by_person(
    repo_context: RepoContext, verdict: str, promise_id: str, person_id: str
) -> None:
    repo_context.mark()
    _attempt_append(repo_context, "verdicts", _verdict(promise_id, verdict, person_id=person_id))


@when("I upsert a verdict instead of appending a version")
def upsert_a_verdict(repo_context: RepoContext) -> None:
    repo_context.mark()
    _attempt(repo_context, "verdicts", {**_verdict("prm:0001", "kept"), "id": "prm:0001:v1"})


@when("I append a version of a person instead of upserting")
def append_version_of_a_person(repo_context: RepoContext) -> None:
    repo_context.mark()
    _attempt_append(repo_context, "people", _person("person:B000944"))


@when(parsers.parse('I save a member vote with position "{position}"'))
def save_member_vote_bad_position(repo_context: RepoContext, position: str) -> None:
    repo_context.mark()
    _attempt(
        repo_context,
        "member_votes",
        _member_vote("us-house:119-2:452", "person:B000944", position=position),
    )


@when("I save a member vote whose id disagrees with its natural key")
def save_member_vote_wrong_id(repo_context: RepoContext) -> None:
    repo_context.mark()
    row = _member_vote("us-house:119-2:452", "person:B000944")
    _attempt(repo_context, "member_votes", {**row, "id": "us-house:119-2:452:WRONG"})


def _attempt(repo_context: RepoContext, table: str, row: dict[str, Any]) -> None:
    try:
        repo_context.repo(table).upsert(row)
    except Exception as exc:  # noqa: BLE001 - the Then steps assert on the type
        repo_context.error = exc


def _attempt_append(repo_context: RepoContext, table: str, row: dict[str, Any]) -> None:
    try:
        repo_context.repo(table).append_version(row)
    except Exception as exc:  # noqa: BLE001 - the Then steps assert on the type
        repo_context.error = exc


# --- Then: rejection ------------------------------------------------------


@then("the write is rejected as a missing reference")
def rejected_missing_reference(repo_context: RepoContext) -> None:
    assert isinstance(repo_context.error, MissingReferenceError), (
        f"expected MissingReferenceError, got {repo_context.error!r}"
    )


@then(parsers.parse('the missing reference names the table "{table}" and the id "{row_id}"'))
def missing_reference_names(repo_context: RepoContext, table: str, row_id: str) -> None:
    error = repo_context.error
    assert isinstance(error, MissingReferenceError)
    assert error.referenced_table == table
    assert error.referenced_id == row_id
    # The message has to be actionable on its own: an ingest run's log line is
    # often all a developer gets.
    assert table in str(error)
    assert row_id in str(error)


@then("the write is rejected as an append-only violation")
def rejected_append_only(repo_context: RepoContext) -> None:
    assert isinstance(repo_context.error, AppendOnlyViolationError), (
        f"expected AppendOnlyViolationError, got {repo_context.error!r}"
    )


@then("the write is rejected as invalid")
def rejected_invalid(repo_context: RepoContext) -> None:
    assert isinstance(repo_context.error, RowValidationError), (
        f"expected RowValidationError, got {repo_context.error!r}"
    )


@then(parsers.parse('no row was written to "{table}"'))
def no_row_written(repo_context: RepoContext, table: str) -> None:
    assert repo_context.rows(table) == []


# --- Then: storage --------------------------------------------------------


@then(parsers.parse('the member vote "{vote_id}" is stored'))
def member_vote_stored(repo_context: RepoContext, vote_id: str) -> None:
    assert repo_context.error is None, f"unexpected error: {repo_context.error!r}"
    assert repo_context.repo("member_votes").get(vote_id) is not None


@then(parsers.parse('the district "{district_id}" is stored'))
def district_stored(repo_context: RepoContext, district_id: str) -> None:
    assert repo_context.error is None, f"unexpected error: {repo_context.error!r}"
    assert repo_context.repo("districts").get(district_id) is not None


@then(parsers.parse('the jurisdiction "{jurisdiction_id}" is stored'))
def jurisdiction_stored(repo_context: RepoContext, jurisdiction_id: str) -> None:
    assert repo_context.error is None, f"unexpected error: {repo_context.error!r}"
    assert repo_context.repo("jurisdictions").get(jurisdiction_id) is not None


@then(parsers.parse('exactly {count:d} row exists in "{table}"'))
@then(parsers.parse('exactly {count:d} rows exist in "{table}"'))
def exactly_n_rows(repo_context: RepoContext, count: int, table: str) -> None:
    assert repo_context.error is None, f"unexpected error: {repo_context.error!r}"
    assert len(repo_context.rows(table)) == count


@then(parsers.parse('the person "{person_id}" has canonical name "{name}"'))
def person_has_name(repo_context: RepoContext, person_id: str, name: str) -> None:
    person = repo_context.repo("people").get(person_id)
    assert person is not None
    assert person.canonical_name == name


@then(parsers.parse('the person row still carries the id field "{person_id}"'))
def person_row_carries_id(repo_context: RepoContext, person_id: str) -> None:
    stored = [row for row in repo_context.rows("people") if row.get("id") == person_id]
    assert len(stored) == 1, f"expected one row carrying id {person_id}, found {len(stored)}"


@then(parsers.parse('no reference lookup was made against "{table}"'))
def no_reference_lookup(repo_context: RepoContext, table: str) -> None:
    assert not any(f"/tables/{table}/" in call.path for call in repo_context.calls_since_mark)


# --- Then: upsert addressing ---------------------------------------------


@then("the update was addressed by ZeroDB's internal row UUID")
def update_addressed_by_uuid(repo_context: RepoContext) -> None:
    import uuid as uuid_module

    puts = [call for call in repo_context.calls_since_mark if call.method == "PUT"]
    assert puts, "an upsert of an existing row must PUT, not insert a duplicate"
    segment = puts[-1].path.rsplit("/", 1)[-1]
    uuid_module.UUID(segment)  # raises if it is not ZeroDB's internal UUID


@then(parsers.parse('no request path contained the natural-key id "{row_id}"'))
def no_path_contained_natural_key(repo_context: RepoContext, row_id: str) -> None:
    assert not any(row_id in call.path for call in repo_context.calls_since_mark)


# --- Then: append-only versioning ----------------------------------------


@then(parsers.parse('exactly {count:d} verdict row for promise "{promise_id}" is current'))
@then(parsers.parse('exactly {count:d} verdict rows for promise "{promise_id}" are current'))
def exactly_n_current(repo_context: RepoContext, count: int, promise_id: str) -> None:
    current = [
        row
        for row in repo_context.rows("verdicts")
        if row.get("promise_id") == promise_id and row.get("is_current") is True
    ]
    assert len(current) == count, f"expected {count} current rows, found {len(current)}"


@then(parsers.parse('the current verdict for promise "{promise_id}" is "{verdict}"'))
def current_verdict_is(repo_context: RepoContext, promise_id: str, verdict: str) -> None:
    current = repo_context.repo("verdicts").current(promise_id=promise_id)
    assert current is not None
    assert current.verdict == verdict


@then(parsers.parse('the superseded verdict "{verdict}" is still stored'))
def superseded_still_stored(repo_context: RepoContext, verdict: str) -> None:
    superseded = [
        row
        for row in repo_context.rows("verdicts")
        if row.get("verdict") == verdict and row.get("is_current") is False
    ]
    assert len(superseded) == 1, "history must be kept, not overwritten"


@then("the only row rewritten was to clear a superseded is_current flag")
def only_rewrite_clears_flag(repo_context: RepoContext) -> None:
    puts = [call for call in repo_context.calls_since_mark if call.method == "PUT"]
    assert len(puts) == 1, f"expected exactly one rewrite, saw {len(puts)}"
    row_data = (puts[0].json or {}).get("row_data", {})
    assert row_data.get("is_current") is False
    # Everything else about the superseded row must survive the rewrite: the
    # verdict itself is the historical record.
    assert row_data.get("verdict") == "kept"
    assert row_data.get("id")
