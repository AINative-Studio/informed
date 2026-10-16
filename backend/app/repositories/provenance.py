"""Provenance: the rule that no evidence goes untraced (story 16.3).

===============================================================================
THE RULE, AND WHY IT NEEDED PROMOTING
===============================================================================

datamodel.md, "Integrity checks":

    Every NoSQL row with a `source_url` has a matching `provenance_log` entry,
    so no evidence goes untraced.

Story 5.1 implemented that correctly for one source. It wrote the
`roll_calls` row, then called its own `write_provenance` helper, and its PR
noted: "a future source writing to provenance_log may want the same helper
promoted into the repository layer if the pattern repeats." This module is
that promotion, and story 16.3 is the story that asks what "enforced" means
when there is exactly one compliant caller and nothing stopping the second one
from being non-compliant.

Three enforcement designs were on the table. All three were built, because they
catch different failures and none of them is sufficient alone:

**(a) A runtime test per ingest module.** Write a row through the module's real
write path, assert a provenance entry appeared. Proves the mechanism works, but
only for paths a test happens to exercise — and a brand-new adapter with no
test is exactly the case that forgets.

**(b) A static check.** Parse every module under `app/ingest/` and look for a
sourced-model write with no provenance. Covers code no test touches, but it is
pattern matching: a write spelled a way the check does not recognize slips
through.

**(c) Make forgetting impossible in the repository.** `Repository.upsert` sees
the model, so it can tell whether the row it is about to write carries a
`source_url` — and if it does, it writes the provenance entry itself. There is
then no separate step to forget.

**(c) is the primary mechanism, and this module implements it.** (a) and (b)
run in CI as `tests/test_provenance_integrity.py`, guarding the two things (c)
cannot guard by itself: that the auto-write actually produces a conforming row
(a), and that no future code reaches around the repository to `ZeroDBClient`
directly (b).

===============================================================================
WHY (c) HAD TO REFUSE, NOT JUST DEFAULT
===============================================================================

The repository can see that a row needs provenance. What it cannot invent is
the provenance *itself*: `adapter_version` names the code that did the fetching,
and `fetched_at` is when THAT code fetched — neither is a property of the row,
and both are the audit trail's whole content. A repository handed neither has
two options:

  * write a provenance row with a placeholder adapter_version, or
  * refuse the write.

It refuses (`MissingProvenanceError`). A placeholder would satisfy the letter of
the integrity check while making the log useless — a journalist following the
rule to its row would find "unknown adapter, time of write" where "which code
fetched this, and when" should be. prd.md's "evidence over opinion" treats a
broken citation as worse than no claim, and an unattributed provenance entry is
a broken citation that *looks* satisfied. Refusing is loud, happens before
anything is written, and the fix is one argument at the call site.

The refusal is scoped as narrowly as the rule: it applies only when the model
declares a `source_url` field AND the row being written actually carries a
non-null value for it. A `roll_calls` row with no source (a hand-entered
correction) traces to nothing, so there is nothing to log, and refusing it
would block a legitimate write in the name of a rule about cited rows.

===============================================================================
CONTENT-ADDRESSED IDS, KEPT FROM 5.1
===============================================================================

`provenance_log`'s documented id is `prov:{sha256}` — content-addressed, not
`{entity}:v{n}` — so `Repository.append_version` (which numbers versions from
what is stored) does not apply and `Repository.upsert` is the wrong verb. The
write is a check-then-insert: read by the content-addressed id, insert only if
absent. Story 5.1's reasoning, kept verbatim in effect:

  * a byte-identical re-fetch hashes identically, lands on the row that already
    exists, and writes nothing — idempotent re-ingest for free;
  * genuinely changed content hashes differently and appends a SECOND entry,
    which is correct: the record now has two fetches on file at two times, and
    datamodel.md's reproducibility goal is served by keeping both.

**What hash, when the row has no `content_hash` field.** `roll_calls` carries
one (the sha256 of the fetched record). `bills` carries a `source_url` and no
hash column at all, and a future sourced table may carry neither. So when the
row has no `content_hash`, the hash falls back to `content_hash_for` over the
row's own stored contents. That keeps the id content-addressed and the write
idempotent for every sourced table rather than leaving a hole for the tables
whose adapter happens not to hash its input. The two are not interchangeable
and the difference is worth knowing: a `content_hash` tracks the SOURCE record
(so a re-fetch that changed a field this transform ignores still appends an
entry), while the fallback tracks the STORED row (so it appends only when
something Informed kept actually changed). Where a hash exists it is preferred,
because "what did the source say, and when" is the stronger audit claim.

===============================================================================
WHAT THIS DOES NOT GUARANTEE
===============================================================================

Not atomic, for the same reason nothing in `base.py` is: ZeroDB has no
transaction. The entity row is written first, then its provenance entry, as two
requests. A crash between them leaves a row with a `source_url` and no
provenance entry — precisely the state datamodel.md's nightly integrity audit
looks for, which is why that audit (story 16.2) is the backstop rather than
this module claiming to be one.

The order is deliberate: entity first. The inverse — provenance first — would
leave a provenance entry pointing at an `entity_id` that does not exist, which
reads as "this was fetched and then deleted" and is a worse lie than "this was
written and the log entry is pending".
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.repositories.errors import MissingProvenanceError
from app.repositories.models import ProvenanceLog, RowModel

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: The field whose presence on a model triggers datamodel.md's integrity rule.
#: One constant rather than the literal in six places, so the rule's trigger is
#: greppable and a rename cannot leave half the checks looking for the old name.
SOURCE_URL_FIELD = "source_url"

#: The table the rule writes into.
PROVENANCE_TABLE = ProvenanceLog.table_name

#: Where the ingest adapters live. The static check walks this directory rather
#: than a list of modules, so a source added in a future story is covered the
#: day it lands.
INGEST_PACKAGE_PATH = Path(__file__).resolve().parent.parent / "ingest"


def utc_now_iso() -> str:
    """The current instant as an ISO 8601 string with an explicit offset.

    Explicit offset, never a naive timestamp: an audit log whose times cannot
    be ordered against another system's is not an audit log.
    """
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ProvenanceContext:
    """Who fetched a record, and a clock saying when.

    The two things a row cannot tell the repository about its own provenance:

        adapter_version: the code that did the fetching
            (`"house_rollcalls/v1"`). Bumped when a transform's field mapping
            changes in a way that would make an old entry describe a different
            shape than this version produces — the same purpose
            `extractor_version` and `methodology_version` serve elsewhere in
            datamodel.md.
        now: the clock, injectable
            So a test can assert `fetched_at` exactly. Defaults to a real UTC
            clock; nothing in production passes it.
        on_write: notified for each entry actually inserted
            Optional. An ingest adapter's run report wants to say what this run
            newly recorded, and the repository's own return value is the entity
            row, not the provenance entry. Called ONLY for a genuine insert —
            never for an unchanged re-ingest that landed on the existing
            content-addressed row — so a report built from it counts fetches
            recorded rather than rows touched.

    Frozen, so one context is safely shared by every repository an adapter
    builds. `on_write`, if given, is the one place a caller can accumulate
    state; that is the caller's own object, not this one's.
    """

    adapter_version: str
    now: Callable[[], str] = field(default=utc_now_iso)
    on_write: Callable[[ProvenanceLog], None] | None = None

    def __post_init__(self) -> None:
        if not self.adapter_version:
            raise ValueError("a ProvenanceContext needs a non-empty adapter_version")

    def fetched_at(self) -> str:
        """When the fetch this context describes happened: now, by the clock."""
        return self.now()

    def notify(self, written: ProvenanceLog | None) -> None:
        """Hand a newly-inserted entry to `on_write`, if there is one."""
        if written is not None and self.on_write is not None:
            self.on_write(written)


def content_hash_for(record: Any) -> str:
    """The sha256 hex digest of one record, for a content-addressed id.

    Promoted verbatim in behaviour from story 5.1's
    `house_rollcalls.content_hash_for`, including the reason for
    `sort_keys=True`: key order in a JSON object carries no meaning, so hashing
    raw bytes would make two semantically-equal responses (a reordered object,
    different whitespace) hash differently — exactly the spurious "content
    changed" the idempotency design depends on NOT happening. The hash is a
    function of the data, not of the source's serialization choices.

    `default=str` covers a value JSON cannot serialize on its own (a datetime a
    caller forgot to stringify). Stringifying is right for a hash input — two
    equal values stringify equally — where it would be wrong for storage.
    """
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def provenance_id(content_hash: str) -> str:
    """datamodel.md's documented id for this table: `prov:{sha256}`."""
    return f"prov:{content_hash}"


