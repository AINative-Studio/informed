"""The Senate LIS-member-id to bioguide-id crosswalk (story 8.2).

What this is, and why it exists
================================

Story 8.1's `senate_rollcalls.py` ingests the Senate's own roll-call XML from
senate.gov. That feed identifies senators by an `lis_member_id` -- the
Senate's Legislative Information System member id, e.g. `S428` -- and by
nothing else. Confirmed live during this story against the real 8.1 data
(`https://www.senate.gov/legislative/LIS/roll_call_votes/vote1192/vote_119_2_00254.xml`):

    <member>
      <member_full>Alsobrooks (D-MD)</member_full>
      <last_name>Alsobrooks</last_name>
      <first_name>Angela</first_name>
      <party>D</party>
      <state>MD</state>
      <vote_cast>Nay</vote_cast>
      <lis_member_id>S428</lis_member_id>     <-- the ONLY id in the record
    </member>

Everything else in this project identifies a person by `bioguide_id`:
datamodel.md keys `people` as `person:{bioguide}`, `member_votes` as
`{roll_call_id}:{bioguide}`, and `member_votes.person_id` references a
`people` row. ZeroDB has no joins and no foreign keys (CLAUDE.md), so the
crosswalk between the two cannot be a live join -- **it is itself data this
module writes and that downstream code reads**, which is exactly what
backlog.md's "ZeroDB data: people" line for this story means.

Where the crosswalk is stored, and why not a new table
=======================================================

On `people.external_ids`, under the key `lis`.

datamodel.md's `people` row already has `external_ids` as a field and already
names **"By external id"** as one of the table's two main queries. That is
precisely the query this story needs, already documented, already in the
model (`app.repositories.models.Person.external_ids: dict[str, str]`). A
dedicated `lis_crosswalk` table would be a 23rd table datamodel.md does not
list, holding one string per senator that the `people` row is specified to
hold -- and it would then need its own integrity rule to stay in step with
`people` as senators come and go. Writing the mapping where the data model
already says identifiers live keeps one row per senator as the single place
their identity is recorded.

**Confirmed live (2026-10-19) before writing a line of this module**: the
live project's `people` table holds 542 rows, of which exactly **100 already
carry `external_ids.lis`** -- written by story 2.2's
`app.ingest.legislators.transform_legislator`, which stringifies *every* id
under the source's `id:` map, `lis` among them. So this module is not
introducing a new field or a new convention; it is making that mapping a
**deliberate, verified, alertable** deliverable instead of an incidental
by-product, and it reads it back under the same key.

Where the real crosswalk data comes from (four sources checked live)
=====================================================================

This was researched by actually fetching each candidate, not assumed:

  1. **`senate.gov/general/contact_information/senators_cfm.xml`** -- HTTP
     200, 100 `<member>` elements, carries `<bioguide_id>`. **It does NOT
     carry an LIS id**: the complete tag set is `member_full`, `last_name`,
     `first_name`, `party`, `state`, `address`, `phone`, `email`, `website`,
     `class`, `bioguide_id`, `leadership_position`, `last_updated` -- zero
     occurrences of `lis_member_id`. So the Senate's "member_full" feed is
     a bioguide roster, not a crosswalk; on its own it cannot map `S428` to
     anything. (`senate.gov/legislative/LIS/member_full/member_full.xml`,
     the path one might guess by symmetry with the roll-call feed, answers
     302 -- it does not exist.)
  2. **Congress.gov's member API** (`GET /v3/member/{bioguideId}`, with this
     project's real verified key) -- HTTP 200, and the member record carries
     `bioguideId`, `depiction`, `partyHistory`, `previousNames`, `terms`,
     addresses and legislation links. **No LIS id, under any spelling.** So
     Congress.gov cannot supply this crosswalk either.
  3. **The roll-call XML itself** -- carries `lis_member_id`, `last_name`,
     `first_name`, `party`, `state`. Name+state could be *matched* against
     source 1's bioguide roster, but that is fuzzy name matching on two
     fields with known collisions in congressional history, for an
     acceptance criterion that demands 100%. Rejected: a crosswalk built by
     matching strings is not a crosswalk, it is a heuristic.
  4. **`unitedstates/congress-legislators`' `legislators-current.yaml`** --
     the source prd.md already names and story 2.2 already ingests. HTTP
     200, 539 records, and **exactly 100 of them carry `id.lis`, every one
     of which also carries `id.bioguide`** -- the id pair this story needs,
     published as data rather than inferred. Those 100 are exactly the 100
     records whose latest term is a Senate term. **This is the source.**

The honest judgment call: there is **no official U.S. Senate publication of
an LIS-to-bioguide crosswalk** -- sources 1 and 2, the two obvious official
candidates, were both fetched and neither carries both ids. The
congress-legislators project (CC0, maintained by congress.gov/GovTrack
contributors, updated within days of any membership change) is the
authoritative crosswalk that actually exists, and it is already this
codebase's roster source, so this story adds no new dependency. If the
Senate ever publishes its own, `fetch_roster` is the one function to change.

The acceptance criterion, and how it is actually measured
==========================================================

"100% of sitting senators map." Two halves, both of which can be faked and
neither of which is faked here:

  * **The denominator is every sitting senator, including any the source has
    no LIS id for.** `sitting_senators_from_roster` selects on the roster
    record's **latest term type being `sen`** -- never on "has an `id.lis`",
    which would make the criterion circular and trivially 100%. A senator
    the source has no LIS id for is still counted in `sitting_count` and
    named in `CoverageReport.unmapped_person_ids`. `test_a_sitting_senator_
    with_no_lis_id_is_still_a_sitting_senator` is the test that pins this.
  * **An empty sitting Senate is not complete coverage.** 100% of zero is
    the roster failing to load, which is the failure this check exists to
    catch; `CoverageReport.is_complete` is false when `sitting_count == 0`.

"Latest term" rather than "any term" matters in both directions, and both are
tested: a senator with a prior House term (Blackburn, Scott, live) must still
count as sitting; a former senator now in the House must not.

An unmapped id raises an alert
===============================

Three mechanisms, all built on what `app.observability` already established
in story 16.5 -- no new alerting infrastructure invented, consistent with
`app.observability.ingest_lag`'s own stated posture ("there is no deployed
alerting pipeline in this codebase ... what this module builds is the METRIC
a real alerting system would evaluate once one exists"):

  1. **A structured log line at ERROR**, with `extra={"story": "8.2",
     "operation": "resolve_lis_member_id", "lis_member_id": ...}` --
     `app.observability.logging_config`'s documented convention, so a log
     aggregator can filter on `operation` the same way it can for every
     other tagged line in this codebase. ERROR, not WARNING: an unmapped
     senator means that senator's votes are missing from scoring, which is
     a correctness problem for a voter looking at their record, not a
     curiosity. Logged **once per distinct id** (a single unmapped senator
     appears on every roll call -- 500 rows in the live 8.1 data -- and 500
     identical lines is how a real alert gets ignored), while the **counter
     counts every occurrence** so the scrape stays honest.
  2. **`lis_crosswalk_unmapped_senators` / `lis_crosswalk_coverage_complete`
     gauges**, rendered by `render_coverage_prometheus_text` in exactly the
     shape `render_ingest_lag_prometheus_text` renders its own
     `job_ingest_lag_over_threshold` gauge. `coverage_complete == 0` is the
     condition a Prometheus alerting rule fires on.
  3. **`require_bioguide_id`, which raises `UnmappedLisMemberIdError`** --
     for a caller that must not proceed on partial data (the live
     verification script, and any future backfill that would otherwise
     write a half-complete Senate record). A raise is itself an alert, so
     it increments the counter too.

The public interface downstream stories build on
=================================================

Stories 8.3 (vote-type classifier), 8.4 (bundled flag) and 8.5 (no-recorded-
vote handling) all need to resolve a LIS id. The surface they should use, and
which this module will keep stable:

    crosswalk = load_crosswalk(client)              # reads people, once
    crosswalk.resolve_bioguide_id("S428")           # -> "A000382" | None
    crosswalk.resolve_bioguide_id("S428", alert=True)   # ... and alerts
    crosswalk.require_bioguide_id("S428")           # -> str, or raises
    crosswalk.resolve_person_id("S428")             # -> "person:A000382"

`load_crosswalk` reads `people` **once** into memory, rather than a row read
per resolution: 100 senators times 500+ member_votes rows is 50,000 reads
against the ~60 req/min ceiling datamodel.md records, which is a four-hour
ingest. The crosswalk is 100 entries; it belongs in a dict.

What this module does NOT do
=============================

It does not write `member_votes`, and it does not re-resolve 8.1's rows. 8.1
already wrote 500 real `member_votes` rows keyed by bioguide
(`person:Y000064`, confirmed live) by resolving the same YAML in memory, so
there is nothing to repair -- what was missing was the mapping being durable,
queryable and alertable, which is what this module adds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import yaml

from app.ingest.legislators import LEGISLATORS_CURRENT_URL
from app.repositories import Repository
from app.repositories.models import Person
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: The `external_ids` key the crosswalk lives under. Not a new convention:
#: story 2.2's `app.ingest.legislators.transform_legislator` already writes
#: every source id under the source's own key, so 100 of the live project's
#: 542 `people` rows carry `external_ids["lis"]` today (confirmed live
#: 2026-10-19). Reading and writing the same key is what makes this story a
#: deliberate, verified deliverable rather than a second, competing mapping.
LIS_EXTERNAL_ID_KEY = "lis"

#: The crosswalk's source. Deliberately imported from `app.ingest.legislators`
#: rather than re-spelled, so the two can never drift to different URLs --
#: see the module docstring for the four sources checked live and why this is
#: the only one that actually publishes both ids.
LEGISLATORS_YAML_URL = LEGISLATORS_CURRENT_URL

#: The roster `terms[].type` value that means "this term was a Senate term".
#: The source's own vocabulary (`sen` / `rep`), same as
#: `app.ingest.legislators` reads.
SENATE_TERM_TYPE = "sen"

#: Mirrors `app.ingest.senate_rollcalls.DEFAULT_MIN_INTERVAL_SECONDS` and
#: every other ingest adapter's pacing: the ~60 req/min ceiling datamodel.md
#: records for live sources.
DEFAULT_MIN_INTERVAL_SECONDS = 1.1

#: One page large enough to hold the whole `people` table in a single read.
#: The live table is 542 rows; `Repository.query` pages, and the client's
#: filter fallback is documented as unreliable past ~25k rows
#: (datamodel.md "Open questions"), so this module reads pages until a short
#: page rather than assuming one read suffices.
_PEOPLE_PAGE_SIZE = 500

#: Cap on pages read while loading the crosswalk, so a pathological server
#: response cannot spin forever. 20 pages x 500 rows = 10,000 people, ~18x
#: the live table and ~18x the full 539-member Congress.
_MAX_PEOPLE_PAGES = 20


class LisCrosswalkSourceError(Exception):
    """The roster source could not be fetched or did not parse as expected."""


class UnmappedLisMemberIdError(Exception):
    """A LIS member id has no bioguide mapping in the crosswalk.

    Raised by `LisCrosswalk.require_bioguide_id` -- the strict path, for a
    caller that must not proceed on partial data. See the module docstring's
    "An unmapped id raises an alert" section.
    """


@dataclass(frozen=True)
class SittingSenator:
    """One currently-serving senator, as the roster describes them.

    Attributes:
        bioguide_id: the bioguide id, e.g. `A000382`. Always present -- a
            roster record without one is skipped by
            `sitting_senators_from_roster`, because `people` is keyed on it.
        lis_member_id: the Senate LIS member id, e.g. `S428`, or `None` when
            the roster carries none. `None` is deliberately representable:
            such a senator still counts toward the acceptance criterion's
            denominator (see the module docstring).
        canonical_name: for the `people` row's own field, matching story
            2.2's spelling.
    """

    bioguide_id: str
    lis_member_id: str | None
    canonical_name: str

    @property
    def person_id(self) -> str:
        """datamodel.md's documented `people` id: `person:{bioguide}`."""
        return f"person:{self.bioguide_id}"


