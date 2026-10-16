"""Live-confirmed behaviour the reference-data seed depends on (story 2.1).

Same purpose as `test_zerodb_live_shapes.py` and
`test_repository_live_shapes.py`: each test here pins something measured
against the LIVE "informed" project during story 2.1, not inferred from the
docs or from the fake. The measurement output is in the story's PR.

What was measured, on 2026-10-16, with the seed run three times (twice via
`scripts/verify_seed_live.py` and once via a full `scripts/bootstrap.py`):

  * `jurisdictions` held exactly 1 row and `bodies` exactly 2 afterwards, read
    unfiltered — so the upsert's find step really does find these rows rather
    than appending. The second and third runs issued `PUT .../rows/{uuid}`,
    not another `POST .../rows`, which is the mechanism rather than the
    symptom.
  * **`seats` round-trips as an `int`, not a `float`.** ZeroDB's table schema
    declares it `"number"` (bootstrap.py's `TABLE_SCHEMAS`) and the live rows
    came back `435` and `100`, not `435.0`/`100.0`. Pinned below because
    `Body.seats` is typed `int` and Pydantic would coerce a float silently —
    so a future ZeroDB change to returning floats would not raise, it would
    just mean a stored `434.9999` quietly became 434.
  * The seeded rows re-validate against their models after a live round trip,
    with every field intact. That is not automatic: ZeroDB's `PUT` replaces
    `row_data` wholesale (story 1.2's finding), so an upsert that wrote a
    partial payload would strip fields and the model would then reject the
    stored row.

Nothing here makes a live call. The payloads are the live ones, replayed
through the fake, which is what makes these runnable in CI without
credentials.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.repositories import Repository
from app.repositories.models import Body, Jurisdiction
from app.seed import FEDERAL_BODIES, US_FEDERAL, Seeder
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

#: Copied verbatim from an unfiltered live read of the `jurisdictions` table
#: after the seed ran three times (project id redacted). Note the field names
#: and types exactly as ZeroDB returned them.
LIVE_JURISDICTION_ROW: dict[str, Any] = {
    "active": True,
    "adapter_name": "us-federal",
    "country_code": "US",
    "id": "us-federal",
    "level": "federal",
    "name": "United States",
}

#: Likewise for `bodies`. Both rows, in the order live returned them — which
#: is NOT insertion order (story 1.4 established that ZeroDB's unfiltered row
#: order is unspecified), and is why nothing anywhere depends on it.
LIVE_BODY_ROWS: list[dict[str, Any]] = [
    {
        "chamber": "upper",
        "id": "us-senate",
        "jurisdiction_id": "us-federal",
        "name": "U.S. Senate",
        "seats": 100,
    },
    {
        "chamber": "lower",
        "id": "us-house",
        "jurisdiction_id": "us-federal",
        "name": "U.S. House of Representatives",
        "seats": 435,
    },
]


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
    client.create_table("jurisdictions", {"id": "string"})
    client.create_table("bodies", {"id": "string"})
    return client


# --- the live payloads still fit the models -------------------------------


def test_the_live_jurisdiction_row_validates_as_a_jurisdiction() -> None:
    """A field renamed or dropped by ZeroDB fails here, not in story 2.2's ingest."""
    assert Jurisdiction.model_validate(LIVE_JURISDICTION_ROW) == US_FEDERAL


def test_the_live_body_rows_validate_as_the_seeded_bodies() -> None:
    stored = {row["id"]: Body.model_validate(row) for row in LIVE_BODY_ROWS}
    expected = {str(body.id): body for body in FEDERAL_BODIES}
    assert stored == expected


def test_live_seat_counts_come_back_as_ints_not_floats() -> None:
    """ZeroDB's schema says `"number"`; live returns `int`.

    `Body.seats` is typed `int`, and Pydantic coerces a whole float silently —
    so if ZeroDB ever starts returning `434.99` for a `"number"` column, no
    exception would surface; the seat count would just be wrong by one. This
    test is the only place that change is visible, which is why it asserts on
    the *type* of the live value rather than on its equality with 435.
    """
    for row in LIVE_BODY_ROWS:
        assert isinstance(row["seats"], int), f"{row['id']} seats is {type(row['seats'])}"
        assert not isinstance(row["seats"], bool)


def test_the_seeded_row_payload_is_complete_so_a_wholesale_put_loses_nothing() -> None:
    """ZeroDB's `PUT` replaces `row_data` wholesale (story 1.2's finding).

    The seed re-writes all three rows on every run, so a payload missing a
    field would strip it from the stored row on the second run. Asserting the
    written payload carries every model field is what makes the "fields
    survive a re-run" live check mean something.
    """
    for model, live_row in (
        (US_FEDERAL, LIVE_JURISDICTION_ROW),
        *((body, next(r for r in LIVE_BODY_ROWS if r["id"] == body.id)) for body in FEDERAL_BODIES),
    ):
        assert set(model.row_data()) == set(live_row), (
            f"{model.id}: written payload and live row disagree on fields"
        )


# --- the mechanism behind "exactly one row" -------------------------------


def test_the_second_seed_run_updates_rather_than_appending(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """The live run's second pass issued PUTs, not a second round of POSTs.

    Pinned on the recorded calls rather than the row count, because the row
    count is the symptom and this is the cause: ZeroDB's `POST /rows` always
    appends, so "exactly one row" holds only as long as the upsert's find step
    resolves the natural-key id to an internal UUID and PUTs it.
    """
    seeder = Seeder(client, pace=lambda: None)
    seeder.run()
    fake.calls.clear()
    seeder.run()

    row_writes = [
        call for call in fake.calls if call.method in {"POST", "PUT"} and "/rows" in call.path
    ]
    inserts = [call for call in row_writes if call.method == "POST" and call.path.endswith("/rows")]
    updates = [call for call in row_writes if call.method == "PUT"]

    assert not inserts, f"the second run appended {len(inserts)} new row(s)"
    assert len(updates) == 3, f"expected 3 updates, saw {len(updates)}"


def test_rereading_a_seeded_row_after_two_runs_still_validates(client: ZeroDBClient) -> None:
    """The live "fields survive the second run" check, reproduced on the fake."""
    seeder = Seeder(client, pace=lambda: None)
    seeder.run()
    seeder.run()

    jurisdictions: Repository[Jurisdiction] = Repository(client=client, model=Jurisdiction)
    bodies: Repository[Body] = Repository(client=client, model=Body)

    assert jurisdictions.get("us-federal") == US_FEDERAL
    for body in FEDERAL_BODIES:
        assert bodies.get(str(body.id)) == body
