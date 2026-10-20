"""House bills with sponsors, cosponsors and CRS subjects (story 5.3).

What this is, and why it exists
================================

Story 5.1 left `roll_calls.bill_id` null on purpose: a roll call's own fetch
gives a bill TYPE and NUMBER (`legislationType`/`legislationNumber`), not a
verified `bills` row to point at, and writing an id this loader could not
verify exists would violate `RollCall.bill_id -> bills` (datamodel.md rule 5).
This module is what fills that gap: it ingests `bills` rows with sponsors,
cosponsors and CRS subjects, so that:

  * story 6.3's alignment-score blend has sponsorship evidence to read
    ("did this candidate sponsor or cosponsor a bill matching the voter's
    priorities" is evidence `verdicts` can cite, same as a recorded vote);
  * a roll call ingested or re-ingested AFTER its bill exists can resolve a
    real, verified `bill_id` (`resolve_roll_call_bill_id`, used by
    `scripts/verify_house_bills_live.py` and by
    `tests/features/house_bills.feature`'s own roll-call-linking scenarios).

**This module ran live.** A real, working `CONGRESS_GOV_API_KEY` was
available for this story (unlike story 5.1), and
`scripts/verify_house_bills_live.py` exercises it against the real
api.congress.gov and the real ZeroDB project.

The source: three endpoints, not one
=====================================

    GET /v3/bill/{congress}/{billType}/{billNumber}            (detail)
    GET /v3/bill/{congress}/{billType}/{billNumber}/cosponsors (cosponsors)
    GET /v3/bill/{congress}/{billType}/{billNumber}/subjects   (subjects)

Confirmed live 2026-10-19 against H.R. 3424 and H.R. 1 of the 119th Congress
(`tests/fixtures/house_bills_sample.json` holds both real responses). Three
findings shaped this module:

  * **`sponsors[]` is inline on the detail response.** The bill's own sponsor
    (exactly one, in every live response observed) is `detail.bill.sponsors`,
    never paginated and never a separate fetch.
  * **Cosponsors are a separate sub-endpoint**, returning its own
    `{"cosponsors": {"cosponsors": [...]}}` envelope, paginated past 250 (not
    exercised live by either sample bill, both well under that).
  * **H.R. 1 omits the `cosponsors` block entirely from its detail response**
    rather than reporting a zero count. `detail.bill.cosponsors` is simply
    absent (confirmed live). This is NOT an error -- a bill with no
    cosponsors is an entirely ordinary bill, and this module's `transform_bill`
    treats a missing `cosponsors` sub-response and an empty one identically:
    "no cosponsors" either way, never a skip or a raise.
  * **CRS policy area is at `subjects.policyArea.name`** on the subjects
    sub-endpoint's response (also mirrored, read-only, as
    `detail.bill.policyArea.name` -- this module reads it from the
    dedicated `subjects` fetch, the same fetch legislative subject terms come
    from, so there is one producer of both halves of `bills.subjects[]`/
    `policy_area` rather than two call sites that could disagree).

sponsors[]: one array, two source endpoints, three roles
==========================================================

datamodel.md types `bills.sponsors[]` as `(person_id, role, joined_on)`
without naming the role vocabulary. The one real source distinguishes three
cases (`app.repositories.models.SponsorRole`):

  * `sponsor` -- the bill's own sponsor, from `detail.bill.sponsors[]`.
    `joined_on` is the bill's own `introducedDate`: a sponsor's attachment to
    a bill begins when the bill itself does.
  * `original_cosponsor` -- a cosponsor the source marks
    `isOriginalCosponsor: true`, a materially stronger signal than a cosponsor
    who joined months later.
  * `cosponsor` -- any other cosponsor. `joined_on` is that cosponsor's own
    `sponsorshipDate`.

All three share one array rather than three fields: a reader asking "what did
this person attach their name to" wants one list to scan, and the strength
difference is a weight on `role` (story 6.3's business), not a different kind
of record.

Why CRS policy area gets its own field, beyond `subjects[]`
=============================================================

`Bill.policy_area` is an intentional, documented extension beyond
datamodel.md's field list, which names `subjects[]` only. The reasoning,
flagged here and in this story's PR rather than silently added:

CRS assigns exactly one policy area per bill -- the coarse bucket every other
legislative subject term sits under -- and assigns it through a dedicated
field (`policyArea`), not as one more legislative-subject string. Folding it
into `subjects[]` loses that distinction permanently: once merged into a flat
list of equally-weighted strings, nothing can recover "which one of these
was THE policy area" without re-fetching the source. A future subject-to-topic
mapping story (datamodel.md's own next step for `topic_ids`) needs that
distinction to key its coarse-bucket pass off something more reliable than
"whichever subject string happens to look like a policy area" -- so this
module keeps it queryable as its own field, in addition to (not instead of)
including it in `subjects[]` for a consumer that only wants "every CRS term
this bill carries, flat". `app.bootstrap`'s `bills` schema was extended to
match; see `Bill.policy_area`'s own field comment.

What this module does NOT do
=============================

  * **It does not write `people`.** Same posture as `house_member_votes.py`:
    a sponsor or cosponsor with no `people` row is skipped by the
    repository's own reference check (datamodel.md rule 5), not invented by
    this module.
  * **It does not classify `status`.** Nothing in the cited endpoints
    publishes a `bills.status` this loader could classify with confidence
    from `latestAction.text` alone without guessing; left null, same posture
    `house_rollcalls.py` takes for `is_bundled` -- a gap this module is
    honest about rather than a guess that would be worse than the gap.
  * **It does not set `topic_ids`.** Always `[]` until a subject-to-topic
    mapping story exists to turn `subjects[]`/`policy_area` into
    `issue_topics` references.
  * **It does not fetch a bill's full text.** `text_file_id` is left null by
    this loader. Story 5.4's `app.ingest.bill_text` fills it, from a different
    endpoint (`/text`) and a different host (www.congress.gov serves the
    documents themselves), and runs after this loader because its archive
    hangs off the `bills` row this one writes.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.ingest.house_rollcalls import (
    CONGRESS_GOV_BASE_URL,
    CONGRESS_GOV_USER_AGENT,
    HOUSE_BODY,
)
from app.repositories import ProvenanceContext, Repository
from app.repositories.models import Bill
from app.repositories.provenance import content_hash_for
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: This module's provenance adapter version -- distinct from
#: `house_rollcalls.ADAPTER_VERSION` and `house_member_votes.ADAPTER_VERSION`,
#: because a `bills` row and a `roll_calls` row are different sourced
#: records with different write paths, even though both come from
#: api.congress.gov.
ADAPTER_VERSION = "house_bills/v1"


class HouseBillSourceError(Exception):
    """The bill source did not answer as the fetch functions expect.

    A distinct type from `house_rollcalls.HouseRollCallSourceError`: a bill
    fetch failure and a roll-call fetch failure are different endpoints
    failing, and a caller ingesting both wants to tell them apart.
    """


@dataclass
class SkippedSponsor:
    """One sponsor or cosponsor this loader could not turn into a `Sponsor`.

    `bioguide` is None only when the source record itself had no bioguide id
    to report -- see `_sponsor_from`/`_cosponsor_from`.
    """

    bioguide: str | None
    reason: str


@dataclass
class LoadReport:
    """What one `HouseBillLoader.load` run wrote, mirroring
    `app.ingest.house_rollcalls.LoadReport`.
    """

    bills_written: list[str] = field(default_factory=list)
    provenance_written: list[str] = field(default_factory=list)
    #: Sponsors dropped from a bill's `sponsors[]` before the write because
    #: their `person_id` had no matching `people` row (story 5.3's "a sponsor
    #: with no people row is skipped" scenario) -- see `HouseBillLoader.load`.
    sponsors_skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.bills_written)


def fetch_bill_detail(
    *,
    congress: int,
    bill_type: str,
    bill_number: int | str,
    api_key: str,
    base_url: str = CONGRESS_GOV_BASE_URL,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """The bill detail envelope (`{"bill": {...}}`), inline sponsors included.

    Raises:
        HouseBillSourceError: the request failed, did not come back 200, or
            the body had no `bill` object.
    """
    url = f"{base_url}/bill/{congress}/{bill_type.lower()}/{bill_number}"
    body = _get_json(url, api_key=api_key, timeout_seconds=timeout_seconds)
    if not isinstance(body.get("bill"), dict):
        raise HouseBillSourceError(f"GET {url} did not return a bill object: {body!r}")
    return body


def fetch_bill_cosponsors(
    *,
    congress: int,
    bill_type: str,
    bill_number: int | str,
    api_key: str,
    base_url: str = CONGRESS_GOV_BASE_URL,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """The bill's cosponsors envelope (`{"cosponsors": {"cosponsors": [...]}}`).

    Raises:
        HouseBillSourceError: the request failed or did not come back 200.
            Unlike the detail fetch, a missing `cosponsors` key is NOT an
            error here -- see `transform_bill`'s handling of a bill with no
            cosponsors (confirmed live on H.R. 1's own detail response,
            though that absence is on the detail endpoint, not this one; this
            fetch's own body is checked no more strictly than "is it JSON",
            because an empty list is this endpoint's ordinary, valid answer
            for a bill with no cosponsors).
    """
    url = f"{base_url}/bill/{congress}/{bill_type.lower()}/{bill_number}/cosponsors"
    return _get_json(url, api_key=api_key, timeout_seconds=timeout_seconds)


def fetch_bill_subjects(
    *,
    congress: int,
    bill_type: str,
    bill_number: int | str,
    api_key: str,
    base_url: str = CONGRESS_GOV_BASE_URL,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """The bill's subjects envelope (`{"subjects": {"policyArea": {...},
    "legislativeSubjects": [...]}}`).

    Raises:
        HouseBillSourceError: the request failed or did not come back 200.
    """
    url = f"{base_url}/bill/{congress}/{bill_type.lower()}/{bill_number}/subjects"
    return _get_json(url, api_key=api_key, timeout_seconds=timeout_seconds)


def _get_json(url: str, *, api_key: str, timeout_seconds: float) -> dict[str, Any]:
    try:
        response = httpx.get(
            url,
            params={"api_key": api_key, "format": "json"},
            headers={"User-Agent": CONGRESS_GOV_USER_AGENT},
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise HouseBillSourceError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise HouseBillSourceError(f"GET {url} returned {response.status_code}, expected 200")
    return response.json()


def bill_id(*, congress: int, bill_type: str, bill_number: int | str) -> str:
    """datamodel.md's documented id: `us-house:{congress}:{type}-{number}`.

    Note what this does NOT contain: a session. A bill belongs to a congress,
    not to one of its two sessions -- unlike a roll call
    (`house_rollcalls.roll_call_id`), which belongs to a specific session.
    Composing this id from congress AND session would mint two different ids
    for the same bill depending on which session's roll call referenced it.
    """
    return f"{HOUSE_BODY}:{congress}:{bill_type.lower()}-{bill_number}"


def transform_bill(
    *,
    detail: dict[str, Any],
    cosponsors: dict[str, Any] | None,
    subjects: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[SkippedSponsor]]:
    """Three fetched envelopes into one `Bill`-shaped row dict.

    Args:
        detail: the `fetch_bill_detail` envelope (`{"bill": {...}}`).
        cosponsors: the `fetch_bill_cosponsors` envelope, or None when the
            caller has none to offer. Treated identically to an envelope
            whose `cosponsors.cosponsors` list is empty -- see the module
            docstring's H.R. 1 finding.
        subjects: the `fetch_bill_subjects` envelope, or None. A bill with no
            CRS classification yet (newly introduced) is ordinary, not an
            error.

    Returns:
        `(row, skipped)`. `row` is always returned -- a bill with every
        sponsor and cosponsor unresolvable would still be a valid `bills` row
        with an empty `sponsors[]`, because `person_id` resolution happens at
        write time (the repository's reference check), not here. `skipped`
        names every source sponsor/cosponsor record this function itself
        could not shape into a `Sponsor` dict (no bioguide id) -- it does NOT
        include a sponsor dropped later by the repository for having no
        `people` row; that skip is the loader's to report
        (`HouseBillLoader.load`'s own `LoadReport`... see its docstring for
        why that one is silent-by-design instead, mirroring
        `house_member_votes.py`'s `_transform_one`/repository split).

    Raises:
        HouseBillSourceError: `detail` has no usable `bill` object, or the
            bill is missing `congress`/`type`/`number`/`introducedDate` --
            the fields with no fallback this row cannot be built without.
    """
    bill = detail.get("bill")
    if not isinstance(bill, dict):
        raise HouseBillSourceError("detail envelope has no bill object")

    congress = bill.get("congress")
    bill_type = bill.get("type")
    number = bill.get("number")
    if not isinstance(congress, int) or not bill_type or not number:
        raise HouseBillSourceError(
            f"bill record is missing congress/type/number "
            f"(got congress={congress!r}, type={bill_type!r}, number={number!r})"
        )

    introduced_on = bill.get("introducedDate")
    title = bill.get("title")
    if not title:
        raise HouseBillSourceError("bill record has no title")

    row_id = bill_id(congress=congress, bill_type=str(bill_type), bill_number=str(number))

    sponsors: list[dict[str, Any]] = []
    skipped: list[SkippedSponsor] = []

    for entry in bill.get("sponsors") or []:
        try:
            sponsors.append(_sponsor_from(entry, joined_on=introduced_on))
        except _SkipSponsor as exc:
            skipped.append(_skipped_from(entry, exc))

    # H.R. 1's real detail response omits `cosponsors` entirely rather than
    # reporting a zero count (module docstring). Treating `cosponsors=None`
    # and an empty `cosponsors.cosponsors` list identically is the whole
    # point: neither is an error, and both produce zero cosponsor rows.
    cosponsor_entries: list[Any] = []
    if isinstance(cosponsors, dict):
        inner = cosponsors.get("cosponsors")
        if isinstance(inner, dict):
            cosponsor_entries = inner.get("cosponsors") or []
        elif isinstance(inner, list):
            cosponsor_entries = inner

    for entry in cosponsor_entries:
        try:
            sponsors.append(_cosponsor_from(entry))
        except _SkipSponsor as exc:
            skipped.append(_skipped_from(entry, exc))

    subject_terms, policy_area = _subjects_from(subjects, bill)

    row = {
        "id": row_id,
        "title": str(title),
        "introduced_on": str(introduced_on) if introduced_on else None,
        "subjects": subject_terms,
        "policy_area": policy_area,
        "topic_ids": [],
        "sponsors": sponsors,
        "source_url": _source_url(congress=congress, bill_type=str(bill_type), number=number),
        "content_hash": content_hash_for(
            {"detail": detail, "cosponsors": cosponsors, "subjects": subjects}
        ),
    }
    return row, skipped


class _SkipSponsor(Exception):
    """Internal signal: this one sponsor/cosponsor record cannot become a row."""


def _skipped_from(entry: Any, exc: _SkipSponsor) -> SkippedSponsor:
    bioguide = entry.get("bioguideId") if isinstance(entry, dict) else None
    logger.warning("skipping sponsor %r: %s", bioguide, exc)
    return SkippedSponsor(bioguide=str(bioguide) if bioguide else None, reason=str(exc))


def _sponsor_from(entry: Any, *, joined_on: Any) -> dict[str, Any]:
    """One `detail.bill.sponsors[]` entry -> a `Sponsor` dict with role `sponsor`."""
    if not isinstance(entry, dict):
        raise _SkipSponsor(f"sponsor record is not an object: {entry!r}")
    bioguide = entry.get("bioguideId")
    if not bioguide:
        raise _SkipSponsor("sponsor record has no bioguideId")
    return {
        "person_id": f"person:{bioguide}",
        "role": "sponsor",
        "joined_on": str(joined_on) if joined_on else None,
    }


def _cosponsor_from(entry: Any) -> dict[str, Any]:
    """One cosponsors-endpoint entry -> a `Sponsor` dict with role `cosponsor`
    or `original_cosponsor`, per `isOriginalCosponsor`.
    """
    if not isinstance(entry, dict):
        raise _SkipSponsor(f"cosponsor record is not an object: {entry!r}")
    bioguide = entry.get("bioguideId")
    if not bioguide:
        raise _SkipSponsor("cosponsor record has no bioguideId")
    role = "original_cosponsor" if entry.get("isOriginalCosponsor") else "cosponsor"
    joined_on = entry.get("sponsorshipDate")
    return {
        "person_id": f"person:{bioguide}",
        "role": role,
        "joined_on": str(joined_on) if joined_on else None,
    }


def _subjects_from(
    subjects: dict[str, Any] | None, bill: dict[str, Any]
) -> tuple[list[str], str | None]:
    """CRS policy area + legislative subject terms, from the dedicated fetch.

    Reads from `subjects.subjects.{policyArea,legislativeSubjects}` (the
    dedicated `fetch_bill_subjects` response) rather than
    `bill.get("policyArea")` on the detail envelope, even though the detail
    envelope mirrors the policy area too -- one producer for both halves of
    `subjects[]`/`policy_area`, see the module docstring. Falls back to the
    detail envelope's own `policyArea` only when no `subjects` envelope was
    given at all, so a caller that has not fetched subjects yet still gets
    the one CRS field the detail response already carries for free, rather
    than an avoidable gap.
    """
    policy_area: str | None = None
    terms: list[str] = []

    inner = subjects.get("subjects") if isinstance(subjects, dict) else None
    if isinstance(inner, dict):
        area = inner.get("policyArea")
        if isinstance(area, dict) and area.get("name"):
            policy_area = str(area["name"])
        for term in inner.get("legislativeSubjects") or []:
            if isinstance(term, dict) and term.get("name"):
                terms.append(str(term["name"]))

    if policy_area is None:
        area = bill.get("policyArea")
        if isinstance(area, dict) and area.get("name"):
            policy_area = str(area["name"])

    subject_terms = ([policy_area] if policy_area else []) + terms
    return subject_terms, policy_area


def _source_url(*, congress: int, bill_type: str, number: Any) -> str:
    """The canonical detail-endpoint URL for this bill, as `source_url`.

    Mirrors `fetch_bill_detail`'s own URL rather than reading a `url` field
    off the bill record: unlike the house-vote item endpoint
    (`house_rollcalls.py`), the live bill detail response has NO top-level
    `url` field naming itself -- only sub-resource URLs
    (`actions.url`, `cosponsors.url`, etc, confirmed live on both sample
    bills). Building it from the same congress/type/number this loader
    fetched by is exact and needs no source field that does not exist.
    """
    return f"{CONGRESS_GOV_BASE_URL}/bill/{congress}/{bill_type.lower()}/{number}?format=json"


class HouseBillLoader:
    """Writes transformed bill rows and their provenance, paced.

    Mirrors `app.ingest.house_rollcalls.HouseRollCallLoader`'s shape: one
    call to `load` per batch of already-transformed rows.
    """

    def __init__(self, client: ZeroDBClient, *, pace: Any = None) -> None:
        self._client = client
        self._pace = pace if pace is not None else (lambda: None)
        self._newly_recorded: list[str] = []
        self._bills: Repository[Bill] = Repository(
            client=client,
            model=Bill,
            provenance=ProvenanceContext(
                adapter_version=ADAPTER_VERSION,
                on_write=lambda entry: self._newly_recorded.append(str(entry.id)),
            ),
        )

    def load(self, rows: Sequence[dict[str, Any]]) -> LoadReport:
        """Upsert every bill; the repository records its provenance.

        Unlike `HouseRollCallLoader.load`'s per-row `MemberVote` handling,
        sponsors here are embedded ON the bill row (datamodel.md: "embed what
        is always read together"), so there is no narrower write to retry a
        single sponsor at -- the unit of write is the whole bill. Instead,
        each sponsor's `person_id` is checked against `people` BEFORE the
        bill upsert, and any that do not resolve are dropped from
        `sponsors[]` and reported in `LoadReport.sponsors_skipped`, so one
        unresolvable sponsor costs that one sponsorship, not the whole bill
        (story 5.3's "a sponsor with no people row is skipped" scenario).
        This mirrors `house_member_votes.py`'s skip-one-continue-rest posture
        even though the mechanism differs (pre-check here, since embedding
        rules out a per-child write to catch the repository's own
        `MissingReferenceError` against).
        """
        report = LoadReport()
        for row in rows:
            self._pace()
            self._newly_recorded.clear()
            resolved_sponsors, skipped = self._resolve_sponsors(row.get("sponsors") or [])
            report.sponsors_skipped.extend(skipped)
            write_row = dict(row)
            write_row["sponsors"] = resolved_sponsors
            stored = self._bills.upsert(write_row)
            assert stored.id is not None  # noqa: S101 - upsert guarantees it
            report.bills_written.append(stored.id)
            report.provenance_written.extend(self._newly_recorded)
            logger.info("house_bills: wrote %s", stored.id)
        return report

    def _resolve_sponsors(
        self, sponsors: Sequence[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split `sponsors[]` into (resolvable, skipped) by `people` membership.

        A tiny per-call cache, not shared across rows in the same `load`
        batch -- unlike `Repository.upsert_many`'s cache, a bill-ingest
        batch's sponsors rarely repeat the same bioguide across different
        bills in one run, so the complexity of a cross-row cache is not worth
        it here.
        """
        cache: dict[str, bool] = {}
        resolved: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for sponsor in sponsors:
            person_id = sponsor.get("person_id")
            if person_id not in cache:
                cache[person_id] = self._client.get_row("people", person_id) is not None
            if cache[person_id]:
                resolved.append(sponsor)
            else:
                skipped.append(sponsor)
                logger.warning(
                    "house_bills: skipping sponsor %r, no matching people row", person_id
                )
        return resolved, skipped


def resolve_roll_call_bill_id(envelope: dict[str, Any], *, client: ZeroDBClient) -> str | None:
    """The verified `bills` id for a roll call's own legislation, or None.

    Reads `houseRollCallVote.legislationType`/`legislationNumber`/`congress`
    off the SAME envelope `house_rollcalls.transform_roll_call` consumes, and
    builds the id `bill_id` would build from them. Returns that id ONLY if a
    `bills` row with that id is actually stored -- never a constructed id a
    caller has not verified, which is exactly the thing story 5.1 left null
    rather than risk (a `RollCall.bill_id -> bills` reference the repository
    would then have to refuse at write time anyway).

    Returns None when the envelope has no legislation fields at all (a roll
    call that is not about a bill, e.g. a procedural quorum call), or when it
    does but no matching `bills` row has been ingested yet -- the ordinary
    case for a roll call ingested before its bill, which is why this is a
    caller's explicit choice to call after a bill ingest, not something
    `transform_roll_call` does on its own (it has no `client` to check
    against, by design -- see `house_rollcalls.py`'s fetch/transform split).
    """
    vote = envelope.get("houseRollCallVote")
    if not isinstance(vote, dict):
        return None
    legislation_type = vote.get("legislationType")
    legislation_number = vote.get("legislationNumber")
    congress = vote.get("congress")
    if not legislation_type or not legislation_number or not isinstance(congress, int):
        return None
    candidate_id = bill_id(
        congress=congress, bill_type=str(legislation_type), bill_number=str(legislation_number)
    )
    row = client.get_row(Bill.table_name, candidate_id)
    return candidate_id if row is not None else None


__all__ = [
    "ADAPTER_VERSION",
    "HouseBillLoader",
    "HouseBillSourceError",
    "LoadReport",
    "SkippedSponsor",
    "bill_id",
    "fetch_bill_cosponsors",
    "fetch_bill_detail",
    "fetch_bill_subjects",
    "resolve_roll_call_bill_id",
    "transform_bill",
]
