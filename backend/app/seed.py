"""Fixed reference data every environment needs (story 2.1).

What this is, and why it is not in `bootstrap.py`
=================================================

`bootstrap.py` creates empty *structure* — tables, vector indexes, the graph
ontology. This module writes *rows*: the three that story 2.1 names, which are
not data any ingest run produces and not test fixtures either. They are the
root of the reference tree, and every later story hangs off them:

  * story 2.2's legislator loader writes `people.terms[].body_id = "us-house"`;
  * story 2.3's district resolver writes `districts.body_id`;
  * story 2.4's ballot lookup reads races per chamber;
  * epic 17's non-US jurisdictions add a sibling of `us-federal` and do not
    touch this one.

Two concerns, two modules, one command. `Bootstrapper.run()` calls `Seeder`
immediately after the tables exist, because story 2.1's acceptance criterion
is literally "rows `us-federal`, `us-house` and `us-senate` exist **after
bootstrap**" — an operator who bootstraps an environment must not then have to
know about a second step. Keeping the row-writing here rather than inlining it
into `Bootstrapper` keeps "create the schema" and "seed the reference tree"
separately testable and separately runnable, and means a later story that adds
seed rows never edits the schema module.

Why these rows are PERMANENT
============================

Every live verification script before this one (stories 1.3, 1.4, 1.5) deleted
every row it wrote. **This one deliberately does not.** Those scripts wrote
synthetic rows carrying a `zzz-live-check-*` marker, which exist only to prove
a code path and are debris afterwards. These three rows are production
reference data: deleting them would orphan every row a later story writes
against them, and the next bootstrap would simply write them back. A
`us-federal` row found in the live project is not forgotten test debris — it is
this module's output, and it is supposed to be there.

Idempotency
===========

`Repository.upsert` is the whole mechanism: it finds the row by its
natural-key id and replaces it, or inserts when there is none, so running the
seed any number of times leaves exactly one row per id (datamodel.md rule 1).
Note "replaces", not "skips": the seed is the single source of truth for these
three rows, so a hand-edit to a seeded row is restored on the next run rather
than preserved. That is deliberate — a row silently diverging from the
constants below would make this module a lie — and it is why the report counts
rows *written* rather than rows *created*.

Nothing here is atomic (see `repositories/base.py`): two bootstraps racing
could both find nothing and both insert. Acceptable for a seed that runs
manually per environment, and the next run collapses the duplicate it would
otherwise find.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from app.repositories import Repository
from app.repositories.models import Body, Jurisdiction
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The seed data. Three rows, fixed, matching datamodel.md's id patterns and
# field lists exactly.
# ---------------------------------------------------------------------------

#: The United States federal jurisdiction. datamodel.md gives `us-federal` as
#: its own first example of a `jurisdictions` id.
#:
#: `adapter_name` is `"us-federal"` — the same slug as the id. The alternative
#: considered was a class path (`app.ingest.adapters.USFederalAdapter`), and it
#: was rejected because the `JurisdictionAdapter` interface does not exist
#: until story 17.1: a stored import path pointing at nothing would look like a
#: wiring bug rather than an unbuilt feature, and would have to be rewritten
#: when 17.1 chooses its own module layout. A slug is a stable key into
#: whatever registry 17.1 builds, and reads as an intent rather than a broken
#: reference.
US_FEDERAL = Jurisdiction(
    id="us-federal",
    name="United States",
    country_code="US",
    level="federal",
    adapter_name="us-federal",
    active=True,
)

#: Both chambers of the 119th Congress, in `jurisdiction_id` order after the
#: jurisdiction itself. Seat counts are the real ones — 435 House seats
#: (statutory, 2 U.S.C. 2a) and 100 Senate seats (two per state) — not sample
#: values; story 2.2's loader checks the delegation it loads against them.
#:
#: `name` is each chamber's own formal name, while `chamber` is the normalised
#: `lower`/`upper` that cross-jurisdiction queries filter on. See
#: `models.Chamber` for why both exist.
FEDERAL_BODIES: tuple[Body, ...] = (
    Body(
        id="us-house",
        jurisdiction_id="us-federal",
        name="U.S. House of Representatives",
        chamber="lower",
        seats=435,
    ),
    Body(
        id="us-senate",
        jurisdiction_id="us-federal",
        name="U.S. Senate",
        chamber="upper",
        seats=100,
    ),
)


def seeded_row_ids() -> set[tuple[str, str]]:
    """Every (table, id) this module writes.

    Used by the live verification script to read back exactly what the seed
    claims to have written, and by a unit test that pins the set so a fourth
    row cannot be added here casually — a row added to this module is seeded
    permanently into every environment, which is a decision for the story that
    needs it rather than a drive-by edit.
    """
    ids = {(US_FEDERAL.table_name, str(US_FEDERAL.id))}
    ids.update((body.table_name, str(body.id)) for body in FEDERAL_BODIES)
    return ids


@dataclass
class SeedReport:
    """What one seed run wrote, for the bootstrap CLI's summary.

    Both lists hold every row the run wrote, whether or not its stored
    contents changed — the seed restates these rows rather than skipping
    existing ones (see the module docstring), so "written" is the only count
    it can honestly report. A run against an already-seeded project therefore
    reports the same three rows as a cold one, and the idempotency claim is
    about the stored row *count*, which the live verification and the feature
    file both check directly.
    """

    jurisdictions_written: list[str] = field(default_factory=list)
    bodies_written: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.jurisdictions_written) + len(self.bodies_written)


class Seeder:
    """Writes the fixed reference rows, idempotently, in dependency order.

    The jurisdiction goes first and both chambers after it, because
    `Body.jurisdiction_id` is a declared reference and the repository resolves
    it before writing (datamodel.md rule 5) — seeding a chamber first would be
    refused against a project that had never been seeded. The ordering is
    asserted on the recorded calls in
    `tests/features/seed_reference_data.feature`, not just implied by this
    docstring, because the final state looks identical either way.

    `pace` is the same injectable pacer `Bootstrapper` uses, for the same
    reason: the live project throttles at 60 requests/minute, and the seed's
    writes land immediately after a cold bootstrap's ~32 calls rather than in
    a quiet minute of their own. Tests inject a no-op.
    """

    def __init__(self, client: ZeroDBClient, *, pace: Callable[[], None] | None = None) -> None:
        self._client = client
        self._pace = pace if pace is not None else (lambda: None)
        self._jurisdictions: Repository[Jurisdiction] = Repository(
            client=client, model=Jurisdiction
        )
        self._bodies: Repository[Body] = Repository(client=client, model=Body)

    def run(self) -> SeedReport:
        """Write all three rows and report them.

        Raises:
            MissingReferenceError: a chamber's jurisdiction is not stored. In
                practice unreachable from here — this method writes the
                jurisdiction first — so it would mean the write succeeded and
                the read back could not find it, which is the ZeroDB filter
                behaviour story 1.4 pinned and worth failing loudly on rather
                than seeding half a tree.
            ZeroDBError: a live call failed.
        """
        report = SeedReport()

        self._pace()
        self._jurisdictions.upsert(US_FEDERAL.row_data())
        report.jurisdictions_written.append(str(US_FEDERAL.id))
        logger.info("seed: wrote jurisdiction %r", US_FEDERAL.id)

        for body in FEDERAL_BODIES:
            self._pace()
            self._bodies.upsert(body.row_data())
            report.bodies_written.append(str(body.id))
            logger.info("seed: wrote body %r", body.id)

        return report


__all__ = [
    "FEDERAL_BODIES",
    "US_FEDERAL",
    "SeedReport",
    "Seeder",
    "seeded_row_ids",
]
