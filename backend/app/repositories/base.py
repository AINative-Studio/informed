"""The generic repository every Informed table is written through (story 1.4).

===============================================================================
WHY THIS LAYER EXISTS
===============================================================================

ZeroDB gives Informed a schema-flexible document store and nothing else: no
foreign keys, no unique constraints, no check constraints, and — the finding
that shapes this whole module — **no upsert by natural key**. `POST /rows`
always appends, and `PUT`/`DELETE` address a row by ZeroDB's own internal UUID,
which has no relationship to Informed's deterministic ids
(`us-house:119-2:452`). All of that is documented in datamodel.md's "Open
questions" and was confirmed live during stories 1.2 and 1.3.

So every rule datamodel.md states about how rows relate lives in code, here:

  1. **Deterministic ids from natural keys** — `upsert` derives the id from the
     model's `natural_key` and rejects a supplied id that contradicts it, so
     two ingest adapters cannot mint two ids for one record.
  2. **Idempotent re-ingest** — `upsert` finds the existing row by natural-key
     id and replaces it, or inserts when there is none. Re-ingesting the same
     record never produces a duplicate (story 1.4's second acceptance
     criterion).
  5. **Integrity lives in code** — `upsert` resolves every declared reference
     before writing and refuses the write when one is missing (story 1.4's
     first acceptance criterion).
  6. **Append-only where history matters** — `append_version` is a separate
     method, for `verdicts`, `candidate_positions` and `provenance_log`. It
     appends a new row and moves `is_current`; it never updates history in
     place.

Rules 1, 2 and 5 are the ordinary write path. Rule 6 is deliberately NOT
folded into it: `upsert` and `append_version` mean different things, and a
single method that guessed which the caller wanted would eventually overwrite a
verdict someone had cited. Calling the wrong one raises
`AppendOnlyViolationError` in both directions rather than reinterpreting the
call.

===============================================================================
WHAT THIS LAYER DOES *NOT* GUARANTEE
===============================================================================

**Nothing here is atomic**, because ZeroDB offers no compare-and-set, no
transaction and no unique index. Specifically:

  * Two workers upserting the same id concurrently can both find nothing and
    both insert, producing the duplicate rule 1 forbids.
  * `append_version` writes the new row and clears the previous `is_current` as
    two requests. A crash between them leaves two current rows — the state
    datamodel.md's integrity check looks for.

Both are acceptable *today* because ingest is single-writer per table (one
scheduled run per source) and the nightly integrity audit datamodel.md
specifies is what catches a torn write. Neither is acceptable once concurrent
ingest arrives. The fix needs something from ZeroDB — a conditional write, or a
unique constraint on `row_data.id` — which is why it is raised as an open
question rather than worked around with a lock this process cannot enforce
across workers.

The new row is written BEFORE the old flag is cleared, deliberately. Of the two
possible torn states, "two rows claim to be current" is recoverable by the audit
(the higher version wins); "no row is current" would make a candidate's verdict
disappear from the UI until someone noticed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import ValidationError

from app.repositories.errors import (
    AppendOnlyViolationError,
    MissingReferenceError,
    RowValidationError,
)
from app.repositories.models import RowModel
from app.zerodb.client import ZeroDBClient
from app.zerodb.models import Row

logger = logging.getLogger(__name__)

#: How many rows one `query` call reads. A page, never "everything":
#: `member_votes` reaches ~600k rows per Congress (datamodel.md), so a caller
#: that wants more must page explicitly rather than be handed a silent subset.
DEFAULT_PAGE_SIZE = 100


class Repository[ModelT: RowModel]:
    """Reads and writes one table's rows as validated models.

    One instance per table, built from the model that declares it:

        >>> people = Repository(client=client, model=Person)
        >>> people.table_name
        'people'
        >>> people.upsert({"id": "person:B000944", "canonical_name": "..."})

    Instances hold no per-request state and are safe to share, like the
    `ZeroDBClient` underneath them. The reference cache lives for the duration
    of a single `upsert`/`upsert_many`/`append_version` call and is discarded
    afterwards, so a later call cannot keep writing children against a parent
    that has since been deleted.
    """

    def __init__(self, *, client: ZeroDBClient, model: type[ModelT]) -> None:
        self._client = client
        self._model = model

    @property
    def table_name(self) -> str:
        """The ZeroDB table this repository owns, per the model's declaration."""
        return self._model.table_name

    @property
    def model(self) -> type[ModelT]:
        return self._model

    @property
    def is_append_only(self) -> bool:
        """Whether this table keeps history instead of updating in place.

        True exactly for the tables datamodel.md rule 6 names: those whose
        model declares a `version_key`.
        """
        return self._model.version_key is not None

    # =====================================================================
    # Writes
    # =====================================================================

    def upsert(self, row: dict[str, Any], *, check_references: bool = True) -> ModelT:
        """Write one row under its deterministic natural-key id, idempotently.

        The order of operations matters and is the same every time:

          1. **Validate** against the model. A malformed row must not cost a
             round trip, let alone a reference lookup.
          2. **Derive or verify the id** from the model's natural key.
          3. **Resolve every declared reference.** A missing one raises
             `MissingReferenceError` and nothing is written.
          4. **Find-then-write**, through `ZeroDBClient.upsert_row`, which
             resolves the natural-key id to ZeroDB's internal UUID and either
             replaces that row or inserts a new one.

        Step 4 is a full replacement, not a merge: an upsert states the
        record's whole current contents, so a field the caller dropped should
        disappear. Use `ZeroDBClient.update_row` directly for a genuine partial
        patch — the repository does not offer one, because every ingest path
        re-ingests complete records.

        Args:
            row: The record's full contents. `id` may be omitted when the model
                has a natural key to derive it from.
            check_references: Set False only for a trusted bulk load that
                inserts parents and children in one pass and relies on the
                nightly integrity audit instead. Never the default, so skipping
                the check is always visible in the calling code.

        Returns:
            The validated model as stored, with `id` populated.

        Raises:
            RowValidationError: the row does not fit the model, or its `id`
                contradicts its natural key.
            MissingReferenceError: a referenced id does not exist.
            AppendOnlyViolationError: this table keeps history; use
                `append_version`.
        """
        self._refuse_if_append_only("upsert")
        instance = self._validated(row)
        assert instance.id is not None  # noqa: S101 - _validated guarantees it
        if check_references:
            self._check_references(instance, cache={})
        stored = self._client.upsert_row(self.table_name, instance.id, instance.row_data())
        return self._parse_row(stored)

    def upsert_many(
        self, rows: Iterable[dict[str, Any]], *, check_references: bool = True
    ) -> list[ModelT]:
        """Upsert a batch, resolving each referenced id at most once.

        The cache is what makes this worth having over a loop of `upsert`: the
        several hundred `member_votes` rows for one roll call all reference that
        same roll call, and re-reading it per row would multiply an ingest run's
        request count by the chamber's size — straight into the 60 req/min
        ceiling story 1.2 measured.

        Not a transaction. Rows are written in order, and a
        `MissingReferenceError` on row 40 leaves rows 1-39 written. That is the
        right failure for an idempotent ingest: fix the data and re-run, and the
        first 39 upserts are no-ops. A caller that needs all-or-nothing has to
        validate the whole batch first, which `check_references=False` plus a
        separate validation pass allows.
        """
        self._refuse_if_append_only("upsert_many")
        cache: dict[tuple[str, str], bool] = {}
        stored: list[ModelT] = []
        for row in rows:
            instance = self._validated(row)
            assert instance.id is not None  # noqa: S101 - _validated guarantees it
            if check_references:
                self._check_references(instance, cache=cache)
            written = self._client.upsert_row(self.table_name, instance.id, instance.row_data())
            stored.append(self._parse_row(written))
        return stored

    def append_version(self, row: dict[str, Any], *, check_references: bool = True) -> ModelT:
        """Append a new version of a record and move `is_current` onto it.

        For the append-only tables of datamodel.md rule 6 only: `verdicts`,
        `candidate_positions`, `provenance_log`. Those rows are the
        reproducibility record — "what did Informed say about this promise in
        March, and on what evidence" — so a change is a new row, never an edit.

        What it does, in order:

          1. Validates the row, ignoring any `is_current` the caller set (it is
             this method's to assign, so no caller can mint a second current
             row).
          2. Resolves the references, as `upsert` does.
          3. Reads the current row for this version key to learn the next
             version number and which row to supersede.
          4. **Inserts** the new row with `is_current = True` and the id
             `{key}:v{n}` — `insert_row`, not `upsert_row`, because appending is
             the whole point: ZeroDB's append-always behaviour is the desired
             semantics here rather than something to work around.
          5. Clears `is_current` on the superseded row, if there was one,
             through `ZeroDBClient.update_row` — which read-merge-writes, so the
             superseded verdict's own fields survive. A naive
             `PUT {"is_current": false}` would replace the row wholesale and
             destroy the history this table exists to keep (confirmed live in
             story 1.2).

        Steps 4 and 5 are two requests and are not atomic; see the module
        docstring for why the new row is written first.

        Raises:
            AppendOnlyViolationError: this table is not append-only; use
                `upsert`.
            RowValidationError, MissingReferenceError: as `upsert`.
        """
        version_key = self._model.version_key
        if version_key is None:
            raise AppendOnlyViolationError(
                f"{self.table_name} is not append-only; use upsert() instead of append_version()",
                table=self.table_name,
            )

        # `is_current` and `id` are assigned here, from stored state, so a
        # caller cannot set either: both are how "exactly one current row"
        # is kept true.
        proposed = {key: value for key, value in row.items() if key not in {"is_current", "id"}}
        draft = self._validated(proposed, require_id=False)
        if check_references:
            self._check_references(draft, cache={})

        key_values = self._version_key_values(draft, version_key)
        previous = self._current_row(key_values)
        prefix = self._version_id_prefix(draft, key_values)
        next_version = self._next_version_number(previous, prefix=prefix)

        row_id = f"{prefix}:v{next_version}"
        payload = {**draft.row_data(), "id": row_id, "is_current": True}
        instance = self._validated(payload, versioned=True)
        written = self._client.insert_row(self.table_name, instance.row_data())

        if previous is not None:
            previous_id = previous.row_data.get("id")
            if previous_id:
                # Read-merge-write: `update_row` merges, so every other field of
                # the superseded row survives the flag flip.
                self._client.update_row(self.table_name, str(previous_id), {"is_current": False})

        return self._parse_row(written)

    def delete(self, row_id: str) -> None:
        """Delete one row by its natural-key id.

        Refused on append-only tables: datamodel.md's retention table keeps
        `verdicts`, `candidate_positions` and `provenance_log` indefinitely for
        reproducibility, so a delete there is a bug rather than a request.

        This is for `voter_profiles` deletion (where the row goes and the data
        key is shredded) and for test teardown.

        Raises:
            AppendOnlyViolationError: the table is append-only.
            ZeroDBNotFoundError: no row carries that id.
        """
        self._refuse_if_append_only("delete")
        self._client.delete_row(self.table_name, row_id)

    # =====================================================================
    # Reads
    # =====================================================================

    def get(self, row_id: str) -> ModelT | None:
        """One row by natural-key id, or None when it is absent.

        None rather than an exception: "no such row" is the ordinary answer a
        reference check needs, and a raised not-found would be
        indistinguishable from a missing table.
        """
        row = self._client.get_row(self.table_name, row_id)
        return None if row is None else self._parse_row(row)

    def query(
        self,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - matches the client's field
        limit: int = DEFAULT_PAGE_SIZE,
        skip: int = 0,
    ) -> list[ModelT]:
        """A page of rows matching a flat equality filter, as models.

        A page, not a table: see `DEFAULT_PAGE_SIZE`. Note the client applies
        the filter client-side as well as sending it, because live ZeroDB was
        observed to drop matching rows for a server-side filter (datamodel.md
        "Open questions") — so a filter here is reliable within a page but
        cannot be assumed to have narrowed the page server-side.
        """
        page = self._client.query_rows(self.table_name, filter=filter, limit=limit, skip=skip)
        return [self._parse_row(row) for row in page.rows]

    def current(self, **version_key: Any) -> ModelT | None:
        """The live row for one version key on an append-only table.

        `verdicts.current(promise_id="prm:0001")`,
        `candidate_positions.current(person_id=…, stance_id=…)`. The keyword
        names must be exactly the model's `version_key`, so a typo'd or
        incomplete key raises rather than quietly returning some other row's
        current version.

        Raises:
            AppendOnlyViolationError: the table is not append-only, so nothing
                is marked current.
            RowValidationError: the keys given are not the model's version key.
        """
        expected = self._model.version_key
        if expected is None:
            raise AppendOnlyViolationError(
                f"{self.table_name} is not append-only, so it has no current row",
                table=self.table_name,
            )
        if set(version_key) != set(expected):
            raise RowValidationError(
                f"current() on {self.table_name} needs exactly {list(expected)}, "
                f"got {sorted(version_key)}",
                table=self.table_name,
            )
        row = self._current_row(version_key)
        return None if row is None else self._parse_row(row)

    # =====================================================================
    # Internals
    # =====================================================================

    def _validated(
        self, row: dict[str, Any], *, require_id: bool = True, versioned: bool = False
    ) -> ModelT:
        """Validate a row and settle its deterministic id.

        Outcomes, all of them before any request is made:

          * the row does not fit the model — raise, naming the fields;
          * the model has a natural key and the row's `id` disagrees with it —
            raise, because the caller's id scheme has drifted from
            datamodel.md's and silently preferring either one would hide it;
          * otherwise, return the instance with `id` filled in from the natural
            key when the caller omitted it.

        `versioned` is set by `append_version` for a row whose id already
        carries a `:v{n}` suffix that no natural key can predict. The id is then
        checked against the natural key as a *prefix* rather than replaced, so
        the drift check still applies without the version number tripping it.
        """
        try:
            instance = self._model.model_validate(row)
        except ValidationError as exc:
            fields = ", ".join(
                ".".join(str(part) for part in error["loc"]) for error in exc.errors()
            )
            raise RowValidationError(
                f"{self.table_name} row is invalid: {exc.error_count()} problem(s) "
                f"in {fields or 'the row'}",
                table=self.table_name,
                field=fields or None,
            ) from exc

        derived = self._model.build_id(instance.model_dump())
        if derived is not None and not versioned:
            if instance.id is not None and instance.id != derived:
                raise RowValidationError(
                    f"{self.table_name} row id {instance.id!r} contradicts its natural key "
                    f"{list(self._model.natural_key)}, which gives {derived!r}",
                    table=self.table_name,
                    field="id",
                )
            instance = instance.model_copy(update={"id": derived})
        elif versioned and derived is not None and instance.id is not None:
            # An append-only row's id is `{natural key}:v{n}`; `build_id` cannot
            # know `n`, so only the prefix is checked. This still catches a
            # caller whose natural-key values disagree with the id it was
            # assigned, which is the drift worth catching.
            expected_prefix = f"{derived}:v"
            if not instance.id.startswith(expected_prefix):
                raise RowValidationError(
                    f"{self.table_name} row id {instance.id!r} contradicts its natural key "
                    f"{list(self._model.natural_key)}, which gives the prefix "
                    f"{expected_prefix!r}",
                    table=self.table_name,
                    field="id",
                )

        if require_id and instance.id is None:
            raise RowValidationError(
                f"{self.table_name} row needs an id: the model has no natural key to "
                "derive one from, so the caller must supply it",
                table=self.table_name,
                field="id",
            )
        return instance

    def _check_references(self, instance: ModelT, *, cache: dict[tuple[str, str], bool]) -> None:
        """Resolve every declared reference, or raise naming the first gap.

        This is datamodel.md rule 5 and story 1.4's first acceptance criterion.
        A field holding None is skipped: an optional reference that is absent is
        not a dangling one.

        `cache` maps (table, id) to whether it resolved, within one call. It is
        never shared between calls — see the module docstring and
        `upsert_many`.
        """
        values = instance.model_dump()
        for field_name, referenced_table in self._model.references.items():
            referenced_id = values.get(field_name)
            if referenced_id is None:
                continue
            referenced_id = str(referenced_id)
            key = (referenced_table, referenced_id)
            if key not in cache:
                cache[key] = self._client.get_row(referenced_table, referenced_id) is not None
            if not cache[key]:
                raise MissingReferenceError(
                    table=self.table_name,
                    row_id=str(instance.id),
                    field=field_name,
                    referenced_table=referenced_table,
                    referenced_id=referenced_id,
                )

    def _refuse_if_append_only(self, operation: str) -> None:
        if self.is_append_only:
            raise AppendOnlyViolationError(
                f"{self.table_name} is append-only (datamodel.md rule 6): history is "
                f"never updated in place, so {operation}() is refused. Use "
                "append_version() to add a version and move is_current.",
                table=self.table_name,
            )

    def _version_key_values(self, instance: ModelT, version_key: Sequence[str]) -> dict[str, Any]:
        values = instance.model_dump()
        return {name: values[name] for name in version_key}

    def _version_id_prefix(self, instance: ModelT, key_values: dict[str, Any]) -> str:
        """The part of an append-only row's id that comes before `:v{n}`.

        The model's natural key when it declares one, so
        `candidate_positions` gets datamodel.md's documented
        `{person_id}:{stance_id}:{method_ver}`; otherwise the version key, so
        `verdicts` gets `{promise_id}` and the whole id reads
        `prm:0001:v1` — again exactly datamodel.md's pattern.

        The difference matters: `candidate_positions` is current per
        (person, stance) but identified per (person, stance, methodology), so
        a recompute under a NEW methodology must not be treated as v2 of the
        old one's id, while still superseding it as the current position.
        """
        derived = self._model.build_id(instance.model_dump())
        if derived is not None:
            return derived
        return ":".join(str(value) for value in key_values.values())

    def _current_row(self, key_values: dict[str, Any]) -> Row | None:
        """The `is_current` row for one version key, or None.

        Reads with the flag in the filter rather than scanning versions: the
        flag is the index into history, and a promise can accumulate many
        verdicts over a cycle.
        """
        page = self._client.query_rows(
            self.table_name,
            filter={**key_values, "is_current": True},
            limit=2,
        )
        if not page.rows:
            return None
        if len(page.rows) > 1:
            # datamodel.md's integrity check ("exactly one is_current = true
            # row") has been violated — by a torn write, or by a direct write
            # that bypassed this layer. Log it and supersede the newest, so the
            # next append repairs rather than compounds the damage; the nightly
            # audit is what reports it.
            logger.error(
                "%s has %d current rows for %s; expected exactly one",
                self.table_name,
                len(page.rows),
                sorted(key_values),
            )
        return max(page.rows, key=lambda row: _version_number(row.row_data.get("id")))

    @staticmethod
    def _next_version_number(previous: Row | None, *, prefix: str) -> int:
        """The next `v{n}` for `prefix`, counted from the superseded row's id.

        Derived from stored state rather than from a count of rows: a counted
        version would collide after a row was ever removed, and the id is the
        one place the version is recorded.

        Only counts on from the superseded row when that row shares this
        prefix. `candidate_positions` supersedes across methodology versions —
        the current row for (person, stance) may be under the previous
        methodology's id — and continuing its numbering would put a v2 under an
        id whose v1 does not exist.
        """
        if previous is None:
            return 1
        previous_id = previous.row_data.get("id")
        if not isinstance(previous_id, str) or not previous_id.startswith(f"{prefix}:v"):
            return 1
        return _version_number(previous_id) + 1

    def _parse_row(self, row: Row) -> ModelT:
        """Validate a stored row into the model.

        Raises rather than skipping a row that no longer fits: a short list
        would make an ingest or a page look complete while silently dropping
        records, and in a system whose whole claim is cited evidence, a quietly
        missing record is worse than a loud failure.
        """
        try:
            return self._model.model_validate(row.row_data)
        except ValidationError as exc:
            row_id = row.row_data.get("id", "<no id>")
            raise RowValidationError(
                f"stored {self.table_name} row {row_id!r} does not fit "
                f"{self._model.__name__}: {exc.error_count()} problem(s)",
                table=self.table_name,
            ) from exc


def _version_number(row_id: Any) -> int:
    """The `n` in an id ending `:v{n}`, or 0 when there is none.

    0 for an unparseable id so that `_next_version_number` still produces 1 and
    a malformed historical id cannot stall every future append.
    """
    if not isinstance(row_id, str):
        return 0
    _, separator, suffix = row_id.rpartition(":v")
    if not separator or not suffix.isdigit():
        return 0
    return int(suffix)


__all__ = ["DEFAULT_PAGE_SIZE", "Repository"]
