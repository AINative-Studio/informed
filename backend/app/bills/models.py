"""The response shape for `GET /v1/bills/{bill_id}/text` (story 5.4).

Story 5.4: *"As a voter, I want to open the exact bill text a vote was on, so
that I can verify the evidence myself."* The whole point of this response is
that a voter can check it — so it carries, alongside the URL that opens the
archived document, everything needed to answer "is this really what the bill
said?" without trusting Informed:

  * `sha256` — the hash of the archived bytes, so a reader who downloads the
    document can verify nothing was altered between capture and delivery. This
    is the field that makes the archive an *evidence* artifact rather than
    just a convenient copy.
  * `source_url` — the www.congress.gov document the bytes were captured from.
    A voter can open the primary source directly and compare, which is
    prd.md's "every claim links to a primary source" applied to the bill text
    itself.
  * `version_type` / `version_date` — WHICH of a bill's text versions this is
    ("Referred in Senate", "Enrolled Bill", ...). A bill may have four or more
    published versions, and the archived one is the newest, which is not
    necessarily the one a particular roll call voted on. Stating the version
    plainly is how a voter can tell; see
    `app.ingest.bill_text`'s "Which version gets archived" section for why
    per-roll-call selection is a named gap rather than a silent guess.

`extra="forbid"`, like `app.summary_card.models`: this is a public, unauthen-
ticated read, and nothing voter-shaped should ever be attachable to it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class BillTextResponse(BaseModel):
    """A short-lived URL for one bill's archived text, with its provenance."""

    model_config = ConfigDict(extra="forbid")

    bill_id: str = Field(description='The bill, e.g. "us-house:119:hr-3424".')
    file_id: str = Field(description="The ZeroDB Files id of the archived document.")
    url: str = Field(
        description=(
            "A short-lived presigned URL that serves the archived bytes. "
            "Expires — see `expires_in_seconds` — so a link cannot be shared "
            "past its access check."
        )
    )
    expires_in_seconds: int = Field(
        description="How long `url` stays valid. At most 3600 (ZeroDB's ceiling)."
    )
    sha256: str = Field(
        description=(
            "sha256 of the archived bytes, as measured at capture. Hash what "
            "`url` serves and compare: that is the integrity check that makes "
            "this document citable evidence."
        )
    )
    size_bytes: int | None = Field(
        default=None, description="Size of the archived document, when ZeroDB recorded one."
    )
    content_type: str | None = Field(
        default=None, description='Media type of the archived document, e.g. "application/xml".'
    )
    source_url: str | None = Field(
        default=None,
        description="The www.congress.gov document these bytes were captured from.",
    )
    captured_at: str | None = Field(
        default=None, description="When Informed captured the document (ISO 8601, UTC)."
    )
    version_type: str | None = Field(
        default=None,
        description=(
            'Which published text version this is — "Introduced in House", '
            '"Engrossed in House", "Enrolled Bill", and so on. The archived '
            "version is the newest the source publishes, which is not "
            "necessarily the version a given roll call voted on."
        ),
    )
    version_date: str | None = Field(
        default=None,
        description=(
            "The version's own publication date, when the source gives one. "
            "Null for an enrolled text, which Congress.gov publishes without "
            "a date."
        ),
    )
    format_type: str | None = Field(
        default=None,
        description='The published format archived — "Formatted XML", "PDF", and so on.',
    )


__all__ = ["BillTextResponse"]
