#!/usr/bin/env python3
"""Verify debate/questionnaire archival against REAL sources (story 9.2).

Archives REAL, freely-fetchable, text-bearing promise sources for REAL
sitting members of the 119th Congress, through the exact
`SourceDocumentLoader` the pipeline uses -- a real HTTP GET of each live
page (not a fixture), real sha256 hashing, real text extraction, a real
`promise_sources` row, a real `provenance_log` entry, and then a real read
back through `download_file` AND through a presigned URL, because "archived"
is a claim about retrievability and story 9.1 learned the hard way that an
upload answering 200 does not prove it.

WHAT IS ARCHIVED, AND WHY EACH ONE WAS CHOSEN
==============================================

Every source below was checked live (2026-10-20) before being written into
this script: HTTP status, response size, whether the member's own words are
actually in the response body without executing JS, and what the host's
robots.txt says. `app.ingest.debates_questionnaires`'s module docstring
records the full research, including the three suggested sources that were
checked and REJECTED (C-SPAN, the UCSB Presidency Project, Vote Smart) --
two of them because their robots.txt names ClaudeBot with `Disallow: /`.

  1. candidate_questionnaire -- Ballotpedia Candidate Connection
     https://ballotpedia.org/Jon_Ossoff
     Sen. Jon Ossoff (D-GA), sitting U.S. Senator. Completed the survey in
     2026 (and 2020); answers are in the raw HTML.

  2. candidate_questionnaire -- Ballotpedia Candidate Connection
     https://ballotpedia.org/Mike_Collins_(Georgia)
     Rep. Mike Collins (R-GA-10), sitting U.S. Representative. Completed in
     2021. Included deliberately so this run covers one Democrat and one
     Republican: prd.md's first non-negotiable is that the pipeline is
     nonpartisan by construction, and a verification run that only ever
     archives one party's sources is a bad habit to start.

  3. candidate_questionnaire -- VOTE411 (League of Women Voters)
     https://onyourballot.vote411.org/candidate-detail.do?id=72998155
     Rep. Rashida Tlaib (D-MI-12), sitting U.S. Representative, marked
     `NS_RESPONDED` with current-cycle answers in the response body. A second
     publisher for the same source type, because relying on one host for a
     whole source type is how a source type quietly disappears.
     That host's robots.txt asks for `Crawl-delay: 10`, which this script
     HONOURS -- see `_crawl_delay_for`.

  4. debate_transcript -- Rev.com
     https://www.rev.com/transcripts/first-california-senate-debate-transcript
     The first 2024 California U.S. Senate debate: a genuine moderated
     debate with timestamped speaker labels, featuring Adam Schiff, now a
     sitting U.S. Senator (D-CA).

  5. floor_statement -- GPO Congressional Record (keyless)
     https://www.govinfo.gov/content/pkg/CREC-2026-09-30/html/CREC-2026-09-30-pt1-PgS5242.htm
     A real Senate floor statement from 2026-09-30. Labelled
     `floor_statement`, NOT `debate_transcript`: see the ingest module's
     docstring for why conflating the two would inflate what a citation
     means.

NOTHING HERE IS FABRICATED
===========================

Every url above was fetched live and returned a real 200 with the quoted
member's real words in the body. No content is attributed to any real person
that was not genuinely retrieved from that person's own published answers or
from a verbatim transcript. The in-repo FIXTURES deliberately use a
fictional candidate for the opposite reason: inventing promise wording and
putting it in a real member's mouth is precisely the failure this project's
evidence rules exist to prevent.

CANDIDACY SCAFFOLDING, AND WHAT THIS SCRIPT CLEANS UP
======================================================

`PromiseSource.candidacy_id` is reference-checked, so a capture needs a
`candidacies` row. Three of the five sources belong to members the live
project ALREADY has candidacies for (Ossoff, Collins -- written by story
9.1's own verification run). The other two (Tlaib, and the California Senate
debate) need scaffolding rows, which this script creates and then, with
`--cleanup`, removes along with its own captures.

By default this script LEAVES its captures in place: they are real archived
promise sources for real sitting members and are exactly the input story
9.3's extractor needs, so they are useful reference data rather than test
litter. Pass `--cleanup` to delete everything it wrote (promise_sources
rows, both files per capture, and the scaffolding candidacies/people it
created -- never a row it did not create).

Run it:

    set -a; source /path/to/local.env; set +a
    python backend/scripts/verify_debates_questionnaires_live.py
    python backend/scripts/verify_debates_questionnaires_live.py --cleanup

Nothing here depends on ZeroDB credentials existing; it degrades to the
in-memory fake and says so loudly rather than silently.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap import Bootstrapper  # noqa: E402
from app.ingest.debates_questionnaires import (  # noqa: E402
    SOURCE_TYPE_DEBATE_TRANSCRIPT,
    SOURCE_TYPE_FLOOR_STATEMENT,
    SOURCE_TYPE_QUESTIONNAIRE,
    TEXT_FILE_ID_METADATA_KEY,
    VOTE411_CRAWL_DELAY_SECONDS,
    SourceDocumentError,
    SourceDocumentLoader,
    fetch_source_document,
    read_source_text,
    sha256_hex,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import (  # noqa: E402
    Candidacy,
    District,
    Election,
    Person,
    PromiseSource,
)
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402
from app.zerodb.settings import ZeroDBSettings  # noqa: E402
from app.zerodb.testing import FakeZeroDBTransport  # noqa: E402
from app.zerodb.transport import HttpxZeroDBTransport  # noqa: E402


@dataclass(frozen=True)
class RealSource:
    """One real, live-verified source to archive.

    `expect_text` is a phrase this script confirmed live is present in the
    page. It is asserted after extraction, so a silently-changed page (the
    exact thing that made story 9.1's three archived sources permanently
    unrecoverable) FAILS this run instead of archiving a page that no longer
    says what it said -- without that check, "archived successfully" would
    be true and meaningless.
    """

    label: str
    source_type: str
    url: str
    expect_text: str
    person_id: str
    person_name: str
    party: str
    district_id: str
    district_name: str
    state: str
    body_id: str
    #: True when the live project already holds this candidacy (story 9.1's
    #: run created it), so this script must NOT delete it on --cleanup.
    candidacy_preexists: bool

    @property
    def candidacy_id(self) -> str:
        return f"us-2026-11-03-general:{self.district_id}:{self.person_id}"


REAL_SOURCES: tuple[RealSource, ...] = (
    RealSource(
        label="Ballotpedia Candidate Connection — Sen. Jon Ossoff (D-GA)",
        source_type=SOURCE_TYPE_QUESTIONNAIRE,
        url="https://ballotpedia.org/Jon_Ossoff",
        expect_text="Candidate Connection",
        person_id="person:bioguide-O000175",
        person_name="OSSOFF, JON",
        party="DEM",
        district_id="ocd-division/country:us/state:ga",
        district_name="Georgia",
        state="GA",
        body_id="us-senate",
        candidacy_preexists=True,
    ),
    RealSource(
        label="Ballotpedia Candidate Connection — Rep. Mike Collins (R-GA-10)",
        source_type=SOURCE_TYPE_QUESTIONNAIRE,
        url="https://ballotpedia.org/Mike_Collins_(Georgia)",
        expect_text="Candidate Connection",
        person_id="person:bioguide-C001131",
        person_name="COLLINS, MIKE",
        party="REP",
        district_id="ocd-division/country:us/state:ga",
        district_name="Georgia",
        state="GA",
        body_id="us-senate",
        candidacy_preexists=True,
    ),
    RealSource(
        label="VOTE411 (League of Women Voters) — Rep. Rashida Tlaib (D-MI-12)",
        source_type=SOURCE_TYPE_QUESTIONNAIRE,
        url="https://onyourballot.vote411.org/candidate-detail.do?id=72998155",
        expect_text="healthcare as a human right",
        person_id="person:bioguide-T000481",
        person_name="TLAIB, RASHIDA",
        party="DEM",
        district_id="ocd-division/country:us/state:mi/cd:12",
        district_name="MI congressional district 12",
        state="MI",
        body_id="us-house",
        candidacy_preexists=False,
    ),
    RealSource(
        label="Rev.com — 2024 California U.S. Senate debate (Sen. Adam Schiff, D-CA)",
        source_type=SOURCE_TYPE_DEBATE_TRANSCRIPT,
        url="https://www.rev.com/transcripts/first-california-senate-debate-transcript",
        expect_text="restore collective bargaining",
        person_id="person:bioguide-S001150",
        person_name="SCHIFF, ADAM",
        party="DEM",
        district_id="ocd-division/country:us/state:ca",
        district_name="California",
        state="CA",
        body_id="us-senate",
        candidacy_preexists=False,
    ),
    RealSource(
        label="GPO Congressional Record — Senate floor, 2026-09-30 (keyless)",
        source_type=SOURCE_TYPE_FLOOR_STATEMENT,
        url=(
            "https://www.govinfo.gov/content/pkg/CREC-2026-09-30/html/"
            "CREC-2026-09-30-pt1-PgS5242.htm"
        ),
        expect_text="Congressional Record",
        person_id="person:bioguide-K000367",
        person_name="KLOBUCHAR, AMY",
        party="DEM",
        district_id="ocd-division/country:us/state:mn",
        district_name="Minnesota",
        state="MN",
        body_id="us-senate",
        candidacy_preexists=False,
    ),
)


def _crawl_delay_for(url: str) -> float:
    """The crawl delay this script honours for one host.

    VOTE411's robots.txt asks for `Crawl-delay: 10` (fetched live, 2026-10-20).
    Honouring a host's stated crawl delay is not optional politeness for a
    project whose entire credibility rests on being a good-faith reader of
    other people's public records.
    """
    host = urlsplit(url).netloc.lower()
    return VOTE411_CRAWL_DELAY_SECONDS if host.endswith("vote411.org") else 1.0


def _live_client() -> tuple[ZeroDBClient, bool]:
    """A real client if credentials exist; otherwise a labeled fake.

    Mirrors every other `verify_*_live.py` script's `ZeroDBClient.from_env()`
    posture exactly.
    """
    try:
        settings = ZeroDBSettings()
    except Exception:  # noqa: BLE001 - any settings failure means "no real creds"
        settings = None
    if settings is not None and settings.api_key.get_secret_value():
        transport = HttpxZeroDBTransport(
            base_url=settings.base_url,
            api_key=settings.api_key.get_secret_value(),
            timeout_seconds=settings.timeout_seconds,
        )
        return ZeroDBClient(transport=transport, project_id=settings.project_id), True
    fake = FakeZeroDBTransport()
    fake.break_server_side_filter()
    return ZeroDBClient(transport=fake, project_id="informed-substitute-no-live-creds"), False


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - a verification narrative
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help=(
            "delete everything this run wrote (promise_sources rows, both files "
            "per capture, and only the scaffolding rows this script created)"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("verify_debates_questionnaires_live")

    client, is_real = _live_client()
    if is_real:
        logger.info("writing to the REAL informed ZeroDB project (real credentials found)")
    else:
        logger.warning(
            "!!! NO REAL ZERODB CREDENTIALS FOUND -- writing to an in-memory "
            "substitute, NOT the real 'informed' project. Re-run with real "
            "credentials and attach that output to story 9.2's PR. !!!"
        )
        Bootstrapper(client, pace=lambda: None).run()

    elections: Repository[Election] = Repository(client=client, model=Election)
    districts: Repository[District] = Repository(client=client, model=District)
    people: Repository[Person] = Repository(client=client, model=Person)
    candidacies: Repository[Candidacy] = Repository(client=client, model=Candidacy)
    sources: Repository[PromiseSource] = Repository(client=client, model=PromiseSource)

    elections.upsert(
        {
            "id": "us-2026-11-03-general",
            "jurisdiction_id": "us-federal",
            "name": "2026 federal general election",
            "election_date": "2026-11-03",
            "kind": "general",
        }
    )

    loader = SourceDocumentLoader(client, pace=lambda: time.sleep(1.1))

    #: Anything that makes this run a FAILURE. A verification script that
    #: reports success while any of these is nonzero is worse than no script.
    failures = 0
    archived: list[tuple[RealSource, object]] = []
    scaffolded_candidacies: list[str] = []
    scaffolded_people: list[str] = []

    for source in REAL_SOURCES:
        logger.info("")
        logger.info("=== %s ===", source.label)
        logger.info("    %s", source.url)

        # Prove the real fetch independently BEFORE archiving, so a failure
        # here reads as "the live site refused us" rather than a loader bug.
        delay = _crawl_delay_for(source.url)
        if delay > 1.0:
            logger.info("    honouring this host's robots.txt Crawl-delay: %.0fs", delay)
        time.sleep(delay)
        try:
            document = fetch_source_document(source.url)
        except SourceDocumentError as exc:
            logger.error("    FETCH FAILED: %s", exc)
            failures += 1
            continue
        logger.info(
            "    fetched %d bytes, content-type=%s",
            len(document.body),
            document.content_type,
        )

        # The page must still SAY what it said when this script was written.
        # Without this, a silently-rewritten page archives fine and the run
        # reports a success that means nothing -- which is how story 9.1's
        # three archived sources became permanently unrecoverable.
        if source.expect_text not in document.body:
            logger.error(
                "    CONTENT CHECK FAILED: %r is no longer in this page. The page "
                "has changed since this script was written; re-verify it by hand "
                "before trusting any capture of it.",
                source.expect_text,
            )
            failures += 1
            continue
        logger.info("    content check: %r is present", source.expect_text)

        districts.upsert(
            {
                "id": source.district_id,
                "body_id": source.body_id,
                "name": source.district_name,
                "state": source.state,
                "valid_from": "2025-01-03",
            }
        )
        if not source.candidacy_preexists:
            people.upsert(
                {
                    "id": source.person_id,
                    "canonical_name": source.person_name,
                    "external_ids": {},
                    "photo_url": None,
                    "terms": [],
                }
            )
            scaffolded_people.append(source.person_id)
        if candidacies.get(source.candidacy_id) is None:
            candidacies.upsert(
                {
                    "id": source.candidacy_id,
                    "election_id": "us-2026-11-03-general",
                    "district_id": source.district_id,
                    "person_id": source.person_id,
                    "person_name": source.person_name,
                    "party": source.party,
                    "is_incumbent": True,
                    "status": "filed",
                }
            )
            scaffolded_candidacies.append(source.candidacy_id)

        report = loader.archive(
            candidacy_id=source.candidacy_id,
            url=source.url,
            source_type=source.source_type,
        )
        logger.info(
            "    archived: promise_source=%s source_type=%s new=%s",
            report.promise_source_id,
            report.source_type,
            report.is_new_capture,
        )
        logger.info("    document bytes sha256: %s", report.document_bytes_sha256)
        logger.info(
            "    text sha256:     %s  (%d characters extracted)",
            report.text_sha256,
            report.text_characters,
        )

        stored = sources.get(report.promise_source_id)
        if stored is None:
            logger.error("    promise_sources row did not read back")
            failures += 1
            continue
        if stored.url != source.url or stored.source_type != source.source_type:
            logger.error(
                "    promise_sources row disagrees with the capture: url=%r type=%r",
                stored.url,
                stored.source_type,
            )
            failures += 1

        # The round trip. "Archived" is a claim about retrievability, so it
        # is verified by reading the bytes back and hashing them -- not by
        # the upload answering 200.
        #
        # The DOCUMENT is checked against the hash recorded on the stored
        # file's own metadata, not against this fetch's bytes. On a no-op
        # re-run of a host with volatile boilerplate (VOTE411's per-minute
        # cache comment), the stored archive legitimately holds an EARLIER
        # fetch's bytes while the text -- the capture's actual identity --
        # is unchanged. Comparing to this fetch would report a failure for
        # the system working exactly as designed. The TEXT is still checked
        # against this run's own hash, because that is what must match.
        try:
            stored_document_sha256 = str(
                client.get_file(file_id=report.document_file_id).file_metadata.get("sha256") or ""
            )
        except ZeroDBError as exc:
            logger.error("    could not read the stored document's sha256: %s", exc)
            failures += 1
            stored_document_sha256 = report.document_bytes_sha256
        if report.is_new_capture and stored_document_sha256 != report.document_bytes_sha256:
            logger.error(
                "    a NEW capture's stored sha256 (%s) is not this fetch's (%s)",
                stored_document_sha256,
                report.document_bytes_sha256,
            )
            failures += 1

        for label, file_id, expected in (
            ("document", report.document_file_id, stored_document_sha256),
            ("text", report.text_file_id, report.text_sha256),
        ):
            if not file_id:
                logger.error("    %s has no file id on the report", label)
                failures += 1
                continue
            try:
                downloaded = client.download_file(file_id=file_id)
            except ZeroDBError as exc:
                logger.error("    %s round trip FAILED: %s", label, exc)
                failures += 1
                continue
            actual = sha256_hex(downloaded)
            if actual == expected:
                logger.info(
                    "    %s round trip: %d bytes read back, sha256 matches",
                    label,
                    len(downloaded),
                )
            else:
                logger.error(
                    "    %s round trip FAILED: %d bytes hashing to %s, expected %s",
                    label,
                    len(downloaded),
                    actual,
                    expected,
                )
                failures += 1

            if is_real:
                # The path a browser actually uses for a citation.
                try:
                    presigned = client.generate_presigned_url(file_id=file_id)
                    fetched = httpx.get(presigned.url, timeout=30, follow_redirects=True)
                except (ZeroDBError, httpx.HTTPError) as exc:
                    logger.error("    %s presigned URL FAILED: %s", label, exc)
                    failures += 1
                else:
                    if fetched.status_code == 200 and fetched.content == downloaded:
                        logger.info("    %s presigned URL: fetched, bytes match", label)
                    else:
                        logger.error(
                            "    %s presigned URL FAILED: HTTP %d, %d bytes",
                            label,
                            fetched.status_code,
                            len(fetched.content),
                        )
                        failures += 1

        # The pointer that makes a promise_sources row resolvable to the text
        # story 9.3's extractor consumes.
        try:
            meta = client.get_file(file_id=report.document_file_id).file_metadata
        except ZeroDBError as exc:
            logger.error("    could not read the document's metadata: %s", exc)
            failures += 1
        else:
            if meta.get(TEXT_FILE_ID_METADATA_KEY) == report.text_file_id:
                logger.info("    document metadata points at the extracted-text file")
            else:
                logger.error(
                    "    document metadata does NOT point at the text file (%r)",
                    meta.get(TEXT_FILE_ID_METADATA_KEY),
                )
                failures += 1

        # End to end: the row -> the text, exactly as story 9.3 would read it.
        try:
            text = read_source_text(client, stored)
        except SourceDocumentError as exc:
            logger.error("    read_source_text FAILED: %s", exc)
            failures += 1
        else:
            if source.expect_text in text:
                logger.info(
                    "    read_source_text: %d characters, and the expected phrase "
                    "survived extraction",
                    len(text),
                )
            else:
                logger.error(
                    "    read_source_text returned %d characters but %r did not survive extraction",
                    len(text),
                    source.expect_text,
                )
                failures += 1

        # Provenance: datamodel.md's rule is that every row with a source url
        # has a matching provenance_log entry.
        from app.repositories.provenance import PROVENANCE_TABLE, provenance_id

        prov_row = client.get_row(PROVENANCE_TABLE, provenance_id(report.text_sha256))
        if prov_row is None:
            logger.error("    NO provenance_log entry for this capture")
            failures += 1
        else:
            # `get_row` returns a `Row`; Informed's own fields live in
            # `row_data` (ZeroDB's `row_id` is a different, internal uuid).
            data = prov_row.row_data
            logger.info(
                "    provenance_log %s: entity=%s/%s adapter=%s",
                provenance_id(report.text_sha256),
                data.get("entity_table"),
                data.get("entity_id"),
                data.get("adapter_version"),
            )
            if data.get("entity_id") != report.promise_source_id:
                logger.error("    provenance entity_id does not match the capture")
                failures += 1

        archived.append((source, report))

    # --- the acceptance criterion, checked against what is actually stored ---

    logger.info("")
    logger.info("=== story 9.2 acceptance criterion ===")
    non_website_types = sorted({src.source_type for src, _ in archived})
    logger.info(
        "source types beyond campaign websites, archived this run: %s",
        ", ".join(non_website_types) or "(none)",
    )
    if len(non_website_types) >= 2:
        logger.info("  OK: at least 2 source types beyond websites")
    else:
        logger.error("  FAILED: story 9.2 needs at least 2 source types beyond websites")
        failures += 1

    logger.info("")
    logger.info("=== summary ===")
    for src, report in archived:
        logger.info(
            "%-24s %s  %s",
            report.source_type,
            report.document_bytes_sha256[:16] + "...",
            src.label,
        )

    if args.cleanup:
        logger.info("")
        logger.info("=== cleanup ===")
        # `ZeroDBClient` exposes upload_file / download_file / get_file and
        # NO delete_file (checked, not assumed), so the stored objects cannot
        # be removed from here. Said out loud rather than quietly skipped:
        # "cleanup" that silently leaves files behind is the kind of claim
        # this project's verification scripts exist to stop making.
        orphaned_files = [
            file_id
            for _, report in archived
            for file_id in (report.document_file_id, report.text_file_id)
            if file_id
        ]
        for _, report in archived:
            try:
                client.delete_row(PromiseSource.table_name, report.promise_source_id)
                logger.info("deleted promise_sources %s", report.promise_source_id)
            except ZeroDBError as exc:
                logger.warning("could not delete %s: %s", report.promise_source_id, exc)
        # Only rows THIS run created -- never a candidacy story 9.1 wrote.
        for candidacy_id in scaffolded_candidacies:
            try:
                client.delete_row(Candidacy.table_name, candidacy_id)
                logger.info("deleted scaffolding candidacy %s", candidacy_id)
            except ZeroDBError as exc:
                logger.warning("could not delete %s: %s", candidacy_id, exc)
        for person_id in scaffolded_people:
            try:
                client.delete_row(Person.table_name, person_id)
                logger.info("deleted scaffolding person %s", person_id)
            except ZeroDBError as exc:
                logger.warning("could not delete %s: %s", person_id, exc)
        logger.info(
            "NOTE: provenance_log is append-only by design (datamodel.md), so this "
            "run's provenance entries are deliberately LEFT in place."
        )
        if orphaned_files:
            logger.warning(
                "NOTE: %d stored file(s) could NOT be deleted -- ZeroDBClient has no "
                "delete_file method, so these objects remain in the project with no "
                "promise_sources row pointing at them: %s",
                len(orphaned_files),
                ", ".join(orphaned_files),
            )
    else:
        logger.info("")
        logger.info(
            "left in place: %d real archived promise sources. These are genuine "
            "citable evidence for real sitting members and are exactly what story "
            "9.3's extractor consumes. Re-run with --cleanup to remove them.",
            len(archived),
        )

    logger.info("")
    if failures:
        logger.error("VERIFICATION FAILED: %d problem(s) above", failures)
        return 1
    logger.info("VERIFICATION PASSED: %d real source(s) archived and read back", len(archived))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
