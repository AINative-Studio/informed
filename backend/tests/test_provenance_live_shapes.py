"""Live-confirmed facts story 16.3's enforcement rests on.

Same purpose as `test_repository_live_shapes.py`: each test here pins something
measured against the LIVE "informed" project on 2026-10-16, not inferred, and
reproduces it against the fake so it cannot regress unnoticed. The measurement
commands and raw output are in this story's PR; the script that produced them
is `backend/scripts/verify_provenance_live.py`, which passed all ten of its
checks.

Two findings shape this story, and both are recorded here rather than only in a
PR nobody will re-read:

**1. No live row carries a `source_url` at all.** The retrofit question stories
2.1, 2.2, 3.1 and 3.2 raise, answered from the data: 831 rows across all 23
tables were read (every one of `people`'s 539, paged), and not one carries a
`source_url`. `people`, `jurisdictions`, `bodies`, `districts`, `issue_topics`
and `stances` have no such field in datamodel.md's field lists, so those
stories are exempt from the rule rather than in violation of it — and
`roll_calls`, the one sourced table with an adapter, is still empty because
story 5.1 could not run live for lack of a Congress.gov API key. **No backfill
was needed.**

**2. A content-addressed id means two rows with the same hash ARE one row.**
Obvious in retrospect, and it cost this story's first live run four failed
checks: the verification script's decoy provenance rows used content hashes
that collided with the row under test's, so the real write's check-then-insert
correctly found the decoy and wrote nothing, and the script reported the
mechanism broken while the mechanism was right. The property is now pinned
below, because a future test or fixture can make exactly the same mistake.
"""

from __future__ import annotations

import pytest

from app.bootstrap import Bootstrapper
from app.repositories import ProvenanceContext, Repository
from app.repositories.models import District, IssueTopic, Person, RollCall, Stance
from app.repositories.provenance import provenance_id, requires_provenance
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

#: Every table measured live on 2026-10-16, with its row count and how many of
#: those rows carried a `source_url`. The second number is zero everywhere,
#: which is the finding.
LIVE_ROW_COUNTS: dict[str, tuple[int, int]] = {
    "audit_runs": (0, 0),
    "bills": (0, 0),
    "bodies": (2, 0),
    "candidacies": (0, 0),
    "candidate_cards": (0, 0),
    "candidate_positions": (0, 0),
    "candidate_statements": (0, 0),
    "corrections": (0, 0),
    "districts": (7, 0),
    "elections": (0, 0),
    "issue_topics": (80, 0),
    "jurisdictions": (1, 0),
    "member_votes": (0, 0),
    "methodology_versions": (0, 0),
    "people": (539, 0),
    "promise_sources": (0, 0),
    "promises": (0, 0),
    "roll_calls": (0, 0),
    "schema_versions": (1, 0),
    "stances": (201, 0),
    "verdicts": (0, 0),
    "voter_profile_keys": (0, 0),
    "voter_profiles": (0, 0),
}


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    """A fake whose server-side filter is as broken as the live API's.

    `write_provenance`'s check-then-insert is a `get_row` by the
    content-addressed id, which goes through the client-side filter fallback
    the live API's broken `/query` filter forces (story 1.4). A fake with a
    working filter never exercises that path, which is how the one-page bug
    stayed invisible in CI while being reproducible live in one request.
    """
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    zerodb = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(zerodb, pace=lambda: None).run()
    return zerodb


def _roll_call_row(content_hash: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "us-house:119-2:452",
        "voted_at": "2026-03-04T15:00:00Z",
        "question": "On Passage",
        "vote_type": "final_passage",
        "result": "Passed",
        "source_url": "https://api.congress.gov/v3/house-vote/119/2/452",
        "content_hash": content_hash,
    }
    row.update(overrides)
    return row


def _provenance_ids(fake: FakeZeroDBTransport) -> set[str]:
    return {str(row["row_data"]["id"]) for row in fake.rows.get("provenance_log", {}).values()}


# =========================================================================
# Finding 1: the tables carrying no source_url are exempt, not in violation
# =========================================================================


