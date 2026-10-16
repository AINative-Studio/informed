"""The upcoming federal elections, as permanent reference data (story 2.6).

Why this is a seed and not an ingest
=====================================

`app.seed` writes the three rows at the root of the reference tree
(`us-federal`, `us-house`, `us-senate`). This module writes the next row up
the same tree: the election every candidacy hangs off.

It is a seed for the same reason those three are. A candidacy id is
`{election_id}:{district_id}:{person_id}` and `election_id` is a declared
reference (`models.Candidacy`), so the election row has to exist before a
single candidacy can be written — and there is no source to ingest it from.
The date of a US federal general election is not published by an API; it is
fixed by statute (2 U.S.C. 7: "the Tuesday next after the first Monday in
November, in every even numbered year"), so it is a constant, stated here and
checkable against the law by eye.

Why `us-2026-11-03-general`, and why it is real
================================================

datamodel.md gives `us-2026-11-03-general` as its own worked example of an
`elections` id, and that date is a genuine election, not a placeholder:
2026-11-03 is the Tuesday after the first Monday of November 2026 (Nov 1 is a
Sunday, Nov 2 the first Monday, Nov 3 the Tuesday), and it is the date of the
2026 US midterm general election — all 435 House seats and 35 of the 100
Senate seats, electing the 120th Congress. Verified against the statutory rule
above and against published 2026 election coverage during this story
(2026-10-16, today being well before it).

So the documented example id and the real next federal general election are
the same row, and this module seeds exactly that one.

Why only ONE election, and why not the primaries
=================================================

Story 2.6's acceptance criterion is "upcoming federal **general**-election
candidacies". The 2026 primaries are not upcoming — today is 2026-10-16 and
every state's federal primary has already been held — so seeding them would be
back-filling history this story has no source for and no story needs. A later
story that wants primary results adds rows here; `UPCOMING_ELECTIONS` is a
tuple precisely so that is an append rather than a redesign.

The 2028 general election is a real, knowable future date
(2028-11-07 by the same statute) and is deliberately NOT seeded: `candidacies`
for it cannot exist for two more years, and a row for an election nobody can
populate is a thing that looks like data and is not. It is seeded by the story
that first needs it.

Why these rows are PERMANENT
=============================

Same posture as `app.seed`, for the same reason: this is production reference
data, not the `zzz-live-check-*` debris stories 1.3-1.5 cleaned up. Deleting
the election row would orphan every candidacy written against it, and the next
seed run would simply write it back.

Idempotency
============

`Repository.upsert` finds the row by its natural-key id and replaces it, or
inserts when there is none, so running this any number of times leaves exactly
one row per id (datamodel.md rule 1). "Replaces", not "skips": this module is
the single source of truth for these rows, so a hand-edited election date is
restored on the next run rather than preserved — exactly the call `app.seed`
documents for the jurisdiction and chambers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from app.repositories import Repository
from app.repositories.models import Election
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: The next US federal general election. See the module docstring for why this
#: date is statutory rather than sourced, and why it is the only row here.
#:
#: `name` is written for a voter to read, because it reaches the ballot view
#: directly — ui-prd.md's races list names the contest, and
#: "2026 United States general election" is what that contest is called.
#: `jurisdiction_id` is `us-federal`, the row `app.seed` writes, which is why
#: this seeder must run after that one.
US_2026_GENERAL = Election(
    id="us-2026-11-03-general",
    jurisdiction_id="us-federal",
    name="2026 United States general election",
    election_date="2026-11-03",
    kind="general",
)

#: Every election this module seeds, in the order it writes them. A tuple, and
#: ordered, so a later story adding the 2028 general (or the 2028 primaries)
#: appends rather than restructures — and so `app.ingest.candidacies` has a
#: documented place to read "the election to load candidacies for" from.
UPCOMING_ELECTIONS: tuple[Election, ...] = (US_2026_GENERAL,)


def seeded_row_ids() -> set[tuple[str, str]]:
    """Every (table, id) this module writes.

    Mirrors `app.seed.seeded_row_ids` and serves the same two purposes: the
    live verification script reads back exactly what the seed claims to have
    written, and a unit test pins the set so a second election cannot be added
    here casually — a row added here is seeded permanently into every
    environment, which is a decision for the story that needs it.
    """
    return {(election.table_name, str(election.id)) for election in UPCOMING_ELECTIONS}


@dataclass
class ElectionSeedReport:
    """What one seed run wrote.

    Holds every row written, whether or not its stored contents changed — this
    seeder restates its rows rather than skipping existing ones (see the module
    docstring), so "written" is the only count it can honestly report, exactly
    as `app.seed.SeedReport` documents for the same reason.
    """

    elections_written: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.elections_written)


class ElectionSeeder:
    """Writes the upcoming-election rows, idempotently.

    `pace` is the same injectable pacer `Bootstrapper`, `Seeder` and every
    ingest loader take, for the same reason: the live project throttles at
    ~60 requests/minute and each upsert costs a find-then-write pair. Tests
    inject a no-op.
    """

    def __init__(self, client: ZeroDBClient, *, pace: Callable[[], None] | None = None) -> None:
        self._elections: Repository[Election] = Repository(client=client, model=Election)
        self._pace = pace if pace is not None else (lambda: None)

    def run(self) -> ElectionSeedReport:
        """Write every upcoming election and report it.

        Raises:
            MissingReferenceError: `us-federal` is not stored. That means
                `app.seed` has not run against this project — the fix is to
                bootstrap it, and failing loudly here is better than writing
                an election belonging to a jurisdiction Informed has never
                heard of.
            ZeroDBError: a live call failed.
        """
        report = ElectionSeedReport()
        for election in UPCOMING_ELECTIONS:
            self._pace()
            self._elections.upsert(election.row_data())
            report.elections_written.append(str(election.id))
            logger.info("seed_elections: wrote election %r", election.id)
        return report


__all__ = [
    "UPCOMING_ELECTIONS",
    "US_2026_GENERAL",
    "ElectionSeedReport",
    "ElectionSeeder",
    "seeded_row_ids",
]
