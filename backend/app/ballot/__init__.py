"""The ballot endpoint: `GET /v1/ballot?address=` (story 1.5).

prd.md's Core API describes it as "districts, races and candidates for an
address". Story 1.5 ships the districts and the sitting officeholders — the
walking skeleton — and epic 2 adds races and candidacies on top of the same
payload.

Module map:
    models.py        — the response shapes (and what they deliberately cannot carry)
    service.py       — address -> district -> delegation, over the 1.4 repositories
    router.py        — `/v1/ballot`'s HTTP surface: query validation and status codes only
    cards.py         — the `candidate_cards` read model (story 7.2)
    cards_service.py — assembles `GET /v1/ballot/cards`'s response from it (story 7.1)
    cards_router.py  — `/v1/ballot/cards`'s HTTP surface
"""

from app.ballot.cards_router import router as cards_router
from app.ballot.cards_service import BallotCardsService
from app.ballot.models import BallotCardsResponse, BallotResponse, DistrictSummary, Officeholder
from app.ballot.router import ballot_service, router, zerodb_client
from app.ballot.service import BallotService

__all__ = [
    "BallotCardsResponse",
    "BallotCardsService",
    "BallotResponse",
    "BallotService",
    "DistrictSummary",
    "Officeholder",
    "ballot_service",
    "cards_router",
    "router",
    "zerodb_client",
]
