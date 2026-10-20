#!/usr/bin/env python3
"""Verify bill-text archival (story 5.4) against the LIVE "informed" ZeroDB
project, the real api.congress.gov and the real www.congress.gov documents.

What this proves, in order:

  1. **The text-versions endpoint answers as this module reads it**, for two
     real bills of the 119th Congress -- H.R. 3424 (four versions, all dated)
     and H.R. 1 (six versions, one undated, and its latest-dated version
     listed LAST). The second is the envelope that corrected this module's
     first draft; see `app.ingest.bill_text`'s "Which version gets archived".
  2. **A real bill's text is fetched from www.congress.gov and stored in
     ZeroDB Files with its sha256** -- story 5.4's first acceptance criterion,
     against the real object store, with the sha256 ZeroDB measured over the
     body it received compared against the one computed here.
  3. **The stored bytes download back byte-for-byte** through
     `ZeroDBClient.download_file`, and re-hash to the same sha256. An archive
     that cannot be read back is not an archive (story 9.1's lesson).
  4. **A presigned URL round-trips the real bytes.** The URL is minted through
     the live API and then fetched with a plain HTTP client holding no ZeroDB
     credential at all -- which is exactly what a browser does -- and what
     comes back is hashed and compared. This is story 5.4's second acceptance
     criterion ("the UI opens it via a presigned URL") checked end to end
     rather than asserted from the response shape.
  5. **The API endpoint serves that URL**, through the real FastAPI app wired
     to the live client, with the sha256, the source url and the version it
     names.
  6. **A LARGE document archives correctly.** H.R. 1's enrolled/public-law
     text runs to roughly a megabyte, against H.R. 3424's four kilobytes. The
     size path is checked with a real large file, not assumed.
  7. **Re-archiving is idempotent**: a second run over unchanged text uploads
     nothing and reports `uploaded=False`.
  8. **A dangling `text_file_id` is repaired** rather than trusted -- the
     state story 9.1's archives were left in.

CLEANUP: this run's own data is deleted at the end, and the result is
re-checked and reported. Specifically:

  * the `bills` rows and their `provenance_log` entries this script wrote are
    deleted, like `verify_house_bills_live.py` does for the same reason --
    these bills ARE real public record, but running this verification
    repeatedly must not accumulate duplicate rows against the shared live
    project.
  * **the archived FILES are deleted too, and the deletion is confirmed.**
    This script used to leak every file it uploaded -- including H.R. 1's
    2.8 MB public-law text, on every single run -- because `ZeroDBClient` had
    no `delete_file`. It has one now, so each file this run uploads is deleted
    through it and then re-checked: `download_file` on the deleted id must
    answer `ZeroDBNotFoundError`, which is what proves the object actually
    left the store rather than merely losing the row that pointed at it.

Run it with the live credentials sourced into the shell -- never passed on the
command line, never printed:

    set -a; . /Users/aideveloper/informed/local.env; set +a
    python backend/scripts/verify_bill_text_live.py
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.bills.router import router as bills_router  # noqa: E402
from app.bills.router import zerodb_client_for_bills  # noqa: E402
from app.ingest.bill_text import (  # noqa: E402
    BillTextLoader,
    BillTextSourceError,
    fetch_text_versions,
    select_text_version,
)
from app.ingest.house_bills import (  # noqa: E402
    HouseBillLoader,
    HouseBillSourceError,
    fetch_bill_detail,
    fetch_bill_subjects,
    transform_bill,
)
from app.repositories import ProvenanceContext, Repository  # noqa: E402
from app.repositories.models import Bill  # noqa: E402
from app.repositories.provenance import PROVENANCE_TABLE, provenance_id  # noqa: E402
from app.zerodb import ZeroDBClient  # noqa: E402
from app.zerodb.errors import ZeroDBError, ZeroDBNotFoundError  # noqa: E402
from app.zerodb.settings import ZeroDBSettings  # noqa: E402
from app.zerodb.transport import HttpxZeroDBTransport, ZeroDBResponse  # noqa: E402

CONGRESS = 119
#: The small bill: four text versions, all dated, ~4 KB of XML.
SMALL_BILL = ("hr", 3424)
#: The large bill: six text versions, one UNDATED and listed first, the
#: latest-dated one listed LAST, and roughly a megabyte of text.
LARGE_BILL = ("hr", 1)

logger = logging.getLogger("verify_bill_text_live")


@dataclass
class _RecordingTransport:
    """Wraps a real transport, recording `(method, path)` for every call --
    identical in shape to every other live verify script in this family.
    """

    inner: HttpxZeroDBTransport
    calls: list[tuple[str, str]] = field(default_factory=list)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> ZeroDBResponse:
        self.calls.append((method, path))
        return self.inner.request(method, path, json=json, params=params, files=files, data=data)

    def close(self) -> None:
        self.inner.close()


def live_row_count(client: ZeroDBClient, table: str, row_id: str) -> int:
    """How many rows in `table` carry this natural-key id, read live."""
    page = client.query_rows(table, limit=500)
    return sum(1 for row in page.rows if row.row_data.get("id") == row_id)


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - a linear script of checks
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    api_key = os.environ.get("CONGRESS_GOV_API_KEY")
    if not api_key:
        logger.error(
            "CONGRESS_GOV_API_KEY is not set. source local.env first: "
            "set -a; . /Users/aideveloper/informed/local.env; set +a"
        )
        return 1

    settings = ZeroDBSettings()
    recording = _RecordingTransport(
        inner=HttpxZeroDBTransport(
            base_url=settings.base_url,
            api_key=settings.api_key.get_secret_value(),
            timeout_seconds=settings.timeout_seconds,
        )
    )
    client = ZeroDBClient(
        transport=recording, project_id=settings.project_id, max_attempts=settings.max_attempts
    )
    logger.info("project: %s", client.project_id)

    bills: Repository[Bill] = Repository(
        client=client, model=Bill, provenance=ProvenanceContext(adapter_version="verify/5.4")
    )

    failures: list[str] = []
    written_bill_ids: list[str] = []
    written_provenance_ids: list[str] = []
    uploaded_file_ids: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        if condition:
            logger.info("PASS  %s", label)
        else:
            failures.append(f"{label}{f' — {detail}' if detail else ''}")
            logger.error("FAIL  %s %s", label, detail)

    def seed_bill(bill_type: str, number: int) -> str:
        """Ingest the bill row the archive hangs off, from the real source."""
        detail = fetch_bill_detail(
            congress=CONGRESS, bill_type=bill_type, bill_number=number, api_key=api_key
        )
        subjects = fetch_bill_subjects(
            congress=CONGRESS, bill_type=bill_type, bill_number=number, api_key=api_key
        )
        # `cosponsors=None`: this script verifies text archival, not story
        # 5.3's sponsorship handling (verify_house_bills_live.py covers that),
        # and a bill row with no sponsors is a perfectly valid bills row.
        row, _ = transform_bill(detail=detail, cosponsors=None, subjects=subjects)
        report = HouseBillLoader(client, pace=lambda: None).load([row])
        written_bill_ids.extend(report.bills_written)
        written_provenance_ids.extend(report.provenance_written)
        return report.bills_written[0]

    try:
        # ---- 1. the live text-versions endpoint, both shapes ---------------
        small_versions = fetch_text_versions(
            congress=CONGRESS,
            bill_type=SMALL_BILL[0],
            bill_number=SMALL_BILL[1],
            api_key=api_key,
        )
        large_versions = fetch_text_versions(
            congress=CONGRESS,
            bill_type=LARGE_BILL[0],
            bill_number=LARGE_BILL[1],
            api_key=api_key,
        )
        small_list = small_versions.get("textVersions") or []
        large_list = large_versions.get("textVersions") or []
        logger.info(
            "live: H.R. %d lists %d text version(s); H.R. %d lists %d",
            SMALL_BILL[1],
            len(small_list),
            LARGE_BILL[1],
            len(large_list),
        )
        check(
            "the live text-versions endpoint returns versions for both bills",
            bool(small_list) and bool(large_list),
        )

        large_dates = [v.get("date") for v in large_list]
        check(
            "H.R. 1 still lists an undated version (the live finding this "
            "module's selector was corrected for)",
            None in large_dates,
            f"dates={large_dates}",
        )
        dated = [d for d in large_dates if d]
        check(
            "H.R. 1's latest-dated version is NOT first in the source's own "
            "list order, so textVersions[0] would be wrong",
            bool(dated) and large_dates[0] != max(dated),
            f"first={large_dates[0]!r}, latest={max(dated) if dated else None!r}",
        )
        chosen_large = select_text_version(large_versions)
        check(
            "select_text_version picks H.R. 1's latest-dated version live",
            chosen_large is not None and chosen_large.date == max(dated),
            f"chose {chosen_large.version_type if chosen_large else None!r} "
            f"dated {chosen_large.date if chosen_large else None!r}",
        )
        if chosen_large is not None:
            logger.info(
                "live: H.R. 1's selected version is %r dated %r, formats=%s",
                chosen_large.version_type,
                chosen_large.date,
                sorted(chosen_large.formats),
            )

        # ---- 2/3. archive the small bill's text, live ----------------------
        small_bill_id = seed_bill(*SMALL_BILL)
        logger.info("seeded %s from the live API", small_bill_id)

        loader = BillTextLoader(client, pace=lambda: None, api_key=api_key)
        small_report = loader.archive(bill_id=small_bill_id)
        if small_report.text_file_id:
            uploaded_file_ids.append(small_report.text_file_id)
        logger.info(
            "archived %s: file=%s version=%r format=%r %d bytes sha256=%s",
            small_bill_id,
            small_report.text_file_id,
            small_report.version_type,
            small_report.format_type,
            small_report.size_bytes or 0,
            small_report.sha256,
        )
        check(
            "a real bill's text archives to ZeroDB Files",
            small_report.archived and small_report.uploaded and bool(small_report.text_file_id),
            f"report={small_report!r}",
        )
        check(
            "the archived document's source url is a www.congress.gov document",
            bool(small_report.source_url)
            and str(small_report.source_url).startswith("https://www.congress.gov/"),
            f"source_url={small_report.source_url!r}",
        )

        assert small_report.text_file_id is not None  # noqa: S101 - checked above

        # ZeroDB's own measured sha256 over the uploaded body is compared
        # against this module's inside `BillTextLoader.archive`, which refuses
        # the archive when they disagree -- so an `archived=True` report above
        # is already proof they agreed. It cannot be re-checked from here: the
        # measurement is ONLY on the upload response. Confirmed live (2026-10-19,
        # and re-confirmed by this very check on the first run of this script):
        # `GET /projects/{id}/database/files/{file_id}` answers with
        # `sha256_hash: null` even for a file whose upload reported one. What
        # this script can still verify independently is the next check -- the
        # caller-supplied `file_metadata["sha256"]`, which DOES survive the
        # round trip -- plus the byte-for-byte download below.
        stored = client.get_file(file_id=small_report.text_file_id)
        check(
            "the metadata route still drops ZeroDB's measured sha256_hash "
            "(the live quirk this module's upload-time check works around)",
            stored.sha256_hash is None,
            f"sha256_hash is now {stored.sha256_hash!r}; if ZeroDB started "
            f"returning it, app.zerodb.testing's matching note and "
            f"app.ingest.bill_text's comment can both be simplified",
        )
        check(
            "the file's metadata records the sha256, source_url and captured_at",
            stored.file_metadata.get("sha256") == small_report.sha256
            and stored.file_metadata.get("source_url") == small_report.source_url
            and bool(stored.file_metadata.get("captured_at")),
            f"metadata={stored.file_metadata!r}",
        )

        downloaded = client.download_file(file_id=small_report.text_file_id)
        check(
            "the stored bytes download back and re-hash to the archived sha256",
            hashlib.sha256(downloaded).hexdigest() == small_report.sha256,
            f"downloaded {len(downloaded)} bytes hashing to "
            f"{hashlib.sha256(downloaded).hexdigest()}",
        )

        # Compare against the primary source itself, fetched independently.
        assert small_report.source_url is not None  # noqa: S101 - checked above
        primary = httpx.get(
            small_report.source_url,
            headers={"User-Agent": "Informed/0.1 live-verify"},
            timeout=120.0,
            follow_redirects=True,
        )
        check(
            "the archived bytes are byte-for-byte what www.congress.gov serves",
            primary.status_code == 200 and primary.content == downloaded,
            f"primary {primary.status_code}, {len(primary.content)} bytes vs "
            f"{len(downloaded)} archived",
        )

        # ---- 4. the presigned URL round-trips the real bytes ---------------
        presigned = client.generate_presigned_url(file_id=small_report.text_file_id)
        logger.info("minted presigned url expiring in %s minute(s)", presigned.expiry_minutes)
        # No ZeroDB credential on this client at all -- a browser's position.
        with httpx.Client(timeout=120.0, follow_redirects=True) as anonymous:
            fetched = anonymous.get(presigned.url)
        check(
            "the presigned URL serves the archived bytes to a client holding no ZeroDB credential",
            fetched.status_code == 200
            and hashlib.sha256(fetched.content).hexdigest() == small_report.sha256,
            f"status={fetched.status_code}, {len(fetched.content)} bytes hashing to "
            f"{hashlib.sha256(fetched.content).hexdigest() if fetched.content else None}",
        )

        # ---- 5. the API endpoint, against the live client ------------------
        app = FastAPI()
        app.include_router(bills_router)
        app.dependency_overrides[zerodb_client_for_bills] = lambda: client
        with TestClient(app) as api:
            api_response = api.get(f"/v1/bills/{small_bill_id}/text")
        check(
            "GET /v1/bills/{bill_id}/text answers 200 against the live project",
            api_response.status_code == 200,
            f"{api_response.status_code}: {api_response.text[:300]}",
        )
        if api_response.status_code == 200:
            body = api_response.json()
            logger.info(
                "api: sha256=%s version=%r format=%r expires_in=%ss",
                body["sha256"],
                body.get("version_type"),
                body.get("format_type"),
                body["expires_in_seconds"],
            )
            check(
                "the API response's sha256 is the archived document's",
                body["sha256"] == small_report.sha256,
                f"api={body['sha256']!r} vs {small_report.sha256!r}",
            )
            check(
                "the API response names the primary source and the version",
                body.get("source_url") == small_report.source_url
                and body.get("version_type") == small_report.version_type,
                f"source_url={body.get('source_url')!r}, version={body.get('version_type')!r}",
            )
            check(
                "the API's own presigned URL round-trips the archived bytes",
                hashlib.sha256(httpx.get(body["url"], timeout=120.0).content).hexdigest()
                == small_report.sha256,
            )

        # ---- 6. a genuinely large document --------------------------------
        large_bill_id = seed_bill(*LARGE_BILL)
        logger.info("seeded %s from the live API", large_bill_id)
        large_report = loader.archive(bill_id=large_bill_id)
        if large_report.text_file_id:
            uploaded_file_ids.append(large_report.text_file_id)
        logger.info(
            "archived %s: file=%s version=%r format=%r %d bytes sha256=%s",
            large_bill_id,
            large_report.text_file_id,
            large_report.version_type,
            large_report.format_type,
            large_report.size_bytes or 0,
            large_report.sha256,
        )
        check(
            "a large bill's text archives too",
            large_report.archived and large_report.uploaded,
            f"report={large_report!r}",
        )
        check(
            "the large document is substantially larger than the small one "
            "(the size path is exercised for real, not assumed)",
            (large_report.size_bytes or 0) > 10 * (small_report.size_bytes or 1),
            f"large={large_report.size_bytes}, small={small_report.size_bytes}",
        )
        if large_report.text_file_id:
            large_bytes = client.download_file(file_id=large_report.text_file_id)
            check(
                "the large document downloads back byte-for-byte",
                hashlib.sha256(large_bytes).hexdigest() == large_report.sha256
                and len(large_bytes) == large_report.size_bytes,
                f"{len(large_bytes)} bytes hashing to {hashlib.sha256(large_bytes).hexdigest()}",
            )
            large_presigned = client.generate_presigned_url(file_id=large_report.text_file_id)
            with httpx.Client(timeout=180.0, follow_redirects=True) as anonymous:
                large_fetched = anonymous.get(large_presigned.url)
            check(
                "the large document's presigned URL round-trips every byte",
                large_fetched.status_code == 200
                and hashlib.sha256(large_fetched.content).hexdigest() == large_report.sha256,
                f"status={large_fetched.status_code}, {len(large_fetched.content)} bytes",
            )

        # ---- 7. idempotency ------------------------------------------------
        before = len(recording.calls)
        second = loader.archive(bill_id=small_bill_id)
        upload_calls = [
            (m, p)
            for m, p in recording.calls[before:]
            if m == "POST" and p.endswith("/files/upload")
        ]
        check(
            "re-archiving unchanged text reports uploaded=False",
            second.archived and not second.uploaded,
            f"report={second!r}",
        )
        check(
            "re-archiving unchanged text makes NO upload call at all",
            not upload_calls,
            f"upload calls on the second run: {upload_calls}",
        )
        check(
            "the re-run still points at the same file",
            second.text_file_id == small_report.text_file_id,
            f"{second.text_file_id!r} vs {small_report.text_file_id!r}",
        )

        # ---- 8. a dangling text_file_id is repaired ------------------------
        stored_bill = bills.get(small_bill_id)
        assert stored_bill is not None  # noqa: S101 - just written
        broken_row = stored_bill.row_data()
        broken_row["id"] = small_bill_id
        broken_row["text_file_id"] = "00000000-0000-0000-0000-000000000000"
        bills.upsert(broken_row)
        repaired = loader.archive(bill_id=small_bill_id)
        if repaired.text_file_id and repaired.text_file_id not in uploaded_file_ids:
            uploaded_file_ids.append(repaired.text_file_id)
        check(
            "a text_file_id with no readable file behind it is repaired by "
            "re-uploading, not trusted",
            repaired.archived and repaired.uploaded and bool(repaired.text_file_id),
            f"report={repaired!r}",
        )
        if repaired.text_file_id:
            repaired_bytes = client.download_file(file_id=repaired.text_file_id)
            check(
                "the repaired archive holds the right bytes",
                hashlib.sha256(repaired_bytes).hexdigest() == small_report.sha256,
            )

        # ---- provenance ----------------------------------------------------
        expected_provenance = provenance_id(str(small_report.sha256))
        provenance_row = client.get_row(PROVENANCE_TABLE, expected_provenance)
        check(
            "a provenance_log entry names the archived FILE, with the document url as its source",
            provenance_row is not None
            and provenance_row.row_data.get("entity_table") == "files"
            and provenance_row.row_data.get("source_url") == small_report.source_url,
            f"row={provenance_row.row_data if provenance_row else None!r}",
        )
        if provenance_row is not None:
            written_provenance_ids.append(expected_provenance)
        if large_report.sha256:
            large_provenance = provenance_id(str(large_report.sha256))
            if client.get_row(PROVENANCE_TABLE, large_provenance) is not None:
                written_provenance_ids.append(large_provenance)

    except (HouseBillSourceError, BillTextSourceError) as exc:
        failures.append(f"Congress.gov source error: {exc}")
        logger.error("FAIL  live fetch failed: %s", exc)
    except ZeroDBError as exc:
        failures.append(f"ZeroDB error: {exc}")
        logger.error("FAIL  live call failed: %s", exc)

    finally:
        # ---- cleanup: rows AND files, each re-checked ---------------------
        logger.info("cleaning up this run's own rows and files")
        for bill_row_id in dict.fromkeys(written_bill_ids):
            client.delete_row(Bill.table_name, bill_row_id)
        for prov_id in dict.fromkeys(written_provenance_ids):
            client.delete_row(PROVENANCE_TABLE, prov_id)

        # The files this run uploaded. Before `ZeroDBClient.delete_file`
        # existed this script leaked every one of them -- H.R. 1's 2.8 MB
        # public-law text included -- on each run.
        deleted_file_ids: list[str] = []
        for file_id in dict.fromkeys(uploaded_file_ids):
            try:
                client.delete_file(file_id=file_id)
            except ZeroDBNotFoundError:
                # Already gone: an earlier repair step may have replaced it.
                deleted_file_ids.append(file_id)
            else:
                deleted_file_ids.append(file_id)

        cleanup_failures: list[str] = []
        for bill_row_id in dict.fromkeys(written_bill_ids):
            if live_row_count(client, Bill.table_name, bill_row_id):
                cleanup_failures.append(f"bill {bill_row_id} still present")
        for prov_id in dict.fromkeys(written_provenance_ids):
            if live_row_count(client, PROVENANCE_TABLE, prov_id):
                cleanup_failures.append(f"provenance_log {prov_id} still present")
        # A deleted file must be unreadable. Checking the CONTENT route, not
        # just the metadata row, is the point: story 9.1's whole lesson is that
        # a file row and the bytes behind it can disagree, so only a failed
        # download proves the object itself is gone.
        for file_id in deleted_file_ids:
            try:
                client.download_file(file_id=file_id)
            except ZeroDBNotFoundError:
                pass
            else:
                cleanup_failures.append(f"file {file_id} still downloadable after delete")

        if cleanup_failures:
            failures.extend(cleanup_failures)
            logger.error("FAIL  cleanup incomplete: %s", cleanup_failures)
        else:
            logger.info(
                "PASS  cleanup confirmed: %d bills, %d provenance_log rows and "
                "%d archived file(s) deleted, each re-checked",
                len(set(written_bill_ids)),
                len(set(written_provenance_ids)),
                len(deleted_file_ids),
            )

        recording.close()

    if failures:
        logger.error("%d check(s) FAILED:", len(failures))
        for failure in failures:
            logger.error("  - %s", failure)
        return 1
    logger.info("all live checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