@dataclass
class RefreshReport:
    """What one `LisCrosswalkWriter.refresh` run wrote.

    Attributes:
        mapped: `person_id` of every senator whose LIS id was written.
        without_lis_id: `person_id` of every sitting senator the roster
            carries no LIS id for. Reported, never written as a blank
            mapping -- an `external_ids["lis"] = ""` row would read back as
            a mapping that resolves nothing, which is strictly worse than
            no key at all.
        missing_people_rows: `person_id` of every sitting senator with no
            `people` row to write onto. Story 2.2's loader is what creates
            those rows; this module updates them and never invents one, so
            a senator missing from `people` is a finding for the roster
            load, not something to paper over here.
    """

    mapped: list[str] = field(default_factory=list)
    without_lis_id: list[str] = field(default_factory=list)
    missing_people_rows: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CoverageReport:
    """The acceptance criterion, measured.

    Attributes:
        sitting_count: how many sitting senators the roster names -- the
            denominator, including any with no LIS id.
        mapped_count: how many of them the crosswalk resolves to.
        unmapped_person_ids: `person_id` of each one it does not, sorted.
        unmapped_lis_ids: LIS ids present in the crosswalk that belong to no
            sitting senator -- a former senator's stale mapping. Reported
            because it is worth seeing, but NOT a coverage failure: the
            criterion is about sitting senators mapping, and a retired
            senator's id still resolving is how a historical roll call from
            the 118th Congress keeps joining to a person.
    """

    sitting_count: int
    mapped_count: int
    unmapped_person_ids: list[str]
    unmapped_lis_ids: list[str]

    @property
    def is_complete(self) -> bool:
        """True only when every sitting senator maps AND there is at least
        one sitting senator. See the module docstring: 100% of zero is the
        roster failing to load, not the criterion being met.
        """
        return self.sitting_count > 0 and self.mapped_count == self.sitting_count

    @property
    def percentage(self) -> float:
        """`mapped_count / sitting_count` as a percentage; 0.0 when there are
        no sitting senators, rather than a ZeroDivisionError.
        """
        if self.sitting_count == 0:
            return 0.0
        return 100.0 * self.mapped_count / self.sitting_count


