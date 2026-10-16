"""The ballot endpoint: `GET /v1/ballot?address=` (story 1.5).

prd.md's Core API describes it as "districts, races and candidates for an
address". Story 1.5 ships the districts and the sitting officeholders — the
walking skeleton — and epic 2 adds races and candidacies on top of the same
payload.

Module map:
    models.py  — the response shape (and what it deliberately cannot carry)
    service.py — address -> district -> delegation, over the 1.4 repositories
    router.py  — the HTTP surface: query validation and status codes only
"""

from app.ballot.models import BallotResponse, DistrictSummary, Officeholder
from app.ballot.router import ballot_service, router, zerodb_client
from app.ballot.service import BallotService

__all__ = [
    "BallotResponse",
    "BallotService",
    "DistrictSummary",
    "Officeholder",
    "ballot_service",
    "router",
    "zerodb_client",
]
