"""NEGATIVE CONTROL for story 16.3's integrity check. Deliberately broken.

The same bug as `forgets_provenance.py`, spelled positionally rather than with
keywords, so the check cannot be satisfied by a keyword-only pattern match.
Never imported, never executed; see `forgets_provenance.py` for the full note.

Do NOT "fix" this file.
"""

from __future__ import annotations

from app.repositories import Repository
from app.repositories.models import RollCall


def build(client: object) -> Repository[RollCall]:
    # THE BUG: a sourced model, no provenance context, positional arguments.
    return Repository(client, RollCall)  # type: ignore[call-arg,arg-type]
