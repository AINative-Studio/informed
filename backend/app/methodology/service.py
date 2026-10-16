"""Reads `methodology_versions` for publication (story 12.2).

Builds on story 4.2's `MethodologyVersionService` rather than re-reading the
table beside it. 4.2 owns two decisions this story must not re-litigate:

  * what the stored row looks like (`MethodologyVersion`), and
  * what happens when a project has no row yet — it is seeded, so that a fresh
    environment cannot disagree with every other one about where its weights
    came from (see that module's docstring).

What this adds is the three reads a public page needs and 4.2 had no reason to
build: one specific version by id *including superseded ones*, and the whole
list for the changelog.

Why "including superseded ones" is the load-bearing part: datamodel.md marks
the live row with `is_current` and never updates history in place, and prd.md
requires every verdict to link the methodology version that produced it. A
verdict published under `2026.1` is only honestly cited if `2026.1`'s real
weights are still readable after `2027.1` ships. 4.2's reader only ever asks
for `is_current = True`, which is right for its job (what are today's weights)
and useless for this one.
"""

from __future__ import annotations

import logging

from app.methodology.models import (
    MethodologyVersionsResponse,
    PublishedMethodology,
    version_path,
)
from app.profiles.methodology import (
    TABLE_NAME,
    MethodologyVersion,
    MethodologyVersionService,
)
from app.zerodb.client import ZeroDBClient
from app.zerodb.errors import ZeroDBNotFoundError

#: How many rows one changelog read asks for. `methodology_versions` grows by
#: roughly one row per methodology publication — a handful per year at most —
#: so this is many years of headroom, and `versions()` warns rather than
#: silently truncating if a page ever comes back full.
PAGE_SIZE = 500

#: Nothing voter-derived is reachable from this module, so this logger cannot
#: leak political-opinion data the way one in `app.profiles` could. It carries
#: row counts and table names only — never a row's contents.
logger = logging.getLogger(__name__)


class PublicMethodologyService:
    """The read side of the public methodology page.

    Holds no per-request state; safe to share across requests like every other
    `*Service` in this codebase.
    """

    def __init__(self, client: ZeroDBClient) -> None:
        self._client = client
        self._versions = MethodologyVersionService(client)

    def current(self) -> PublishedMethodology:
        """The methodology in force, seeding the default if none is stored.

        Delegates the seeding decision to story 4.2 rather than making a
        second one: a fresh environment's methodology page must show the same
        weights its profile editor is using, and the only way to guarantee
        that is for both to come from one reader.
        """
        return _publish(self._versions.current_version())

    def version(self, version: str) -> PublishedMethodology:
        """One methodology version by its id, current or superseded.

        Raises:
            ZeroDBNotFoundError: if no such version was ever published. A 404
                rather than an empty document, because "which methodology
                produced this verdict" has a definite answer and an empty one
                would read as "none" — the opposite of what a citation claims.
        """
        row = self._client.get_row(TABLE_NAME, version)
        if row is None:
            raise ZeroDBNotFoundError(
                f"no methodology version {version!r} has been published",
                status_code=404,
                error_code="NOT_FOUND",
            )
        return _publish(MethodologyVersion.model_validate(row.row_data))

    def versions(self) -> list[PublishedMethodology]:
        """Every published version, newest first.

        A pure read: unlike `current()`, this never seeds. A changelog request
        is a question about history, and "none yet" is the honest answer for a
        project that has published none — not a row written as a side effect
        of someone loading a page.
        """
        page = self._client.query_rows(TABLE_NAME, limit=PAGE_SIZE)
        if page.has_more or len(page.rows) >= PAGE_SIZE:
            # A silently truncated changelog is the worst failure this endpoint
            # has: it would read as a complete history while omitting versions,
            # on a page whose entire purpose is that the history is complete.
            # `methodology_versions` should never approach PAGE_SIZE, so this
            # firing means something other than a methodology publication is
            # writing to the table. Logged rather than raised — serving a
            # partial changelog beats serving none — and `verify_public_
            # methodology_live.py` reports the live row count on every run.
            logger.warning(
                "methodology_versions returned a full page of %d row(s); the published "
                "changelog may be omitting versions",
                len(page.rows),
            )
        published = [_publish(MethodologyVersion.model_validate(row.row_data)) for row in page.rows]
        # Sorted by `published_at`, NOT by the id as a string: datamodel.md's
        # pattern is `{year}.{sequence}`, so the tenth methodology of 2026 is
        # `2026.10`, which a lexicographic sort files between `2026.1` and
        # `2026.2`. The changelog is a chronology. The version id breaks ties
        # so the order is total and the response is stable across calls.
        published.sort(key=lambda entry: (entry.published_at, entry.version), reverse=True)
        return published

    def versions_response(self) -> MethodologyVersionsResponse:
        return MethodologyVersionsResponse(versions=self.versions())


def _publish(stored: MethodologyVersion) -> PublishedMethodology:
    """Map one stored row onto its published document.

    Field-by-field rather than `model_dump()`-and-rename, so that a field
    added to the stored row by a later story does not silently appear on a
    public, uncredentialed endpoint. `PublishedMethodology`'s `extra="forbid"`
    is the second half of that guard.
    """
    return PublishedMethodology(
        version=stored.id,
        published_at=stored.published_at,
        weights=stored.weights,
        changelog_url=stored.changelog_url,
        is_current=stored.is_current,
        path=version_path(stored.id),
    )


__all__ = ["PAGE_SIZE", "PublicMethodologyService"]
