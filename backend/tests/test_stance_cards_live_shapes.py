"""Live-confirmed behaviour the stance cards depend on (story 3.2).

Same purpose as `test_issue_taxonomy_live_shapes.py` and
`test_seed_live_shapes.py`: each test here pins something measured against the
LIVE "informed" project during story 3.2, not inferred from the docs or from
the fake. The measurement output is in the story's PR.

What was measured, on 2026-10-16, with `scripts/verify_stance_cards_live.py`
(which seeds twice per invocation) against the project that already held story
3.1's 80 permanent `issue_topics` rows:

  * `stances` held exactly **201 rows** afterwards, read unfiltered, covering
    all **60 subtopics** with at least 3 each — so the upsert's find step
    really does find these rows rather than appending. The second run issued
    201 `PUT .../rows/{uuid}` and **zero** `POST .../rows`, which is the
    mechanism rather than the symptom.
  * **`direction_code` round-trips as a string, sign included.** ZeroDB's
    `stances` schema declares the column `"string"` (bootstrap.py's
    `TABLE_SCHEMAS`) and `"+1"` came back as `"+1"`, not as `1`, `1.0` or
    `"1"`. Pinned below because the whole balance check partitions on the sign:
    a `"+1"` silently coerced to the integer `1` would still compare equal
    after `int()`, but a `"+1"` stored as `"1"` would break `Stance`'s own
    field pattern on the next read and make the row invisible.
  * **`wording_version` round-trips as an `int`, not a `float`** — the same
    hazard story 2.1 pinned for `Body.seats` and 3.1 for `taxonomy_version`.
  * **An id with three colons survives as a row id.**
    `en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs` has three, one
    more than any id seeded before this story, and `ZeroDBClient`'s
    find-then-write resolves it by matching `row_data.id` rather than by
    parsing it. Confirmed by the 201-row, zero-duplicate result.
  * **No stored row carried a `default`, `preselected`, `display_order` or
    `rank` key**, read from raw `row_data` rather than through the model.
    `extra="forbid"` is what refuses such a write, but what the live read
    proves is that nothing in the ZeroDB write path adds one of its own.
  * **A stance referencing a subtopic that does not exist was refused live**,
    with `MissingReferenceError` naming `issue_topics` and the missing id — so
    `scripts/seed_taxonomy.py` is a real prerequisite rather than a documented
    convention.

Nothing here makes a live call. The payloads are the live ones, replayed
through the fake, which is what makes these runnable in CI without credentials.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from app.repositories import Repository
from app.repositories.models import Stance
from app.seed_stances import STANCES, StanceSeeder, stance_rows, stances_by_topic
from app.seed_taxonomy import EXPECTED_SUBTOPIC_COUNT, TaxonomySeeder
from app.stances.service import StanceService
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

#: Copied verbatim from an unfiltered live read of `stances` after the seed had
#: run twice (project id redacted). prd.md's own anchor subtopic, with one card
#: from each side of its axis. Note the field names and types exactly as ZeroDB
#: returned them — `direction_code` as a signed string, `wording_version` as an
#: int.
LIVE_POSITIVE_ROW: dict[str, Any] = {
    "id": "en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs",
    "topic_id": "en-US:prescription-drug-pricing:v1",
    "label": "Cap what patients pay at the pharmacy counter",
    "description": (
        "Set a legal ceiling on monthly out-of-pocket costs for insulin and other "
        "maintenance drugs, and cap annual drug spending for people on Medicare."
    ),
    "direction_code": "+1",
    "wording_version": 1,
}

LIVE_NEGATIVE_ROW: dict[str, Any] = {
    "id": "en-US:prescription-drug-pricing:v1:let-the-market-set-prices",
    "topic_id": "en-US:prescription-drug-pricing:v1",
    "label": "Leave drug prices to competition, not federal negotiation",
    "description": (
        "Repeal federal price negotiation authority and rely on generic entry, pharmacy "
        "benefit competition and faster approvals to bring prices down."
    ),
    "direction_code": "-1",
    "wording_version": 1,
}

#: The live counts, as read unfiltered.
LIVE_TOTAL_ROWS = 201
LIVE_SUBTOPICS_COVERED = 60

#: Field names checked against raw live `row_data` and found absent.
LIVE_ABSENT_FIELDS = (
    "default",
    "is_default",
    "preselected",
    "selected",
    "recommended",
    "display_order",
    "rank",
    "sort_order",
    "position",
)


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
    client.create_table("stances", {"id": "string"})
    TaxonomySeeder(client, pace=lambda: None).run()
    return client


# --- the live payloads still fit the model ---------------------------------


@pytest.mark.parametrize("live_row", [LIVE_POSITIVE_ROW, LIVE_NEGATIVE_ROW])
def test_the_live_row_validates_as_a_stance(live_row: dict[str, Any]) -> None:
    """A field renamed or dropped by ZeroDB fails here, not in story 3.4's embeddings."""
    stored = Stance.model_validate(live_row)
    assert stored.topic_id == "en-US:prescription-drug-pricing:v1"
    assert stored.wording_version == 1