def requires_provenance(model: type[RowModel]) -> bool:
    """Whether datamodel.md's integrity rule applies to this model's rows.

    True when the model declares a `source_url` field — and False for
    `ProvenanceLog` itself, which declares one because it RECORDS the source
    url of another row. Requiring provenance for a provenance entry is a
    recursion with no base case and no auditing value: the entry already says
    which adapter wrote it and when.
    """
    if model is ProvenanceLog:
        return False
    return SOURCE_URL_FIELD in model.model_fields


def models_requiring_provenance(namespace: Any = None) -> tuple[type[RowModel], ...]:
    """Every `RowModel` in a namespace whose rows need a provenance entry.

    Derived, never a list. `app.repositories.models` is scanned by default, so
    a model added in a future story appears here the moment it declares a
    `source_url` — which is the difference between a rule and a checklist
    someone has to remember to extend.

    Scanning a module's public classes rather than walking
    `RowModel.__subclasses__()`: a subclass defined anywhere (a test, a
    throwaway script) stays in `__subclasses__()` for the life of the process,
    and a model whose table nothing creates would then look like a violation of
    the rule this function exists to state. The module is the authoritative
    list of Informed's tables; the subclass graph is not.

    Returned sorted by table name so a parametrized test's ids are stable.
    """
    if namespace is None:
        from app.repositories import models as namespace  # noqa: PLC0415 - avoids a cycle

    found = [
        value
        for value in vars(namespace).values()
        if isinstance(value, type)
        and issubclass(value, RowModel)
        and value is not RowModel
        and hasattr(value, "table_name")
        and requires_provenance(value)
    ]
    return tuple(sorted(dict.fromkeys(found), key=lambda model: model.table_name))


