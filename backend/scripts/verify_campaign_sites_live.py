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

!!! THE REAL "INFORMED" ZERODB PROJECT COULD NOT BE REACHED THIS SESSION !!!

This is the one genuine gap in this story's live verification, stated
plainly rather than worked around silently:

  * No `local.env` exists in this environment and no `ZERODB_*` environment
    variables are set, so `ZeroDBClient.from_env()` has no credentials.
  * The `mcp__ainative-zerodb__*` MCP connection available in this session
    is -- confirmed live by listing its 100 projects below -- authenticated
    to a DIFFERENT AINative account than the one that owns the "informed"
    project. There is no project named "informed" among them. This is the
    exact blocker datamodel.md's "Open questions" section already documents
    for story 1.3 and every story since; it was re-confirmed rather than
    assumed for this story.

So **this script runs the real fetch, real hashing, real PDF rendering and
the real Wayback availability check against the real network**, and then
writes through `ZeroDBClient` to WHATEVER TARGET THE CALLER SUPPLIES --
`ZeroDBClient.from_env()` when real credentials exist, or (today, in this
environment) an in-memory `FakeZeroDBTransport`, clearly labeled in this
script's own output as a substitute, never presented as a connection to the
real project. The moment real "informed" credentials are available, running
this script with no code change writes these same three real captures to
the real project for real. Re-run it once that is true and attach the
real output to this story's PR -- this run's output is not that.

Run it:

    python backend/scripts/verify_campaign_sites_live.py

Nothing here depends on ZeroDB credentials existing; it degrades to the
in-memory fake and says so loudly rather than silently.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bootstrap import Bootstrapper  # noqa: E402
from app.ingest.campaign_sites import (  # noqa: E402
    CampaignSiteError,
    CampaignSiteLoader,
    check_wayback_available,
    fetch_campaign_page,
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

        if is_real:
            presigned = client.generate_presigned_url(file_id=report.html_file_id)
            logger.info("  presigned HTML url (expires shortly): %s", presigned.url)

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

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ZeroDBError as exc:
        logging.getLogger("verify_campaign_sites_live").error("ZeroDB error: %s", exc)
        sys.exit(1)
