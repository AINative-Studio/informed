"""Response shapes for `GET /v1/topics/{topic_id}/stances` (story 4.1).

Story 3.2 built `Stance` (the stored row) and `StanceService` (the
per-request shuffle). Neither is an HTTP response model: `Stance.id` is not
documented as part of any API contract yet, and `Stance.direction_code` is an
internal scoring signal (`app.seed_stances.DIRECTION_CODES`) that ui-prd.md
2.2 never asks the onboarding screen to show or sort by — showing it would let
a client reconstruct the axis order the randomization exists to hide. This
module is the voter-facing subset.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StanceOption(BaseModel):
    """One stance card, as a voter picks from it.

    No field here can hold a default, a rank or a "most common" flag — the
    same absence `Stance` itself enforces with `extra="forbid"` (story 3.2).
    `direction_code` is deliberately NOT exposed: it is scoring's internal
    signed position on the subtopic's axis, and showing it on the wire would
    let a client sort the menu back into spectrum order, undoing the
    randomization this endpoint exists to serve.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="`stances` row id — what a saved interest's stance_id cites")
    label: str = Field(description="The outcome the voter wants, in plain words")
    description: str = Field(description="A sentence of context for the label")


class StanceOptionsResponse(BaseModel):
    """What `GET /v1/topics/{topic_id}/stances` returns.

    `options` carries whatever order `StanceService.for_topic` produced for
    THIS call — a fresh shuffle every time (story 3.2) — never the same order
    twice in a row by design. The client is the one that must hold this order
    steady for the rest of the onboarding session (ui-prd.md 2.2: "randomized
    ... every session"); see `app.stances.service`'s module docstring for why
    that split is deliberate and not a gap.
    """

    model_config = ConfigDict(extra="forbid")

    topic_id: str = Field(description="The subtopic these options belong to")
    options: list[StanceOption] = Field(
        description=(
            "Stance cards for this subtopic, in a fresh random order every "
            "call. Empty for a topic with no cards (a top-level topic, or an "
            "id the taxonomy does not recognise) rather than a 404 — the "
            "caller asked a well-formed question and gets an honest, if "
            "empty, answer."
        )
    )


__all__ = ["StanceOption", "StanceOptionsResponse"]