# --- fetching ----------------------------------------------------------------


def fetch_roster(*, url: str = LEGISLATORS_YAML_URL, timeout_seconds: float = 30.0) -> list[Any]:
    """The raw `legislators-current.yaml` records, parsed.

    The one function in this module that reaches the network, kept separate
    from `sitting_senators_from_roster` for the same reason every other
    ingest adapter here separates fetch from transform: CI must not depend
    on raw.githubusercontent.com being reachable (CLAUDE.md). The test suite
    drives the transform from `tests/fixtures/lis_crosswalk_sample.yaml`.

    Raises:
        LisCrosswalkSourceError: the request failed, did not come back 200,
            or the body did not parse as a YAML list.
    """
    try:
        response = httpx.get(url, timeout=timeout_seconds)
    except httpx.HTTPError as exc:
        raise LisCrosswalkSourceError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise LisCrosswalkSourceError(f"GET {url} returned {response.status_code}, expected 200")
    try:
        records = yaml.safe_load(response.text)
    except yaml.YAMLError as exc:
        raise LisCrosswalkSourceError(f"GET {url} did not parse as YAML: {exc}") from exc
    if not isinstance(records, list):
        raise LisCrosswalkSourceError(f"GET {url} did not return a YAML list")
    return records


# --- transform ---------------------------------------------------------------


