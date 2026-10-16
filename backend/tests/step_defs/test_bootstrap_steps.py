"""Step definitions for backend/tests/features/bootstrap.feature (story 1.3).

Every scenario runs against the in-memory fake transport from story 1.2
(`FakeZeroDBTransport`), never live ZeroDB, so CI needs no credentials — the
same rule story 1.2's own suite follows. Pacing is replaced with a recording
no-op so the suite does not actually sleep; a separate unit test
(`test_bootstrap_unit.py`) checks the real pacer's timing behaviour in
isolation.
"""

from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from app.bootstrap import Bootstrapper, get_schema_version
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

scenarios("../features/bootstrap.feature")


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(transport=fake, project_id="proj-test")


@pytest.fixture
def pacer_calls() -> list[None]:
    """Records every pacer invocation without sleeping."""
    return []


@pytest.fixture
def bootstrapper(client: ZeroDBClient, pacer_calls: list[None]) -> Bootstrapper:
    return Bootstrapper(client, pace=lambda: pacer_calls.append(None))


@pytest.fixture
def result() -> dict[str, Any]:
    """Carries values between Given/When and Then steps."""
    return {}


@given("a ZeroDB client backed by an in-memory fake")
def client_with_fake(client: ZeroDBClient) -> ZeroDBClient:
    return client


# --- running the bootstrap --------------------------------------------------


@when("I run the bootstrap")
def run_bootstrap(bootstrapper: Bootstrapper, result: dict[str, Any]) -> None:
    result["report"] = bootstrapper.run()


@when("I run the bootstrap again")
def run_bootstrap_again(bootstrapper: Bootstrapper, result: dict[str, Any]) -> None:
    result["report"] = bootstrapper.run()


@when("I run the bootstrap with a call pacer")
def run_bootstrap_with_pacer(bootstrapper: Bootstrapper, result: dict[str, Any]) -> None:
    result["report"] = bootstrapper.run()


@given("the bootstrap has already run once")
def bootstrap_already_ran(
    bootstrapper: Bootstrapper, fake: FakeZeroDBTransport, result: dict[str, Any]
) -> None:
    result["first_report"] = bootstrapper.run()
    # Scenarios that inspect `fake.calls` after the second run (e.g. "no POST
    # calls were made to create a table") care only about what the SECOND run
    # did; the first run's calls would otherwise always make that assertion
    # trivially false.
    fake.calls.clear()


@given("the recorded schema version is noted")
def note_schema_version(client: ZeroDBClient, result: dict[str, Any]) -> None:
    result["noted_version"] = get_schema_version(client)


# --- tables ------------------------------------------------------------


@then(parsers.parse("exactly {count:d} tables exist"))
def exactly_n_tables(client: ZeroDBClient, count: int) -> None:
    assert len(client.list_tables(limit=1000)) == count


@then(parsers.parse('the table "{table_name}" exists'))
def table_exists(client: ZeroDBClient, table_name: str) -> None:
    names = {table.table_name for table in client.list_tables(limit=1000)}
    assert table_name in names


@then(parsers.parse('no "{method}" calls were made to create a table'))
def no_table_creation_calls(fake: FakeZeroDBTransport, method: str) -> None:
    # Table creation is specifically a POST to the bare collection endpoint
    # (".../database/tables"); a GET there lists, and a POST one segment
    # deeper (".../database/tables/{name}/rows") inserts a row into an
    # existing table — neither of those is "creating a table" and must not
    # trip this assertion.
    assert not any(
        call.method == method and call.path.rsplit("/", 1)[-1] == "tables" for call in fake.calls
    )


# --- vector namespaces ---------------------------------------------------


@then(parsers.parse("exactly {count:d} vector namespaces have an index"))
def exactly_n_namespaces_indexed(client: ZeroDBClient, count: int) -> None:
    assert len(client.list_vector_indexes()) == count


@then(parsers.parse('the namespace "{namespace}" has an HNSW index at {dims:d} dimensions'))
def namespace_has_hnsw_index(client: ZeroDBClient, namespace: str, dims: int) -> None:
    indexes = {index.namespace: index for index in client.list_vector_indexes()}
    assert namespace in indexes
    index = indexes[namespace]
    assert index.index_type == "hnsw"
    assert index.dimensions == dims


# --- ontology -------------------------------------------------------------


@then("the ontology is registered")
def ontology_is_registered(fake: FakeZeroDBTransport) -> None:
    assert fake.ontology
    assert fake.ontology.get("status") == "upserted"


@then(
    parsers.parse('the ontology includes the predicate "{predicate}" from "{source}" to "{target}"')
)
def ontology_includes_predicate(
    fake: FakeZeroDBTransport, predicate: str, source: str, target: str
) -> None:
    predicates = fake.ontology.get("predicates", {})
    assert predicate in predicates
    assert predicates[predicate]["source"] == source
    assert predicates[predicate]["target"] == target


@then("the ontology was already current on the second run")
def ontology_already_current(result: dict[str, Any]) -> None:
    assert result["report"].ontology_already_current is True


# --- schema version -------------------------------------------------------


@then("a schema version row is recorded")
def schema_version_recorded(client: ZeroDBClient) -> None:
    assert get_schema_version(client) is not None


@then("the schema version is queryable afterwards")
def schema_version_queryable(client: ZeroDBClient, result: dict[str, Any]) -> None:
    version = get_schema_version(client)
    assert version == result["report"].schema_version


@then("the schema version is unchanged from what was noted")
def schema_version_unchanged(client: ZeroDBClient, result: dict[str, Any]) -> None:
    assert get_schema_version(client) == result["noted_version"]
    assert result["report"].schema_version_changed is False


# --- pacing -----------------------------------------------------------------


@then("the pacer was invoked before every ZeroDB write call")
def pacer_invoked(pacer_calls: list[None], result: dict[str, Any]) -> None:
    report = result["report"]
    expected_min_calls = len(report.tables_created) + len(report.namespaces_indexed) + 1
    # +1 for the ontology upsert, which always calls through. The schema
    # version write adds one more when it actually writes.
    if report.schema_version_changed:
        expected_min_calls += 1
    assert len(pacer_calls) >= expected_min_calls
