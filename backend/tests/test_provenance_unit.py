"""Unit tests for the provenance-writing helper and its enforcement (story 16.3).

Story 16.3's acceptance criterion is datamodel.md's integrity rule turned into
something the code cannot get wrong: "Every row with a `source_url` has a
`provenance_log` entry; a CI test enforces it."

Three layers are tested here, in the order they fail:

  1. `app.repositories.provenance` — the generic helper, promoted from story
     5.1's `house_rollcalls.write_provenance`, keeping its content-addressed
     id (`prov:{sha256}`) and check-then-insert idempotency.
  2. `Repository.upsert`/`upsert_many` writing that entry automatically for any
     model carrying a `source_url`, and REFUSING the write when it has no
     provenance to write. That refusal is the enforcement: an ingest adapter
     cannot forget a step the repository takes for it.
  3. `app.repositories.provenance_check` — the structural check CI runs, which
     discovers every `source_url`-bearing model and every ingest module rather
     than hardcoding today's single one.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from app.bootstrap import Bootstrapper
from app.repositories import MissingProvenanceError, ProvenanceContext, Repository
from app.repositories.models import Person, ProvenanceLog, RollCall, RowModel
from app.repositories.provenance import (
    content_hash_for,
    models_requiring_provenance,
    provenance_id,
    write_provenance,
)
from app.zerodb import ZeroDBClient
from app.zerodb.testing import FakeZeroDBTransport

ADAPTER = "test_adapter/v1"


@pytest.fixture
def fake() -> FakeZeroDBTransport:
    transport = FakeZeroDBTransport()
    # The live API's server-side filter drops matching rows (story 1.4), and
    # the content-addressed check-then-insert rests on the client's paging
    # fallback working under exactly that.
    transport.break_server_side_filter()
    return transport


@pytest.fixture
def client(fake: FakeZeroDBTransport) -> ZeroDBClient:
    zerodb = ZeroDBClient(
        transport=fake, project_id="proj-test", backoff_base=0.0, sleep=lambda _: None
    )
    Bootstrapper(zerodb, pace=lambda: None).run()
    return zerodb


@pytest.fixture
def context() -> ProvenanceContext:
    """A context with a frozen clock, so `fetched_at` is assertable."""
    return ProvenanceContext(adapter_version=ADAPTER, now=lambda: "2026-10-16T12:00:00+00:00")


def _roll_call_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "us-house:119-2:452",
        "voted_at": "2026-03-11T14:22:00-04:00",
        "question": "On Motion to Suspend the Rules and Pass",
        "vote_type": "final_passage",
        "result": "Passed",
        "source_url": "https://api.congress.gov/v3/house-vote/119/2/452",
        "content_hash": "a" * 64,
    }
    row.update(overrides)
    return row


def _provenance_rows(fake: FakeZeroDBTransport) -> list[dict[str, Any]]:
    return [row["row_data"] for row in fake.rows.get("provenance_log", {}).values()]


# =========================================================================
# 1. The helper itself
# =========================================================================


def test_provenance_id_is_the_documented_content_addressed_pattern() -> None:
    assert provenance_id("deadbeef") == "prov:deadbeef"


def test_content_hash_is_stable_under_key_reordering() -> None:
    """Promoted from story 5.1: the hash is a function of the data, not of the
    source's serialization choices, or an unchanged re-fetch would look changed.
    """
    assert content_hash_for({"a": 1, "b": 2}) == content_hash_for({"b": 2, "a": 1})


def test_content_hash_changes_when_the_content_changes() -> None:
    assert content_hash_for({"result": "Passed"}) != content_hash_for({"result": "Agreed to"})


def test_write_provenance_inserts_the_row_under_its_content_addressed_id(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    written = write_provenance(
        client,
        entity_table="roll_calls",
        entity_id="us-house:119-2:452",
        source_url="https://api.congress.gov/v3/house-vote/119/2/452",
        fetched_at="2026-10-16T12:00:00+00:00",
        content_hash="b" * 64,
        adapter_version=ADAPTER,
    )
    assert written is not None
    assert written.id == f"prov:{'b' * 64}"
    rows = _provenance_rows(fake)
    assert len(rows) == 1
    assert rows[0]["entity_table"] == "roll_calls"
    assert rows[0]["entity_id"] == "us-house:119-2:452"
    assert rows[0]["adapter_version"] == ADAPTER


def test_write_provenance_is_idempotent_for_identical_content(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    kwargs: dict[str, Any] = {
        "entity_table": "roll_calls",
        "entity_id": "us-house:119-2:452",
        "source_url": "https://example.org/x",
        "fetched_at": "2026-10-16T12:00:00+00:00",
        "content_hash": "c" * 64,
        "adapter_version": ADAPTER,
    }
    first = write_provenance(client, **kwargs)
    second = write_provenance(client, **kwargs)
    assert first is not None
    assert second is None, "a second write of identical content must add nothing"
    assert len(_provenance_rows(fake)) == 1


def test_write_provenance_appends_a_second_row_for_changed_content(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """datamodel.md's reproducibility goal: the record now has two fetches on
    file, at two different times, and keeping both is the point of an
    append-only log.
    """
    for content_hash in ("d" * 64, "e" * 64):
        write_provenance(
            client,
            entity_table="roll_calls",
            entity_id="us-house:119-2:452",
            source_url="https://example.org/x",
            fetched_at="2026-10-16T12:00:00+00:00",
            content_hash=content_hash,
            adapter_version=ADAPTER,
        )
    assert len(_provenance_rows(fake)) == 2


# =========================================================================
# 2. Repository enforcement
# =========================================================================


def test_upsert_of_a_source_url_row_writes_its_provenance_entry(
    client: ZeroDBClient, fake: FakeZeroDBTransport, context: ProvenanceContext
) -> None:
    repo = Repository(client=client, model=RollCall, provenance=context)
    repo.upsert(_roll_call_row())
    rows = _provenance_rows(fake)
    assert len(rows) == 1
    assert rows[0]["entity_table"] == "roll_calls"
    assert rows[0]["entity_id"] == "us-house:119-2:452"
    assert rows[0]["source_url"] == "https://api.congress.gov/v3/house-vote/119/2/452"
    assert rows[0]["fetched_at"] == "2026-10-16T12:00:00+00:00"
    assert rows[0]["adapter_version"] == ADAPTER


def test_upsert_without_a_provenance_context_refuses_a_source_url_row(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """The enforcement. A repository with nothing to write provenance FROM must
    not write the evidence row either, or the untraceable row is exactly what
    datamodel.md's integrity check would later find.
    """
    repo = Repository(client=client, model=RollCall)
    with pytest.raises(MissingProvenanceError) as exc:
        repo.upsert(_roll_call_row())
    assert "roll_calls" in str(exc.value)
    assert exc.value.table == "roll_calls"
    assert fake.rows.get("roll_calls", {}) == {}, "nothing may be written"
    assert _provenance_rows(fake) == []


def test_upsert_without_provenance_is_fine_for_a_row_whose_source_url_is_absent(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """`roll_calls.source_url` is optional in the model. A row that genuinely
    has none traces to nothing, so there is nothing to log and nothing to
    refuse — refusing it would block legitimate writes (a hand-entered
    correction) for a rule about rows that DO cite a source.
    """
    repo = Repository(client=client, model=RollCall)
    repo.upsert(_roll_call_row(source_url=None, content_hash=None))
    assert repo.get("us-house:119-2:452") is not None
    assert _provenance_rows(fake) == []


def test_a_model_with_no_source_url_field_needs_no_provenance_context(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """`people` has no `source_url` field at all per datamodel.md, so story
    2.2's loader is correctly exempt — confirmed live: all 539 stored rows
    carry only canonical_name/external_ids/photo_url/terms.
    """
    repo = Repository(client=client, model=Person)
    repo.upsert({"id": "person:B000944", "canonical_name": "Test Person"})
    assert repo.get("person:B000944") is not None
    assert _provenance_rows(fake) == []


def test_upsert_is_idempotent_in_both_tables(
    client: ZeroDBClient, fake: FakeZeroDBTransport, context: ProvenanceContext
) -> None:
    repo = Repository(client=client, model=RollCall, provenance=context)
    repo.upsert(_roll_call_row())
    repo.upsert(_roll_call_row())
    assert len(fake.rows.get("roll_calls", {})) == 1
    assert len(_provenance_rows(fake)) == 1


def test_changed_content_hash_appends_a_second_provenance_row(
    client: ZeroDBClient, fake: FakeZeroDBTransport, context: ProvenanceContext
) -> None:
    repo = Repository(client=client, model=RollCall, provenance=context)
    repo.upsert(_roll_call_row())
    repo.upsert(_roll_call_row(result="Agreed to", content_hash="f" * 64))
    assert len(fake.rows.get("roll_calls", {})) == 1
    assert len(_provenance_rows(fake)) == 2


def test_a_source_url_row_with_no_content_hash_is_still_traceable(
    client: ZeroDBClient, fake: FakeZeroDBTransport, context: ProvenanceContext
) -> None:
    """`content_hash` is `roll_calls`-specific; `bills` has a `source_url` and
    no hash field at all. The provenance id then falls back to a hash of the
    row's own stored contents, so the entry is still content-addressed and
    still idempotent — a table without a hash column must not be a hole in the
    rule.
    """
    repo = Repository(client=client, model=RollCall, provenance=context)
    repo.upsert(_roll_call_row(content_hash=None))
    repo.upsert(_roll_call_row(content_hash=None))
    rows = _provenance_rows(fake)
    assert len(rows) == 1
    assert rows[0]["id"].startswith("prov:")
    assert len(rows[0]["id"]) == len("prov:") + 64


def test_upsert_many_writes_one_provenance_row_per_row(
    client: ZeroDBClient, fake: FakeZeroDBTransport, context: ProvenanceContext
) -> None:
    repo = Repository(client=client, model=RollCall, provenance=context)
    repo.upsert_many(
        [
            _roll_call_row(
                id=f"us-house:119-2:{roll}",
                source_url=f"https://api.congress.gov/v3/house-vote/119/2/{roll}",
                content_hash=str(roll) * 32,
            )
            for roll in (451, 452, 453)
        ]
    )
    assert len(fake.rows.get("roll_calls", {})) == 3
    assert len(_provenance_rows(fake)) == 3


def test_upsert_many_without_a_provenance_context_refuses_before_writing_anything(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """Refused on the FIRST row, so a batch cannot get halfway through writing
    untraceable evidence before anyone notices.
    """
    repo = Repository(client=client, model=RollCall)
    with pytest.raises(MissingProvenanceError):
        repo.upsert_many([_roll_call_row(id=f"us-house:119-2:{roll}") for roll in (451, 452)])
    assert fake.rows.get("roll_calls", {}) == {}


def test_provenance_log_itself_is_exempt_from_the_rule(
    client: ZeroDBClient, fake: FakeZeroDBTransport
) -> None:
    """`provenance_log` carries a `source_url` of its own, and must not need a
    provenance entry for its provenance entry — that recursion has no base
    case and no auditing value.
    """
    assert ProvenanceLog not in models_requiring_provenance()
    write_provenance(
        client,
        entity_table="roll_calls",
        entity_id="us-house:119-2:452",
        source_url="https://example.org/x",
        fetched_at="2026-10-16T12:00:00+00:00",
        content_hash="0" * 64,
        adapter_version=ADAPTER,
    )
    assert len(_provenance_rows(fake)) == 1


def test_a_provenance_context_on_a_model_with_no_source_url_is_rejected() -> None:
    """Passing one is a sign the caller has the wrong model or the wrong table;
    silently ignoring it would hide that.
    """
    with pytest.raises(MissingProvenanceError):
        Repository(
            client=None,  # type: ignore[arg-type]
            model=Person,
            provenance=ProvenanceContext(adapter_version=ADAPTER),
        )


def test_provenance_context_defaults_to_a_real_utc_clock() -> None:
    stamped = ProvenanceContext(adapter_version=ADAPTER).fetched_at()
    assert stamped.endswith("+00:00"), stamped


# =========================================================================
# 3. The structural check CI runs
# =========================================================================


def test_models_requiring_provenance_discovers_roll_calls_and_not_people() -> None:
    discovered = models_requiring_provenance()
    assert RollCall in discovered
    assert Person not in discovered


def test_models_requiring_provenance_is_derived_not_hardcoded() -> None:
    """A new model that declares a `source_url` must appear WITHOUT this check
    being edited — that is the difference between a rule and a list.

    Checked by scanning a throwaway namespace rather than defining a subclass
    for real: a `RowModel` subclass defined in a test would stay visible to
    every later discovery walk in the same session, and a model whose table
    nothing creates would then look like a violation of the rule it is here
    to demonstrate.
    """

    class FutureSourcedRow(RowModel):
        table_name = "future_sourced_rows"
        source_url: str | None = None

    class FutureUnsourcedRow(RowModel):
        table_name = "future_unsourced_rows"
        name: str = ""

    namespace = types.SimpleNamespace(
        FutureSourcedRow=FutureSourcedRow,
        FutureUnsourcedRow=FutureUnsourcedRow,
        NotAModel=int,
    )
    discovered = models_requiring_provenance(namespace=namespace)
    assert discovered == (FutureSourcedRow,)