def test_the_two_live_rows_are_on_opposite_sides_of_their_axis() -> None:
    """The pair was chosen so the live read itself demonstrates the span.

    If a future ZeroDB change mangled signs — dropping a leading `+`, or
    normalising `-1` — this is where it shows, because the two rows would stop
    straddling zero.
    """
    positive = int(LIVE_POSITIVE_ROW["direction_code"])
    negative = int(LIVE_NEGATIVE_ROW["direction_code"])
    assert positive > 0 > negative


def test_live_direction_code_comes_back_as_a_signed_string() -> None:
    """ZeroDB's schema says `"string"`; live returns `"+1"`, sign intact.

    Two distinct failures are pinned here. A `direction_code` returned as the
    *integer* `1` would still pass every `int()`-based balance check in this
    story while failing `Stance`'s field pattern, so the row would vanish from
    the menu on the next validated read. A `"+1"` normalised to `"1"` would do
    the same. Both are silent, and both are checked.
    """
    for row in (LIVE_POSITIVE_ROW, LIVE_NEGATIVE_ROW):
        code = row["direction_code"]
        assert isinstance(code, str), f"{row['id']} direction_code is {type(code)}"
        assert code[0] in "+-0", f"{row['id']} direction_code lost its sign: {code!r}"
        Stance.model_validate(row)  # the field pattern is the real assertion


def test_live_wording_version_comes_back_as_an_int_not_a_float() -> None:
    """ZeroDB's schema says `"number"`; live returns `1`.

    The hazard story 2.1 pinned for `Body.seats` and 3.1 for
    `taxonomy_version`: Pydantic coerces a whole float silently, so a change to
    returning `1.0` would raise nowhere and would just stop matching
    `wording_version == 1` in any pre-validation comparison — the live
    verifier's own version sweep, for one.
    """
    for row in (LIVE_POSITIVE_ROW, LIVE_NEGATIVE_ROW):
        assert isinstance(row["wording_version"], int)
        assert not isinstance(row["wording_version"], bool)


def test_no_live_row_carried_a_default_or_order_field() -> None:
    """Story 3.2's third acceptance criterion, as measured rather than asserted.

    The model forbids these fields, so a write carrying one is refused. What
    the live read adds is that nothing in ZeroDB's own write path injects an
    ordering or selection key of its own — which a document store could
    plausibly do (a `position` for insertion order, say) and which a later UI
    story would then find and honour.
    """
    for row in (LIVE_POSITIVE_ROW, LIVE_NEGATIVE_ROW):
        carried = sorted(set(row) & set(LIVE_ABSENT_FIELDS))
        assert not carried, f"{row['id']} carries {carried}"
    assert set(LIVE_POSITIVE_ROW) == {
        "id",
        "topic_id",
        "label",
        "description",
        "direction_code",
        "wording_version",
    }


def test_the_seeded_row_payload_is_complete_so_a_wholesale_put_loses_nothing() -> None:
    """ZeroDB's `PUT` replaces `row_data` wholesale (story 1.2's finding).

    The seed re-writes all 201 rows on every run, so a payload missing a field
    would strip it on the second run — and the missing field would most likely
    be `direction_code`, which leaves a card with no place on its axis and so
    unscoreable by epic 4.
    """
    by_id = {str(row.id): row for row in stance_rows()}
    for live_row in (LIVE_POSITIVE_ROW, LIVE_NEGATIVE_ROW):
        model = by_id[live_row["id"]]
        assert set(model.row_data()) == set(live_row), (
            f"{live_row['id']}: written payload and live row disagree on fields"
        )
        assert model.row_data() == live_row, (
            f"{live_row['id']}: a stored value drifted from what the seed declares"
        )


