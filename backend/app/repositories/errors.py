"""Domain exceptions for the repository layer (story 1.4).

ZeroDB has no foreign keys, no unique constraints and no check constraints, so
every rule datamodel.md states about how rows relate is enforced here instead.
These exceptions are what "enforced" looks like from a caller's side: an ingest
adapter, an agent or an API handler catches one of these and never a
`pydantic.ValidationError` or a raw `ZeroDBError`.

They are deliberately separate from `app.zerodb.errors`. A `ZeroDBError` means
ZeroDB itself refused or failed; a `RepositoryError` means Informed's own rules
refused a write that ZeroDB would have accepted quite happily — which is the
more dangerous of the two, because nothing downstream would have noticed.

Every one of these carries the table, field and id involved rather than only a
message: an ingest run writes thousands of rows unattended, and a log line
reading "missing reference" without naming which id is a log line nobody can
act on.
"""


class RepositoryError(Exception):
    """Base class for every rule the repository layer enforces."""


class RowValidationError(RepositoryError):
    """The row does not match its Pydantic model, or contradicts its own id.

    Raised before any ZeroDB request: a row this layer knows is malformed must
    not cost a round trip, and must certainly not be written. ZeroDB's
    `schema_definition` is not a validator — it accepts a misspelled field name
    without complaint, and the row then becomes invisible to every query that
    expects the correct spelling — so this model check is the only thing
    standing between a typo and silently unreadable data.
    """

    def __init__(self, message: str, *, table: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.table = table
        self.field = field


class MissingReferenceError(RepositoryError):
    """A row references an id that does not exist in the referenced table.

    This is datamodel.md rule 5 ("integrity lives in code") and the first
    acceptance criterion of story 1.4. In a relational database a foreign key
    would reject this write; in ZeroDB nothing would, and the orphaned row would
    surface later as a verdict citing a roll call that cannot be loaded — a
    broken citation, which prd.md's "evidence over opinion" principle treats as
    worse than no claim at all.
    """

    def __init__(
        self,
        *,
        table: str,
        row_id: str,
        field: str,
        referenced_table: str,
        referenced_id: str,
    ) -> None:
        super().__init__(
            f"{table} row {row_id!r} references {referenced_table} id "
            f"{referenced_id!r} via {field!r}, which does not exist"
        )
        self.table = table
        self.row_id = row_id
        self.field = field
        self.referenced_table = referenced_table
        self.referenced_id = referenced_id


class AppendOnlyViolationError(RepositoryError):
    """A write used the wrong semantics for the table it addressed.

    datamodel.md rule 6: `verdicts`, `candidate_positions` and `provenance_log`
    are never updated in place; a new row is appended and `is_current` marks the
    live one. Those tables are the reproducibility record — "what did Informed
    say about this promise in March, and on what evidence" — so an in-place
    update there destroys evidence rather than correcting it.

    Raised in both directions, deliberately. An `upsert` on an append-only table
    would overwrite history; an `append_version` on a plain table would litter
    `people` with versions nothing reads. Each is a mistake worth a loud failure
    rather than a quiet reinterpretation of what the caller meant.
    """

    def __init__(self, message: str, *, table: str) -> None:
        super().__init__(message)
        self.message = message
        self.table = table


class MissingProvenanceError(RepositoryError):
    """A row citing a source would have been written with nothing to trace it to.

    datamodel.md's integrity check: "Every NoSQL row with a `source_url` has a
    matching `provenance_log` entry, so no evidence goes untraced." Story 16.3
    makes the repository write that entry itself — but it can only do so when it
    was told WHO fetched the record (`adapter_version`) and WHEN
    (`ProvenanceContext`'s clock). Neither is a property of the row, and both
    are the audit trail's entire content.

    So a repository asked to write a sourced row with no `ProvenanceContext`
    raises this instead, before anything reaches ZeroDB. The alternative —
    writing a provenance entry with a placeholder adapter_version — would
    satisfy the letter of the integrity check while leaving a journalist who
    follows the citation at "unknown adapter, time of write". prd.md's
    "evidence over opinion" treats a broken citation as worse than no claim,
    and an unattributed provenance entry is a broken citation that looks
    satisfied.

    Also raised, in the other direction, for a `ProvenanceContext` handed to a
    model that has no `source_url` at all: that is a sign the caller has the
    wrong model or the wrong table, and ignoring it quietly would hide the
    mistake.
    """

    def __init__(self, message: str, *, table: str) -> None:
        super().__init__(message)
        self.message = message
        self.table = table


__all__ = [
    "AppendOnlyViolationError",
    "MissingProvenanceError",
    "MissingReferenceError",
    "RepositoryError",
    "RowValidationError",
]