def write_provenance(
    client: ZeroDBClient,
    *,
    entity_table: str,
    entity_id: str,
    source_url: str,
    fetched_at: str,
    content_hash: str,
    adapter_version: str,
) -> ProvenanceLog | None:
    """Insert one `provenance_log` row if, and only if, it is not already there.

    Promoted from story 5.1's `house_rollcalls.write_provenance`, unchanged in
    behaviour, generic in its arguments. A plain check-then-insert through
    `ZeroDBClient` rather than `Repository.append_version` or
    `Repository.upsert`, because the id is content-addressed: there is no
    version number to compute, no `is_current` flag to move, and ids for
    different content never collide so there is nothing an upsert could
    usefully replace. See the module docstring.

    Returns the written row, or None when a row with this exact content hash
    already existed and nothing was written — which is how a caller counts
    "what did this run actually record".
    """
    row_id = provenance_id(content_hash)
    if client.get_row(PROVENANCE_TABLE, row_id) is not None:
        return None
    row = ProvenanceLog.model_validate(
        {
            "id": row_id,
            "entity_table": entity_table,
            "entity_id": entity_id,
            "source_url": source_url,
            "fetched_at": fetched_at,
            "adapter_version": adapter_version,
        }
    )
    client.insert_row(PROVENANCE_TABLE, row.row_data())
    return row


