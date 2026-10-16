"""Live-confirmed behaviour the issue taxonomy depends on (story 3.1).

Same purpose as `test_seed_live_shapes.py` and `test_repository_live_shapes.py`:
each test here pins something measured against the LIVE "informed" project
during story 3.1, not inferred from the docs or from the fake. The measurement
output is in the story's PR.

What was measured, on 2026-10-16, with the seed run four times (twice via
`scripts/verify_issue_taxonomy_live.py`, which seeds twice per invocation):

  * `issue_topics` held exactly **80 rows** afterwards, read unfiltered —
    20 with `parent_id` null and 60 with a parent — so the upsert's find step
    really does find these rows rather than appending. Runs 2, 3 and 4 issued
    80 `PUT .../rows/{uuid}` and **zero** `POST .../rows`, which is the
    mechanism rather than the symptom.
  * **`parent_id` round-trips as JSON `null`, not as a missing key or `""`.**
    Pinned below because the live tree walk partitions parents from children on
    exactly that value: a top-level row coming back with `parent_id` absent, or
    as an empty string, would be classified either way depending on the test
    and would silently change the 20/60 split.
  * **`taxonomy_version` round-trips as an `int`, not a `float`.** ZeroDB's
    table schema declares it `"number"` (bootstrap.py's `TABLE_SCHEMAS`) and
    the live rows came back `1`, not `1.0`. The same hazard story 2.1 pinned
    for `Body.seats`: Pydantic coerces a whole float silently, so a future
    ZeroDB change to returning floats would not raise — the version would just
    stop comparing equal to a literal `1` anywhere that compares before
    validation.
  * The seeded rows re-validate against `IssueTopic` after a live round trip
    with every field intact. Not automatic: ZeroDB's `PUT` replaces `row_data`
    wholesale (story 1.2's finding), so an upsert writing a partial payload
    would strip `parent_id` and orphan the subtopic.
  * Ids containing **colons survive as row ids**. `en-US:healthcare:v1` has two
    colons, and `ZeroDBClient`'s find-then-write resolves it to an internal
    UUID by matching `row_data.id` rather than by parsing it, so no segment is
    ever interpreted. Confirmed by the 80-row, zero-duplicate result.

Nothing here makes a live call. The payloads are the live ones, replayed
through the fake, which is what makes these runnable in CI without credentials.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.repositories import Repository
from app.repositories.models import IssueTopic
from app.seed_taxonomy import (
    EXPECTED_SUBTOPIC_COUNT,
    EXPECTED_TOPIC_COUNT,
    TaxonomySeeder,
    taxonomy_rows,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

#: Copied verbatim from an unfiltered live read of `issue_topics` after the
#: seed had run four times (project id redacted). One parent and one child —
#: prd.md's own "Healthcare -> Prescription drug pricing" pair. Note the field
#: names and types exactly as ZeroDB returned them, `parent_id: None` included.
LIVE_PARENT_ROW: dict[str, Any] = {
    "id": "en-US:healthcare:v1",
    "locale": "en-US",
    "name": "Healthcare",
    "parent_id": None,
    "taxonomy_version": 1,
}

LIVE_CHILD_ROW: dict[str, Any] = {
    "id": "en-US:prescription-drug-pricing:v1",
    "locale": "en-US",
    "name": "Prescription drug pricing",
    "parent_id": "en-US:healthcare:v1",
    "taxonomy_version": 1,
}

#: The live counts, as read unfiltered.
LIVE_TOTAL_ROWS = 80
LIVE_PARENT_COUNT = 20
LIVE_CHILD_COUNT = 60


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table("issue_topics", {"id": "string"})
    return client


# --- the live payloads still fit the model ---------------------------------


def test_the_live_parent_row_validates_as_an_issue_topic() -> None:
    """A field renamed or dropped by ZeroDB fails here, not in story 3.2's stances."""
    stored = IssueTopic.model_validate(LIVE_PARENT_ROW)
    assert stored.parent_id is None
    assert stored.name == "Healthcare"


def test_the_live_child_row_validates_and_points_at_the_live_parent() -> None:
    child = IssueTopic.model_validate(LIVE_CHILD_ROW)
    assert child.parent_id == LIVE_PARENT_ROW["id"]


