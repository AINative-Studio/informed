"""Step definitions for tests/features/public_methodology.feature (story 12.2).

Runs against story 1.2's in-memory fake transport over a real FastAPI
`TestClient`, never live ZeroDB, so CI needs no credentials — the rule every
backend feature in this suite follows. The live confirmation lives in
`tests/test_public_methodology_live_shapes.py` and
`scripts/verify_public_methodology_live.py`.

`break_server_side_filter()` is applied for the reason story 3.1's and 3.2's
steps apply it: live ZeroDB's server-side `/query` filter matches nothing
(story 1.4's finding), so every read here rests on the client's own paging
fallback. A fake with a working filter would make "the current endpoint
follows `is_current`" pass for the wrong reason — through a server-side
`filter={"is_current": True}` that does not actually work in production.

The requests carry NO `Authorization` header anywhere in this file, and that
absence is the point of the "with no authentication" steps: prd.md requires
the methodology page to be public, and a step that quietly authenticated would
prove nothing about it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import TABLE_SCHEMAS
from app.main import create_app
from app.methodology.router import zerodb_client_for_methodology
from app.profiles.methodology import (
    DEFAULT_METHODOLOGY_VERSION,
    DEFAULT_TIER_WEIGHTS,
    TABLE_NAME,
    MethodologyVersionService,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/public_methodology.feature")

#: Words that would mean voter data had leaked into a public, uncredentialed
#: response. Checked against the raw JSON text, not against the model, because
#: the question is what bytes a journalist (or a scraper) actually receives.
VOTER_WORDS = (
    "voter",
    "profile",
    "interest",
    "payload",
    "dealbreaker",
    "user_id",
    "stance",
)


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def zerodb(fake: FakeZeroDBTransport) -> ZeroDBClient:
    client = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table(TABLE_NAME, TABLE_SCHEMAS[TABLE_NAME])
    return client


@pytest.fixture
def methodology_api(zerodb: ZeroDBClient) -> Iterator[TestClient]:
    """The real app with only the ZeroDB dependency swapped for the fake."""
    app = create_app()
    app.dependency_overrides[zerodb_client_for_methodology] = lambda: zerodb
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seen() -> dict[str, Any]:
    """Carries the response between When and Then."""
    return {}


def _body(seen: dict[str, Any]) -> dict[str, Any]:
    response: Response = seen["response"]
    return response.json()


# --- Given ----------------------------------------------------------------


@given("a ZeroDB client backed by an in-memory fake")
def _client_backed_by_fake(zerodb: ZeroDBClient) -> None:
    assert zerodb is not None


@given("the methodology_versions table exists")
def _table_exists(zerodb: ZeroDBClient) -> None:
    tables = zerodb.list_tables()
    assert any(table.table_name == TABLE_NAME for table in tables)


@given("the current methodology version has been published")
def _current_published(zerodb: ZeroDBClient) -> None:
    """Story 4.2's own seed path, used verbatim.

    Deliberately NOT a hand-written row: the whole point of this story is to
    publish what 4.2 actually stores, so the fixture that sets the scene has
    to be 4.2's writer rather than a parallel copy of its field shape.
    """
    MethodologyVersionService(zerodb).current_version()


@given(parsers.parse('methodology version "{old}" has been superseded by "{new}"'))
def _superseded(zerodb: ZeroDBClient, old: str, new: str) -> None:
    """The append-only supersede move datamodel.md documents.

    The old row stays, flipped to `is_current: False`; the new one is written
    beside it. Story 6.1 owns doing this through a service; here it is done
    directly, because 12.2's subject is what a reader can still fetch
    afterwards, not how the move is performed.
    """
    MethodologyVersionService(zerodb).current_version()
    zerodb.update_row(TABLE_NAME, old, {"is_current": False})
    zerodb.upsert_row(
        TABLE_NAME,
        new,
        {
            "id": new,
            "published_at": "2027-01-01T00:00:00Z",
            "weights": {"tiers": {"primary": 4.0, "secondary": 2.0, "nice_to_have": 1.0}},
            "changelog_url": f"https://informed.example/methodology/{new}",
            "is_current": True,
        },
    )


# --- When -----------------------------------------------------------------


@when("anyone requests the methodology with no authentication")
def _request_current(methodology_api: TestClient, seen: dict[str, Any]) -> None:
    seen["response"] = methodology_api.get("/v1/methodology")
    assert "authorization" not in {
        key.lower() for key in seen["response"].request.headers
    }, "this scenario is about a PUBLIC read; the request must carry no credentials"


@when(parsers.parse('anyone requests methodology version "{version}"'))
def _request_version(methodology_api: TestClient, seen: dict[str, Any], version: str) -> None:
    seen["response"] = methodology_api.get(f"/v1/methodology/{version}")


@when("anyone requests the list of methodology versions")
def _request_versions(methodology_api: TestClient, seen: dict[str, Any]) -> None:
    seen["response"] = methodology_api.get("/v1/methodology/versions")


# --- Then -----------------------------------------------------------------


@then(parsers.parse("the response is {status:d}"))
def _status_is(seen: dict[str, Any], status: int) -> None:
    assert seen["response"].status_code == status, seen["response"].text


@then(parsers.parse('the response names methodology version "{version}"'))
def _names_version(seen: dict[str, Any], version: str) -> None:
    assert _body(seen)["version"] == version


@then(
    parsers.parse(
        "the published tier weights are primary {primary:g}, secondary {secondary:g} "
        "and nice_to_have {nice:g}"
    )
)
def _tier_weights_are(
    seen: dict[str, Any], primary: float, secondary: float, nice: float
) -> None:
    tiers = _body(seen)["weights"]["tiers"]
    assert tiers == {"primary": primary, "secondary": secondary, "nice_to_have": nice}


@then("the response includes a changelog_url")
def _includes_changelog(seen: dict[str, Any]) -> None:
    changelog_url = _body(seen)["changelog_url"]
    assert changelog_url
    assert changelog_url.startswith("http")


@then("the response includes the date it was published")
def _includes_published_at(seen: dict[str, Any]) -> None:
    assert _body(seen)["published_at"]


@then("the unpopulated weight groups are reported as not yet published")
def _unpopulated_groups_reported(seen: dict[str, Any]) -> None:
    """datamodel.md documents three weight groups: `tiers`, `evidence_blend`
    and `vote_types`. Only `tiers` is in force today — evidence blending and
    vote-type weighting belong to epic 6's scorer, which does not exist. A
    public methodology page that silently omitted them would let a reader
    assume the published page is the whole methodology; one that invented
    numbers would be worse. So they are named and reported as unpublished.
    """
    body = _body(seen)
    assert set(body["weights_not_yet_published"]) == {"evidence_blend", "vote_types"}


@then("no weight group is given an invented value")
def _no_invented_weights(seen: dict[str, Any]) -> None:
    weights = _body(seen)["weights"]
    for group in _body(seen)["weights_not_yet_published"]:
        assert group not in weights or not weights[group]


@then(parsers.parse('the response carries the canonical public path for version "{version}"'))
def _carries_canonical_path(seen: dict[str, Any], version: str) -> None:
    """The half of "every verdict links to its methodology version" that is
    demonstrable today: the link a verdict will cite is published with the
    version itself, so epic 11 has one place to read it from rather than
    rebuilding the URL by hand in every caller.
    """
    assert _body(seen)["path"] == f"/v1/methodology/{version}"


@then("the response says this version is not current")
def _says_not_current(seen: dict[str, Any]) -> None:
    assert _body(seen)["is_current"] is False


@then("the response says this version is current")
def _says_current(seen: dict[str, Any]) -> None:
    assert _body(seen)["is_current"] is True


@then(parsers.parse('the listed versions are "{first}" then "{second}"'))
def _listed_in_order(seen: dict[str, Any], first: str, second: str) -> None:
    listed = [entry["version"] for entry in _body(seen)["versions"]]
    assert listed == [first, second]


@then("every listed version carries a public path and a changelog_url")
def _every_listed_citable(seen: dict[str, Any]) -> None:
    for entry in _body(seen)["versions"]:
        assert entry["path"] == f"/v1/methodology/{entry['version']}"
        assert entry["changelog_url"].startswith("http")


@then("exactly one listed version is marked current")
def _exactly_one_current(seen: dict[str, Any]) -> None:
    current = [entry for entry in _body(seen)["versions"] if entry["is_current"]]
    assert len(current) == 1


@then("the response body mentions no voter, profile or interest data")
def _no_voter_data(seen: dict[str, Any]) -> None:
    text = seen["response"].text.lower()
    leaked = [word for word in VOTER_WORDS if word in text]
    assert not leaked, f"public methodology response mentions {leaked}"


def test_the_feature_file_covers_the_published_tier_weights() -> None:
    """A guard on the fixture, not on the app.

    The feature file hard-codes 3.0/1.5/0.5 so a reader of the Gherkin sees the
    real numbers. If prd.md's tier table ever changes, this fails here rather
    than letting a scenario quietly assert last year's methodology.
    """
    assert DEFAULT_TIER_WEIGHTS == {"primary": 3.0, "secondary": 1.5, "nice_to_have": 0.5}
    assert DEFAULT_METHODOLOGY_VERSION == "2026.1"