def write_provenance_for_row(
    client: ZeroDBClient,
    *,
    model: type[RowModel],
    row_data: dict[str, Any],
    context: ProvenanceContext,
) -> ProvenanceLog | None:
    """The provenance entry for one just-written row, or None if it needs none.

    Called by `Repository.upsert`/`upsert_many` right after the entity row is
    written. Returns None — writing nothing — in the two cases the rule does
    not reach:

      * the model has no `source_url` field at all (`people`, `districts`,
        every reference table: see datamodel.md's field lists);
      * the row's `source_url` is null, so it cites nothing to trace.

    The hash prefers the row's own `content_hash` and falls back to hashing the
    stored row; the module docstring explains why both exist and what the
    difference means for an audit.
    """
    if not requires_provenance(model):
        return None
    source_url = row_data.get(SOURCE_URL_FIELD)
    if not source_url:
        return None

    row_id = row_data.get("id")
    if not row_id:
        # Unreachable through the repository, which settles the id before it
        # writes. Guarded anyway: a provenance entry whose entity_id is empty
        # points at nothing, which is worse than no entry because it looks
        # like the rule was satisfied.
        raise MissingProvenanceError(
            f"cannot record provenance for a {model.table_name} row with no id",
            table=model.table_name,
        )

    content_hash = row_data.get("content_hash") or content_hash_for(row_data)
    written = write_provenance(
        client,
        entity_table=model.table_name,
        entity_id=str(row_id),
        source_url=str(source_url),
        fetched_at=context.fetched_at(),
        content_hash=str(content_hash),
        adapter_version=context.adapter_version,
    )
    context.notify(written)
    return written


# =========================================================================
# The static check CI runs over the ingest tree
# =========================================================================


@dataclass(frozen=True)
class ProvenanceViolation:
    """One place in the source that would write untraceable evidence."""

    module: Path
    line: int
    table: str
    detail: str

    def __str__(self) -> str:
        return f"{self.module}:{self.line}: {self.table}: {self.detail}"


def _sourced_model_names() -> dict[str, str]:
    """Model class name to table name, for every model the rule covers.

    The static check works on names because it never imports the module it
    parses — importing an ingest module to inspect it would run its import-time
    code, and a check that executes the thing it is checking is a check that
    can be defeated by a side effect.
    """
    return {model.__name__: model.table_name for model in models_requiring_provenance()}