def test_live_parent_id_comes_back_as_null_not_a_missing_key_or_empty_string() -> None:
    """The 20/60 split is partitioned on exactly this value.

    A top-level row whose `parent_id` came back absent would still validate
    (the field is optional) but would be classified by `"parent_id" in row`
    rather than by `row["parent_id"] is None`, and an empty string would be
    classified as a *child* with a parent that does not exist. Asserting the
    live shape is what keeps every parent/child count in this story honest.
    """
    assert "parent_id" in LIVE_PARENT_ROW, "ZeroDB dropped the null key entirely"
    assert LIVE_PARENT_ROW["parent_id"] is None
    assert LIVE_PARENT_ROW["parent_id"] != ""


def test_live_taxonomy_version_comes_back_as_an_int_not_a_float() -> None:
    """ZeroDB's schema says `"number"`; live returns `int`.

    The hazard story 2.1 pinned for `Body.seats`, in the one other place a
    `"number"` column is compared against a literal: Pydantic coerces a whole
    float silently, so a change to returning `1.0` would raise nowhere and
    would just stop matching `taxonomy_version == 1` in any pre-validation
    comparison (the live verifier's own locale/version sweep, for one).
    """
    for row in (LIVE_PARENT_ROW, LIVE_CHILD_ROW):
        assert isinstance(row["taxonomy_version"], int), (
            f"{row['id']} taxonomy_version is {type(row['taxonomy_version'])}"
        )
        assert not isinstance(row["taxonomy_version"], bool)


def test_the_seeded_row_payload_is_complete_so_a_wholesale_put_loses_nothing() -> None:
    """ZeroDB's `PUT` replaces `row_data` wholesale (story 1.2's finding).

    The seed re-writes all 80 rows on every run, so a payload missing a field
    would strip it on the second run — and for a subtopic the missing field
    would be `parent_id`, which orphans it.
    """
    by_id = {str(row.id): row for row in taxonomy_rows()}
    for live_row in (LIVE_PARENT_ROW, LIVE_CHILD_ROW):
        model = by_id[live_row["id"]]
        assert set(model.row_data()) == set(live_row), (
            f"{live_row['id']}: written payload and live row disagree on fields"
        )


# --- the mechanism behind "exactly 80 rows" --------------------------------


def test_the_live_counts_match_what_the_seed_declares() -> None:
    assert LIVE_TOTAL_ROWS == EXPECTED_TOPIC_COUNT + EXPECTED_SUBTOPIC_COUNT
    assert LIVE_PARENT_COUNT == EXPECTED_TOPIC_COUNT
    assert LIVE_CHILD_COUNT == EXPECTED_SUBTOPIC_COUNT


def test_the_second_seed_run_updates_rather_than_appending(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """The live run's later passes issued 80 PUTs and zero POSTs.

    Pinned on the recorded calls rather than the row count, because the row
    count is the symptom and this is the cause: ZeroDB's `POST /rows` always
    appends, so "exactly one row per id" holds only as long as the upsert's
    find step resolves the natural-key id to an internal UUID and PUTs it.
    """
    seeder = TaxonomySeeder(client, pace=lambda: None)
    seeder.run()
    fake.calls.clear()
    seeder.run()

    inserts = [call for call in fake.calls if call.method == "POST" and call.path.endswith("/rows")]
    updates = [call for call in fake.calls if call.method == "PUT" and "/rows/" in call.path]

    assert not inserts, f"the second run appended {len(inserts)} new row(s)"
    assert len(updates) == LIVE_TOTAL_ROWS, (
        f"expected {LIVE_TOTAL_ROWS} updates, saw {len(updates)}"
    )


def test_rereading_every_row_after_two_runs_still_validates(client: ZeroDBClient) -> None:
    """The live "all 80 rows exist and still validate" check, on the fake."""
    seeder = TaxonomySeeder(client, pace=lambda: None)
    seeder.run()
    seeder.run()

    topics: Repository[IssueTopic] = Repository(client=client, model=IssueTopic)
    for expected in taxonomy_rows():
        assert topics.get(str(expected.id)) == expected, f"{expected.id} did not round-trip"


def test_colons_in_an_id_do_not_break_the_find_then_write(client: ZeroDBClient) -> None:
    """`en-US:healthcare:v1` has two colons and is matched whole, never parsed.

    Confirmed live by the 80-row zero-duplicate result; reproduced here because
    this is the first table whose ids carry more than one colon *and* whose
    rows reference each other by that id.
    """
    seeder = TaxonomySeeder(client, pace=lambda: None)
    seeder.run()

    topics: Repository[IssueTopic] = Repository(client=client, model=IssueTopic)
    stored = topics.get("en-US:prescription-drug-pricing:v1")
    assert stored is not None
    assert stored.id == "en-US:prescription-drug-pricing:v1"
    assert stored.parent_id == "en-US:healthcare:v1"
