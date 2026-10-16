"""Live-confirmed behaviour the legislator loader depends on (story 2.2).

Same purpose as `test_repository_live_shapes.py` and `test_seed_live_shapes.py`:
each test here pins something measured against the LIVE "informed" project
during story 2.2, not inferred from the docs or the fake. The measurement
output is in the story's PR (`scripts/verify_legislators_live.py`'s run).

What was measured, live, on 2026-10-16, loading the fixture roster
(tests/fixtures/legislators_current_sample.yaml -- Maria Cantwell, Eleanor
Holmes Norton, Bernie Sanders) twice against the real "informed" project:

  * **The first load issued `POST .../people/rows` once per person** (three
    inserts), and **the second load issued `PUT .../people/rows/{uuid}` once
    per person** -- never a second `POST .../people/rows`. This is the live
    proof that `Repository.upsert`'s find-then-write (via
    `ZeroDBClient.upsert_row`) really finds the existing row on a re-ingest of
    this loader's rows specifically, not merely in the abstract per story
    1.4's own live check. Pinned here as the mechanism, not just the
    row-count symptom, exactly as `test_seed_live_shapes.py` already pins the
    same mechanism for `app.seed`.
  * **A senator's stored `terms[]` round-tripped with both the 118th- and
    119th-Congress-overlapping terms intact** after two loads -- the
    boundary dates (`2019-01-03`, `2025-01-03`, `2031-01-03`) survived
    ZeroDB's JSON round trip as the same strings, not reformatted or
    coerced, which matters because `BallotService._is_current`
    (story 1.5) parses them as ISO dates on every ballot lookup.
  * **Exactly one live row carried each bioguide id after two loads**, read
    unfiltered via the client's paging fallback (the same mechanism story
    1.4 built after live ZeroDB's server-side `/query` filter was found to
    match nothing) -- so the loader's idempotency claim holds against the
    real API's actual filter behaviour, not only against the fake.
  * **The full live roster (`legislators-current.yaml`, fetched live) parsed
    and transformed with zero skips across all 539 current records** -- see
    the story's PR for the dry-run transcript. Every record carried a
    bioguide id and at least one term overlapping the 118th or 119th
    Congress, so none of `transform_legislators`' skip paths were exercised
    by real data; `test_legislators_unit.py` is what proves those paths
    still behave correctly, since the live roster itself never reaches them.

Nothing here makes a live call. The payloads are the live ones, replayed
through the fake, which is what makes these runnable in CI without
credentials.
"""

from __future__ import annotations

from app.ingest.legislators import LegislatorLoader
from app.repositories import Repository
from app.repositories.models import Person
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

_CANTWELL_ROW = {
    "id": "person:C000127",
    "canonical_name": "Maria Cantwell",
    "external_ids": {"bioguide": "C000127", "govtrack": "300018"},
    "photo_url": None,
    "terms": [
        {
            "body_id": "us-senate",
            "district_id": "ocd-division/country:us/state:wa",
            "party": "Democrat",
            "start": "2019-01-03",
            "end": "2025-01-03",
        },
        {
            "body_id": "us-senate",
            "district_id": "ocd-division/country:us/state:wa",
            "party": "Democrat",
            "start": "2025-01-03",
            "end": "2031-01-03",
        },
    ],
}


def _client() -> tuple[ZeroDBClient, FakeZeroDBTransport]:
    fake = FakeZeroDBTransport()
    fake.break_server_side_filter()
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table("people", {"id": "string"})
    return client, fake


def test_a_second_load_of_the_same_person_issues_put_not_a_second_post() -> None:
    """Pins the live-confirmed upsert mechanism, not just the row count.

    Mirrors exactly what `scripts/verify_legislators_live.py` observed live:
    the first load's write is `POST .../rows` (an insert); the second load's
    write for the same bioguide id is `PUT .../rows/{uuid}` (a replace),
    because `upsert_row`'s find step locates the existing row.
    """
    client, fake = _client()
    loader = LegislatorLoader(client, pace=lambda: None)

    loader.load([_CANTWELL_ROW])
    first_calls = [call for call in fake.calls if call.method in {"POST", "PUT"}]
    assert any(
        call.method == "POST" and call.path.endswith("/people/rows") for call in first_calls
    ), f"expected an insert POST on the first load, got {first_calls!r}"

    fake.calls.clear()
    loader.load([_CANTWELL_ROW])
    second_calls = [call for call in fake.calls if call.method in {"POST", "PUT"}]
    assert not any(
        call.method == "POST" and call.path.endswith("/people/rows") for call in second_calls
    ), f"a second load must never re-POST an insert, got {second_calls!r}"
    assert any(call.method == "PUT" for call in second_calls), (
        f"expected a PUT (replace) on the second load, got {second_calls!r}"
    )


def test_terms_round_trip_across_two_loads_with_boundary_dates_intact() -> None:
    """The 118th/119th-overlapping term dates survive a second load unchanged.

    Live-confirmed on Maria Cantwell's real terms: `BallotService._is_current`
    parses `start`/`end` as ISO dates on every ballot lookup, so a round trip
    that reformatted or coerced them would silently break delegation matching
    the moment story 2.3 seeds matching districts.
    """
    client, _fake = _client()
    loader = LegislatorLoader(client, pace=lambda: None)

    loader.load([_CANTWELL_ROW])
    loader.load([_CANTWELL_ROW])

    people: Repository[Person] = Repository(client=client, model=Person)
    stored = people.get("person:C000127")
    assert stored is not None
    assert [(t.start, t.end) for t in stored.terms] == [
        ("2019-01-03", "2025-01-03"),
        ("2025-01-03", "2031-01-03"),
    ]


def test_exactly_one_row_per_bioguide_after_two_loads() -> None:
    """Live-confirmed row count under the client's broken-filter workaround.

    `break_server_side_filter()` on the fake reproduces what story 1.4
    measured live -- a server-side `/query` filter that matches nothing --
    so this is the same code path `verify_legislators_live.py` exercised
    against the real API, not an easier one.
    """
    client, fake = _client()
    loader = LegislatorLoader(client, pace=lambda: None)

    loader.load([_CANTWELL_ROW])
    loader.load([_CANTWELL_ROW])

    stored_rows = [
        row
        for row in fake.rows.get("people", {}).values()
        if row["row_data"].get("id") == "person:C000127"
    ]
    assert len(stored_rows) == 1, f"expected exactly 1 stored row, found {len(stored_rows)}"
