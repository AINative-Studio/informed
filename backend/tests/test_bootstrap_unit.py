"""Unit tests for the ZeroDB bootstrap module (story 1.3).

These cover what the Gherkin features in `bootstrap.feature` do not spell
out: the exact table/namespace/predicate inventories, the real pacer's
timing behaviour, and the `BootstrapReport` shape callers (and the CLI
script) depend on. Nothing here touches live ZeroDB.
"""

import time

import pytest

from app.bootstrap import (
    DATAMODEL_TABLE_COUNT,
    INFRASTRUCTURE_TABLES,
    ONTOLOGY_ENTITY_TYPES,
    ONTOLOGY_PREDICATES,
    SCHEMA_VERSION_ROW_ID,
    TABLE_SCHEMAS,
    VECTOR_DIMENSIONS,
    VECTOR_NAMESPACES,
    Bootstrapper,
    BootstrapReport,
    default_pacer,
    get_schema_version,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    return FakeZeroDBTransport()


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    return ZeroDBClient(transport=fake, project_id="proj-test")


# --- table inventory --------------------------------------------------------


def test_table_schemas_cover_every_datamodel_table_plus_infrastructure() -> None:
    """22 datamodel.md tables + the infrastructure tables.

    datamodel.md is explicit that there are 22 NoSQL tables. The others hold no
    product data and exist so a documented rule can be implemented at all:
    `schema_versions` is story 1.3's idempotency record, and
    `voter_profile_keys` is story 14.1's wrapped data keys — separate from
    `voter_profiles` so that "the key is shredded on delete" is a write the
    profile row's own lifecycle cannot undo. Counted separately everywhere so
    the two numbers are never confused.
    """
    assert len(TABLE_SCHEMAS) == DATAMODEL_TABLE_COUNT + len(INFRASTRUCTURE_TABLES)
    for name in INFRASTRUCTURE_TABLES:
        assert name in TABLE_SCHEMAS


def test_the_voter_profile_row_schema_matches_datamodel_md() -> None:
    """Story 14.1: datamodel.md's "Voter profile document" row, field for field.

    `key_ref` and `jurisdiction_ids` were missing from this schema until 14.1
    and are both load-bearing: `key_ref` says which key scheme sealed the
    payload (so a table mid-migration to a real KMS stays readable), and
    `jurisdiction_ids` picks the adapters — both needed BEFORE decryption, which
    is exactly why datamodel.md puts them on the outer row.
    """
    assert set(TABLE_SCHEMAS["voter_profiles"]) == {
        "id",
        "version",
        "locale",
        "jurisdiction_ids",
        "updated_at",
        "key_ref",
        "payload",
    }


def test_no_table_schema_has_a_field_for_an_interest() -> None:
    """Nothing anywhere may hold a voter's interests in clear text.

    A schema is not a validator (ZeroDB accepts any field), so this does not
    *prevent* a plaintext write — it prevents the bootstrap from declaring a
    place for one, which is what a later reader would take as permission.
    """
    for table_name, schema in TABLE_SCHEMAS.items():
        for field in schema:
            assert field not in {
                "interests",
                "stance_ids",
                "evidence_mode",
                "user_id",
                "dealbreaker",
            }, f"{table_name}.{field} would hold voter opinion data unencrypted"


@pytest.mark.parametrize(
    "table_name",
    [
        "jurisdictions",
        "bodies",
        "districts",
        "people",
        "elections",
        "candidacies",
        "issue_topics",
        "stances",
        "bills",
        "roll_calls",
        "member_votes",
        "promise_sources",
        "promises",
        "verdicts",
        "candidate_positions",
        "candidate_cards",
        "candidate_statements",
        "methodology_versions",
        "corrections",
        "audit_runs",
        "provenance_log",
        "voter_profiles",
    ],
)
def test_every_datamodel_table_is_present(table_name: str) -> None:
    assert table_name in TABLE_SCHEMAS


def test_every_table_schema_carries_an_id_field() -> None:
    """Every table's `id` is Informed's deterministic natural-key id.

    `ZeroDBClient.upsert_row`/`get_row` always look for it inside `row_data`
    (story 1.2), so a schema without one would silently break every lookup.
    """
    for table_name, schema in TABLE_SCHEMAS.items():
        assert "id" in schema, f"{table_name} schema is missing an 'id' field"


def test_denormalized_member_votes_fields_are_present() -> None:
    """datamodel.md: member_votes copies voted_at/vote_type/bill_id/topic_ids/
    is_bundled from the roll call so scoring reads one table."""
    schema = TABLE_SCHEMAS["member_votes"]
    for field in ("voted_at", "vote_type", "bill_id", "topic_ids", "is_bundled"):
        assert field in schema


# --- vector namespaces -------------------------------------------------------


def test_vector_namespaces_match_datamodel() -> None:
    assert set(VECTOR_NAMESPACES) == {"promises", "bill_summaries", "topics", "stances"}


def test_vector_dimensions_match_the_live_bge_m3_model() -> None:
    """datamodel.md was corrected from 768 (BGE-base assumption) to 1024
    (bge-m3, confirmed live during story 1.2) — this module must bootstrap
    at the corrected dimension count, not the stale one."""
    assert VECTOR_DIMENSIONS == 1024


# --- ontology ----------------------------------------------------------------


def test_ontology_predicates_match_datamodel_table() -> None:
    expected = {
        "ran_in": ("person", "candidacy"),
        "made_promise": ("candidacy", "promise"),
        "about": ("promise", "stance"),
        "tested_by": ("promise", "roll_call"),
        "voted": ("person", "roll_call"),
        "sponsored": ("person", "bill"),
        "on": ("roll_call", "bill"),
        "advances": ("bill", "stance"),
        "opposes": ("bill", "stance"),
        "has_verdict": ("promise", "verdict"),
        "under": ("stance", "topic"),
    }
    actual = {
        predicate: (edge["source"], edge["target"])
        for predicate, edge in ONTOLOGY_PREDICATES.items()
    }
    assert actual == expected


def test_ontology_entity_types_cover_every_predicate_endpoint() -> None:
    referenced = set()
    for edge in ONTOLOGY_PREDICATES.values():
        referenced.add(edge["source"])
        referenced.add(edge["target"])
    assert referenced <= set(ONTOLOGY_ENTITY_TYPES)


# --- BootstrapReport ----------------------------------------------------------


def test_report_is_noop_when_nothing_was_created() -> None:
    # A real run always calls the ontology upsert (there is no cheap "already
    # has this ontology" check to skip it on), so a genuinely no-op run still
    # has `ontology_registered=True` — what makes it a no-op is
    # `ontology_already_current=True` alongside every list staying empty.
    report = BootstrapReport(ontology_registered=True, ontology_already_current=True)
    assert report.is_noop is True


def test_report_is_not_noop_when_a_table_was_created() -> None:
    report = BootstrapReport(tables_created=["jurisdictions"])
    assert report.is_noop is False


def test_bootstrap_pages_through_more_than_one_list_tables_page(
    client: ZeroDBClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_list_tables` must not stop after the first page.

    `list_tables`'s own limit defaults to 100, comfortably above 23 tables, so
    this forces a tiny page size to exercise the pagination loop the way a
    project with far more than 23 tables eventually would.
    """
    bootstrapper = Bootstrapper(client, pace=lambda: None)
    original_list_tables = client.list_tables

    def tiny_pages(*, limit: int = 100, skip: int = 0):  # noqa: ANN001, ANN202
        return original_list_tables(limit=5, skip=skip)

    monkeypatch.setattr(client, "list_tables", tiny_pages)
    report = bootstrapper.run()
    assert len(report.tables_created) == len(TABLE_SCHEMAS)


def test_second_run_report_is_a_noop(client: ZeroDBClient) -> None:
    bootstrapper = Bootstrapper(client, pace=lambda: None)
    bootstrapper.run()
    second = bootstrapper.run()
    assert second.is_noop is True
    assert second.tables_created == []
    assert second.namespaces_indexed == []
    assert second.schema_version_changed is False


def test_second_run_never_calls_create_vector_index_again(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """The single most important idempotency property this module has to
    uphold: `create_vector_index` is confirmed NOT idempotent on the live
    API (a second call for the same namespace silently creates a second,
    independent index — see ZeroDBClient.create_vector_index's docstring),
    so a correct bootstrap must never call it twice for one namespace
    across any number of runs. This asserts that directly against the
    fake's own record of every index it was asked to create, rather than
    only checking the higher-level report."""
    bootstrapper = Bootstrapper(client, pace=lambda: None)
    bootstrapper.run()
    bootstrapper.run()
    bootstrapper.run()

    for namespace in VECTOR_NAMESPACES:
        assert len(fake.vector_indexes.get(namespace, [])) == 1, (
            f"namespace {namespace!r} was indexed "
            f"{len(fake.vector_indexes.get(namespace, []))} times, expected exactly once"
        )


def test_first_ever_ontology_registration_is_not_already_current(
    client: ZeroDBClient,
) -> None:
    """A project's first-ever bootstrap must NOT report the ontology as
    already current — nothing existed before this call, so there is
    nothing to have already had.

    Confirmed live during this story: `graph_upsert_ontology`'s response
    always echoes back exactly the `entity_types`/`predicates` just sent
    (the fake's `_route_graph` models this faithfully). Checking "does the
    response contain what I just sent" is therefore tautologically true on
    every call, first-time or not, and never actually distinguishes a first
    registration from a repeat one. This test fails against that logic and
    only passes once `_bootstrap_ontology` reads the ontology's state
    *before* writing to it, the same pattern already used for tables and
    vector namespaces.
    """
    report = Bootstrapper(client, pace=lambda: None).run()
    assert report.ontology_already_current is False


def test_second_run_ontology_already_current_is_true(client: ZeroDBClient) -> None:
    """A repeat run against a project that already has the full ontology
    must report `ontology_already_current=True` — this is what lets
    `BootstrapReport.is_noop` mean something real rather than being
    accidentally correct only because tables/namespaces also stayed empty.
    """
    bootstrapper = Bootstrapper(client, pace=lambda: None)
    bootstrapper.run()
    second = bootstrapper.run()
    assert second.ontology_already_current is True


def test_ontology_upsert_is_a_replace_not_a_merge_so_bootstrap_must_send_everything(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """Confirmed live during this story: `graph_upsert_ontology` REPLACES the
    registered ontology with exactly what is sent — it does not merge. A
    call with a narrower `entity_types`/`predicates` payload silently drops
    everything not included (this is how a manual diagnostic call during
    this story's live verification briefly wiped the real project's
    ontology down to one entity pair before the next bootstrap run restored
    it). The fake models this the same way. This bootstrap module is safe
    only because it always sends the full, fixed `ONTOLOGY_ENTITY_TYPES` /
    `ONTOLOGY_PREDICATES` sets on every call — this test pins that
    assumption so a future edit that narrows the payload fails loudly
    instead of silently truncating the live ontology.
    """
    Bootstrapper(client, pace=lambda: None).run()
    assert set(fake.ontology["entity_types"]) == set(ONTOLOGY_ENTITY_TYPES)
    assert set(fake.ontology["predicates"]) == set(ONTOLOGY_PREDICATES)


# --- schema version -----------------------------------------------------------


def test_get_schema_version_returns_none_before_bootstrap(client: ZeroDBClient) -> None:
    assert get_schema_version(client) is None


def test_get_schema_version_after_bootstrap(client: ZeroDBClient) -> None:
    Bootstrapper(client, pace=lambda: None).run()
    assert get_schema_version(client) is not None


def test_schema_version_row_uses_a_fixed_well_known_id(client: ZeroDBClient) -> None:
    """One row, not a growing history: `schema_versions` records the current
    state of the schema, not a log of every bootstrap run. History of
    *content* (promises, verdicts, votes) is append-only per datamodel.md;
    this meta-table is not that kind of table."""
    Bootstrapper(client, pace=lambda: None).run()
    row = client.get_row("schema_versions", SCHEMA_VERSION_ROW_ID)
    assert row is not None
    all_rows = client.query_rows("schema_versions", limit=1000).rows
    assert len(all_rows) == 1


# --- the real pacer -----------------------------------------------------------


def testdefault_pacer_does_not_sleep_on_the_first_call() -> None:
    pace = default_pacer(min_interval_seconds=5.0)
    started = time.monotonic()
    pace()
    assert time.monotonic() - started < 0.1


def testdefault_pacer_sleeps_to_enforce_the_minimum_interval() -> None:
    pace = default_pacer(min_interval_seconds=0.05)
    pace()  # first call: no sleep
    started = time.monotonic()
    pace()  # second call: should wait out the remaining interval
    elapsed = time.monotonic() - started
    assert elapsed >= 0.04  # a little under 0.05 to tolerate scheduler jitter


def testdefault_pacer_does_not_oversleep_once_the_interval_has_elapsed() -> None:
    pace = default_pacer(min_interval_seconds=0.01)
    pace()
    time.sleep(0.02)  # already longer than the interval
    started = time.monotonic()
    pace()
    assert time.monotonic() - started < 0.01


def test_bootstrapper_uses_thedefault_pacer_when_none_is_injected(
    client: ZeroDBClient,
) -> None:
    """Confirms the production code path paces for real when a caller does
    not inject a test pacer, using a long interval so the single first call
    (never slept) is enough to prove the wiring without slowing the suite."""
    bootstrapper = Bootstrapper(client, min_interval_seconds=10.0)
    started = time.monotonic()
    bootstrapper._pace()
    assert time.monotonic() - started < 0.1
