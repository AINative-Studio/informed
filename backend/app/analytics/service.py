"""Records usage events and answers k-anonymous aggregate counts
(story 14.7).

Why events, and why one topic per event type
================================================

datamodel.md's `events` primitive (`zerodb_create_event`,
`ZeroDBClient.create_event`/`list_events`, story 1.2) already exists and is
exactly the right shape for this: a topic plus a small payload, published
and read back with no table, no schema migration and no join. Each usage
event type is published under its own topic, prefixed `analytics.` —
`analytics.ballot_viewed`, `analytics.onboarding_completed` — so
`list_events(topic=...)` (which the live API filters server-side, unlike
`Repository.query`'s flat-filter case — see that module's docstring for why
NoSQL table filters needed a client-side fallback and events do not) reads
exactly one event type's stream without reading every other type's events
to discard them.

Why `record` takes no identity parameter
===========================================

`record(event_type, bucket=None)` — that is the whole signature. There is
no `user_id`, `ip`, or `address` parameter for a caller to pass, which is
what makes "this event carries no identity" a fact about the method you can
read rather than a habit every call site has to maintain. `bucket` is the
one piece of context a caller may supply, and it is documented as coarse on
purpose: a two-letter state code identifies roughly a million-plus people
even in the smallest state, where a ZIP code or a precinct would not.
Whether a given bucket is coarse enough is a judgment call made at each call
site, not by this service — this service's job is to refuse to accept
anything shaped like a personal identifier, not to know geography.

Why the aggregate is computed by reading every matching event
==================================================================

There is no server-side COUNT in ZeroDB's events API — `list_events` returns
rows, paged. For the volumes this story's acceptance criterion cares about
(a bucket needs to reach 50 before anything is revealed at all, so the
interesting range is small two- and three-digit counts), reading and
counting the matching page is simpler and more honestly verifiable than
trying to maintain a running counter in a second table that could drift
from the events it is supposed to summarize. A future story moving to a
materialized counter should keep this service's public shape
(`record`/`aggregate`) and change only what is inside `aggregate`.

Why the threshold check happens here and nowhere else
=========================================================

`aggregate` is the only path by which a count can become visible, and the
check is the first thing it does after counting: below `K_ANONYMITY_THRESHOLD`
events for the (event_type, bucket) slice, the real count is never computed
into anything the caller could read — `AggregateResult.count` is `None`
unconditionally, enforced again by that model's own validator (belt and
suspenders: the model refuses to carry a count alongside
`sufficient=False` even if a future edit here tried).
"""

from __future__ import annotations

from app.analytics.models import AggregateResult, RecordedEvent
from app.zerodb.client import ZeroDBClient

#: backlog.md 14.7: "Aggregates require k >= 50." A named constant so the
#: acceptance criterion is checkable by reading one line
#: (`test_k_anonymity_threshold_is_fifty`) rather than hunting a literal.
K_ANONYMITY_THRESHOLD = 50

#: Every analytics event is published under a topic namespaced this way, so
#: `list_events(topic=...)` reads one event type's stream without reading
#: every other type's events only to discard them.
_TOPIC_PREFIX = "analytics."

#: How many events `aggregate` reads per `list_events` page. Generous
#: relative to the threshold this story cares about (a slice only becomes
#: interesting once it nears 50), and large table scans are exactly what
#: paging protects against for a slice that somehow grew very large.
_PAGE_SIZE = 500

#: Hard cap on how many events `aggregate` will read for one slice before
#: giving up and logging rather than looping forever. Far above any volume
#: this story's acceptance criterion is about; it exists so a slice that
#: somehow grew unbounded cannot turn one aggregate request into an
#: unbounded scan.
_MAX_EVENTS_SCANNED = 50_000


class AnalyticsService:
    """Writes coarse usage events and answers k-anonymous aggregate counts."""

    def __init__(self, *, client: ZeroDBClient) -> None:
        self._client = client

    def record(self, event_type: str, bucket: str | None = None) -> RecordedEvent:
        """Record one coarse usage event.

        The ONLY parameters are the event type and an optional coarse
        bucket — see the module docstring. Nothing identifying a person,
        a device or a request can be passed here even by a caller trying
        to, because there is no parameter for it.
        """
        payload: dict[str, str] = {"bucket": bucket} if bucket is not None else {}
        event = self._client.create_event(topic=f"{_TOPIC_PREFIX}{event_type}", payload=payload)
        return RecordedEvent(
            event_type=event_type,
            recorded_at=event.published_at or "",
            payload=dict(event.event_payload),
        )

    def aggregate(self, event_type: str, *, bucket: str | None = None) -> AggregateResult:
        """The k-anonymous aggregate count for one (event_type, bucket) slice.

        Reveals the real count only when at least `K_ANONYMITY_THRESHOLD`
        events exist for this exact slice; otherwise `count` is `None` and
        `sufficient` is `False` — never the real, small number, which is
        itself re-identifying information for a rare event in a small
        bucket.
        """
        count = self._count(event_type, bucket=bucket)
        sufficient = count >= K_ANONYMITY_THRESHOLD
        return AggregateResult(
            event_type=event_type,
            bucket=bucket,
            sufficient=sufficient,
            count=count if sufficient else None,
            threshold=K_ANONYMITY_THRESHOLD,
        )

    def _count(self, event_type: str, *, bucket: str | None) -> int:
        """How many recorded events match this event type and bucket.

        Pages through every event published under this type's topic,
        matching the bucket client-side — `list_events`'s own filter is on
        `topic` only, so a per-bucket slice is counted here rather than by
        the transport.
        """
        topic = f"{_TOPIC_PREFIX}{event_type}"
        count = 0
        skip = 0
        while skip < _MAX_EVENTS_SCANNED:
            page = self._client.list_events(topic=topic, limit=_PAGE_SIZE, skip=skip)
            for event in page.events:
                if bucket is None or event.event_payload.get("bucket") == bucket:
                    count += 1
            if len(page.events) < _PAGE_SIZE and not page.has_more:
                break
            skip += _PAGE_SIZE
        return count


__all__ = ["K_ANONYMITY_THRESHOLD", "AnalyticsService"]
