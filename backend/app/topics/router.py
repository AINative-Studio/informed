"""HTTP surface for `GET /v1/topics` (story 4.1).

Not one of prd.md's Core API table's six endpoints by name, but required by
every onboarding path that table's `POST /v1/profiles` sits downstream of
(ui-prd.md 2.2: quick, guided and advanced all start by showing the taxonomy).
Thin, following `app.ballot.router`'s shape: validate the query, call
`TopicService`, return its response model untouched.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from app.seed_taxonomy import TAXONOMY_LOCALE, TAXONOMY_VERSION
from app.topics.models import TopicSuggestRequest, TopicSuggestResponse, TopicTreeResponse
from app.topics.service import TopicService
from app.zerodb import ZeroDBClient
from app.zerodb.errors import ZeroDBValidationError

router = APIRouter(prefix="/v1", tags=["topics"])


def zerodb_client_for_topics() -> ZeroDBClient:
    """The ZeroDB client for this request.

    A separate dependency function from `app.ballot.router.zerodb_client`,
    even though both just call `ZeroDBClient.from_env()`, because FastAPI's
    `dependency_overrides` key is the function object: a test overriding the
    ballot router's client must not have to know this router shares it, and a
    later router that needs a *different* client for a test is not blocked by
    reusing one name for two meanings.
    """
    return ZeroDBClient.from_env()


def topic_service(
    client: Annotated[ZeroDBClient, Depends(zerodb_client_for_topics)],
) -> TopicService:
    return TopicService(client)


@router.get(
    "/topics",
    response_model=TopicTreeResponse,
    summary="The issue taxonomy as a tree, for onboarding",
    description=(
        "Every top-level topic (story 3.1: about 20) with its subtopics "
        "nested underneath (about 60 total). This is the tree every "
        "onboarding path reads from — quick (pick 3 primary topics), guided "
        "(story 4.8) and advanced (story 4.7) — so a client builds its "
        "picker once against this shape.\n\n"
        "Topics are returned alphabetically by name. There is no 'default', "
        "'suggested' or 'popular' topic: prd.md requires the app to never "
        "suggest what a voter should prioritize, and alphabetical order is "
        "the one ordering that cannot be read as an endorsement."
    ),
)
def get_topics(
    service: Annotated[TopicService, Depends(topic_service)],
    locale: Annotated[
        str,
        Query(description='Taxonomy locale, e.g. "en-US". Only en-US is published so far.'),
    ] = TAXONOMY_LOCALE,
    taxonomy_version: Annotated[
        int,
        Query(ge=1, description="Published taxonomy_version to read. Defaults to the current one."),
    ] = TAXONOMY_VERSION,
) -> TopicTreeResponse:
    return service.tree(locale=locale, taxonomy_version=taxonomy_version)


@router.post(
    "/topics/suggest",
    response_model=TopicSuggestResponse,
    summary="Map a voter's free-text concern to up to 3 topic suggestions",
    description=(
        "Story 3.5: a voter who does not know the taxonomy's names describes "
        "a concern in their own words and gets back up to 3 candidate topics "
        "by semantic similarity against story 3.4's `topics` vector "
        "namespace.\n\n"
        "**Read-only.** This endpoint writes nothing: no row, no vector, no "
        "cache entry. The free text is used only to call ZeroDB's embedding "
        "search for this one request and is never stored or logged. "
        "`saved` is always `false` and `confirmation_required` is always "
        "`true` in the response — per prd.md, a mapped topic is 'shown back "
        "for confirmation (never silently)'. To actually use a suggestion, "
        "the voter picks its `topic_id` and the client calls the existing "
        "`POST /v1/profiles` endpoint with it, exactly as if the voter had "
        "found that topic by browsing the tree."
    ),
    responses={
        422: {"description": "Blank or missing free text"},
    },
)
def suggest_topics(
    service: Annotated[TopicService, Depends(topic_service)],
    body: Annotated[TopicSuggestRequest, Body()],
) -> TopicSuggestResponse:
    try:
        suggestions = service.suggest(text=body.text, locale=body.locale)
    except ZeroDBValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="text is required to suggest topics",
        ) from exc
    return TopicSuggestResponse(suggestions=suggestions)


__all__ = [
    "get_topics",
    "router",
    "suggest_topics",
    "topic_service",
    "zerodb_client_for_topics",
]