def check_source(source: str, *, module: Path) -> list[ProvenanceViolation]:
    """Parse one module's source and report every untraceable write path.

    What it looks for, and why each one:

      1. **`Repository(...)` built for a sourced model with no `provenance=`.**
         The main case. Such a repository cannot write provenance, so every
         sourced row written through it would be refused at runtime — this
         finds it at check time, naming the file and line, instead of at 3am
         in an unattended ingest run.
      2. **A direct `client.insert_row`/`upsert_row` into a sourced table.**
         The reach-around. `app.repositories.__init__` states that everything
         above the repository layer depends on it "never on `ZeroDBClient`
         directly"; a write that bypasses the repository also bypasses the
         automatic provenance entry, and (c)'s guarantee is only as good as
         that rule holding.

    Deliberately NOT a type checker. It matches on the model's class NAME as
    written at the construction site, which is how every call site in this
    codebase spells it (`Repository(client=client, model=RollCall)`). An alias
    (`model=M` where `M = RollCall`) would slip past, and that is an accepted
    limit: this layer is the backstop for code no test exercises, while the
    runtime layer (`Repository` refusing the write) is what actually makes the
    rule hold. A static check that tried to resolve arbitrary expressions would
    be a type checker with a worse error message.
    """
    tree = ast.parse(source, filename=str(module))
    sourced = _sourced_model_names()
    violations: list[ProvenanceViolation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee_name(node.func)
        if callee == "Repository":
            violation = _check_repository_call(node, module=module, sourced=sourced)
            if violation is not None:
                violations.append(violation)
        elif callee in {"insert_row", "upsert_row"}:
            violation = _check_direct_write(node, module=module, callee=callee)
            if violation is not None:
                violations.append(violation)

    return violations


def _callee_name(func: ast.expr) -> str | None:
    """The bare name a call is made through: `Repository`, `insert_row`, ..."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _check_repository_call(
    node: ast.Call, *, module: Path, sourced: dict[str, str]
) -> ProvenanceViolation | None:
    """A `Repository(...)` construction: is it sourced, and is it provenanced?"""
    model_name = _model_argument_name(node)
    if model_name is None or model_name not in sourced:
        return None
    has_provenance = any(keyword.arg == "provenance" for keyword in node.keywords)
    # `Repository(**kwargs)` — the arguments are not knowable statically, so it
    # is left to the runtime refusal rather than reported as a certainty.
    if any(keyword.arg is None for keyword in node.keywords):
        return None
    if has_provenance:
        return None
    return ProvenanceViolation(
        module=module,
        line=node.lineno,
        table=sourced[model_name],
        detail=(
            f"Repository(model={model_name}) is built without provenance=, but "
            f"{model_name} rows carry a source_url, so datamodel.md requires a "
            "matching provenance_log entry. Pass "
            "provenance=ProvenanceContext(adapter_version=...)."
        ),
    )


def _model_argument_name(node: ast.Call) -> str | None:
    """The model a `Repository(...)` was built for, keyword or positional.

    `Repository(client=c, model=RollCall)` is the spelling every call site in
    this codebase uses, but `Repository(c, RollCall)` is legal Python and the
    same mistake, so the second positional argument counts too.
    """
    for keyword in node.keywords:
        if keyword.arg == "model" and isinstance(keyword.value, ast.Name):
            return keyword.value.id
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Name):
        return node.args[1].id
    return None


def _check_direct_write(node: ast.Call, *, module: Path, callee: str) -> ProvenanceViolation | None:
    """A `client.insert_row("roll_calls", ...)` that reaches around the layer.

    Only a literal table name is reported. A computed one cannot be resolved
    without executing the module, and guessing would make the check cry wolf on
    the generic helper in this very file.
    """
    table = _literal_table_argument(node)
    if table is None:
        return None
    sourced_tables = {model.table_name for model in models_requiring_provenance()}
    if table not in sourced_tables:
        return None
    return ProvenanceViolation(
        module=module,
        line=node.lineno,
        table=table,
        detail=(
            f"{callee}({table!r}, ...) writes a row carrying a source_url directly "
            "through ZeroDBClient, bypassing the repository that would write its "
            "provenance_log entry. Write it through "
            "Repository(model=..., provenance=ProvenanceContext(...)) instead."
        ),
    )


def _literal_table_argument(node: ast.Call) -> str | None:
    for keyword in node.keywords:
        if keyword.arg in {"table", "table_name"} and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            return value if isinstance(value, str) else None
    if node.args and isinstance(node.args[0], ast.Constant):
        value = node.args[0].value
        return value if isinstance(value, str) else None
    return None


def check_ingest_module(path: Path) -> list[ProvenanceViolation]:
    """`check_source` over one file, read from disk."""
    return check_source(Path(path).read_text(encoding="utf-8"), module=Path(path))


def check_ingest_tree(root: Path | None = None) -> list[ProvenanceViolation]:
    """Every module under `app/ingest/`, checked.

    Walks the directory rather than a list of adapters, so story 5.2's
    `member_votes` loader and story 6.x's promise-source adapters are covered
    the day they are written, by a check nobody has to remember to extend.
    """
    directory = INGEST_PACKAGE_PATH if root is None else Path(root)
    violations: list[ProvenanceViolation] = []
    for path in sorted(directory.rglob("*.py")):
        violations.extend(check_ingest_module(path))
    return violations


__all__ = [
    "INGEST_PACKAGE_PATH",
    "PROVENANCE_TABLE",
    "SOURCE_URL_FIELD",
    "ProvenanceContext",
    "ProvenanceViolation",
    "check_ingest_module",
    "check_ingest_tree",
    "check_source",
    "content_hash_for",
    "models_requiring_provenance",
    "provenance_id",
    "requires_provenance",
    "utc_now_iso",
    "write_provenance",
    "write_provenance_for_row",
]
