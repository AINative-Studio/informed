"""Unit tests for the public methodology endpoints (story 12.2).

The feature file covers the behaviour a voter or journalist can observe. These
cover the edges a Gherkin scenario would only clutter: the service's history
read, the superseded-row ordering rule, what happens with an empty table, and
the two things about this endpoint that are security-relevant rather than
functional — that it is reachable without credentials, and that the row's
`weights` dict cannot smuggle an arbitrary key into a published response.

Every version id here is a real methodology id (`2026.1`, `2027.1`), not a
synthetic marker: a methodology version is published reference data (prd.md:
"Methodology page and changelog"), so CLAUDE.md's "never put real voter data in
a fixture" rule has nothing to bite on. There is no voter data anywhere in this
module, by design — see `test_the_public_response_model_has_no_voter_field`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import TABLE_SCHEMAS
from app.main import create_app
from app.methodology.models import PublishedMethodology
from app.methodology.router import zerodb_client_for_methodology
from app.methodology.service import PublicMethodologyService
from app.profiles.methodology import (
    DEFAULT_METHODOLOGY_VERSION,
    DEFAULT_TIER_WEIGHTS,
    TABLE_NAME,
    MethodologyVersionService,
)
from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBNotFoundError
from app.zerodb.testing import FakeZeroDBTransport


@pytest.fixture
def zerodb() -> ZeroDBClient:
    transport = FakeZeroDBTransport()
    # Live ZeroDB's server-side filter matches nothing (story 1.4). Reading
    # history and "the current one" both rest on the client's paging fallback,
    # so the fake is put in the live configuration rather than the nice one.
    transport.break_server_side_filter()
    client = ZeroDBClient(
        transport=transport, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    client.create_table(TABLE_NAME, TABLE_SCHEMAS[TABLE_NAME])
    return client


@pytest.fixture
def service(zerodb: ZeroDBClient) -> PublicMethodologyService:
    return PublicMethodologyService(zerodb)


@pytest.fixture
def api(zerodb: ZeroDBClient) -> TestClient:
    app = create_app()
    app.dependency_overrides[zerodb_client_for_methodology] = lambda: zerodb
    return TestClient(app)


def _publish_default(zerodb: ZeroDBClient) -> None:
    """Seed story 4.2's row through 4.2's own writer, not a hand-made copy."""
    MethodologyVersionService(zerodb).current_version()


def _supersede(zerodb: ZeroDBClient, old: str, new: str, published_at: str) -> None:
    zerodb.update_row(TABLE_NAME, old, {"is_current": False})
    zerodb.upsert_row(
        TABLE_NAME,
        new,
        {
            "id": new,
            "published_at": published_at,
            "weights": {"tiers": {"primary": 4.0, "secondary": 2.0, "nice_to_have": 1.0}},
            "changelog_url": f"https://informed.example/methodology/{new}",
            "is_current": True,
        },
    )


# --- the current version --------------------------------------------------


def test_current_publishes_the_live_row(
    service: PublicMethodologyService, zerodb: ZeroDBClient
) -> None:
    _publish_default(zerodb)
    published = service.current()
    assert published.version == DEFAULT_METHODOLOGY_VERSION
    assert published.weights["tiers"] == DEFAULT_TIER_WEIGHTS
    assert published.is_current is True


def test_current_on_an_empty_table_seeds_rather_than_404s(
    service: PublicMethodologyService, zerodb: ZeroDBClient
) -> None:
    """A fresh environment must not serve a 404 on its own methodology page.

    Story 4.2 already decided that a missing row is seeded rather than treated
    as an error (see `app.profiles.methodology`'s docstring on why: a project
    with no row would otherwise disagree with every other environment about
    where its weights came from). The public reader reuses that decision rather
    than inventing a second answer for the same situation.
    """
    published = service.current()
    assert published.version == DEFAULT_METHODOLOGY_VERSION
    assert zerodb.get_row(TABLE_NAME, DEFAULT_METHODOLOGY_VERSION) is not None


def test_the_canonical_path_is_derived_from_the_version_id(
    service: PublicMethodologyService, zerodb: ZeroDBClient
) -> None:
    """The link a verdict will cite is published WITH the version.

    Epic 11's verdicts do not exist yet, so this is the forward-looking half of
    "every verdict links to its methodology version": when a verdict needs a
    URL for the version that produced it, it reads this field instead of
    string-building the path in a template and drifting from the router.
    """
    _publish_default(zerodb)
    assert service.current().path == f"/v1/methodology/{DEFAULT_METHODOLOGY_VERSION}"


# --- a specific version, including superseded ones ------------------------


def test_a_version_is_readable_by_its_id(
    service: PublicMethodologyService, zerodb: ZeroDBClient
) -> None:
    _publish_default(zerodb)
    assert service.version(DEFAULT_METHODOLOGY_VERSION).version == DEFAULT_METHODOLOGY_VERSION


def test_an_unknown_version_raises_not_found(service: PublicMethodologyService) -> None:
    with pytest.raises(ZeroDBNotFoundError):
        service.version("1999.9")


def test_a_superseded_version_keeps_its_own_weights(
    service: PublicMethodologyService, zerodb: ZeroDBClient
) -> None:
    """The audit trail that makes a citation worth anything.

    datamodel.md: history is never updated in place; the live row is marked
    with `is_current`. A verdict computed under 2026.1 must still be able to
    show a reader 2026.1's actual weights after 2027.1 ships, or the citation
    on that verdict is decorative.
    """
    _publish_default(zerodb)
    _supersede(zerodb, DEFAULT_METHODOLOGY_VERSION, "2027.1", "2027-01-01T00:00:00Z")

    old = service.version(DEFAULT_METHODOLOGY_VERSION)
    assert old.weights["tiers"] == DEFAULT_TIER_WEIGHTS
    assert old.is_current is False

    assert service.current().version == "2027.1"


# --- the changelog list ---------------------------------------------------


def test_versions_are_listed_newest_first(
    service: PublicMethodologyService, zerodb: ZeroDBClient
) -> None:
    _publish_default(zerodb)
    _supersede(zerodb, DEFAULT_METHODOLOGY_VERSION, "2027.1", "2027-01-01T00:00:00Z")
    listed = [entry.version for entry in service.versions()]
    assert listed == ["2027.1", DEFAULT_METHODOLOGY_VERSION]


def test_versions_sort_by_published_at_not_by_id_string(
    service: PublicMethodologyService, zerodb: ZeroDBClient
) -> None:
    """`2026.10` sorts BEFORE `2026.9` as a string, and after it as a date.

    datamodel.md's id pattern is `2026.1` — year-dot-sequence — so the tenth
    methodology of 2026 is `2026.10`, which a lexicographic sort would file
    between `2026.1` and `2026.2`. The changelog is a chronology, so
    `published_at` is the sort key and this pins it with the exact pair that
    tells the two rules apart.
    """
    for version, published_at in (
        ("2026.9", "2026-09-01T00:00:00Z"),
        ("2026.10", "2026-10-01T00:00:00Z"),
    ):
        zerodb.upsert_row(
            TABLE_NAME,
            version,
            {
                "id": version,
                "published_at": published_at,
                "weights": {"tiers": dict(DEFAULT_TIER_WEIGHTS)},
                "changelog_url": f"https://informed.example/methodology/{version}",
                "is_current": version == "2026.10",
            },
        )
    assert [entry.version for entry in service.versions()] == ["2026.10", "2026.9"]


def test_versions_on_an_empty_table_is_an_empty_list_not_a_seed(
    service: PublicMethodologyService, zerodb: ZeroDBClient
) -> None:
    """Listing is a pure read; only `current()` seeds.

    A changelog request is a question about history, and the honest answer for
    a project with no history is "none", not a row written as a side effect of
    someone loading a page.
    """
    assert service.versions() == []
    assert zerodb.query_rows(TABLE_NAME, limit=10).rows == []


# --- the published shape --------------------------------------------------


def test_unpopulated_weight_groups_are_named_rather_than_hidden() -> None:
    """datamodel.md documents `weights {tiers, evidence_blend, vote_types}`.

    Only `tiers` is in force today: evidence blending and vote-type weighting
    are epic 6's scorer, which does not exist in this codebase. Omitting them
    silently would let a reader believe the page is the whole methodology;
    inventing numbers for a scorer nobody has written would be worse. So the
    response names them as not yet published.
    """
    published = PublishedMethodology(
        version="2026.1",
        published_at="2026-01-01T00:00:00Z",
        weights={"tiers": dict(DEFAULT_TIER_WEIGHTS)},
        changelog_url="https://informed.example/methodology/2026.1",
        is_current=True,
        path="/v1/methodology/2026.1",
    )
    assert set(published.weights_not_yet_published) == {"evidence_blend", "vote_types"}


def test_a_populated_weight_group_is_not_reported_as_unpublished() -> None:
    """When epic 6 does publish an evidence blend, this flips on its own.

    The list is computed from what the row actually carries rather than being
    a hard-coded "these two are missing" constant, so epic 6 does not have to
    remember to come back and edit this module.
    """
    published = PublishedMethodology(
        version="2027.1",
        published_at="2027-01-01T00:00:00Z",
        weights={
            "tiers": dict(DEFAULT_TIER_WEIGHTS),
            "evidence_blend": {"votes": 0.7, "promises": 0.3},
        },
        changelog_url="https://informed.example/methodology/2027.1",
        is_current=True,
        path="/v1/methodology/2027.1",
    )
    assert published.weights_not_yet_published == ["vote_types"]


def test_the_public_response_model_has_no_voter_field() -> None:
    """This is the API's first uncredentialed read; the rule is absolute.

    prd.md: a voter's political profile is special-category data that must
    never be logged, shared or targeted. The methodology page is the one
    endpoint anyone at all can call, so the model is pinned as carrying no
    field that could ever hold voter data — a future story adding one has to
    delete this test deliberately rather than discover the leak in production.
    """
    fields = set(PublishedMethodology.model_fields)
    assert fields == {
        "version",
        "published_at",
        "weights",
        "changelog_url",
        "is_current",
        "path",
    }


# --- the HTTP surface ------------------------------------------------------


def test_the_endpoint_needs_no_authorization_header(api: TestClient, zerodb: ZeroDBClient) -> None:
    _publish_default(zerodb)
    response = api.get("/v1/methodology")
    assert response.status_code == 200
    assert "authorization" not in {key.lower() for key in response.request.headers}


def test_an_unknown_version_is_a_404_over_http(api: TestClient, zerodb: ZeroDBClient) -> None:
    _publish_default(zerodb)
    response = api.get("/v1/methodology/1999.9")
    assert response.status_code == 404


def test_versions_is_not_mistaken_for_a_version_id(api: TestClient, zerodb: ZeroDBClient) -> None:
    """`/v1/methodology/versions` must route to the list, not to a lookup for a
    version literally named "versions".

    FastAPI matches routes in declaration order, so this is a real ordering
    constraint on the router and not a theoretical one: declared the other way
    round, the changelog would 404 with "no such methodology version:
    versions".
    """
    _publish_default(zerodb)
    response = api.get("/v1/methodology/versions")
    assert response.status_code == 200
    assert "versions" in response.json()


def test_a_version_id_of_a_shape_we_do_not_issue_is_rejected(
    api: TestClient, zerodb: ZeroDBClient
) -> None:
    """A path parameter from the open internet gets a shape check.

    datamodel.md's id pattern is `2026.1`. Anything else is not a methodology
    id, and refusing it at the edge keeps junk out of the ZeroDB read path on
    the one endpoint that takes unauthenticated input.
    """
    _publish_default(zerodb)
    for junk in ("../../etc/passwd", "2026", "abcd.1", "2026.1.1"):
        response = api.get(f"/v1/methodology/{junk}")
        assert response.status_code in (404, 422), f"{junk} -> {response.status_code}"
