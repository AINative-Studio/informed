"""Seeding story 2.1's reference data into a fake ZeroDB (story 2.3).

Story 2.3 declared `districts.body_id` a reference to `bodies` — the call story
1.4's `District` docstring explicitly deferred to this story, because 2.3 is
what writes real district rows and so is what makes the check load-bearing.

The consequence for tests is that a `districts` row can no longer be written
into an empty fake: its chamber must exist first, and the chamber's
jurisdiction before that (`Body` references `jurisdictions`). Rather than have
four step files and two unit-test modules each hand-roll that two-row prelude —
and drift from it — they all call `seed_reference_data` here.

The rows come from `app.seed`'s own `US_FEDERAL` and `FEDERAL_BODIES` constants
rather than being retyped, so a fixture cannot claim a chamber the seed does not
actually write. That is the same reason story 2.3's step defs import
`_provisional_district_id` instead of hard-coding an OCD-ID: a test that copies
a value cannot notice when the value changes.
"""

from __future__ import annotations

from app.repositories import Repository
from app.repositories.models import Body, Jurisdiction
from app.seed import FEDERAL_BODIES, US_FEDERAL
from app.zerodb import ZeroDBClient

#: Every table a `districts` write now transitively needs, in dependency order.
#: `create_table` on the fake is idempotent for these purposes, so a caller that
#: already made some of them loses nothing by passing them again.
REFERENCE_TABLES = ("jurisdictions", "bodies")


def create_reference_tables(client: ZeroDBClient) -> None:
    """Create the tables `seed_reference_data` writes into."""
    for table in REFERENCE_TABLES:
        client.create_table(table, {"id": "string"})


def seed_reference_data(client: ZeroDBClient) -> None:
    """Write `us-federal`, `us-house` and `us-senate`, in reference order.

    The jurisdiction first, because `Body.jurisdiction_id` is itself a declared
    reference — the same ordering story 2.1's seed asserts rather than assumes
    (`tests/features/seed_reference_data.feature`).
    """
    Repository(client=client, model=Jurisdiction).upsert(
        US_FEDERAL.row_data() | {"id": US_FEDERAL.id}
    )
    bodies: Repository[Body] = Repository(client=client, model=Body)
    for body in FEDERAL_BODIES:
        bodies.upsert(body.row_data() | {"id": body.id})


__all__ = ["REFERENCE_TABLES", "create_reference_tables", "seed_reference_data"]
