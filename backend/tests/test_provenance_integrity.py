"""The CI integrity check for story 16.3's acceptance criterion.

    "Every row with a `source_url` has a `provenance_log` entry; a CI test
    enforces it."

This file IS that CI test. It runs in the ordinary `pytest` suite (CLAUDE.md:
no separate manual process), and it is structural rather than a list: it
discovers every `RowModel` that declares a `source_url` and every module under
`app/ingest/`, so a source added in a future story is covered the day it lands
rather than the day someone remembers to edit a check.

Three complementary layers, because no single one of them is sufficient:

  1. **A runtime write test per sourced model.** Writing a row of that model
     through a provenance-aware repository must produce the matching
     `provenance_log` entry; writing it through a repository with no provenance
     context must be refused. This is the one that proves the mechanism works,
     not merely that it is called.
  2. **A static check of every ingest module.** Each one is parsed, and every
     `Repository(...)` construction for a sourced model is checked for a
     `provenance=` argument. This catches the one gap layer 1 cannot: a module
     that is never exercised by a test.
  3. **A negative control.** A deliberately-broken ingest module (a fixture
     under `tests/fixtures/provenance_violations/`) is fed to the same check,
     and the check MUST report it. Without this the suite could pass
     vacuously — a check that discovers nothing and asserts nothing about it
     is green forever.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.bootstrap import Bootstrapper
from app.repositories import MissingProvenanceError, ProvenanceContext, Repository
from app.repositories.models import RowModel
from app.repositories.provenance import (
    APP_PACKAGE_PATH,
    INGEST_PACKAGE_PATH,
    ProvenanceViolation,
    check_app_tree,
    check_app_tree_includes,
    check_ingest_module,
    check_ingest_tree,
    models_requiring_provenance,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

VIOLATION_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "provenance_violations"

SOURCED_MODELS = models_requiring_provenance()

#: One minimally-valid row per sourced model, so layer 1 can actually write
#: one. A model added without an entry here fails
#: `test_every_sourced_model_has_a_sample_row` rather than being skipped — the
#: whole point is that a new sourced table cannot slip through unexercised.
SAMPLE_ROWS: dict[str, dict[str, Any]] = {
    "roll_calls": {
        "id": "us-house:119-2:452",
        "voted_at": "2026-03-11T14:22:00-04:00",
        "question": "On Motion to Suspend the Rules and Pass",
        "vote_type": "final_passage",
        "result": "Passed",
        "source_url": "https://api.congress.gov/v3/house-vote/119/2/452",
        "content_hash": "a" * 64,
    },
    "bills": {
        "id": "us-house:119:hr-3424",
        "title": "SPACE Act of 2025",
        "introduced_on": "2025-05-15",
        "subjects": ["Government Operations and Politics", "Congressional oversight"],
        "policy_area": "Government Operations and Politics",
        "sponsors": [
            {"person_id": "person:O000177", "role": "sponsor", "joined_on": "2025-05-15"}
        ],
        "source_url": "https://api.congress.gov/v3/bill/119/hr/3424?format=json",
        "content_hash": "b" * 64,
    },
}


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    zerodb = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(zerodb, pace=lambda: None).run()
    return zerodb


def _provenance_rows(fake: FakeZeroDBTransport) -> list[dict[str, Any]]:
    return [row["row_data"] for row in fake.rows.get("provenance_log", {}).values()]


# =========================================================================
# Discovery is real
# =========================================================================


def test_at_least_one_model_carries_a_source_url() -> None:
    """Guards against a vacuous pass. If discovery broke — a renamed field, a
    moved module — every parametrized test below would silently collect zero
    cases and the suite would stay green while enforcing nothing.
    """
    assert SOURCED_MODELS, "discovery found no source_url-bearing models; the check is vacuous"


def test_roll_calls_is_among_the_discovered_tables() -> None:
    assert "roll_calls" in {model.table_name for model in SOURCED_MODELS}


def test_every_sourced_model_has_a_sample_row() -> None:
    """A new sourced table must be given a sample row here, which is the nudge
    that makes the runtime layer cover it too.
    """
    missing = sorted(
        model.table_name for model in SOURCED_MODELS if model.table_name not in SAMPLE_ROWS
    )
    assert not missing, (
        f"these tables declare a source_url but have no sample row in this file: {missing}. "
        "Add one so the runtime provenance test below actually exercises them."
    )


# =========================================================================
# Layer 1: the runtime invariant, per discovered model
# =========================================================================


@pytest.mark.parametrize("model", SOURCED_MODELS, ids=lambda m: m.table_name)
def test_writing_a_sourced_row_writes_its_provenance_entry(
    model: type[RowModel], client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    row = SAMPLE_ROWS[model.table_name]
    repo = Repository(
        client=client,
        model=model,
        provenance=ProvenanceContext(adapter_version="integrity_check/v1"),
    )
    stored = repo.upsert(dict(row))
    entries = [
        entry
        for entry in _provenance_rows(fake)
        if entry["entity_table"] == model.table_name and entry["entity_id"] == stored.id
    ]
    assert len(entries) == 1, (
        f"writing a {model.table_name} row with a source_url produced "
        f"{len(entries)} provenance_log entries, expected exactly 1"
    )
    assert entries[0]["source_url"] == row["source_url"]
    assert entries[0]["fetched_at"]
    assert entries[0]["adapter_version"] == "integrity_check/v1"


@pytest.mark.parametrize("model", SOURCED_MODELS, ids=lambda m: m.table_name)
def test_writing_a_sourced_row_with_no_provenance_context_is_refused(
    model: type[RowModel], client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    repo = Repository(client=client, model=model)
    with pytest.raises(MissingProvenanceError):
        repo.upsert(dict(SAMPLE_ROWS[model.table_name]))
    assert fake.rows.get(model.table_name, {}) == {}
    assert _provenance_rows(fake) == []


# =========================================================================
# Layer 2: the static check over every ingest module
# =========================================================================


def test_the_ingest_tree_has_no_provenance_violations() -> None:
    violations = check_ingest_tree()
    assert violations == [], "\n".join(str(violation) for violation in violations)


def test_the_whole_app_tree_has_no_provenance_violations() -> None:
    """The ingest tree is where sourced writes BELONG; it is not where they are
    confined to.

    `app/seed.py`, `app/seed_taxonomy.py`, `app/districts/store.py` and
    `app/ballot/service.py` all build repositories today, and all of them are
    for unsourced models — but nothing stops a future `app/seed_bills.py` from
    writing `bills` rows (which carry a `source_url`) outside `app/ingest/`.
    Scanning the whole package rather than one directory means that module is
    covered without this check being edited, which is the same property the
    directory walk gives inside `app/ingest/`.
    """
    violations = check_app_tree()
    assert violations == [], "\n".join(str(violation) for violation in violations)


def test_the_app_tree_check_is_strictly_wider_than_the_ingest_one() -> None:
    """Pinned so the wider check cannot quietly become the narrower one."""
    scanned = {path.name for path in APP_PACKAGE_PATH.rglob("*.py")}
    assert "seed.py" in scanned
    assert "service.py" in scanned
    assert "house_rollcalls.py" in scanned


def test_the_repository_layer_itself_is_excluded() -> None:
    """`provenance.py` writes `insert_row(PROVENANCE_TABLE, ...)` and `base.py`
    writes every sourced table through `upsert_row` — they are the code
    implementing the rule, so the check must not report them. Excluded by path
    rather than by a special case in the matcher, so the exclusion is visible.
    """
    scanned = {path for path in APP_PACKAGE_PATH.rglob("*.py")}
    repository_files = {path for path in scanned if path.parent.name == "repositories"}
    assert repository_files, "the repository layer should exist to be excluded"
    assert all(check_app_tree_includes(path) is False for path in repository_files)


def test_the_ingest_tree_actually_contains_modules() -> None:
    """Another anti-vacuity guard: an empty tree makes the check above green
    for the wrong reason.
    """
    modules = sorted(path.name for path in INGEST_PACKAGE_PATH.glob("*.py"))
    assert [name for name in modules if name != "__init__.py"], modules


# =========================================================================
# Layer 3: the negative control — the check catches a real violation
# =========================================================================


def test_the_check_catches_an_ingest_module_that_forgets_provenance() -> None:
    """The fixture builds a `Repository` for a sourced model with no
    `provenance=`, exactly as a forgetful future adapter would.
    """
    violations = check_ingest_module(VIOLATION_FIXTURES / "forgets_provenance.py")
    assert violations, "the check did not notice a Repository built without provenance"
    assert any("roll_calls" in str(violation) for violation in violations)
    assert any("forgets_provenance" in str(violation.module) for violation in violations)


def test_the_check_passes_a_module_that_remembers_provenance() -> None:
    """The matching positive control. A check that flagged everything would
    pass the test above while being useless.
    """
    assert check_ingest_module(VIOLATION_FIXTURES / "remembers_provenance.py") == []


def test_the_check_catches_a_bare_keyword_free_repository_construction() -> None:
    """`Repository(client, RollCall)` positionally, with no provenance — the
    same mistake spelled differently.
    """
    violations = check_ingest_module(VIOLATION_FIXTURES / "forgets_provenance_positional.py")
    assert violations


def test_a_violation_reads_as_an_actionable_message() -> None:
    violation = ProvenanceViolation(
        module=Path("app/ingest/example.py"), line=12, table="roll_calls", detail="no provenance="
    )
    message = str(violation)
    assert "example.py" in message
    assert "12" in message
    assert "roll_calls" in message
