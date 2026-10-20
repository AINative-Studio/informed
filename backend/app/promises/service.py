"""Assembles one challenger's promises, grouped by one voter's own interests (9.6).

What this module does, in order
================================

1. Read the candidacy and the person it names (same two reads
   `AlignmentService.alignment_for` opens with).
2. Read the voter's decrypted profile through `VoterProfileService` -- the
   only code in this codebase allowed to decrypt one (same posture as
   `app.scoring.service`).
3. Read every `promises` row for this person (story 9.3's extractor output).
4. Group the TESTABLE and non-testable promises alike under the voter's own
   `stance_id`s; count everything else as unmatched rather than dropping it.
5. Read the `promise_sources` row behind each promise, for its url and
   capture date -- the citation prd.md's "evidence over opinion" requires.
6. Derive any prior office from the person's own `people.terms[]` -- not a
   new backend concept; see `_prior_offices_for`.

Nothing is written, and nothing here is cached per datamodel.md's "per-user
alignment scores are computed per request and never stored" -- this is the
same rule applied to a promise grouping instead of a vote-based score: no
table may link a voter's interests to a candidate.

Why this reads `promises` directly rather than widening `AlignmentService`
============================================================================

See `app.promises.models`'s module docstring. In short: `AlignmentService`
is built entirely from `candidate_positions`, which a challenger does not
have by definition, so there is no "add promises to the existing score" path
without conflating two different kinds of evidence (a measured vote-based
position vs. an unscored stated commitment) into one number. This module
keeps them separate, the same way prd.md's "Promise vs. Record" feature is
its own page rather than folded into the ballot score.
"""

from __future__ import annotations

import logging

from app.profiles.service import VoterProfileService
from app.promises.models import ChallengerPromises, PriorOffice, PromiseGroup, PromiseItem
from app.repositories.base import DEFAULT_PAGE_SIZE, Repository
from app.repositories.models import Candidacy, Person, Promise, PromiseSource
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: A page of `promises` for one person. A challenger's own extracted
#: commitments are a handful per campaign, not the hundreds of thousands
#: `member_votes` can reach -- see `app.scoring.positions.PAGE_SIZE`'s sibling
#: reasoning -- so one page is the right default rather than premature paging.
PAGE_SIZE = DEFAULT_PAGE_SIZE


class CandidacyNotFoundError(LookupError):
    """No `candidacies` row exists with the given id.

    Mirrors `app.scoring.service.CandidacyNotFoundError` exactly, down to the
    message shape, so the router's 404 mapping reads the same way for both
    endpoints.
    """


class ProfileNotFoundError(LookupError):
    """The voter has no stored profile, so promises cannot be grouped for them.

    Mirrors `app.scoring.service.ProfileNotFoundError`. A promise grouping is
    just as much "a function of a voter's priorities" as a vote-based score
    is -- there is no meaningful anonymous version of "grouped by MY
    interests" either.
    """


