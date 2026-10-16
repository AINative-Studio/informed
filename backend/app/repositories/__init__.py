"""Informed's repository layer (story 1.4).

The only place Informed's own tables are read and written. Everything above it
— ingest adapters, agents, API handlers — depends on this package and on the
exceptions below, never on `ZeroDBClient` directly, so the rules datamodel.md
states about ids, references and history are enforced once rather than
re-implemented per caller.

    from app.repositories import Repository
    from app.repositories.models import MemberVote

    votes = Repository(client=client, model=MemberVote)
    votes.upsert({"roll_call_id": "us-house:119-2:452", "person_id": "person:B000944", ...})

A table whose rows cite a source — any model declaring a `source_url` — is
written through a repository that was told who fetched them (story 16.3):

    from app.repositories import ProvenanceContext, Repository
    from app.repositories.models import RollCall

    roll_calls = Repository(
        client=client,
        model=RollCall,
        provenance=ProvenanceContext(adapter_version="house_rollcalls/v1"),
    )
    roll_calls.upsert(row)   # writes the row AND its provenance_log entry

Without that context the write is refused rather than producing evidence
nothing can trace — datamodel.md's integrity check made structural. See
`provenance.py` for the full reasoning.

See `base.py` for why the upsert is a find-then-write, and for what this layer
deliberately does not guarantee (nothing here is atomic).
"""

from app.repositories.base import DEFAULT_PAGE_SIZE, Repository
from app.repositories.errors import (
    AppendOnlyViolationError,
    MissingProvenanceError,
    MissingReferenceError,
    RepositoryError,
    RowValidationError,
)
from app.repositories.provenance import (
    ProvenanceContext,
    content_hash_for,
    provenance_id,
    write_provenance,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "AppendOnlyViolationError",
    "MissingProvenanceError",
    "MissingReferenceError",
    "ProvenanceContext",
    "RepositoryError",
    "Repository",
    "RowValidationError",
    "content_hash_for",
    "provenance_id",
    "write_provenance",
]
