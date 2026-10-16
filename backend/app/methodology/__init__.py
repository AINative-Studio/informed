"""The public methodology page and its changelog (story 12.2).

prd.md: "Methodology page and changelog; every verdict links to the
methodology version that produced it."

Module map:
    models.py  — the published wire shapes (12.2)
    service.py — reads `methodology_versions`, superseded rows included (12.2)
    router.py  — `GET /v1/methodology[/versions|/{version}]`, public (12.2)

Distinct from `app.profiles.methodology` (story 4.2), which reads the same
table for an internal purpose: supplying today's tier weights to the profile
editor. That one only ever asks for the current row; this one publishes any
version, which is what makes a citation on a years-old verdict resolvable.
Both share 4.2's stored model and its seeding decision rather than keeping two
opinions about one table.
"""

from app.methodology.models import (
    MethodologyVersionsResponse,
    PublishedMethodology,
    version_path,
)
from app.methodology.router import (
    public_methodology_service,
    router,
    zerodb_client_for_methodology,
)
from app.methodology.service import PublicMethodologyService

__all__ = [
    "MethodologyVersionsResponse",
    "PublicMethodologyService",
    "PublishedMethodology",
    "public_methodology_service",
    "router",
    "version_path",
    "zerodb_client_for_methodology",
]