@pytest.mark.parametrize("model", [Person, District, IssueTopic, Stance])
def test_the_tables_already_loaded_live_carry_no_source_url(model: type) -> None:
    """`people` (539 rows), `districts` (7), `issue_topics` (80) and `stances`
    (201, from story 3.2, which landed while this story was in flight) were all
    loaded before this story and all passed the live audit. They pass because
    their models declare no `source_url` — datamodel.md's field lists do not
    give them one — so datamodel.md's integrity rule does not reach them and
    stories 2.1, 2.2, 3.1 and 3.2 needed no backfill.
    """
    assert "source_url" not in model.model_fields
    assert not requires_provenance(model)


def test_roll_calls_is_the_only_loaded_table_the_rule_reaches() -> None:
    """Of the tables with data live, none is sourced; of the sourced models,
    `roll_calls` is the only one with an adapter today, and it is still empty
    because story 5.1 has no API key. That is why the live audit found zero
    violations rather than a backlog.
    """
    assert requires_provenance(RollCall)
    assert LIVE_ROW_COUNTS["roll_calls"] == (0, 0)


def test_no_live_row_carried_a_source_url() -> None:
    """831 rows, 23 tables, every one of `people`'s 539 paged. Pinned as a
    table rather than a sentence so a future reader can see which tables were
    measured and at what size.
    """
    total = sum(count for count, _ in LIVE_ROW_COUNTS.values())
    sourced = sum(with_source for _, with_source in LIVE_ROW_COUNTS.values())
    assert total == 831
    assert sourced == 0


# =========================================================================
# Finding 2: equal content hashes are one row, by design
# =========================================================================


def test_two_rows_with_the_same_content_hash_share_one_provenance_entry(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """The mistake that cost this story's first live run four failed checks.

    Two DIFFERENT entity rows whose adapters reported the SAME content hash
    land on one provenance entry, because the id is a function of the hash and
    nothing else. That is correct — the same bytes were fetched once — and it
    means a fixture cannot reuse a hash across rows it expects to be distinct.
    """
    repo = Repository(
        client=client,
        model=RollCall,
        provenance=ProvenanceContext(adapter_version="live_shapes/v1"),
    )
    shared_hash = "c" * 64
    repo.upsert(_roll_call_row(shared_hash, id="us-house:119-2:451"))
    repo.upsert(_roll_call_row(shared_hash, id="us-house:119-2:452"))
    assert _provenance_ids(fake) == {provenance_id(shared_hash)}


def test_distinct_content_hashes_give_distinct_entries(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """The other side, so the test above is not satisfied by a broken write
    path that records nothing at all.
    """
    repo = Repository(
        client=client,
        model=RollCall,
        provenance=ProvenanceContext(adapter_version="live_shapes/v1"),
    )
    repo.upsert(_roll_call_row("d" * 64))
    repo.upsert(_roll_call_row("e" * 64, result="Agreed to"))
    assert _provenance_ids(fake) == {provenance_id("d" * 64), provenance_id("e" * 64)}


def test_the_find_step_works_with_the_live_broken_server_side_filter(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """Confirmed live: re-writing identical content adds no second entry, even
    though the row is not the table's first.

    Decoy entries are written ahead of the row under test for the reason story
    1.4 documents: with one row in the table, a find step that only ever
    inspects row 0 passes anyway.
    """
    for index in range(3):
        client.insert_row(
            "provenance_log",
            {
                "id": provenance_id(f"f{index}" * 32),
                "entity_table": "roll_calls",
                "entity_id": f"us-house:119-2:decoy-{index}",
                "source_url": f"https://example.org/decoy-{index}",
                "fetched_at": "2026-10-16T00:00:00+00:00",
                "adapter_version": "live_shapes/v1",
            },
        )
    repo = Repository(
        client=client,
        model=RollCall,
        provenance=ProvenanceContext(adapter_version="live_shapes/v1"),
    )
    repo.upsert(_roll_call_row("0a" * 32))
    repo.upsert(_roll_call_row("0a" * 32))
    assert len(fake.rows.get("provenance_log", {})) == 4, (
        "the content-addressed find step did not find the existing row behind "
        "the decoys — every re-ingest would duplicate its provenance entry"
    )
