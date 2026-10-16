"""POSITIVE CONTROL for story 16.3's integrity check. Deliberately correct.

A check that flagged every module would pass the negative-control test while
being useless, so this fixture pins the other side: a sourced model written
through a repository that WAS given a provenance context must not be reported.

Never imported, never executed.
"""

from __future__ import annotations

from typing import Any

from app.repositories import ProvenanceContext, Repository
from app.repositories.models import Person, RollCall
from app.zerodb.client import ZeroDBClient

ADAPTER_VERSION = "example_source/v1"


class CarefulLoader:
    def __init__(self, client: ZeroDBClient) -> None:
        self._roll_calls: Repository[RollCall] = Repository(
            client=client,
            model=RollCall,
            provenance=ProvenanceContext(adapter_version=ADAPTER_VERSION),
        )
        # A model with no source_url field needs no provenance context, and
        # must not be reported for lacking one.
        self._people: Repository[Person] = Repository(client=client, model=Person)

    def load(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            self._roll_calls.upsert(row)