def sitting_senators_from_roster(records: Iterable[Any]) -> list[SittingSenator]:
    """Every currently-serving senator the roster names, in roster order.

    Selection is on the record's **latest term's** `type` being `sen`, never
    on the presence of an `id.lis` -- see the module docstring's "The
    acceptance criterion" section for why that distinction is the whole
    difference between measuring coverage and faking it.

    A record that is not a dict, carries no `id.bioguide`, or has no terms at
    all is skipped with a warning rather than crashing the whole roster --
    the same "a gap is a skip, not a crash" posture every loader in this
    codebase takes. A blank or non-string `id.lis` is normalized to `None`
    and a non-string, non-blank one is stringified (every live value is a
    string like `S317`, but the file's other id fields are ints, so a future
    schema change must not silently drop every mapping).
    """
    senators: list[SittingSenator] = []
    for record in records:
        if not isinstance(record, dict):
            logger.warning(
                "lis_crosswalk: skipping a non-dict roster record",
                extra={"story": "8.2", "operation": "parse_roster"},
            )
            continue

        terms = record.get("terms")
        if not isinstance(terms, list) or not terms:
            continue
        latest = terms[-1]
        if not isinstance(latest, dict) or latest.get("type") != SENATE_TERM_TYPE:
            continue

        ids = record.get("id")
        if not isinstance(ids, dict):
            continue
        bioguide = ids.get("bioguide")
        if not isinstance(bioguide, str) or not bioguide.strip():
            logger.warning(
                "lis_crosswalk: skipping a Senate roster record with no bioguide id",
                extra={"story": "8.2", "operation": "parse_roster"},
            )
            continue

        senators.append(
            SittingSenator(
                bioguide_id=bioguide.strip(),
                lis_member_id=_normalized_lis_id(ids.get("lis")),
                canonical_name=_canonical_name(record),
            )
        )
    return senators


