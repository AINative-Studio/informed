"""Live-confirmed ZeroDB behaviour the repository layer depends on (story 1.4).

Same purpose as `test_zerodb_live_shapes.py`, one layer up: each test here pins
a fact about the LIVE "informed" project that was measured during story 1.4,
not inferred, and that the repository's correctness rests on. The measurement
commands and their raw output are in the story's PR.

The headline finding, and the reason this file exists:

    **ZeroDB's server-side `/query` filter matches nothing.** Measured live on
    2026-10-16 against the real project. A row inserted into `schema_versions`
    and then queried with `filter={"id": "<that id>"}` came back
    `total=0, data=[]`, under both the bare `id` and the `row_data.id`
    spellings, while the same table read unfiltered returned that row plainly.

Story 1.2 observed this and worked around it by also filtering client-side
within the returned page. What story 1.4's live run established is that **the
workaround as written only covers the first page** — `get_row` asks for
`limit=1`, so the fallback read `GET /rows?limit=1&skip=0` and tested exactly
one row, the table's first. Live, with three seeded rows plus one pre-existing
`schema_versions` row, `get_row` found *none* of the three.

That is not a cosmetic gap. `get_row` is the find step of
`ZeroDBClient.upsert_row` and of the repository's reference check, so on any
table holding more than a page of rows:

  * every re-ingest would insert a duplicate, breaking datamodel.md rule 1 and
    story 1.4's second acceptance criterion;
  * every reference check would reject a reference that does exist, failing
    ingest for valid data.

Story 1.4's first live run passed only because the tables were empty and the
one row written was therefore row 0 — a false green of exactly the kind
stories 1.2 and 1.3 were bitten by. `_fallback_filtered_rows` now pages through
the table instead, and `FakeZeroDBTransport.break_server_side_filter()` makes
the fake reproduce the live behaviour so this cannot regress unnoticed.
"""

from __future__ import annotations

import pytest

from app.repositories import MissingReferenceError, Repository
from app.repositories.models import MemberVote, Person, RollCall
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

TABLES = ("people", "roll_calls", "member_votes")


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    """A fake whose server-side filter is as broken as the live API's.

    Every test in this file runs against this, because a fake with a *working*
    server-side filter never exercises the client-side fallback at all — which
    is precisely how the one-page bug stayed invisible in CI while being
    reproducible live in a single request.
    """
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


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


def person_row(person_id: str, name: str = "Synthetic Member") -> dict:
    return {"id": person_id, "canonical_name": name, "external_ids": {}, "terms": []}


# --- the live finding itself ----------------------------------------------


