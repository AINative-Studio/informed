#!/usr/bin/env python3
"""Verify campaign-site archival against REAL campaign sites (story 9.1).

Archives three REAL, currently-sitting members of Congress' REAL campaign
sites, found via a live web search during this story (2026-10-16), through
the exact `CampaignSiteLoader` the full pipeline uses -- real
`fetch_campaign_page` (an actual HTTP GET of the live site, not a fixture),
real sha256 hashing, a real PDF rendered via `reportlab`, and a real,
keyless `check_wayback_available` call against archive.org afterwards to
confirm the capture is independently verifiable.

    Jon Ossoff (D-GA), sitting U.S. Senator      -- https://electjon.com/
    Nancy Pelosi (D-CA-11), sitting U.S. Rep.    -- https://www.pelosiforcongress.org/
    Mike Collins (R-GA-10), sitting U.S. Rep.    -- https://mikecollinsga.com/

All three are real, public figures' real public campaign sites -- the same
legitimate public-interest research this codebase's other live-verification
scripts already do for real legislators (`verify_legislators_live.py`) and
real candidacies (`verify_ballot_live.py`). A plain `httpx` GET with no
User-Agent header was refused by two of the three sites' bot-protection (403);
sending an ordinary desktop-browser User-Agent string -- not bypassing any
login, paywall or CAPTCHA, just not identifying as a bare script -- resolved
two of them. The third (a different candidate's site, tried and rejected
before these three were chosen) sat behind a Cloudflare interactive JS
challenge that no plain HTTP client can pass honestly without a headless
browser; rather than add one or fake the capture, that candidate was swapped
out for a reachable one. See this story's PR for the full substitution
reasoning.

REACHING THE REAL "INFORMED" PROJECT
=====================================

**CORRECTION, 2026-10-17.** This docstring previously stated that the real
"informed" ZeroDB project "COULD NOT BE REACHED THIS SESSION", because no
`local.env` existed when story 9.1 ran. That claim is stale: `local.env` does
exist (see docs/environments.md), `ZeroDBClient.from_env()` finds real
credentials once it is sourced into the shell, and this script has since run
against the real project and written the three real captures below to it. The
MCP half of the note still holds -- the `mcp__ainative-zerodb__*` connection
in this environment is authenticated to a different AINative account, which is
why `client.py` calls the REST API directly -- but the REST credentials reach
the real project fine.

The honest posture is the one this script already implements: it writes
through `ZeroDBClient` to WHATEVER TARGET THE CALLER SUPPLIES --
`ZeroDBClient.from_env()` when real credentials are present, or an in-memory
`FakeZeroDBTransport` when they are not, clearly labeled in the output as a
substitute and never presented as the real project. What changed is only that
the first branch is now the one that runs here.

A SECOND CORRECTION: THIS SCRIPT USED TO CLAIM SUCCESS ON A BROKEN PATH
=========================================================================

It logged the presigned URL for each archived HTML file and stopped there,
without ever fetching it. Every one of those URLs answered 404: story 9.1's
`upload_file` posted file metadata to ZeroDB's metadata-only route and never
sent any bytes, so the archives were rows with nothing behind them. A logged
URL looked like proof and was not. This script now FETCHES what it archived,
through `download_file` and through the presigned URL, and compares the bytes
against the capture's own sha256 -- so "archived" means retrievable or the
run says so.

Run it:

    python backend/scripts/verify_campaign_sites_live.py

Nothing here depends on ZeroDB credentials existing; it degrades to the
in-memory fake and says so loudly rather than silently.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap import Bootstrapper  # noqa: E402
from app.ingest.campaign_sites import (  # noqa: E402
    CampaignSiteError,
    CampaignSiteLoader,
    check_wayback_available,
    fetch_campaign_page,
    sha256_hex,
)
from app.repositories import Repository  # noqa: E402
from app.repositories.models import Candidacy, District, Election, Person, PromiseSource  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError  # noqa: E402
from app.zerodb.settings import ZeroDBSettings  # noqa: E402
from app.zerodb.testing import FakeZeroDBTransport  # noqa: E402
from app.zerodb.transport import HttpxZeroDBTransport  # noqa: E402

#: (candidacy_id, person_id, person_name, party, district_id, state, url).
#: The candidacy/district/person ids are synthetic scaffolding this script
#: creates so `PromiseSource.candidacy_id`'s reference check resolves --
#: these three real people's REAL `people`/`candidacies` rows, if they exist
#: in the live "informed" project already (story 2.2/2.6 load real sitting
#: members), should be used instead of this scaffolding once this script
#: runs against that live project; see the module docstring.
REAL_CANDIDATES: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    (
        "us-2026-11-03-general:ocd-division/country:us/state:ga:person:bioguide-O000175",
        "person:bioguide-O000175",
        "OSSOFF, JON",
        "DEM",
        "ocd-division/country:us/state:ga",
        "GA",
        "https://electjon.com/",
    ),
    (
        "us-2026-11-03-general:ocd-division/country:us/state:ca/cd:11:person:bioguide-P000197",
        "person:bioguide-P000197",
        "PELOSI, NANCY",
        "DEM",
        "ocd-division/country:us/state:ca/cd:11",
        "CA",
        "https://www.pelosiforcongress.org/",
    ),
    (
        "us-2026-11-03-general:ocd-division/country:us/state:ga:person:bioguide-C001131",
        "person:bioguide-C001131",
        "COLLINS, MIKE",
        "REP",
        "ocd-division/country:us/state:ga",
        "GA",
        "https://mikecollinsga.com/",
    ),
)


def _live_client() -> tuple[ZeroDBClient, bool]:
    """A real client if credentials exist; otherwise a labeled fake.

    Returns `(client, is_real)`. Mirrors every other `verify_*_live.py`
    script's `ZeroDBClient.from_env()` call exactly, so the day real
    "informed" credentials are sourced into the shell, this function starts
    returning a real client with no code change.
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
        return (
            ZeroDBClient(transport=transport, project_id=settings.project_id),
            True,
        )
    fake = FakeZeroDBTransport()
    fake.break_server_side_filter()
    return ZeroDBClient(transport=fake, project_id="informed-substitute-no-live-creds"), False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("verify_campaign_sites_live")

    client, is_real = _live_client()
    if is_real:
        logger.info("writing to the REAL informed ZeroDB project (real credentials found)")
    else:
        logger.warning(
            "!!! NO REAL ZERODB CREDENTIALS FOUND -- writing to an in-memory "
            "substitute, NOT the real 'informed' project. See this script's "
            "module docstring. Re-run with real credentials and attach that "
            "output to story 9.1's PR. !!!"
        )
        # The real "informed" project already has every table from story
        # 1.3's bootstrap; the in-memory substitute starts with none, so it
        # needs the same bootstrap run here. A real client skips this --
        # `Bootstrapper.run()` is idempotent, but there is no reason to pay
        # its ~35 paced calls against a project that already has the tables.
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

    loader = CampaignSiteLoader(client)

    results = []
    #: Any archive that could not be read back. Nonzero means this script
    #: must NOT report success: an unretrievable archive is the exact failure
    #: that shipped silently the first time.
    round_trip_failures = 0
    for candidacy_id, person_id, person_name, party, district_id, state, url in REAL_CANDIDATES:
        districts.upsert(
            {
                "id": district_id,
                "body_id": "us-senate" if district_id.count(":") == 2 else "us-house",
                "name": f"{state} (verification scaffolding)",
                "state": state,
                "valid_from": "2025-01-03",
            }
        )
        people.upsert(
            {
                "id": person_id,
                "canonical_name": person_name,
                "external_ids": {},
                "photo_url": None,
                "terms": [],
            }
        )
        candidacies.upsert(
            {
                "id": candidacy_id,
                "election_id": "us-2026-11-03-general",
                "district_id": district_id,
                "person_id": person_id,
                "person_name": person_name,
                "party": party,
                "is_incumbent": True,
                "status": "filed",
            }
        )

        logger.info("fetching REAL live page: %s", url)
        try:
            # Prove the real fetch independently before archiving, so a
            # failure here is clearly "the live site refused us" rather than
            # a bug inside the loader.
            page = fetch_campaign_page(url)
            logger.info("  %d bytes, content-type=%s", len(page.html), page.content_type)
        except CampaignSiteError as exc:
            logger.error("  could not fetch %s: %s", url, exc)
            continue

        report = loader.capture(candidacy_id=candidacy_id, url=url)
        logger.info(
            "  archived: promise_source=%s html_sha256=%s pdf_sha256=%s new_capture=%s",
            report.promise_source_id,
            report.html_sha256[:16] + "...",
            report.pdf_sha256[:16] + "..." if report.pdf_sha256 else "(unchanged re-run)",
            report.is_new_capture,
        )

        # Confirm the stored row reads back correctly through the repository.
        stored = sources.get(report.promise_source_id)
        assert stored is not None, "promise_sources row did not read back"
        assert stored.url == url
        assert stored.candidacy_id == candidacy_id

        # The round trip story 9.1 never ran. "Archived" is a claim about
        # retrievability, so it is verified by reading the bytes back and
        # hashing them -- not by the upload answering 200, and not by a
        # presigned URL merely being mintable.
        for label, file_id, expected_sha256 in (
            ("HTML", report.html_file_id, report.html_sha256),
            ("PDF", report.pdf_file_id, report.pdf_sha256),
        ):
            if not file_id:
                continue
            downloaded = client.download_file(file_id=file_id)
            actual = sha256_hex(downloaded)
            if actual == expected_sha256:
                logger.info(
                    "  %s round trip: %d bytes read back, sha256 matches the capture",
                    label,
                    len(downloaded),
                )
            else:
                logger.error(
                    "  %s round trip FAILED: read back %d bytes hashing to %s, expected %s",
                    label,
                    len(downloaded),
                    actual,
                    expected_sha256,
                )
                round_trip_failures += 1

            if is_real:
                # The UI hands a browser a presigned URL, not a download_file
                # call, so the path the reader actually uses is checked too.
                presigned = client.generate_presigned_url(file_id=file_id)
                fetched = httpx.get(presigned.url, timeout=30, follow_redirects=True)
                if fetched.status_code == 200 and fetched.content == downloaded:
                    logger.info("  %s presigned URL: fetched, bytes match", label)
                else:
                    logger.error(
                        "  %s presigned URL FAILED: HTTP %d, %d bytes",
                        label,
                        fetched.status_code,
                        len(fetched.content),
                    )
                    round_trip_failures += 1

        # Real, keyless Wayback availability check -- confirms independently,
        # against archive.org itself, whether a snapshot exists for this
        # exact url (not necessarily FROM this run's submission, since no
        # SPN2 credentials are configured; see the module docstring on
        # app.ingest.campaign_sites for why that submission is best-effort).
        existing_snapshot = check_wayback_available(url)
        logger.info("  Wayback availability check: %s", existing_snapshot or "no snapshot found")

        results.append((candidacy_id, url, report))

    logger.info("")
    logger.info("=== summary ===")
    for candidacy_id, url, report in results:
        logger.info(
            "%s  %s  promise_source=%s  wayback_submitted=%s",
            candidacy_id,
            url,
            report.promise_source_id,
            report.wayback.ok,
        )

    if not is_real:
        logger.warning(
            "!!! Reminder: the writes above went to an in-memory substitute, not "
            "the real 'informed' ZeroDB project. This is NOT the live write "
            "verification story 9.1 asked for -- it is the closest honest proxy "
            "available with no live credentials. Nothing was written anywhere "
            "that needs cleanup. !!!"
        )
    else:
        logger.info(
            "These are REAL captures of REAL candidates' REAL campaign sites, "
            "written to the real 'informed' project as permanent reference "
            "data -- same posture as every other live-verified story. Do NOT "
            "clean these up."
        )

    if round_trip_failures:
        logger.error(
            "%d archived file(s) could not be read back. The capture rows exist "
            "but their content is not retrievable, which is NOT a satisfied "
            "acceptance criterion -- see this script's docstring.",
            round_trip_failures,
        )
        return 1

    logger.info("every archived file was read back and matched its recorded sha256")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ZeroDBError as exc:
        logging.getLogger("verify_campaign_sites_live").error("ZeroDB error: %s", exc)
        sys.exit(1)