def _normalized_lis_id(value: Any) -> str | None:
    """`None` for absent or blank; the stringified value otherwise."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _canonical_name(record: dict[str, Any]) -> str:
    """`name.official_full`, else "first [middle] last" -- the same fallback
    story 2.2's `app.ingest.legislators` uses, so a row this module writes
    carries the same name a roster load would give it.
    """
    name = record.get("name")
    if not isinstance(name, dict):
        return ""
    official = name.get("official_full")
    if isinstance(official, str) and official.strip():
        return official.strip()
    parts = [name.get(key) for key in ("first", "middle", "last")]
    return " ".join(str(p).strip() for p in parts if isinstance(p, str) and p.strip())


# --- the crosswalk itself ----------------------------------------------------


class LisCrosswalk:
    """`{lis_member_id: bioguide_id}`, with alerting on an unmapped id.

    Constructed from a plain dict so a test can build one directly; built
    from live data by `load_crosswalk`. See the module docstring's "The
    public interface downstream stories build on" section -- 8.3, 8.4 and
    8.5 should use exactly these four methods.
    """

    def __init__(self, bioguide_by_lis_id: dict[str, str]) -> None:
        self._by_lis_id = dict(bioguide_by_lis_id)
        self._unmapped_alert_count = 0
        self._unmapped_lis_ids: set[str] = set()

    # --- reads ---------------------------------------------------------------

    def resolve_bioguide_id(self, lis_member_id: str, *, alert: bool = False) -> str | None:
        """The bioguide id for one LIS member id, or `None`.

        `alert=False` (the default) is the quiet read a caller uses when it
        has its own handling for a miss. `alert=True` additionally logs the
        miss and counts it -- the path an ingest loop should take, so one
        unmapped senator is visible on the next metrics scrape instead of
        being silently absent from scoring.
        """
        bioguide = self._by_lis_id.get(lis_member_id)
        if bioguide is None and alert:
            self._alert_unmapped(lis_member_id)
        return bioguide

    def require_bioguide_id(self, lis_member_id: str) -> str:
        """The bioguide id for one LIS member id, or raise.

        The strict path, for a caller that must not proceed on partial data.
        A raise is itself an alert, so this counts and logs the miss too.

        Raises:
            UnmappedLisMemberIdError: `lis_member_id` has no mapping.
        """
        bioguide = self._by_lis_id.get(lis_member_id)
        if bioguide is None:
            self._alert_unmapped(lis_member_id)
            raise UnmappedLisMemberIdError(
                f"no bioguide mapping for LIS member id {lis_member_id!r}; "
                f"the crosswalk holds {len(self._by_lis_id)} mappings "
                f"(story 8.2 -- refresh it from the roster)"
            )
        return bioguide

    def resolve_person_id(self, lis_member_id: str, *, alert: bool = False) -> str | None:
        """`person:{bioguide}` -- the exact `member_votes.person_id` value
        story 8.1's rows carry (confirmed live: `person:Y000064`), so a
        caller never has to re-spell the id pattern.
        """
        bioguide = self.resolve_bioguide_id(lis_member_id, alert=alert)
        return None if bioguide is None else f"person:{bioguide}"

    @property
    def bioguide_ids(self) -> set[str]:
        """Every bioguide id the crosswalk maps to -- what `check_coverage`
        measures the sitting Senate against.
        """
        return set(self._by_lis_id.values())

    @property
    def lis_member_ids(self) -> set[str]:
        """Every LIS id the crosswalk maps from."""
        return set(self._by_lis_id)

    def __len__(self) -> int:
        return len(self._by_lis_id)

    def __contains__(self, lis_member_id: object) -> bool:
        return lis_member_id in self._by_lis_id

    # --- alert bookkeeping ---------------------------------------------------

    @property
    def unmapped_alert_count(self) -> int:
        """How many unmapped-id alerts this crosswalk has raised -- EVERY
        occurrence, not every distinct id. The honest number for a scrape.
        """
        return self._unmapped_alert_count

    @property
    def unmapped_lis_ids(self) -> set[str]:
        """The distinct LIS ids alerted on -- one log line was written per
        entry here, which is what keeps a single unmapped senator across 500
        member_votes rows from writing 500 identical lines.
        """
        return set(self._unmapped_lis_ids)

    def _alert_unmapped(self, lis_member_id: str) -> None:
        self._unmapped_alert_count += 1
        if lis_member_id in self._unmapped_lis_ids:
            return
        self._unmapped_lis_ids.add(lis_member_id)
        logger.error(
            "lis_crosswalk: no bioguide mapping for LIS member id %s -- "
            "this senator's votes will be absent from scoring",
            lis_member_id,
            extra={
                "story": "8.2",
                "operation": "resolve_lis_member_id",
                "lis_member_id": lis_member_id,
                "crosswalk_size": len(self._by_lis_id),
            },
        )


def load_crosswalk(client: ZeroDBClient) -> LisCrosswalk:
    """The crosswalk as it currently stands in ZeroDB.

    Reads `people` and collects every row's `external_ids["lis"]`. Reads the
    whole table once into memory rather than a row per resolution: the live
    table is 542 rows and the crosswalk is 100 entries, while a per-row read
    for each of 500+ `member_votes` rows would be tens of thousands of
    requests against the ~60 req/min ceiling datamodel.md records.

    Pages until a short page (or `_MAX_PEOPLE_PAGES`) rather than assuming
    one read covers the table, because the client's filtered read is
    documented as unreliable past the first page under live ZeroDB's broken
    server-side filter (datamodel.md "Open questions").
    """
    people: Repository[Person] = Repository(client=client, model=Person)
    mapping: dict[str, str] = {}
    for page_index in range(_MAX_PEOPLE_PAGES):
        rows = people.query(limit=_PEOPLE_PAGE_SIZE, skip=page_index * _PEOPLE_PAGE_SIZE)
        for person in rows:
            lis_id = person.external_ids.get(LIS_EXTERNAL_ID_KEY)
            if not lis_id or person.id is None:
                continue
            mapping[lis_id] = person.id.removeprefix("person:")
        if len(rows) < _PEOPLE_PAGE_SIZE:
            break
    return LisCrosswalk(mapping)


# --- coverage ----------------------------------------------------------------


def check_coverage(
    crosswalk: LisCrosswalk, sitting_senators: Sequence[SittingSenator]
) -> CoverageReport:
    """Measure story 8.2's acceptance criterion: 100% of sitting senators map.

    Coverage is measured **by bioguide id**, not by LIS id: if the Senate
    reissued a senator's LIS id, the crosswalk row under the new id still
    covers that senator, and the criterion is about senators mapping. A
    senator the roster carries no LIS id for is still in the denominator --
    see the module docstring.
    """
    mapped_bioguides = crosswalk.bioguide_ids
    unmapped = sorted(
        senator.person_id
        for senator in sitting_senators
        if senator.bioguide_id not in mapped_bioguides
    )
    sitting_lis_ids = {
        senator.lis_member_id for senator in sitting_senators if senator.lis_member_id
    }
    return CoverageReport(
        sitting_count=len(sitting_senators),
        mapped_count=len(sitting_senators) - len(unmapped),
        unmapped_person_ids=unmapped,
        unmapped_lis_ids=sorted(crosswalk.lis_member_ids - sitting_lis_ids),
    )


def render_coverage_prometheus_text(coverage: CoverageReport) -> str:
    """`coverage` as Prometheus gauge lines, for a `/metrics` scrape.

    Four gauges, in exactly the shape
    `app.observability.ingest_lag.render_ingest_lag_prometheus_text` renders
    its own:

      * `lis_crosswalk_sitting_senators` -- the denominator.
      * `lis_crosswalk_mapped_senators` -- the numerator.
      * `lis_crosswalk_unmapped_senators` -- the difference; the number an
        operator reads off a panel.
      * `lis_crosswalk_coverage_complete` -- 1 when the acceptance criterion
        is met, 0 otherwise. This is the value a real alerting rule keys on
        (`ALERT lis_crosswalk_incomplete IF lis_crosswalk_coverage_complete
        == 0`), the same way `job_ingest_lag_over_threshold` is for story
        16.5's own alert.
    """
    gauges = (
        (
            "lis_crosswalk_sitting_senators",
            "Sitting senators the roster names (the 100%-coverage denominator).",
            coverage.sitting_count,
        ),
        (
            "lis_crosswalk_mapped_senators",
            "Sitting senators whose LIS id resolves to a bioguide id.",
            coverage.mapped_count,
        ),
        (
            "lis_crosswalk_unmapped_senators",
            "Sitting senators with no LIS-to-bioguide mapping.",
            len(coverage.unmapped_person_ids),
        ),
        (
            "lis_crosswalk_coverage_complete",
            "1 when every sitting senator maps (story 8.2's criterion), else 0.",
            1 if coverage.is_complete else 0,
        ),
    )
    lines: list[str] = []
    for name, help_text, value in gauges:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"


# --- writing -----------------------------------------------------------------


class LisCrosswalkWriter:
    """Writes each sitting senator's LIS id onto their `people` row, paced.

    Mirrors every other loader in this package: `pace` is injected so tests
    run instantly and the live script paces against the documented ~60
    req/min ceiling.
    """

    def __init__(self, client: ZeroDBClient, *, pace: Callable[[], None] | None = None) -> None:
        self._pace = pace if pace is not None else (lambda: None)
        self._people: Repository[Person] = Repository(client=client, model=Person)

    def refresh(self, sitting_senators: Sequence[SittingSenator]) -> RefreshReport:
        """Upsert `external_ids["lis"]` onto each senator's existing row.

        **Reads the row first and merges.** `Repository.upsert` replaces a
        row wholesale (ZeroDB's `PUT` semantics, documented in
        `app.zerodb.client`), so writing `{"external_ids": {"lis": ...}}`
        alone would destroy every other id story 2.2's loader wrote --
        `govtrack`, `fec`, `thomas`, `opensecrets` and the rest. The merge is
        what `test_refreshing_the_crosswalk_preserves_the_other_external_ids`
        pins.

        A senator with no `people` row is **reported, never invented**:
        story 2.2's roster load is what creates those rows, and conjuring a
        thin one here would hide a roster that is out of date behind a
        crosswalk that looks complete.

        A senator the roster carries no LIS id for is reported and left
        alone -- writing `external_ids["lis"] = ""` would read back as a
        mapping that resolves nothing, strictly worse than no key at all.

        Idempotent: a second run with the same roster writes the same values
        onto the same row ids (datamodel.md rule 1), producing no duplicate.
        """
        report = RefreshReport()
        for senator in sitting_senators:
            if senator.lis_member_id is None:
                report.without_lis_id.append(senator.person_id)
                logger.warning(
                    "lis_crosswalk: sitting senator %s has no LIS id in the roster",
                    senator.person_id,
                    extra={
                        "story": "8.2",
                        "operation": "refresh_lis_crosswalk",
                        "person_id": senator.person_id,
                    },
                )
                continue

            self._pace()
            existing = self._people.get(senator.person_id)
            if existing is None:
                report.missing_people_rows.append(senator.person_id)
                logger.warning(
                    "lis_crosswalk: no people row for sitting senator %s -- "
                    "run the roster load (story 2.2) first",
                    senator.person_id,
                    extra={
                        "story": "8.2",
                        "operation": "refresh_lis_crosswalk",
                        "person_id": senator.person_id,
                    },
                )
                continue

            external_ids = dict(existing.external_ids)
            if external_ids.get(LIS_EXTERNAL_ID_KEY) == senator.lis_member_id:
                # Already correct. Still counted as mapped (the crosswalk IS
                # complete for this senator), but no write: re-PUTting an
                # identical row is a wasted request against the rate ceiling,
                # and this path is the common one on every re-run.
                report.mapped.append(senator.person_id)
                continue
            external_ids[LIS_EXTERNAL_ID_KEY] = senator.lis_member_id

            row = existing.model_dump(mode="json", exclude_none=False)
            row["id"] = senator.person_id
            row["external_ids"] = external_ids
            self._people.upsert(row)
            report.mapped.append(senator.person_id)
            logger.info(
                "lis_crosswalk: mapped %s -> %s",
                senator.lis_member_id,
                senator.person_id,
                extra={
                    "story": "8.2",
                    "operation": "refresh_lis_crosswalk",
                    "person_id": senator.person_id,
                },
            )
        return report


__all__ = [
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "LEGISLATORS_YAML_URL",
    "LIS_EXTERNAL_ID_KEY",
    "SENATE_TERM_TYPE",
    "CoverageReport",
    "LisCrosswalk",
    "LisCrosswalkSourceError",
    "LisCrosswalkWriter",
    "RefreshReport",
    "SittingSenator",
    "UnmappedLisMemberIdError",
    "check_coverage",
    "fetch_roster",
    "load_crosswalk",
    "render_coverage_prometheus_text",
    "sitting_senators_from_roster",
]