def test_the_fake_reproduces_the_live_server_side_filter_returning_nothing(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """Guards the fake's fidelity on the finding this file is about.

    If this ever fails, the fake has started answering filters the way the docs
    say rather than the way the live API does, and every test below it becomes
    vacuous.
    """
    client.insert_row("people", {"id": "person:A", "canonical_name": "A"})

    # The raw filtered query, as the live API answers it: nothing.
    response = fake.request(
        "POST",
        "/projects/proj-test/database/tables/people/query",
        json={"filter": {"id": "person:A"}, "limit": 100},
    )
    assert response.json_body["total"] == 0
    assert response.json_body["data"] == []


def test_get_row_finds_a_row_that_is_not_the_first_in_the_table(
    client: ZeroDBClient,
) -> None:
    """The exact live failure: three rows, none of them findable.

    Measured live with three seeded rows behind one pre-existing row, where
    `get_row` returned None for all three. A row's position in a table is not
    something any caller knows or controls, so "findable only when first" means
    "not findable".
    """
    for index in range(3):
        client.insert_row("people", {"id": f"person:SYNTH{index}", "canonical_name": "x"})

    for index in range(3):
        found = client.get_row("people", f"person:SYNTH{index}")
        assert found is not None, f"person:SYNTH{index} was not found"
        assert found.row_data["id"] == f"person:SYNTH{index}"


def test_get_row_still_answers_none_for_a_row_that_really_is_absent(
    client: ZeroDBClient,
) -> None:
    """Paging must not turn "absent" into a false positive.

    The reference check reads this answer directly: a get_row that found
    something for every id would accept every reference.
    """
    for index in range(3):
        client.insert_row("people", {"id": f"person:SYNTH{index}", "canonical_name": "x"})

    assert client.get_row("people", "person:NOBODY") is None


def test_get_row_pages_rather_than_reading_the_whole_table_at_once(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """`member_votes` reaches ~600k rows: the fallback must walk, not slurp.

    Asserted on the requests rather than the result, because a fallback that
    asked for `limit=600000` would return the right row and still be unusable.
    """
    for index in range(5):
        client.insert_row("people", {"id": f"person:SYNTH{index}", "canonical_name": "x"})
    fake.calls.clear()

    client.get_row("people", "person:SYNTH4")

    page_reads = [
        call for call in fake.calls if call.method == "GET" and call.path.endswith("/rows")
    ]
    assert page_reads, "the fallback must read the table when the filter matches nothing"
    for call in page_reads:
        assert (call.params or {}).get("limit", 0) <= 1000, (
            "a fallback page must stay bounded; an unbounded read of member_votes "
            "would pull ~600k rows into memory"
        )


def test_the_fallback_stops_once_it_has_found_the_row(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """A found row ends the walk: the first page must not cost the last one."""
    for index in range(10):
        client.insert_row("people", {"id": f"person:SYNTH{index}", "canonical_name": "x"})
    fake.calls.clear()

    client.get_row("people", "person:SYNTH0")
    reads_for_first = len([c for c in fake.calls if c.method == "GET" and c.path.endswith("/rows")])
    fake.calls.clear()

    client.get_row("people", "person:SYNTH9")
    reads_for_last = len([c for c in fake.calls if c.method == "GET" and c.path.endswith("/rows")])

    assert reads_for_first <= reads_for_last


def test_the_walk_gives_up_at_its_ceiling_rather_than_scanning_forever(
    fake: FakeZeroDBTransport, caplog: pytest.LogCaptureFixture
) -> None:
    """The ceiling is what keeps this workaround off `member_votes`' 600k rows.

    Injected tiny here (2 pages of 2) so the exhaustion path is reachable
    without seeding 25k rows. The behaviour on exhaustion is deliberate and
    worth pinning: the walk reports the row ABSENT rather than raising, because
    a reference check must not crash an ingest run, and it logs at warning so
    the silent wrong answer is at least traceable. That trade-off is only
    acceptable while the ceiling sits far above the tables actually referenced —
    see datamodel.md's open question.
    """
    client = ZeroDBClient(
        transport=fake,
        project_id="proj-test",
        backoff_base=0.0,
        sleep=lambda _: None,
        fallback_page_size=2,
        max_fallback_pages=2,
    )
    client.create_table("people", {"id": "string"})
    for index in range(8):
        client.insert_row("people", {"id": f"person:SYNTH{index}", "canonical_name": "x"})

    with caplog.at_level("WARNING"):
        found = client.get_row("people", "person:SYNTH7")

    assert found is None, "a row beyond the ceiling is reported absent, not raised"
    assert any("gave up after" in record.message for record in caplog.records), (
        "giving up must be logged, or a wrong answer is untraceable"
    )


def test_a_row_within_the_ceiling_is_still_found(fake: FakeZeroDBTransport) -> None:
    """The ceiling must not truncate the walk early."""
    client = ZeroDBClient(
        transport=fake,
        project_id="proj-test",
        backoff_base=0.0,
        sleep=lambda _: None,
        fallback_page_size=2,
        max_fallback_pages=2,
    )
    client.create_table("people", {"id": "string"})
    for index in range(8):
        client.insert_row("people", {"id": f"person:SYNTH{index}", "canonical_name": "x"})

    found = client.get_row("people", "person:SYNTH3")

    assert found is not None
    assert found.row_data["id"] == "person:SYNTH3"


# --- what the finding means for story 1.4's acceptance criteria -----------


def test_re_ingesting_a_record_behind_other_rows_produces_no_duplicate(
    fake: FakeZeroDBTransport, people: Repository[Person]
) -> None:
    """Story 1.4's second acceptance criterion, under live filter behaviour.

    This is the test that would have caught the one-page bug: with the working
    fake it passes trivially, because the server-side filter finds the row.
    With the live-faithful fake it fails unless the fallback pages.
    """
    people.upsert(person_row("person:FIRST"))
    people.upsert(person_row("person:SECOND"))
    people.upsert(person_row("person:THIRD"))

    people.upsert(person_row("person:THIRD", name="Re-ingested"))

    rows = [row["row_data"] for row in fake.rows["people"].values()]
    third = [row for row in rows if row["id"] == "person:THIRD"]
    assert len(third) == 1, f"re-ingest duplicated the row: {len(third)} copies"
    assert third[0]["canonical_name"] == "Re-ingested"


def test_a_reference_to_a_row_behind_other_rows_is_accepted(
    client: ZeroDBClient, people: Repository[Person]
) -> None:
    """Story 1.4's first acceptance criterion, in its false-negative direction.

    A reference check that cannot see past the first row would reject every
    valid reference on a populated table, failing ingest for good data.
    """
    roll_calls: Repository[RollCall] = Repository(client=client, model=RollCall)
    member_votes: Repository[MemberVote] = Repository(client=client, model=MemberVote)
    for index in range(3):
        people.upsert(person_row(f"person:SYNTH{index}"))
        roll_calls.upsert(
            {
                "id": f"us-house:119-2:{index}",
                "voted_at": "2026-03-04T15:00:00Z",
                "question": "On Passage",
                "vote_type": "final_passage",
                "result": "Passed",
                "topic_ids": [],
            }
        )

    stored = member_votes.upsert(
        {
            "roll_call_id": "us-house:119-2:2",
            "person_id": "person:SYNTH2",
            "position": "yea",
            "voted_at": "2026-03-04T15:00:00Z",
            "vote_type": "final_passage",
            "topic_ids": [],
        }
    )

    assert stored.id == "us-house:119-2:2:SYNTH2"


def test_a_genuinely_missing_reference_is_still_rejected(
    client: ZeroDBClient, people: Repository[Person]
) -> None:
    """And the true-positive direction still holds once paging is in."""
    roll_calls: Repository[RollCall] = Repository(client=client, model=RollCall)
    member_votes: Repository[MemberVote] = Repository(client=client, model=MemberVote)
    for index in range(3):
        people.upsert(person_row(f"person:SYNTH{index}"))
    roll_calls.upsert(
        {
            "id": "us-house:119-2:1",
            "voted_at": "2026-03-04T15:00:00Z",
            "question": "On Passage",
            "vote_type": "final_passage",
            "result": "Passed",
            "topic_ids": [],
        }
    )

    with pytest.raises(MissingReferenceError) as caught:
        member_votes.upsert(
            {
                "roll_call_id": "us-house:119-2:1",
                "person_id": "person:NOBODY",
                "position": "yea",
                "voted_at": "2026-03-04T15:00:00Z",
                "vote_type": "final_passage",
                "topic_ids": [],
            }
        )

    assert caught.value.referenced_id == "person:NOBODY"


def test_append_only_versioning_finds_the_current_row_behind_other_rows(
    client: ZeroDBClient, people: Repository[Person]
) -> None:
    """Two current rows is the failure mode here, not a duplicate.

    `append_version` finds the row to supersede with the same filtered read.
    If that read cannot see it, the old row keeps `is_current = true` and the
    new one sets it too — breaking datamodel.md's "exactly one is_current = true
    row per promise" check, which is what the UI reads to decide what a
    candidate's current verdict is.
    """
    from app.repositories.models import Verdict

    client.create_table("verdicts", {"id": "string"})
    client.create_table("promises", {"id": "string"})
    people.upsert(person_row("person:B000944"))
    for index in range(3):
        client.insert_row("promises", {"id": f"prm:{index}", "candidacy_id": "synthetic"})

    verdicts: Repository[Verdict] = Repository(client=client, model=Verdict)
    row = {
        "promise_id": "prm:2",
        "person_id": "person:B000944",
        "verdict": "kept",
        "confidence": 0.9,
        "methodology_version": "2026.1",
        "qa_status": "approved",
    }
    verdicts.append_version(row)
    verdicts.append_version({**row, "verdict": "broken"})

    current = verdicts.query(filter={"promise_id": "prm:2", "is_current": True}, limit=100)
    assert len(current) == 1, f"{len(current)} rows claim to be current"
    assert current[0].verdict == "broken"
