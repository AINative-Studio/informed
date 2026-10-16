"""NEGATIVE CONTROL for story 16.3's integrity check. Deliberately broken.

This file is not importable application code and is never executed. It is the
fixture `tests/test_provenance_integrity.py` feeds to
`app.repositories.provenance.check_ingest_module` to prove the check actually
catches a missing-provenance bug rather than passing vacuously.

What is wrong with it: `roll_calls` carries a `source_url`, so datamodel.md's
integrity rule requires a matching `provenance_log` entry for every row written
— and this module builds its repository without a `provenance=` context, which
is exactly how a future ingest adapter would forget.

Do NOT "fix" this file. Fixing it breaks the test that proves the check works.
"""

from __future__ import annotations

from typing import Any

from app.repositories import Repository
from app.repositories.models import RollCall
from app.zerodb.client import ZeroDBClient


class ForgetfulLoader:
    def __init__(self, client: ZeroDBClient) -> None:
        # THE BUG: no provenance= argument, on a model that carries a source_url.
        self._roll_calls: Repository[RollCall] = Repository(client=client, model=RollCall)

    def load(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            self._roll_calls.upsert(row)
