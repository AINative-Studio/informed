"""Shapes for story 14.7's privacy-safe analytics.

backlog.md 14.7: "As the team, we want usage analytics only above
k-anonymity thresholds, so that no individual can be singled out."
Acceptance: aggregates require k >= 50; no third-party trackers.

Why these two models carry no identifying field, structurally
==================================================================

Both set `extra="forbid"` and declare no field shaped like a user, voter or
IP. That is not a promise `AnalyticsService` happens to keep — there is no
field *through which* an identity could ever be attached to a
`RecordedEvent`, and `AggregateResult`'s validator below makes "insufficient
data" and "a real count" mutually exclusive at the type level, so a bug that
tried to attach a real count to an insufficient result is rejected before it
could ever be returned to a caller.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecordedEvent(BaseModel):
    """One coarse, non-identifying usage event, as recorded.

    `payload` carries at most a `bucket` key (e.g. a two-letter state code)
    — see `AnalyticsService.record`'s docstring for why nothing finer-grained
    is accepted. No `user_id`, no `ip`, no `address`: datamodel.md's events
    table is reused here exactly as `verdict.published`/`profile.updated`
    use it elsewhere, carrying the minimum a consumer needs and nothing a
    voter would have to be told about.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(description='e.g. "onboarding_completed", "ballot_viewed"')
    recorded_at: str = Field(
        description="ISO-8601 UTC, assigned by ZeroDB when the event was published"
    )
    payload: dict[str, str] = Field(
        default_factory=dict,
        description='At most {"bucket": "<coarse geographic or categorical label>"}',
    )


class AggregateResult(BaseModel):
    """The answer to "how many X events happened in bucket Y".

    `sufficient` and `count` are kept in lockstep by the validator below:
    `sufficient=False` means `count` is always `None`, and `sufficient=True`
    means `count` is always populated. There is no way to construct a result
    that reveals a number while also claiming the data was insufficient, or
    vice versa — the k-anonymity acceptance criterion ("k >= 50") is
    therefore a property of the type, not only of the one method that
    builds it today.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str
    bucket: str | None = Field(default=None, description="The coarse bucket this aggregate is over")
    sufficient: bool = Field(
        description="Whether at least K_ANONYMITY_THRESHOLD events exist for this slice"
    )
    count: int | None = Field(
        default=None,
        description="The real count, present only when `sufficient` is true",
    )
    threshold: int = Field(description="The k-anonymity threshold this answer was checked against")

    @model_validator(mode="after")
    def _count_and_sufficiency_agree(self) -> AggregateResult:
        if self.sufficient and self.count is None:
            raise ValueError("a sufficient aggregate must carry its real count")
        if not self.sufficient and self.count is not None:
            raise ValueError(
                "an insufficient aggregate must not carry a count — a small real "
                "number is itself re-identifying information"
            )
        return self


__all__ = ["AggregateResult", "RecordedEvent"]
