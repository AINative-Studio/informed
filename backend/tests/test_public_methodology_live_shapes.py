"""Live-confirmed behaviour the public methodology page depends on (12.2).

Same purpose as `test_stance_cards_live_shapes.py` and
`test_profiles_live_shapes.py`: each test here pins something measured against
the LIVE "informed" project during story 12.2, not inferred from the docs or
from the fake. The measurement output is in the story's PR.

What was measured, on 2026-10-16, with
`scripts/verify_public_methodology_live.py` against the project that already
held story 4.2's permanent `methodology_versions` row. The row came back
verbatim as:

    {
      "changelog_url": "https://github.com/AINative-Studio/informed/blob/main/
                        docs/methodology-changelog.md#20261--the-first-published-methodology",
      "id": "2026.1",
      "is_current": true,
      "published_at": "2026-10-16T21:07:34Z",
      "weights": {"tiers": {"nice_to_have": 0.5, "primary": 3.0, "secondary": 1.5}}
    }

and from it:

  * **`weights` round-trips as a genuinely nested object**, not as a JSON
    string and not flattened to dotted keys. ZeroDB's `methodology_versions`
    schema declares the column `"object"` (bootstrap.py's `TABLE_SCHEMAS`) and
    a two-level dict came back as a two-level dict. Pinned because this is the
    only nested-object column any story has published on the wire so far, and
    `{"tiers": "{\\"primary\\": 3.0}"}` would have validated as
    `dict[str, Any]` while being useless.
  * **Every tier weight round-trips as a `float`, sign and decimal intact** —
    `3.0` came back `3.0`, not `"3.0"` and not `3`. The same hazard story 2.1
    pinned for `Body.seats` and 3.1 for `taxonomy_version`, and the one that
    matters most here: epic 6's scorer will multiply by these.
  * **`is_current` round-trips as a real `bool`**, not `"true"` and not `1`.
    The entire "which methodology is in force" read turns on this field, and
    a truthy string would make *every* row look current.
  * **Exactly one live row was marked `is_current`.** Story 4.2's seed is
    idempotent (its own live check proved that), but "one row, and it is the
    current one" is a different claim from "one row per id" and is the one
    this page depends on.
  * **A version id containing a dot survives as a row id.** `2026.1` is the
    first id in this codebase whose natural key contains a `.` rather than a
    `:`, and `ZeroDBClient`'s find-then-write resolved it by matching
    `row_data.id` without the dot being treated as a path separator.
  * **An absent version id came back as an empty result, not an error.**
    `client.get_row("methodology_versions", "1999.9")` returned `None` live,
    so the service's 404 path is reachable rather than theoretical.
  * **The full HTTP surface answered over a live ZeroDB client with no
    `Authorization` header**: `/v1/methodology` 200, `/v1/methodology/versions`
    200, `/v1/methodology/2026.1` 200, `/v1/methodology/1999.9` 404, and the
    four malformed ids (`../../etc/passwd`, `2026`, `abcd.1`, `2026.1.1`) 404
    or 422 at the edge — never reaching ZeroDB.

Nothing here makes a live call. The payload is the live one, replayed through
the fake, which is what makes these runnable in CI without credentials.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import TABLE_SCHEMAS
from app.main import create_app
from app.methodology.router import zerodb_client_for_methodology
from app.methodology.service import PublicMethodologyService
from app.profiles.methodology import TABLE_NAME
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

#: Copied verbatim from a live unfiltered read of `methodology_versions` on
#: 2026-10-16 (project id redacted). Story 4.2 wrote the row; story 12.2
#: updated `changelog_url` from its `informed.example` placeholder to the real
#: published document via `--set-changelog-url`. This is published reference
#: data — there is no voter data in this table at all.
LIVE_ROW: dict[str, Any] = {
    "changelog_url": (
        "https://github.com/AINative-Studio/informed/blob/main/docs/"
        "methodology-changelog.md#20261--the-first-published-methodology"
    ),
    "id": "2026.1",
    "is_current": True,
    "published_at": "2026-10-16T21:07:34Z",
    "weights": {"tiers": {"nice_to_have": 0.5, "primary": 3.0, "secondary": 1.5}},
}


@pytest.fixture
def zerodb() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table(TABLE_NAME, TABLE_SCHEMAS[TABLE_NAME])
    client.upsert_row(TABLE_NAME, LIVE_ROW["id"], dict(LIVE_ROW))
    return client


@pytest.fixture
def api(zerodb: ZeroDBClient) -> TestClient:
    app = create_app()
    app.dependency_overrides[zerodb_client_for_methodology] = lambda: zerodb
    return TestClient(app)


def test_the_live_row_validates_and_publishes(zerodb: ZeroDBClient) -> None:
    published = PublicMethodologyService(zerodb).current()
    assert published.version == "2026.1"
    assert published.is_current is True
    assert published.changelog_url == LIVE_ROW["changelog_url"]


def test_live_weights_are_a_nested_object_of_floats(zerodb: ZeroDBClient) -> None:
    """The live measurement: `weights` is a real two-level dict of numbers.

    Not a JSON string, not dotted keys, not strings for the values. This is
    the only nested-object column any story publishes on the wire, and epic
    6's scorer will multiply by these numbers.
    """
    published = PublicMethodologyService(zerodb).current()
    tiers = published.weights["tiers"]
    assert tiers == {"primary": 3.0, "secondary": 1.5, "nice_to_have": 0.5}
    for value in tiers.values():
        assert isinstance(value, float)


def test_live_is_current_is_a_real_bool(zerodb: ZeroDBClient) -> None:
    """Measured live: `true`, not `"true"` and not `1`.

    Pinned on the raw `row_data` rather than through the model, because
    Pydantic would coerce `"true"` to `True` on the way in and hide exactly the
    thing being measured. A truthy string in this column would make every row
    look current, and "which methodology is in force" would have no answer.
    """
    row = zerodb.get_row(TABLE_NAME, "2026.1")
    assert row is not None
    assert row.row_data["is_current"] is True


def test_a_dotted_version_id_resolves_as_a_row_id(zerodb: ZeroDBClient) -> None:
    """`2026.1` is the first natural key in this codebase containing a dot.

    Every other id uses colons (`us-house:119-2:452`). Confirmed live that the
    dot is not treated as a path separator by ZeroDB or by the client's
    find-then-write id resolution.
    """
    assert zerodb.get_row(TABLE_NAME, "2026.1") is not None
    assert PublicMethodologyService(zerodb).version("2026.1").version == "2026.1"


def test_an_absent_version_reads_back_as_none_live(zerodb: ZeroDBClient) -> None:
    """Measured live: a miss is an empty result, not an error.

    This is what makes the service's 404 path reachable rather than
    theoretical — if a missing id raised instead, the endpoint would 500 on
    every unknown version.
    """
    assert zerodb.get_row(TABLE_NAME, "1999.9") is None


def test_exactly_one_live_row_is_current(zerodb: ZeroDBClient) -> None:
    listed = PublicMethodologyService(zerodb).versions()
    assert [entry.version for entry in listed] == ["2026.1"]
    assert sum(1 for entry in listed if entry.is_current) == 1


def test_the_live_row_reports_the_two_unpublished_weight_groups(zerodb: ZeroDBClient) -> None:
    """The live row carries only `tiers`, as measured.

    Confirmed against the live project on 2026-10-16: `weights` held exactly
    one key. `evidence_blend` and `vote_types` are epic 6's, and the page says
    so rather than inventing them. When epic 6 publishes one, this test is the
    thing to update — deliberately, with a new live measurement.
    """
    published = PublicMethodologyService(zerodb).current()
    assert set(published.weights) == {"tiers"}
    assert published.weights_not_yet_published == ["evidence_blend", "vote_types"]


def test_the_live_http_statuses_are_reproduced(api: TestClient) -> None:
    """The exact status codes the live run recorded, over the real router."""
    assert api.get("/v1/methodology").status_code == 200
    assert api.get("/v1/methodology/versions").status_code == 200
    assert api.get("/v1/methodology/2026.1").status_code == 200
    assert api.get("/v1/methodology/1999.9").status_code == 404
    for junk, expected in (("2026", 422), ("abcd.1", 422), ("2026.1.1", 422)):
        assert api.get(f"/v1/methodology/{junk}").status_code == expected, junk


def test_the_live_changelog_url_is_a_real_published_location(zerodb: ZeroDBClient) -> None:
    """Story 12.2 replaced 4.2's `informed.example` placeholder.

    4.2 seeded `https://informed.example/methodology/2026.1` — correct for a
    story whose job was the tier weights, and a dead link on a page whose
    whole purpose is that a skeptical reader can follow it. The live row now
    points at `docs/methodology-changelog.md`, which is in this repo.
    """
    published = PublicMethodologyService(zerodb).current()
    assert "informed.example" not in published.changelog_url
    assert published.changelog_url.startswith("https://")
    assert "methodology-changelog" in published.changelog_url