class ChallengerPromisesService:
    """Groups one person's promises under one voter's own interests.

    Holds no per-request state; safe to share across requests like every
    other `*Service` in this codebase.
    """

    def __init__(self, *, client: ZeroDBClient, profiles: VoterProfileService) -> None:
        self._client = client
        self._profiles = profiles
        self._candidacies: Repository[Candidacy] = Repository(client=client, model=Candidacy)
        self._people: Repository[Person] = Repository(client=client, model=Person)
        self._promises: Repository[Promise] = Repository(client=client, model=Promise)
        self._sources: Repository[PromiseSource] = Repository(client=client, model=PromiseSource)

    def promises_for(self, candidacy_id: str, *, user_id: str) -> ChallengerPromises:
        """One candidate's promises, grouped by one voter's own interests.

        Raises:
            CandidacyNotFoundError: no such candidacy.
            ProfileNotFoundError: that voter has no stored profile.

        Deterministic, like `AlignmentService.alignment_for`: groups are
        ordered by `stance_id`, never by ZeroDB's own row order.
        """
        candidacy = self._candidacies.get(candidacy_id)
        if candidacy is None:
            raise CandidacyNotFoundError(f"no candidacy {candidacy_id!r} is known")

        profile = self._profiles.load(user_id)
        if profile is None:
            raise ProfileNotFoundError(
                "no stored profile for this voter; promises cannot be grouped by "
                "interests nobody has stated"
            )

        wanted_stance_ids = {interest.stance_id for interest in profile.interests}

        promises = self._promises_for_person(candidacy.person_id)
        groups, unmatched_count = _group_by_interest(promises, wanted_stance_ids)
        items = self._items_for(groups)

        person = self._people.get(candidacy.person_id)
        prior_offices = _prior_offices_for(person, candidacy) if person is not None else []

        logger.info(
            # Candidacy id and counts only -- never a stance id, which the
            # voter chose and which `app.scoring.service` logs the same way.
            "challenger promises computed for candidacy %s: %d group(s), %d "
            "promise(s) unmatched, %d prior office(s)",
            candidacy_id,
            len(items),
            unmatched_count,
            len(prior_offices),
        )

        return ChallengerPromises(
            candidacy_id=candidacy_id,
            person_id=candidacy.person_id,
            groups=items,
            unmatched_count=unmatched_count,
            prior_offices=prior_offices,
        )

    # --- reads --------------------------------------------------------------

    def _promises_for_person(self, person_id: str) -> list[Promise]:
        """This person's `promises`, filtered locally as well as server-side.

        Same double-filter posture as `AlignmentService._votes_for` and
        `_current_positions`: story 1.4 found live ZeroDB's server-side
        `/query` filter unreliable, and showing one challenger's promises
        under another's candidacy would be exactly that bug made voter-facing.
        """
        page = self._client.query_rows(
            Promise.table_name, filter={"person_id": person_id}, limit=PAGE_SIZE
        )
        if page.has_more or len(page.rows) >= PAGE_SIZE:
            logger.warning(
                "promises returned a full page of %d row(s) for one person; "
                "some promises may be missing from this grouping",
                len(page.rows),
            )
        promises = [Promise.model_validate(row.row_data) for row in page.rows]
        return [promise for promise in promises if promise.person_id == person_id]

    def _items_for(self, groups: dict[str, list[Promise]]) -> list[PromiseGroup]:
        """`PromiseGroup`s, each promise resolved to its citable source.

        Ordered by `stance_id` for a deterministic response -- the same
        posture `AlignmentService` documents for its own ordering.
        """
        items: list[PromiseGroup] = []
        for stance_id in sorted(groups):
            rendered = [
                item
                for item in (self._render(promise) for promise in groups[stance_id])
                if item is not None
            ]
            if rendered:
                items.append(PromiseGroup(stance_id=stance_id, promises=rendered))
        return items

    def _render(self, promise: Promise) -> PromiseItem | None:
        """One promise as `PromiseItem`, or None if its source cannot be read.

        A promise whose `promise_sources` row is missing is a data-integrity
        problem `app.integrity`'s checks exist to catch -- not one this read
        path should paper over with a fabricated url, so it is skipped here
        rather than shown uncited (prd.md: evidence over opinion applies to
        what IS shown, and nothing invented fills the gap).
        """
        source = self._sources.get(promise.source_id)
        if source is None:
            logger.warning(
                "promise %s names source %s, which is not stored; omitted from "
                "this response rather than shown without its citation",
                promise.id,
                promise.source_id,
            )
            return None
        return PromiseItem(
            promise_id=promise.id or "",
            quote_span=promise.quote_span,
            actor=promise.actor,
            action=promise.action,
            object=promise.object,
            direction=promise.direction,
            testable=promise.testable,
            source_url=source.url,
            captured_at=source.captured_at,
        )


def _group_by_interest(
    promises: list[Promise], wanted_stance_ids: set[str]
) -> tuple[dict[str, list[Promise]], int]:
    """Split promises into (groups keyed by the voter's own stance ids, unmatched count).

    A promise with no `stance_id` yet (story 10.1's mapping has not reached
    it) or whose `stance_id` is one the voter did not choose is counted in the
    second element, never dropped silently -- see
    `ChallengerPromises.unmatched_count`'s docstring for why.
    """
    groups: dict[str, list[Promise]] = {}
    unmatched = 0
    for promise in promises:
        if promise.stance_id is not None and promise.stance_id in wanted_stance_ids:
            groups.setdefault(promise.stance_id, []).append(promise)
        else:
            unmatched += 1
    return groups, unmatched


def _prior_offices_for(person: Person, candidacy: Candidacy) -> list[PriorOffice]:
    """Every term on this person's record that is not THIS candidacy's own seat.

    backlog.md 9.6: "a prior record in another office is linked if one
    exists." Not a new backend concept -- `people.terms[]` (datamodel.md,
    embedded on the `people` row, story 2.2's ingest) already carries every
    office someone has held, and `app.ballot.service._officeholders_from`
    already reads this same list to answer "who currently holds a seat". This
    is that list's other half: a term whose `district_id` does NOT match the
    seat this candidacy is running for is, by definition, a different office
    -- whether that term is historical or (for a sitting officeholder running
    for a different seat) still current.

    A term matching this candidacy's OWN `district_id` is excluded: that is
    the seat being run for right now, not a "prior" one, and a challenger
    challenging for the very seat they already hold is not this story's case
    (`is_incumbent` would be true, and `AlignmentService`'s vote-based score
    already covers them).

    Ordered by `start` (oldest first, blank starts last) so a reader sees a
    career in chronological order rather than in whatever order the source
    happened to list terms.
    """
    offices = [term for term in person.terms if term.district_id != candidacy.district_id]
    offices.sort(key=lambda term: term.start or "")
    return [
        PriorOffice(
            body_id=term.body_id,
            district_id=term.district_id,
            party=term.party,
            start=term.start,
            end=term.end,
        )
        for term in offices
    ]


__all__ = [
    "CandidacyNotFoundError",
    "ChallengerPromisesService",
    "ProfileNotFoundError",
]
