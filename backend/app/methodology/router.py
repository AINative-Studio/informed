"""HTTP surface for the public methodology page (story 12.2).

    GET /v1/methodology            — the methodology in force
    GET /v1/methodology/versions   — the changelog: every version, newest first
    GET /v1/methodology/{version}  — one version, current or superseded

prd.md, "Trust, neutrality, privacy and safety": "Methodology page and
changelog; every verdict links to the methodology version that produced it."

## What this story actually delivers, and what it does not

The acceptance criterion in backlog.md is "every verdict and score links to
its methodology version". Neither exists in this codebase yet: verdicts are
epic 11 and computed alignment scores are epic 6. So the criterion cannot be
demonstrated end to end today, and nothing here pretends otherwise — there are
no synthetic verdicts in this story's tests manufactured to make a link look
proven.

What this story does deliver is the other half of that sentence, which is a
prerequisite rather than a consolation: **the link target exists and is
citable.** Concretely, when epic 6 or 11 lands,

  * a verdict can store the version id it was computed under (`2026.1`) —
    already a stable, deterministic natural key, nothing new needed;
  * it can publish the URL to show a reader, read from
    `PublishedMethodology.path` rather than string-built in a template, so the
    link cannot drift from the route that serves it;
  * and that URL still resolves after the methodology is superseded, serving
    the weights that verdict was *actually* computed with — which is the
    difference between a citation and a decoration.

The third point is why this story reads superseded rows at all; story 4.2's
reader only ever asks for `is_current = True`.

## Public means public

This is the first endpoint in the API with no credential of any kind, which is
required (a methodology a journalist must log in to read is not published) and
is also the reason for three deliberate choices here: the path parameter is
shape-checked against datamodel.md's id pattern before any ZeroDB read, the
response model forbids extra fields so a field added to the stored row cannot
leak onto it, and the response carries nothing voter-derived at all.

Rate limiting for public endpoints is prd.md's "Integrity and abuse" item and
belongs to the story that adds it across the public surface, not to this one;
reading the published methodology is a cheap, cacheable, single-row read.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.methodology.models import (
    VERSION_ID_PATTERN,
    MethodologyVersionsResponse,
    PublishedMethodology,
)
from app.methodology.service import PublicMethodologyService
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBNotFoundError

router = APIRouter(prefix="/v1/methodology", tags=["methodology"])


def zerodb_client_for_methodology() -> ZeroDBClient:
    """The ZeroDB client for this request.

    Its own dependency rather than a shared one, for the reason
    `app.topics.router`'s sibling gives: a test overriding one router's client
    must not have to know which other routers share it.
    """
    return ZeroDBClient.from_env()


def public_methodology_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_methodology)],
) -> PublicMethodologyService:
    return PublicMethodologyService(client)


@router.get(
    "",
    response_model=PublishedMethodology,
    summary="The methodology in force, with its weights and changelog link",
    description=(
        "Public and unauthenticated, by design: a methodology a reader has "
        "to log in to see is not published.\n\n"
        "`weights.tiers` carries the interest tier weights a voter's profile "
        "is scored with. `weights_not_yet_published` names the weight groups "
        "datamodel.md documents that this version does not carry yet — today "
        "that is `evidence_blend` and `vote_types`, which belong to the "
        "scorer in epic 6. They are named rather than zeroed or omitted: a "
        "page that hid them would read as the whole methodology, and invented "
        "numbers for an unwritten scorer would be worse than a gap."
    ),
)
def get_current_methodology(
    service: Annotated[PublicMethodologyService, Depends(public_methodology_service)],
) -> PublishedMethodology:
    return service.current()


# DECLARED BEFORE `/{version}` ON PURPOSE. FastAPI matches in declaration
# order, so with these two swapped a request for the changelog would be read
# as a lookup for a version literally named "versions" and 404. Pinned by
# `test_versions_is_not_mistaken_for_a_version_id`.
@router.get(
    "/versions",
    response_model=MethodologyVersionsResponse,
    summary="The changelog: every published methodology version, newest first",
    description=(
        "Every methodology version ever published, newest first by "
        "`published_at`. Each entry carries its own `path` and "
        "`changelog_url`, so a reader can follow any version back to the "
        "weights that were in force when a verdict was computed under it.\n\n"
        "Ordered by publication date rather than by version id as a string: "
        "`2026.10` is the tenth methodology of 2026 and belongs after "
        "`2026.9`, where a lexicographic sort would put it second."
    ),
)
def list_methodology_versions(
    service: Annotated[PublicMethodologyService, Depends(public_methodology_service)],
) -> MethodologyVersionsResponse:
    return service.versions_response()


@router.get(
    "/{version}",
    response_model=PublishedMethodology,
    summary="One methodology version by id, current or superseded",
    responses={404: {"description": "No such methodology version has been published"}},
    description=(
        "The stable citation target for a verdict or score: a superseded "
        "version stays readable here, serving the weights it was actually in "
        "force with, and reports `is_current: false`. History is never "
        "rewritten (datamodel.md), so this is an audit trail rather than a "
        "redirect to today's methodology."
    ),
)
def get_methodology_version(
    service: Annotated[PublicMethodologyService, Depends(public_methodology_service)],
    version: Annotated[
        str,
        Path(
            pattern=VERSION_ID_PATTERN,
            description='A methodology version id, e.g. "2026.1"',
            examples=["2026.1"],
        ),
    ],
) -> PublishedMethodology:
    try:
        return service.version(version)
    except ZeroDBNotFoundError as exc:
        # Mapped by hand rather than left to propagate: a ZeroDB error reaching
        # the client would be a 500, and "that version was never published" is
        # an ordinary answer to a well-formed public question. The message
        # names only the id the caller already sent — no project id, no table
        # name, nothing about the store behind it.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No methodology version {version!r} has been published.",
        ) from exc


__all__ = [
    "get_current_methodology",
    "get_methodology_version",
    "list_methodology_versions",
    "public_methodology_service",
    "router",
    "zerodb_client_for_methodology",
]
