"""HTTP surface for `GET /v1/topics` (story 4.1).

Not one of prd.md's Core API table's six endpoints by name, but required by
every onboarding path that table's `POST /v1/profiles` sits downstream of
(ui-prd.md 2.2: quick, guided and advanced all start by showing the taxonomy).
Thin, following `app.ballot.router`'s shape: validate the query, call
`TopicService`, return its response model untouched.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.seed_taxonomy import TAXONOMY_LOCALE, TAXONOMY_VERSION
from app.topics.models import TopicTreeResponse
from app.topics.service import TopicService
from app.zerodb import ZeroDBClient

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


__all__ = ["get_topics", "router", "topic_service", "zerodb_client_for_topics"]
