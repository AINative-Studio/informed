"""Reading one bill's archived text back out, for the UI (story 5.4).

The second half of story 5.4's acceptance criterion — *"the UI opens it via a
presigned URL"*. `app.ingest.bill_text` stores the document and writes
`bills.text_file_id`; this reads that id, mints a short-lived URL for it, and
returns it alongside the provenance a voter needs to verify the bytes.

Why the service does not fall back to the live source
======================================================

If a bill has no archived text, this raises rather than fetching from
www.congress.gov on the request path. Two reasons, both about what this
endpoint is for:

  * A response assembled by proxying a live fetch would carry a `sha256` of
    bytes nobody archived, so the hash would attest to nothing — it could not
    be re-checked later, and the document it described could change out from
    under a citation. The whole value of the archive is that the bytes are
    pinned.
  * The documents run to megabytes (H.R. 1's enrolled HTML is 1,022,332 bytes,
    measured live). Fetching one inside a 60-requests-per-minute public read
    endpoint turns a cheap lookup into an egress amplifier aimed at
    congress.gov on Informed's behalf.

So "not archived yet" is an honest 404 and the fix is an ingest run, not a
request-path workaround.

Why this reads `bills` through `ZeroDBClient.get_row` and not a Repository
===========================================================================

`Repository` is the write path: it validates references, enforces append-only
tables and writes provenance. `bills` declares a `source_url`, so
`app.repositories.provenance`'s static check correctly refuses a
`Repository(model=Bill)` built without a `ProvenanceContext` — and that check
is deliberately exemption-free ("no module can earn an exemption by how it
happens to spell a call", see `check_app_tree_includes`).

Handing this read-only service a `ProvenanceContext` to satisfy the check would
be exactly the wrong answer: it would declare, in code, that this module is an
adapter that sources `bills` rows, when it never writes one. A fake
`adapter_version` on a request-path read is the kind of thing an audit later
has to untangle.

So this reads the row with `get_row` and validates it with the model, which is
what it actually is: a read. No `Repository`, no write path, nothing for the
provenance rule to be satisfied or circumvented about. `Bill.model_validate`
still gives the same typed row and the same field names.
"""

from __future__ import annotations

import logging
from typing import Any

from app.bills.models import BillTextResponse
from app.repositories.models import Bill
from app.zerodb.client import DEFAULT_PRESIGN_EXPIRATION, ZeroDBClient
from app.zerodb.errors import ZeroDBError

logger = logging.getLogger(__name__)


class BillTextUnavailableError(Exception):
    """There is no archived text to hand this caller.

    One exception for three cases that are indistinguishable to a voter and
    all answer 404: the bill is not ingested, the bill has no archived text
    yet, or the named file is gone from Files. The message says which, for the
    log; the HTTP response does not, because distinguishing "no such bill"
    from "that bill has no text yet" is an enumeration signal with no benefit
    to an honest caller — the bill ids are public and derivable either way, so
    this costs nothing to withhold.
    """


class BillTextService:
    """Mints the UI's presigned URL for one bill's archived text."""

    def __init__(
        self, client: ZeroDBClient, *, expiration: int = DEFAULT_PRESIGN_EXPIRATION
    ) -> None:
        self._client = client
        self._expiration = expiration

    def text_for(self, bill_id: str) -> BillTextResponse:
        """A presigned URL plus provenance for `bill_id`'s archived text.

        Raises:
            BillTextUnavailableError: the bill is unknown, has no archived
                text, or its archive is no longer readable.
        """
        row = self._client.get_row(Bill.table_name, bill_id)
        if row is None:
            raise BillTextUnavailableError(f"no bills row {bill_id!r} is stored")
        bill = Bill.model_validate(row.row_data)
        if not bill.text_file_id:
            raise BillTextUnavailableError(
                f"bill {bill_id!r} has no archived text (run scripts/archive_bill_text.py for it)"
            )

        file_id = bill.text_file_id
        try:
            stored = self._client.get_file(file_id=file_id)
        except ZeroDBError as exc:
            # A `text_file_id` naming a file that is not in Files is a broken
            # citation, not a server fault: answer as "no text" and log loudly
            # enough that the integrity sweep (story 16.3) has something to
            # find.
            logger.warning(
                "bills: %s names text file %s, which Files does not have (%s)",
                bill_id,
                file_id,
                exc,
            )
            raise BillTextUnavailableError(
                f"bill {bill_id!r} names a text file that is not stored"
            ) from exc

        metadata: dict[str, Any] = stored.file_metadata or {}
        digest = stored.sha256_hash or metadata.get("sha256")
        if not digest:
            # Without a hash the response cannot be verified, which is the one
            # thing this endpoint exists to make possible. Refusing is better
            # than serving a URL whose bytes nothing attests to.
            raise BillTextUnavailableError(
                f"bill {bill_id!r}'s text file {file_id} has no recorded sha256"
            )

        presigned = self._client.generate_presigned_url(
            file_id=file_id, expiration=self._expiration
        )
        return BillTextResponse(
            bill_id=bill_id,
            file_id=file_id,
            url=presigned.url,
            expires_in_seconds=self._expiration,
            sha256=str(digest),
            size_bytes=stored.size_bytes,
            content_type=stored.content_type,
            source_url=metadata.get("source_url"),
            captured_at=metadata.get("captured_at"),
            version_type=metadata.get("version_type"),
            version_date=metadata.get("version_date"),
            format_type=metadata.get("format_type"),
        )


__all__ = ["BillTextService", "BillTextUnavailableError"]
