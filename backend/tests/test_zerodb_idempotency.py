"""Natural-key addressing against ZeroDB's internal row ids (story 1.2).

These tests exist because a live check against the real "informed" project
caught two things the first version of the in-memory fake had hidden:

  1. `POST /tables/{t}/rows` always APPENDS. ZeroDB has no upsert-by-natural-key,
     so two `upsert_row` calls with the same Informed id produced two rows —
     exactly the duplicate that datamodel.md rule 1 forbids.
  2. `PUT`/`DELETE /tables/{t}/rows/{row_id}` require ZeroDB's internal UUID.
     Passing Informed's natural-key id (`us-federal`) returns
     422 "Input should be a valid UUID".

So the client has to resolve a natural-key id to the internal `row_id` before
updating or deleting, and has to turn an upsert into find-then-update or insert.
The fake was corrected to append like the real API, so these tests would fail
against the old behaviour.
"""

import pytest

from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBNotFoundError
from app.zerodb.testing import FakeZeroDBTransport

TABLE = "jurisdictions"


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table(TABLE, {"id": "string", "name": "string"})
    return client


def test_the_fake_appends_rows_exactly_as_the_live_api_does(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """Guards the fake's fidelity on the point that caused the original bug.

    A fake that silently de-duplicated would let a real duplicate-row defect
    reach production green.
    """
    client.insert_row(TABLE, {"id": "us-federal", "name": "first"})
    client.insert_row(TABLE, {"id": "us-federal", "name": "second"})

    assert len(fake.rows[TABLE]) == 2


def test_upserting_twice_leaves_one_row_with_the_latest_values(
    client: ZeroDBClient,
) -> None:
    """datamodel.md rule 1: re-ingesting a record is an idempotent upsert."""
    client.upsert_row(TABLE, "us-federal", {"name": "United States"})
    client.upsert_row(TABLE, "us-federal", {"name": "United States of America"})

    page = client.query_rows(TABLE, filter={"id": "us-federal"})

    assert page.total == 1
    assert page.rows[0].row_data["name"] == "United States of America"


def test_upserting_a_second_distinct_id_does_not_disturb_the_first(
    client: ZeroDBClient,
) -> None:
    client.upsert_row(TABLE, "us-federal", {"name": "United States"})
    client.upsert_row(TABLE, "eu", {"name": "European Union"})
    client.upsert_row(TABLE, "us-federal", {"name": "USA"})

    assert client.query_rows(TABLE).total == 2
    assert client.get_row(TABLE, "eu").row_data["name"] == "European Union"
    assert client.get_row(TABLE, "us-federal").row_data["name"] == "USA"


def test_update_row_addresses_zerodbs_internal_uuid_not_the_natural_key(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """The live API answers 422 for a natural-key id in the {row_id} segment."""
    inserted = client.upsert_row(TABLE, "us-federal", {"name": "United States"})
    fake.calls.clear()

    client.update_row(TABLE, "us-federal", {"name": "Updated"})

    put = next(call for call in fake.calls if call.method == "PUT")
    assert put.path.endswith(f"/rows/{inserted.row_id}")
    assert "us-federal" not in put.path
    assert client.get_row(TABLE, "us-federal").row_data["name"] == "Updated"


def test_delete_row_addresses_zerodbs_internal_uuid_not_the_natural_key(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    inserted = client.upsert_row(TABLE, "us-federal", {"name": "United States"})
    fake.calls.clear()

    client.delete_row(TABLE, "us-federal")

    deleted = next(call for call in fake.calls if call.method == "DELETE")
    assert deleted.path.endswith(f"/rows/{inserted.row_id}")
    assert client.get_row(TABLE, "us-federal") is None


def test_an_update_preserves_fields_it_does_not_mention(
    client: ZeroDBClient,
) -> None:
    """A partial update must not drop the natural-key id or untouched fields."""
    client.upsert_row(TABLE, "us-federal", {"name": "United States", "level": "federal"})

    client.update_row(TABLE, "us-federal", {"name": "USA"})

    row = client.get_row(TABLE, "us-federal")
    assert row is not None
    assert row.row_data["id"] == "us-federal"
    assert row.row_data["level"] == "federal"
    assert row.row_data["name"] == "USA"


def test_updating_a_row_that_does_not_exist_raises_not_found(
    client: ZeroDBClient,
) -> None:
    with pytest.raises(ZeroDBNotFoundError):
        client.update_row(TABLE, "person:NOBODY", {"name": "x"})


def test_deleting_a_row_that_does_not_exist_raises_not_found(
    client: ZeroDBClient,
) -> None:
    with pytest.raises(ZeroDBNotFoundError):
        client.delete_row(TABLE, "person:NOBODY")


def test_the_fake_replaces_row_data_on_put_exactly_as_the_live_api_does(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """Guards the fake's fidelity on the third bug the live check caught.

    A fake that merged on PUT would hide the client dropping a row's id.
    """
    inserted = client.insert_row(TABLE, {"id": "us-federal", "name": "United States"})
    assert inserted.row_id is not None

    fake.request(
        "PUT",
        f"/projects/proj-test/database/tables/{TABLE}/rows/{inserted.row_id}",
        json={"row_data": {"name": "Updated"}},
    )

    assert fake.rows[TABLE][inserted.row_id]["row_data"] == {"name": "Updated"}


def test_an_upsert_states_the_whole_record_so_dropped_fields_disappear(
    client: ZeroDBClient,
) -> None:
    """An upsert is the record's full current contents, unlike a partial update."""
    client.upsert_row(TABLE, "us-federal", {"name": "United States", "stale": "x"})
    client.upsert_row(TABLE, "us-federal", {"name": "United States"})

    row = client.get_row(TABLE, "us-federal")
    assert row is not None
    assert "stale" not in row.row_data
    assert row.row_data["id"] == "us-federal"


def test_an_upsert_of_a_new_id_inserts_rather_than_failing(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """The first write of an id must POST, not PUT a row that is not there."""
    client.upsert_row(TABLE, "eu", {"name": "European Union"})

    assert any(call.method == "POST" and call.path.endswith("/rows") for call in fake.calls)
    assert client.get_row(TABLE, "eu") is not None
