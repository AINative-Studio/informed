"""Reading a subtopic's stance cards back, in no particular order (story 3.2).

Which layer owes which half of the criterion
============================================

Story 3.2's acceptance criterion is "display order is randomized per session;
no stance is preselected". Those are two claims, and they belong to two
different layers. Getting the split wrong is how an app ends up with a
first-option bias nobody intended, so it is written down here rather than left
to be inferred.

**The backend owes "no implied ranking."** Every path by which a caller could
obtain a stable order has to be closed:

  * `stances` has no `display_order`, `rank` or `position` column — datamodel.md
    gives it none, and `Stance` pins the absence with a test, because any stored
    order IS a ranking: whichever card sits first gets picked more.
  * The cards are not *written* in axis order either (see `STANCES` in
    `app.seed_stances`), so even an unshuffled insertion-order read does not
    show a menu running from one end of the spectrum to the other.
  * `for_topic` shuffles on every call. Not "returns a set" — Python has no
    ordered-set-free way to hand back rows, and an arbitrary-but-stable order
    is exactly the thing to avoid, because "arbitrary" from storage is in
    practice "the same every time".

**The client owes "per session."** A voter must see the same order for as long
as they are filling in their profile. Cards that re-order between renders — on
every keystroke in a filter box, on a back-navigation, on a refetch — are worse
than a fixed order: they make the list unreadable and they make a careful
comparison of three positions impossible. That consistency needs session state.

Why the backend does not keep that session state
================================================

It could: a `GET /stances?session=…` seeded from a session id would give a
stable per-session order, server-side. It is the wrong trade here, on three
counts.

  1. **It is a new identifier attached to political-opinion data.** prd.md's
     privacy rules make a voter's interests special-category data, promise
     anonymous use with "profile held client-side only", and forbid logging it.
     A server-side session key that the stance endpoint must receive, store and
     log alongside *which topics the voter is browsing* is a re-identification
     surface built for a display detail. The ordering has no security value
     whatever, so paying for it in linkable state is backwards.
  2. **The ordering has to survive offline.** The frontend is an installable
     PWA (CLAUDE.md's stack), so the profile screen must work from cache; an
     order that depends on a server round trip does not.
  3. **The client already has the right state.** React holds the shuffled array
     for the life of the component; a seeded shuffle in session storage pins it
     across a reload. Story 3.6 is where that lands. It is a few lines there and
     a session table here.

So: **shuffled per request server-side, stabilised per session client-side.**
The nice property of that split is that the halves fail independently and the
backend's half fails safe — a client that forgets to shuffle, or re-shuffles on
every render, still never shows a *consistent* first option to anyone. A
backend that returned a stable order would make the client's bug invisible.

What this service is not
========================

There is no `default_for`, no `suggested`, no `top`, and
`test_the_service_exposes_no_method_that_picks_one` fails the build if one
appears. A convenience method returning "the usual pick" would be the single
most load-bearing nudge in the product, and it is the kind of thing added later
for a plausible-sounding reason — an empty-state placeholder, a demo fixture, a
"smart default" for the quick onboarding path. prd.md: "the app may *ask*
clarifying questions but never suggests what a user's interests should be".
"""

from __future__ import annotations

import logging
import random
from typing import Protocol

from app.repositories import Repository
from app.repositories.models import Stance
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: One page big enough to hold the whole `stances` table (~180 rows at wording
#: version 1). The table is read unfiltered and grouped in Python because live
#: ZeroDB's server-side `/query` filter was observed to match nothing (story
#: 1.4's finding, datamodel.md "Open questions"), so a `filter=` here would
#: narrow nothing server-side and the client-side pass is doing the work
#: anyway. Sized with headroom so a later wording version that adds a fourth
#: card per subtopic still fits; `_read_all` warns rather than silently
#: truncating if the table outgrows it.
PAGE_SIZE = 500


class _Rng(Protocol):
    """The slice of `random` this service uses.

    A protocol rather than `random.Random`, so a test can inject a seeded
    `Random` (which satisfies it) and the module-level `random` can be the
    default (which also satisfies it) without either being special-cased.
    """

    def shuffle(self, x: list, /) -> None: ...  # pragma: no cover - structural


class StanceService:
    """Reads stance cards for one or more subtopics, shuffled per call.

    `rng` is injectable because the fairness tests need determinism: asserting
    that no card lands first disproportionately over 600 calls is only a stable
    test with a seeded generator. Production takes the module-level `random`.

    Deliberately NOT `secrets` or `SystemRandom`: this is a display order, not
    a secret. Nothing about a voter's eventual choice is inferable from the
    order they were offered, and a cryptographic generator cannot be seeded for
    a test, which would make the fairness property unassertable — a worse
    outcome for a neutrality guarantee than a predictable shuffle.
    """

    def __init__(self, client: ZeroDBClient, *, rng: _Rng | None = None) -> None:
        self._client = client
        self._rng: _Rng = rng if rng is not None else random
        self._stances: Repository[Stance] = Repository(client=client, model=Stance)

    def for_topic(self, topic_id: str) -> list[Stance]:
        """Every stance card for one subtopic, in a fresh random order.

        Returns an empty list for a top-level topic (which carries no cards by
        design) and for an unknown id. Not an error in either case: "this node
        has no stance cards" is a real answer for a parent, and the caller
        renders its children instead.
        """
        return self.for_topics([topic_id]).get(topic_id, [])

    def for_topics(self, topic_ids: list[str]) -> dict[str, list[Stance]]:
        """Cards for several subtopics at once, each shuffled independently.

        One read for the whole batch rather than one per subtopic: the
        onboarding screen needs three to five subtopics' cards together, and
        live ZeroDB throttles at 60 requests per minute (datamodel.md "Open
        questions").

        **Each subtopic is shuffled separately**, not once over the flat list.
        Shuffling the flat list and then grouping would also give each group a
        random order, but the groups' orders would then move together between
        requests — a correlation a voter comparing two topics side by side over
        a session could actually read, which is the kind of structure this
        whole mechanism exists to remove.

        Every requested id gets a key in the result, with an empty list when
        nothing is stored for it: the caller asked, so it gets an answer rather
        than a hole it has to guess the meaning of.
        """
        if not topic_ids:
            return {}

        wanted = set(topic_ids)
        grouped: dict[str, list[Stance]] = {topic_id: [] for topic_id in topic_ids}
        for row in self._read_all():
            if row.topic_id in wanted:
                grouped[row.topic_id].append(row)

        for cards in grouped.values():
            self._rng.shuffle(cards)
        return grouped

    def _read_all(self) -> list[Stance]:
        """One unfiltered page of `stances`, validated.

        Unfiltered on purpose — see `PAGE_SIZE`. Validated through the
        repository rather than read as raw rows, because a stance id reaches a
        voter's encrypted profile: a row that stopped matching `Stance` must
        fail loudly here rather than be silently dropped from the menu a voter
        is choosing from.
        """
        rows = self._stances.query(limit=PAGE_SIZE)
        if len(rows) >= PAGE_SIZE:
            logger.warning(
                "the stance read returned a full page of %d rows; some subtopics may be "
                "missing cards and PAGE_SIZE needs raising",
                len(rows),
            )
        return rows


__all__ = ["PAGE_SIZE", "StanceService"]
