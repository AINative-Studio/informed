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

See `base.py` for why the upsert is a find-then-write, and for what this layer
deliberately does not guarantee (nothing here is atomic).
"""

from app.repositories.base import DEFAULT_PAGE_SIZE, Repository
from app.repositories.errors import (
    AppendOnlyViolationError,
    MissingReferenceError,
    RepositoryError,
    RowValidationError,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "AppendOnlyViolationError",
    "MissingReferenceError",
    "RepositoryError",
    "Repository",
    "RowValidationError",
]