# --- the mechanism behind "exactly 201 rows" -------------------------------


def test_the_live_counts_match_what_the_seed_declares() -> None:
    assert len(STANCES) == LIVE_TOTAL_ROWS
    assert LIVE_SUBTOPICS_COVERED == EXPECTED_SUBTOPIC_COUNT == len(stances_by_topic())


def test_the_second_seed_run_updates_rather_than_appending(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """The live run's second pass issued 201 PUTs and zero POSTs.

    Pinned on the recorded calls rather than the row count, because the row
    count is the symptom and this is the cause: ZeroDB's `POST /rows` always
    appends, so "exactly one row per id" holds only as long as the upsert's
    find step resolves the natural-key id to an internal UUID and PUTs it.
    """
    seeder = StanceSeeder(client, pace=lambda: None)
    seeder.run()
    fake.calls.clear()
    seeder.run()

    inserts = [
        call
        for call in fake.calls
        if call.method == "POST" and call.path.endswith("/rows") and "/stances/" in call.path
    ]
    updates = [
        call
        for call in fake.calls
        if call.method == "PUT" and "/rows/" in call.path and "/stances/" in call.path
    ]
    assert not inserts, f"the second run appended {len(inserts)} new row(s)"
    assert len(updates) == LIVE_TOTAL_ROWS, (
        f"expected {LIVE_TOTAL_ROWS} updates, saw {len(updates)}"
    )


def test_rereading_every_row_after_two_runs_still_validates(client: ZeroDBClient) -> None:
    """The live "all 201 rows exist and still validate" check, on the fake."""
    seeder = StanceSeeder(client, pace=lambda: None)
    seeder.run()
    seeder.run()

    stances: Repository[Stance] = Repository(client=client, model=Stance)
    for expected in stance_rows():
        assert stances.get(str(expected.id)) == expected, f"{expected.id} did not round-trip"


def test_three_colons_in_an_id_do_not_break_the_find_then_write(client: ZeroDBClient) -> None:
    """`en-US:prescription-drug-pricing:v1:cap-out-of-pocket-costs` has three.

    One more than any id seeded before this story, and the first table whose id
    is literally another table's id plus a suffix. Confirmed live by the
    201-row zero-duplicate result; reproduced here because an id-parsing
    regression would not raise, it would simply stop finding rows and start
    appending them.
    """
    StanceSeeder(client, pace=lambda: None).run()

    stances: Repository[Stance] = Repository(client=client, model=Stance)
    stored = stances.get(LIVE_POSITIVE_ROW["id"])
    assert stored is not None
    assert str(stored.id).count(":") == 3
    assert stored.topic_id == LIVE_POSITIVE_ROW["topic_id"]
    assert str(stored.id).startswith(f"{stored.topic_id}:")


def test_a_live_read_through_the_service_varies_its_order(client: ZeroDBClient) -> None:
    """The live shuffled read, reproduced on the fake.

    The live run sampled 40 reads of four contested subtopics and saw every
    card appear first at least once, with more than one distinct order per
    subtopic. Pinned here on the same seeded RNG so the property is checked in
    CI rather than only in a recorded log.
    """
    StanceSeeder(client, pace=lambda: None).run()
    service = StanceService(client, rng=random.Random(20260316))

    topic_id = LIVE_POSITIVE_ROW["topic_id"]
    expected = {card.row_id for card in stances_by_topic()[topic_id]}
    orders = set()
    firsts = set()
    for _ in range(40):
        ids = tuple(str(row.id) for row in service.for_topic(topic_id))
        assert set(ids) == expected
        orders.add(ids)
        firsts.add(ids[0])
    assert len(orders) > 1
    assert firsts == expected, f"only {len(firsts)} of {len(expected)} cards ever came first"
