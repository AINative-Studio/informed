"""The cross-partisan advisory panel's review workflow (story 3.3).

What this module is for
=======================

prd.md, "Trust, neutrality, privacy and safety":

    Stance cards and issue taxonomy reviewed by a cross-partisan advisory
    panel before each release; wording changes are versioned and public.

Stories 3.1 and 3.2 wrote the wording and the machine-checkable guards on it —
`app.seed_taxonomy.contains_stance_presupposing_word` for topic labels,
`app.seed_stances.contains_partisan_identity_word` and `balance_complaints`
for stance cards. Both modules say in their own docstrings that those checks
are *proxies*, not a proof of fairness, and name this story as the one that
adds the human layer above them. This module is that layer: a ledger of who
read which wording at which version and what they decided, a gate that will
not serve wording nobody has passed, and an editing path that cannot change
live wording without going through both.

Story 3.3's three acceptance criteria, and where each lands
============================================================

**"Unapproved wording cannot be published."** `approved_stances_for_topic` and
`approved_topic_tree` are the gated read paths: a row reaches a voter only if
the ledger carries an in-force `approved` or `grandfathered` decision for the
exact version the row is at. The ungated `StanceService`/`TopicService` paths
are left untouched and still serve everything — see "Why the gate is a
separate read path" below for why that is deliberate rather than a hole.

**"Every edit bumps `wording_version`."** Editing is `propose` then
`decide`, never a write. A proposal stores the new wording in the ledger while
the live row keeps the wording that was approved; approving the proposal is the
only thing that writes the row, and it bumps `wording_version` (a stance) or
`taxonomy_version` (a topic) in the same call. So a bump cannot happen without
a review and a review cannot happen without a bump — the two are one
operation rather than two that a caller could do out of order.

**"The review history is visible."** `history_for` returns every decision ever
recorded about one row, newest first, with the reviewer, the version, the
timestamp, the reason and — for an approved edit — the wording before and
after. Nothing is ever updated or deleted, so the history is the whole record.

The honest hole: the existing 80 topics and 201 stances
=======================================================

Stories 3.1 and 3.2 seeded the live "informed" project before this workflow
existed: 80 `issue_topics` rows and 201 `stances` rows are live now, read on
every request by stories 3.5, 4.1, 4.2 and 4.4. Three options, and only one of
them is honest:

  1. **Mark them `approved`.** Trivial, and a lie. No panel has ever met —
     prd.md lists "who sits on the cross-partisan advisory panel, and how
     members are chosen" as an *open question* — so a row reading
     `decision: approved, reviewer_id: <someone>` would claim a review that
     did not happen, in the one ledger whose entire value is that its claims
     are true. A product whose first principle is "evidence over opinion"
     cannot start its neutrality ledger with a forged entry.
  2. **Leave them unmarked, so the gate blocks them.** Also trivial, and it
     empties the taxonomy picker and every stance card in the product. The
     gate would be technically correct and the app would be down.
  3. **Mark them `grandfathered`.** A fourth decision value that means exactly
     what is true: *this wording is being served, and it has never been
     through a panel.* It passes the gate (so nothing goes dark), it is not
     approval (so no false claim is made), and `queue()` reports every one of
     them as still owing a real review — which is a standing, countable,
     visible debt rather than a paragraph in a PR nobody reads again.

Option 3 is what `grandfather_existing_corpus` writes. Its `reviewer_id` is
the literal `informed:story-3.3-backfill`, not a person: the ledger says a
migration marked these, which is the fact. A real panel decision on any of
those rows supersedes the grandfathered one automatically (the ledger is
append-only and the newest in-force decision wins), and that row then drops
out of `queue()` without anything having to be deleted.

Why the gate is a separate read path, not a filter inside `StanceService`
=========================================================================

`StanceService.for_topic` and `TopicService.tree` are deliberately left
serving everything, and the gated reads are new methods here. Three reasons:

  1. **Blast radius.** Those two services are read by four shipped endpoints
     against 281 live rows. Making the gate the default means every one of
     those endpoints returns nothing the moment the ledger is empty or a
     backfill has not run — in an environment, in a test fixture, in a fresh
     project. A gate that fails closed on the whole product is a worse failure
     than the one it prevents, especially while the gate is new.
  2. **The two questions are different.** "What stance cards exist for this
     subtopic" and "what stance cards may I show a voter" are not the same
     question, and a reviewer screen needs the first one — a panel member must
     be able to see the wording they are judging, which by definition is not
     yet approved. Collapsing them would make the review UI impossible to
     build against this service.
  3. **It keeps the story honest about what it ships.** The gate is proven
     against new and edited content, which is where story 3.3's criterion
     actually bites ("unapproved wording cannot be published" is a statement
     about publishing, and the existing corpus was published before the rule
     existed). Switching the voter-facing endpoints onto the gated path is a
     one-line change in each router, and the right time for it is when a real
     panel has approved the corpus — not while the only approvals in the
     ledger would be grandfathered ones.

That trade is flagged in this story's PR as the open question it is.

Reviewer identity
=================

`reviewer_id` is a plain string the caller supplies, exactly as `user_id` is in
`app.profiles.router` — no authentication layer exists anywhere in this
codebase yet, and building one is a different story. What is enforced is the
one rule that does not depend on a roster: **a reviewer may not approve their
own proposal.** Panel composition, quorum and term limits are governance
decisions prd.md has not answered, so there is no `panel_members` table and no
roster check here; adding one later changes this module's `decide` method and
nothing else, because every decision already records who claimed to make it.

The honest limit: this ledger proves *what was claimed*, not *who claimed it*.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.repositories import Repository
from app.repositories.models import (
    IssueTopic,
    PanelReview,
    ReviewDecision,
    ReviewSubjectTable,
    Stance,
)
from app.seed_stances import contains_partisan_identity_word
from app.seed_taxonomy import contains_stance_presupposing_word
from app.topics.models import TopicNode, TopicTree, TopicTreeResponse
from app.zerodb.client import ZeroDBClient

logger = logging.getLogger(__name__)

#: One page big enough to hold the whole ledger. Mirrors
#: `app.stances.service.PAGE_SIZE`'s reasoning: live ZeroDB's server-side
#: `/query` filter matches nothing (story 1.4's finding, datamodel.md "Open
#: questions"), so every read here is an unfiltered page grouped in Python and
#: a `filter=` would narrow nothing server-side.
#:
#: Sized for a ledger rather than a corpus: the grandfathering backfill alone
#: writes 281 rows, and every subsequent panel pass over the corpus adds up to
#: 281 more. 5000 holds roughly seventeen full review cycles; `_read_all` logs
#: loudly rather than silently truncating when it is reached, because a
#: truncated ledger read would make an approved row look unreviewed and drop it
#: out of a gated response.
PAGE_SIZE = 5000

#: The `reviewer_id` the grandfathering backfill records. Deliberately not a
#: person and deliberately not plausible as one: anyone reading the ledger must
#: be able to tell a migration's mark from a panel member's decision at a
#: glance, and `queue()` keys off this value to report what still owes a real
#: review.
BACKFILL_REVIEWER_ID = "informed:story-3.3-backfill"

#: The decisions that let a row through a gated read. `grandfathered` is in
#: here and is NOT approval — see the module docstring, option 3.
PUBLISHABLE_DECISIONS: frozenset[str] = frozenset({"approved", "grandfathered"})


class PanelReviewError(RuntimeError):
    """Base for every refusal this module raises.

    A `RuntimeError` subclass, like `app.profiles.service.AnonymousModeError`:
    these are domain refusals the router maps to 4xx, not programming errors.
    """


class UnknownSubjectError(PanelReviewError):
    """The row being reviewed or edited does not exist.

    datamodel.md rule 5 ("integrity lives in code") applied to a reference the
    generic repository check cannot express: `PanelReview.subject_id` names a
    row in whichever table `subject_table` says, so it is resolved here.
    """


class VersionMismatchError(PanelReviewError):
    """The decision names a version the row is not at.

    The load-bearing refusal of the whole gate. A reviewer who read version 1
    while version 2 was being published must not have their approval land on
    version 2 — their judgement was about wording they never saw.
    """


class SelfReviewError(PanelReviewError):
    """A reviewer tried to approve their own wording proposal.

    The one panel rule that holds whatever the roster turns out to be. prd.md
    requires a *cross-partisan* review; a proposal passed by its own author has
    had no review at all, cross-partisan or otherwise.
    """


class EmptyProposalError(PanelReviewError):
    """A proposal that changes nothing.

    Refused rather than stored: an approved no-op edit would bump
    `wording_version` without changing a word, which breaks the one thing the
    version number is for — telling a reader that the wording they cited has
    moved. It would also fill a reviewer's queue with nothing to review.
    """


class UnreviewableWordingError(PanelReviewError):
    """Proposed wording that stories 3.1 and 3.2's own guards reject.

    Checked before the proposal is stored, not at approval time, for two
    reasons. A reviewer's time is the scarcest thing in this workflow and
    should not be spent on wording a `frozenset` lookup can reject; and an
    author gets the feedback while they still have the sentence in their head.
    The guards are story 3.1's and 3.2's exact functions, not reimplementations
    — one implementation, now four callers.
    """


class UnknownProposalError(PanelReviewError):
    """No proposal is stored under that id."""


@dataclass(frozen=True)
class ReviewStatus:
    """What the ledger currently says about one row's wording.

    `decision` is None when nothing has ever been recorded — which is also
    what `is_publishable` keys off. "Nothing recorded" is deliberately not a
    `pending` decision value; see `ReviewDecision`'s comment for why the
    absence of a row is the representation of the absence of a decision.

    `version` is the subject version the in-force decision judged, so a caller
    can tell "approved, and the row is still at that version" from "approved,
    but at a version the row has since moved past" — the second is not
    publishable and is exactly what a version bump is supposed to cause.
    """

    subject_table: ReviewSubjectTable
    subject_id: str
    subject_version: int
    decision: ReviewDecision | None = None
    reviewer_id: str | None = None
    decided_at: str | None = None
    reason: str | None = None

    @property
    def is_publishable(self) -> bool:
        """Whether a gated read may serve this row.

        True only for an in-force `approved` or `grandfathered` decision. The
        version check happens in `status_for`, which only considers decisions
        naming the version the row actually carries — so a stale approval of
        version 1 does not publish version 2.
        """
        return self.decision in PUBLISHABLE_DECISIONS

    @property
    def is_panel_approved(self) -> bool:
        """Whether a *person* approved this wording.

        Distinct from `is_publishable` on purpose, and the distinction is the
        whole grandfathering design: a grandfathered row is served and is not
        approved. `queue()` reports on this property, not on the other one.
        """
        return self.decision == "approved" and self.reviewer_id != BACKFILL_REVIEWER_ID


@dataclass(frozen=True)
class WordingProposal:
    """A proposed wording change, as recorded in the ledger.

    Not its own table. A proposal is the `decision: "withdrawn"`-eligible half
    of the same append-only ledger: one row carrying the new wording, the
    previous wording, its author and the version it was written against. That
    keeps "what was proposed" and "what was decided about it" in one ordered
    record rather than two tables that could disagree about which proposal a
    decision judged.

    The author is stored in `reviewer_id` because the ledger has one actor
    field and a proposal's actor is its author. `SelfReviewError` is what stops
    that overloading from letting an author approve themselves.
    """

    proposal_id: str
    subject_table: ReviewSubjectTable
    subject_id: str
    subject_version: int
    author_id: str
    proposed_at: str
    label: str | None = None
    description: str | None = None
    name: str | None = None
    previous_label: str | None = None
    previous_description: str | None = None
    previous_name: str | None = None
    decided: bool = False


@dataclass
class GrandfatherReport:
    """What one grandfathering backfill marked, for the CLI's summary.

    Counts rather than ids in the summary line, but the ids are kept so a
    caller (and the live verifier) can assert on exactly which rows were
    marked rather than on a number that could be right for the wrong rows.
    """

    topics_marked: list[str] = field(default_factory=list)
    stances_marked: list[str] = field(default_factory=list)
    already_marked: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.topics_marked) + len(self.stances_marked)


@dataclass(frozen=True)
class QueueEntry:
    """One row that no panel member has approved at its current version."""

    subject_table: ReviewSubjectTable
    subject_id: str
    subject_version: int
    #: What the ledger says now: `grandfathered` for a row the backfill marked,
    #: `rejected` for wording a reviewer refused, None for a row nobody has
    #: ever looked at. Carried so a reviewer screen can sort its own work —
    #: never-reviewed new wording is more urgent than the standing
    #: grandfathering debt.
    current_decision: ReviewDecision | None
    wording: str


def review_id(
    *,
    subject_table: str,
    subject_id: str,
    subject_version: int,
    reviewer_id: str,
    decision: str,
    reason: str | None,
    proposed: dict[str, str | None] | None = None,
) -> str:
    """`pr:{sha256}` over everything the decision says.

    Content-addressed for the same reason `provenance_log`'s id is (see
    `app.repositories.models.PanelReview`): it makes the write idempotent with
    no bookkeeping. Re-running the backfill, or a reviewer double-clicking
    approve, hashes to a row that already exists and the upsert is a no-op;
    two genuinely different decisions hash differently and both persist, which
    is what an append-only ledger requires.

    The proposed wording is in the hash, so approving a *different* proposal
    for the same row at the same version is a distinct decision rather than a
    silent collision with the first.

    `json.dumps` with sorted keys rather than an f-string: the hash must not
    change because a field was added in a different position, and a separator
    that cannot appear in a key is what stops ("a", "bc") and ("ab", "c")
    hashing alike.
    """
    material = json.dumps(
        {
            "subject_table": subject_table,
            "subject_id": subject_id,
            "subject_version": subject_version,
            "reviewer_id": reviewer_id,
            "decision": decision,
            "reason": reason,
            "proposed": proposed or {},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"pr:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _now() -> str:
    """An ISO-8601 UTC timestamp, `Z`-suffixed, to microsecond resolution.

    The same spelling `app.profiles.service` uses, so every timestamp written
    by this codebase sorts lexicographically against every other one — which
    is what `history_for`'s ordering and `_in_force`'s newest-wins rule both
    rely on.

    **Microseconds rather than `timespec="seconds"`**, which is the one
    deliberate difference from `app.profiles.service._now`. A profile row
    carries one timestamp and nothing orders two of them; this ledger's whole
    read model is "the newest decision about this row wins", and two decisions
    about one row within the same second is not a hypothetical — it is a
    reviewer working through a queue, a proposal approved moments after it was
    written (which `decide` does *twice* in one call, by design), and every
    test that records a sequence. At second resolution those tie, the tie
    breaks on a content hash, and the hash order has nothing to do with the
    order the decisions were made in: the ledger would silently report the
    wrong decision as in force.
    """
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class PanelReviewService:
    """Records panel decisions, gates unreviewed wording, and applies edits.

    Stateless and safe to share across requests, like every other service
    built on `Repository` here. The ledger is read once per public call and
    not cached between calls: a decision made by another process must be
    visible to the next gated read, and a service-lifetime cache would serve
    wording whose approval had been superseded.
    """

    def __init__(self, client: ZeroDBClient, *, pace: Callable[[], None] | None = None) -> None:
        """Build the service.

        `pace` is the same injectable pacer the seeders and the bootstrap use,
        and it is called before each ledger write. It matters for exactly one
        caller — `grandfather_existing_corpus`, which writes 281 rows against a
        project observed to throttle at 60 requests per minute (datamodel.md
        "Open questions"), each one an upsert costing a find and a write.

        **It defaults to a no-op, not to the real pacer**, which is the
        opposite of `TaxonomySeeder`'s and `StanceSeeder`'s default and is
        worth justifying. Those two have exactly one production caller each, a
        CLI, and their only job is a bulk write. This service's production
        caller is an HTTP request handler recording ONE decision, and a pacer
        there would put a deliberate sleep in a request path for no benefit. So
        the bulk caller pays for pacing explicitly — `scripts/backfill_panel_
        review.py` passes `pace=default_pacer()` — and the per-request path
        stays fast. The cost of getting that backwards is visible either way: a
        throttled backfill or a slow endpoint, not silent data loss.
        """
        self._client = client
        self._pace: Callable[[], None] = pace if pace is not None else lambda: None
        self._reviews: Repository[PanelReview] = Repository(client=client, model=PanelReview)
        self._topics: Repository[IssueTopic] = Repository(client=client, model=IssueTopic)
        self._stances: Repository[Stance] = Repository(client=client, model=Stance)

    # =====================================================================
    # Recording decisions
    # =====================================================================

    def record_decision(
        self,
        *,
        subject_table: ReviewSubjectTable,
        subject_id: str,
        subject_version: int,
        reviewer_id: str,
        decision: ReviewDecision,
        reason: str | None = None,
    ) -> PanelReview:
        """Record one panel decision about wording already stored.

        For judging the wording a row currently carries. A decision about a
        *proposed change* goes through `decide` instead, because approving a
        change has to write the row and bump its version — this method writes
        nothing but the ledger entry.

        Args:
            subject_table: `issue_topics` or `stances`.
            subject_id: The row being judged.
            subject_version: The `wording_version`/`taxonomy_version` the
                reviewer read. Checked against the row, not trusted.
            reviewer_id: Who decided. An authentication stand-in; see the
                module docstring.
            decision: `approved`, `rejected`, `withdrawn` or `grandfathered`.
            reason: Required for `rejected`; the model enforces it.

        Raises:
            UnknownSubjectError: no such row.
            VersionMismatchError: the row is not at `subject_version`.
        """
        subject = self._require_subject(subject_table, subject_id)
        stored_version = _version_of(subject)
        if stored_version != subject_version:
            raise VersionMismatchError(
                f"{subject_table} row {subject_id!r} is at version {stored_version}, but the "
                f"decision names version {subject_version}; a reviewer's judgement applies "
                "only to the wording they actually read"
            )
        return self._append(
            subject_table=subject_table,
            subject_id=subject_id,
            subject_version=subject_version,
            reviewer_id=reviewer_id,
            decision=decision,
            reason=reason,
        )

    def grandfather_existing_corpus(
        self, *, reviewer_id: str = BACKFILL_REVIEWER_ID
    ) -> GrandfatherReport:
        """Mark every stored topic and stance `grandfathered`.

        The migration that lets the gate exist without taking the product
        down, and the one place in this codebase that writes a decision for
        rows no human has read. See the module docstring, option 3, for why
        this is `grandfathered` rather than `approved` — the short version is
        that `approved` would be a false claim in the ledger whose whole value
        is that its claims are true.

        Idempotent: the decision's id is content-addressed over (row, version,
        reviewer, decision), so a second run for an unchanged row lands on the
        same id and upserts in place. A row whose version has since been
        bumped by an approved edit gets a *new* grandfathering mark at the new
        version — which is wrong-shaped enough to be worth naming: it would
        mean a reviewer approved an edit and then a backfill re-ran, so the
        approved decision at that version already exists and is newer. The
        grandfathered mark loses the tie by `decided_at`, and `status_for`
        prefers the approval. Running the backfill after a real review
        therefore cannot downgrade an approved row.

        Rows already carrying a decision at their current version are skipped
        rather than re-marked, so the report distinguishes "this run marked it"
        from "it was already covered".
        """
        report = GrandfatherReport()
        ledger = self._read_all()

        for table, rows in (
            ("issue_topics", self._read_subjects("issue_topics")),
            ("stances", self._read_subjects("stances")),
        ):
            for row in rows:
                row_id = str(row.id)
                version = _version_of(row)
                existing = _in_force(ledger, table, row_id, version)
                if existing is not None:
                    report.already_marked.append(row_id)
                    continue
                self._append(
                    subject_table=table,  # type: ignore[arg-type]
                    subject_id=row_id,
                    subject_version=version,
                    reviewer_id=reviewer_id,
                    decision="grandfathered",
                    reason=(
                        "Seeded by story 3.1/3.2 before the panel review workflow existed. "
                        "Served, NOT panel-approved, and still owed a real cross-partisan "
                        "review (prd.md: reviewed by a cross-partisan advisory panel before "
                        "each release)."
                    ),
                )
                if table == "issue_topics":
                    report.topics_marked.append(row_id)
                else:
                    report.stances_marked.append(row_id)

        logger.info(
            "panel review backfill: marked %d topic(s) and %d stance(s) grandfathered; "
            "%d row(s) already carried a decision",
            len(report.topics_marked),
            len(report.stances_marked),
            len(report.already_marked),
        )
        return report

    # =====================================================================
    # Proposing and deciding an edit
    # =====================================================================

    def propose(
        self,
        *,
        subject_table: ReviewSubjectTable,
        subject_id: str,
        author_id: str,
        label: str | None = None,
        description: str | None = None,
        name: str | None = None,
    ) -> WordingProposal:
        """Propose new wording for a row, without changing it.

        This is the half of "every edit bumps `wording_version`" that makes the
        other half safe: the live row keeps the wording that was approved, the
        proposal holds the new wording, and nothing a voter can read has moved.
        A proposal is visible to `queue()` and `history_for` immediately, so a
        reviewer can find it.

        Only the fields that are changing need passing; the rest are read off
        the stored row and recorded as the `previous_*` values, which is what
        makes the history able to show what changed without re-deriving it
        from a seeder constant that may itself have moved.

        Raises:
            UnknownSubjectError: no such row.
            EmptyProposalError: nothing passed, or everything passed matches
                what is already stored.
            UnreviewableWordingError: the proposed wording fails story 3.1's
                or 3.2's own neutrality guard.
        """
        subject = self._require_subject(subject_table, subject_id)
        version = _version_of(subject)

        if subject_table == "stances":
            assert isinstance(subject, Stance)  # noqa: S101 - _require_subject guarantees it
            if name is not None:
                raise UnreviewableWordingError(
                    f"stance {subject_id!r} has a label and a description, not a name"
                )
            proposed = self._stance_proposal(subject, label=label, description=description)
        else:
            assert isinstance(subject, IssueTopic)  # noqa: S101 - as above
            if label is not None or description is not None:
                raise UnreviewableWordingError(
                    f"issue topic {subject_id!r} has a name, not a label and description"
                )
            proposed = self._topic_proposal(subject, name=name)

        stored = self._append(
            subject_table=subject_table,
            subject_id=subject_id,
            subject_version=version,
            reviewer_id=author_id,
            decision="proposed",
            reason=None,
            proposed=proposed,
        )
        logger.info(
            "panel review: %s proposed for %s %r at version %d",
            author_id,
            subject_table,
            subject_id,
            version,
        )
        return self._as_proposal(stored, ledger=self._read_all())

    def decide(
        self,
        *,
        proposal_id: str,
        reviewer_id: str,
        decision: ReviewDecision,
        reason: str | None = None,
    ) -> ReviewStatus:
        """Approve or reject a proposed wording change.

        Approving is the only path in this codebase that edits a live
        `issue_topics` or `stances` row's wording, and it does three things in
        one call, in this order:

          1. writes the ledger decision (so the record exists even if step 2
             fails — an approval nobody can see is worse than an unapplied
             one);
          2. upserts the subject row with the new wording and the bumped
             version;
          3. records the bumped version's own approval, so the gate passes for
             the version the row is now at rather than the one it was at when
             the reviewer read it.

        Step 3 is not ceremony. The gate checks "is there an in-force approval
        for the version this row carries", so without it an approved edit
        would bump the row to version 2 and immediately fail its own gate.

        Not atomic — nothing in ZeroDB is (see `app.repositories.base`). A
        crash between steps 1 and 2 leaves a recorded approval of wording that
        was never applied, which is the recoverable direction: the proposal is
        still in the ledger, the row still carries its previously approved
        wording, and re-deciding is idempotent because every id is
        content-addressed.

        Raises:
            UnknownProposalError: no proposal under that id.
            SelfReviewError: the reviewer is the proposal's author.
            VersionMismatchError: the row has moved since the proposal.
        """
        ledger = self._read_all()
        proposal = next((row for row in ledger if str(row.id) == proposal_id), None)
        if proposal is None or proposal.decision != "proposed":
            raise UnknownProposalError(
                f"no wording proposal is stored under {proposal_id!r}; a decision about "
                "wording already live goes through record_decision() instead"
            )

        if reviewer_id == proposal.reviewer_id:
            raise SelfReviewError(
                f"{reviewer_id!r} proposed this wording for {proposal.subject_id!r} and "
                "cannot also approve it; prd.md requires a cross-partisan review, and a "
                "proposal passed by its own author has had no review at all"
            )

        subject = self._require_subject(proposal.subject_table, proposal.subject_id)
        stored_version = _version_of(subject)
        if stored_version != proposal.subject_version:
            raise VersionMismatchError(
                f"{proposal.subject_table} row {proposal.subject_id!r} has moved to version "
                f"{stored_version} since this was proposed against version "
                f"{proposal.subject_version}; the proposal has to be rewritten against the "
                "wording that is actually live"
            )

        # Step 1: the decision, recorded before anything is applied. An
        # approval nobody can see is worse than one that has not landed yet.
        self._append(
            subject_table=proposal.subject_table,
            subject_id=proposal.subject_id,
            subject_version=proposal.subject_version,
            reviewer_id=reviewer_id,
            decision=decision,
            reason=reason,
            proposed={
                "proposed_label": proposal.proposed_label,
                "proposed_description": proposal.proposed_description,
                "proposed_name": proposal.proposed_name,
                "previous_label": proposal.previous_label,
                "previous_description": proposal.previous_description,
                "previous_name": proposal.previous_name,
            },
            proposal_id=proposal_id,
        )

        if decision != "approved":
            logger.info(
                "panel review: %s %s the proposal for %s %r; the live wording is unchanged",
                reviewer_id,
                decision,
                proposal.subject_table,
                proposal.subject_id,
            )
            return self.status_for(
                subject_table=proposal.subject_table, subject_id=proposal.subject_id
            )

        # Step 2: apply the wording and bump the version. The id is unchanged
        # — see `Stance`'s docstring for why a wording version is a field
        # rather than part of the id: a voter's encrypted profile stores
        # `stance_id`, and re-wording a card under a new id would orphan it.
        next_version = proposal.subject_version + 1
        if proposal.subject_table == "stances":
            assert isinstance(subject, Stance)  # noqa: S101 - table decides the type
            updated = subject.model_copy(
                update={
                    "label": proposal.proposed_label or subject.label,
                    "description": proposal.proposed_description or subject.description,
                    "wording_version": next_version,
                }
            )
            self._stances.upsert(updated.row_data())
        else:
            assert isinstance(subject, IssueTopic)  # noqa: S101 - as above
            updated = subject.model_copy(
                update={
                    "name": proposal.proposed_name or subject.name,
                    "taxonomy_version": next_version,
                }
            )
            self._topics.upsert(updated.row_data())

        # Step 3: approve the version the row is NOW at. Without this the row
        # would bump past its own approval and fail the gate it just passed.
        self._append(
            subject_table=proposal.subject_table,
            subject_id=proposal.subject_id,
            subject_version=next_version,
            reviewer_id=reviewer_id,
            decision="approved",
            reason=reason,
            proposed={
                "proposed_label": proposal.proposed_label,
                "proposed_description": proposal.proposed_description,
                "proposed_name": proposal.proposed_name,
                "previous_label": proposal.previous_label,
                "previous_description": proposal.previous_description,
                "previous_name": proposal.previous_name,
            },
            proposal_id=proposal_id,
        )

        logger.info(
            "panel review: %s approved the wording change to %s %r, now at version %d",
            reviewer_id,
            proposal.subject_table,
            proposal.subject_id,
            next_version,
        )
        return self.status_for(subject_table=proposal.subject_table, subject_id=proposal.subject_id)

    def withdraw(self, *, proposal_id: str, author_id: str) -> ReviewStatus:
        """Pull one's own proposal before anyone has reviewed it.

        Recorded rather than deleted, because the ledger is append-only and
        prd.md's "wording changes are versioned and public" covers a change
        that was floated and dropped as much as one that shipped.

        Only the author may withdraw: a reviewer who wants a proposal gone
        rejects it with a reason, which is a different and more useful record.
        """
        ledger = self._read_all()
        proposal = next((row for row in ledger if str(row.id) == proposal_id), None)
        if proposal is None or proposal.decision != "proposed":
            raise UnknownProposalError(f"no wording proposal is stored under {proposal_id!r}")
        if author_id != proposal.reviewer_id:
            raise SelfReviewError(
                f"{author_id!r} did not propose this wording and cannot withdraw it; "
                "a reviewer who wants it gone rejects it with a reason instead"
            )
        self._append(
            subject_table=proposal.subject_table,
            subject_id=proposal.subject_id,
            subject_version=proposal.subject_version,
            reviewer_id=author_id,
            decision="withdrawn",
            reason="Withdrawn by its author before review.",
            proposal_id=proposal_id,
        )
        return self.status_for(subject_table=proposal.subject_table, subject_id=proposal.subject_id)

    # =====================================================================
    # Reading the ledger
    # =====================================================================

    def status_for(self, *, subject_table: ReviewSubjectTable, subject_id: str) -> ReviewStatus:
        """What the ledger says about one row right now.

        "Right now" means: the newest decision naming the version the row
        actually carries. Two filters, and both matter.

        **Only the row's current version counts.** An approval of version 1
        says nothing about version 2 — that is what a version bump is *for* —
        so a decision at a stale version is history, not status.

        **A proposal is not a status.** `proposed` and `withdrawn` entries are
        skipped when deriving status: a pending proposal does not change
        whether the wording currently stored may be served, and treating it as
        the status would make proposing a change un-publish the live row.

        Returns a `ReviewStatus` with `decision=None` for a row nobody has
        decided anything about — which is the unpublishable default, and is
        why the gate works on a fresh project with an empty ledger.
        """
        subject = self._require_subject(subject_table, subject_id)
        version = _version_of(subject)
        in_force = _in_force(self._read_all(), subject_table, subject_id, version)
        if in_force is None:
            return ReviewStatus(
                subject_table=subject_table, subject_id=subject_id, subject_version=version
            )
        return ReviewStatus(
            subject_table=subject_table,
            subject_id=subject_id,
            subject_version=version,
            decision=in_force.decision,
            reviewer_id=in_force.reviewer_id,
            decided_at=in_force.decided_at,
            reason=in_force.reason,
        )

    def subject(self, *, subject_table: ReviewSubjectTable, subject_id: str) -> IssueTopic | Stance:
        """The reviewed row itself, whatever its review status.

        Public because a reviewer has to be able to read the wording they are
        judging, which by definition has not been approved yet — so the gated
        reads cannot serve this purpose and the router needs an ungated way to
        report the row a decision just landed on.

        Raises:
            UnknownSubjectError: no such row.
        """
        return self._require_subject(subject_table, subject_id)

    def history_for(
        self, *, subject_table: ReviewSubjectTable, subject_id: str
    ) -> list[PanelReview]:
        """Every decision ever recorded about one row, newest first.

        Story 3.3's third acceptance criterion. Unfiltered by version and
        unfiltered by decision kind: a proposal that was withdrawn, a
        rejection with its reason, the grandfathering mark and every approval
        are all part of the record prd.md promises is public.

        Newest first because that is the order a reviewer reads it in — "what
        happened to this card most recently" is the question — and because the
        in-force decision is then the first entry a reader sees for the current
        version.

        Ties on `decided_at` (two decisions in the same second, which the
        one-second timestamp resolution makes reachable) are broken by id, so
        the order is total and the same on every call rather than depending on
        ZeroDB's unspecified row order (datamodel.md "Open questions").

        An unreviewed row returns `[]` rather than raising: "nothing has
        happened to this yet" is a real, renderable answer.
        """
        rows = [
            row
            for row in self._read_all()
            if row.subject_table == subject_table and row.subject_id == subject_id
        ]
        return sorted(rows, key=lambda row: (row.decided_at, str(row.id)), reverse=True)

    def queue(self) -> list[QueueEntry]:
        """Every row no panel member has approved at its current version.

        The standing review debt, made countable. Three kinds of row are in
        here and the caller can tell them apart by `current_decision`:

          * `grandfathered` — the 281 rows stories 3.1 and 3.2 published
            before this workflow existed. The debt this story is honest about
            rather than hiding.
          * `rejected` — wording a reviewer refused and nobody has rewritten.
          * `None` — new wording nobody has looked at. The most urgent, since
            it is also invisible to voters until someone does.

        A `grandfathered` row that a real panel member later approves drops out
        of this list with nothing deleted, because `is_panel_approved` is false
        for the backfill's `reviewer_id` and true for a person's.

        Ordered by table then id so two runs produce the same list — a queue
        that reshuffled between page loads would be unusable, and ZeroDB's row
        order is explicitly unspecified.
        """
        ledger = self._read_all()
        entries: list[QueueEntry] = []
        for table in ("issue_topics", "stances"):
            for row in self._read_subjects(table):  # type: ignore[arg-type]
                row_id = str(row.id)
                version = _version_of(row)
                in_force = _in_force(ledger, table, row_id, version)
                approved_by_a_person = (
                    in_force is not None
                    and in_force.decision == "approved"
                    and in_force.reviewer_id != BACKFILL_REVIEWER_ID
                )
                if approved_by_a_person:
                    continue
                entries.append(
                    QueueEntry(
                        subject_table=table,  # type: ignore[arg-type]
                        subject_id=row_id,
                        subject_version=version,
                        current_decision=None if in_force is None else in_force.decision,
                        wording=row.label if isinstance(row, Stance) else row.name,
                    )
                )
        return sorted(entries, key=lambda entry: (entry.subject_table, entry.subject_id))

    def proposals_awaiting_review(self) -> list[WordingProposal]:
        """Every proposal nobody has decided on yet, oldest first.

        Oldest first, unlike `history_for`: this is a work queue, and the
        wording that has been waiting longest is the next thing to read.
        """
        ledger = self._read_all()
        pending = [
            self._as_proposal(row, ledger=ledger) for row in ledger if row.decision == "proposed"
        ]
        return sorted(
            (proposal for proposal in pending if not proposal.decided),
            key=lambda proposal: (proposal.proposed_at, proposal.proposal_id),
        )

    # =====================================================================
    # Gated reads — "unapproved wording cannot be published"
    # =====================================================================

    def approved_stances_for_topic(self, topic_id: str) -> list[Stance]:
        """Only the stance cards the ledger lets through, for one subtopic.

        Story 3.3's first acceptance criterion: unapproved wording cannot be
        published. A card is served only if the ledger carries an in-force
        `approved` or `grandfathered` decision naming the exact
        `wording_version` the row is at.

        **Not shuffled**, unlike `StanceService.for_topic`. Story 3.2's
        randomization is a property of the voter-facing read, and this is a
        gate a reviewer screen and the voter-facing read both sit behind;
        shuffling here would mean a caller that wants a stable order for a
        review table cannot have one, and the voter-facing path can shuffle
        what it gets back. See the module docstring for why this is a separate
        path rather than a filter inside `StanceService`.

        Returns `[]` for an unknown or top-level topic, as `StanceService`
        does, and — importantly — for a subtopic whose every card is
        unreviewed. An empty menu is the correct, visible consequence of
        unreviewed wording, not an error.
        """
        ledger = self._read_all()
        cards = [row for row in self._stances.query(limit=PAGE_SIZE) if row.topic_id == topic_id]
        return [
            card
            for card in cards
            if _publishable(ledger, "stances", str(card.id), card.wording_version)
        ]

    def approved_topic_tree(self, *, locale: str, taxonomy_version: int) -> TopicTreeResponse:
        """The topic tree with unreviewed topics withheld.

        The same gate as `approved_stances_for_topic`, applied to the taxonomy,
        and ordered alphabetically for the same reason `TopicService.tree`
        orders alphabetically: any other order encodes a judgement about which
        issues matter most.

        A top-level topic whose own wording is unreviewed is dropped **with its
        subtopics**, approved or not: a subtopic rendered under no parent has
        nowhere to appear in the picker, and promoting it to the top level
        would be this service inventing taxonomy structure, which is story
        3.1's job and nobody else's.
        """
        ledger = self._read_all()
        rows = [
            row
            for row in self._topics.query(limit=PAGE_SIZE)
            if row.locale == locale and row.taxonomy_version == taxonomy_version
        ]
        publishable = [
            row
            for row in rows
            if _publishable(ledger, "issue_topics", str(row.id), row.taxonomy_version)
        ]
        published_ids = {str(row.id) for row in publishable}

        top_level = sorted(
            (row for row in publishable if row.parent_id is None), key=lambda row: row.name
        )
        children: dict[str, list[IssueTopic]] = {}
        for row in publishable:
            if row.parent_id is not None and row.parent_id in published_ids:
                children.setdefault(row.parent_id, []).append(row)

        return TopicTreeResponse(
            locale=locale,
            taxonomy_version=taxonomy_version,
            topics=[
                TopicTree(
                    id=str(parent.id),
                    name=parent.name,
                    subtopics=[
                        TopicNode(id=str(child.id), name=child.name)
                        for child in sorted(
                            children.get(str(parent.id), []), key=lambda row: row.name
                        )
                    ],
                )
                for parent in top_level
            ],
        )

    # =====================================================================
    # Internals
    # =====================================================================

    def _append(
        self,
        *,
        subject_table: ReviewSubjectTable,
        subject_id: str,
        subject_version: int,
        reviewer_id: str,
        decision: ReviewDecision,
        reason: str | None,
        proposed: dict[str, str | None] | None = None,
        proposal_id: str | None = None,
    ) -> PanelReview:
        """Write one ledger row under its content-addressed id.

        `Repository.upsert` rather than `append_version`, deliberately. The
        ledger IS append-only in the sense that matters — nothing is ever
        edited or removed — but it is append-only by *id scheme* rather than by
        `is_current` bookkeeping, exactly as `provenance_log` is (see
        `ProvenanceLog`'s docstring). The id is a hash of the decision's whole
        content, so:

          * writing the same decision twice lands on the same row (which is
            what makes the backfill re-runnable without appending 281
            duplicates);
          * two different decisions cannot collide;
          * there is no `is_current` flag to go stale, and no version number to
            compute from stored state — which `append_version` would need and
            would get wrong here, since several decisions about one row at one
            version are all simultaneously true history.
        """
        self._pace()
        fields = proposed or {}
        row_id = review_id(
            subject_table=subject_table,
            subject_id=subject_id,
            subject_version=subject_version,
            reviewer_id=reviewer_id,
            decision=decision,
            reason=reason,
            proposed=fields,
        )
        return self._reviews.upsert(
            {
                "id": row_id,
                "subject_table": subject_table,
                "subject_id": subject_id,
                "subject_version": subject_version,
                "decision": decision,
                "reviewer_id": reviewer_id,
                "decided_at": _now(),
                "reason": reason,
                "proposal_id": proposal_id,
                "proposed_label": fields.get("proposed_label"),
                "proposed_description": fields.get("proposed_description"),
                "proposed_name": fields.get("proposed_name"),
                "previous_label": fields.get("previous_label"),
                "previous_description": fields.get("previous_description"),
                "previous_name": fields.get("previous_name"),
            }
        )

    def _require_subject(
        self, subject_table: ReviewSubjectTable, subject_id: str
    ) -> IssueTopic | Stance:
        """The row being judged, or raise.

        datamodel.md rule 5 for a reference the generic check cannot express:
        `subject_id` names a row in whichever table `subject_table` says, so
        `PanelReview.references` is empty and this resolves it instead — the
        same split `ProvenanceLog` documents for `entity_table`/`entity_id`.

        Refusing is the point: a decision about a row that does not exist would
        sit in the ledger forever, approving nothing, and a typo in a row id
        would present as "approved" in a report while the real row stayed
        unreviewed.
        """
        row: IssueTopic | Stance | None
        if subject_table == "stances":
            row = self._stances.get(subject_id)
        else:
            row = self._topics.get(subject_id)
        if row is None:
            raise UnknownSubjectError(
                f"no {subject_table} row carries the id {subject_id!r}, so there is no "
                "wording to review"
            )
        return row

    def _read_all(self) -> list[PanelReview]:
        """The whole ledger, one unfiltered page.

        Unfiltered for the reason every read in this codebase is: live ZeroDB's
        server-side filter matches nothing (story 1.4). Logged loudly rather
        than silently truncated at the page limit, because a truncated ledger
        read makes an approved row look unreviewed and drops it out of a gated
        response — a silent un-publishing.
        """
        rows = self._reviews.query(limit=PAGE_SIZE)
        if len(rows) >= PAGE_SIZE:
            logger.error(
                "the panel_reviews read returned a full page of %d rows; the ledger has "
                "outgrown PAGE_SIZE and approved wording may now be withheld from gated "
                "reads",
                len(rows),
            )
        return rows

    def _read_subjects(self, subject_table: ReviewSubjectTable) -> list[IssueTopic] | list[Stance]:
        """Every row of one reviewable table, one unfiltered page."""
        if subject_table == "stances":
            return self._stances.query(limit=PAGE_SIZE)
        return self._topics.query(limit=PAGE_SIZE)

    def _stance_proposal(
        self, subject: Stance, *, label: str | None, description: str | None
    ) -> dict[str, str | None]:
        """Validate a proposed stance edit and pair it with what it replaces."""
        if label is None and description is None:
            raise EmptyProposalError(
                f"a proposal for stance {subject.id!r} must change its label, its "
                "description, or both"
            )
        if label == subject.label and description is None:
            raise EmptyProposalError(
                f"the proposed label for stance {subject.id!r} is the label it already has"
            )
        if description == subject.description and label is None:
            raise EmptyProposalError(
                f"the proposed description for stance {subject.id!r} is the one it already has"
            )
        if label == subject.label and description == subject.description:
            raise EmptyProposalError(f"the proposal for stance {subject.id!r} changes nothing")
        for text, what in ((label, "label"), (description, "description")):
            if text is None:
                continue
            if not text.strip():
                raise EmptyProposalError(f"a stance {what} cannot be blank")
            # Story 3.2's own guard, called rather than reimplemented. A
            # frozenset lookup rejecting this is a reviewer's hour saved.
            named_side = contains_partisan_identity_word(text)
            if named_side is not None:
                raise UnreviewableWordingError(
                    f"the proposed {what} names a political side ({named_side!r}); a stance "
                    "card states an outcome, and a voter who holds the position but rejects "
                    "the badge has been pushed off their own view (app.seed_stances, rule 3)"
                )
        return {
            "proposed_label": label,
            "proposed_description": description,
            "proposed_name": None,
            "previous_label": subject.label if label is not None else None,
            "previous_description": subject.description if description is not None else None,
            "previous_name": None,
        }

    def _topic_proposal(self, subject: IssueTopic, *, name: str | None) -> dict[str, str | None]:
        """Validate a proposed topic rename and pair it with what it replaces."""
        if name is None:
            raise EmptyProposalError(
                f"a proposal for issue topic {subject.id!r} must change its name"
            )
        if not name.strip():
            raise EmptyProposalError("an issue topic name cannot be blank")
        if name == subject.name:
            raise EmptyProposalError(
                f"the proposed name for issue topic {subject.id!r} is the name it already has"
            )
        # Story 3.1's own guard, called rather than reimplemented.
        presupposed = contains_stance_presupposing_word(name)
        if presupposed is not None:
            raise UnreviewableWordingError(
                f"the proposed name presupposes a stance ({presupposed!r}); a topic label "
                "names a policy domain or a measurable quantity, never a desired direction "
                "(app.seed_taxonomy, 'nonpartisan by construction')"
            )
        return {
            "proposed_label": None,
            "proposed_description": None,
            "proposed_name": name,
            "previous_label": None,
            "previous_description": None,
            "previous_name": subject.name,
        }

    def _as_proposal(self, row: PanelReview, *, ledger: list[PanelReview]) -> WordingProposal:
        """One ledger row read back as a proposal, with whether it was decided.

        `decided` is derived from the ledger rather than stored on the proposal
        row: the row is never edited (the ledger is append-only), so "has
        anyone decided on this" is a question about what *else* is in the
        ledger — specifically, whether any decision cites this `proposal_id`.
        """
        row_id = str(row.id)
        decided = any(
            other.proposal_id == row_id and other.decision != "proposed" for other in ledger
        )
        return WordingProposal(
            proposal_id=row_id,
            subject_table=row.subject_table,
            subject_id=row.subject_id,
            subject_version=row.subject_version,
            author_id=row.reviewer_id,
            proposed_at=row.decided_at,
            label=row.proposed_label,
            description=row.proposed_description,
            name=row.proposed_name,
            previous_label=row.previous_label,
            previous_description=row.previous_description,
            previous_name=row.previous_name,
            decided=decided,
        )


def _version_of(row: IssueTopic | Stance) -> int:
    """The version a row's wording is published at.

    `wording_version` for a stance, `taxonomy_version` for a topic. One
    function rather than two call sites choosing, because the gate compares
    this number against a decision's `subject_version` in four places and a
    mismatch between two spellings of "the version" would publish unreviewed
    wording silently.
    """
    if isinstance(row, Stance):
        return row.wording_version
    return row.taxonomy_version


#: Ledger entries that are not a judgement on stored wording, and so never
#: become a row's status. A pending proposal must not un-publish the live
#: wording it proposes to replace, and a withdrawn one must not either.
_NON_STATUS_DECISIONS: frozenset[str] = frozenset({"proposed", "withdrawn"})


def _in_force(
    ledger: list[PanelReview], subject_table: str, subject_id: str, version: int
) -> PanelReview | None:
    """The decision currently governing one row at one version, or None.

    Three filters, applied in this order, and each one is load-bearing:

      1. **this row** — table and id both, since a stance id extends its
         topic's id and a prefix match would let a topic's approval publish a
         card;
      2. **this version** — a decision about version 1 governs version 1. This
         is what makes a version bump re-open the question automatically
         instead of carrying an old approval forward, and it is the single
         mechanism behind "every edit bumps `wording_version`" having any
         teeth;
      3. **a judgement, not a proposal** — see `_NON_STATUS_DECISIONS`.

    The newest survivor wins, ties broken by id so the answer is total and
    identical on every call: ZeroDB's row order is explicitly unspecified
    (datamodel.md "Open questions"), so a tie resolved by read order would
    make a row publishable on one request and not the next.

    Why newest-wins rather than, say, rejection-wins: the panel is a group,
    and a later reviewer looking at the same wording is the group changing its
    mind, which is the thing a ledger is for. The earlier decision stays
    readable in `history_for` — nothing is lost, and the scenario
    "reviewer B approves what reviewer A rejected" has a defined outcome
    rather than depending on which row ZeroDB hands back first.
    """
    candidates = [
        row
        for row in ledger
        if row.subject_table == subject_table
        and row.subject_id == subject_id
        and row.subject_version == version
        and row.decision not in _NON_STATUS_DECISIONS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row.decided_at, str(row.id)))


def _publishable(
    ledger: list[PanelReview], subject_table: str, subject_id: str, version: int
) -> bool:
    """Whether a gated read may serve this row at this version.

    The gate itself, in one expression. A row with no in-force decision is not
    publishable — which is what makes the gate fail *closed* for new wording on
    an empty ledger, and is exactly the behaviour story 3.3's first acceptance
    criterion asks for.
    """
    in_force = _in_force(ledger, subject_table, subject_id, version)
    return in_force is not None and in_force.decision in PUBLISHABLE_DECISIONS


__all__ = [
    "BACKFILL_REVIEWER_ID",
    "PAGE_SIZE",
    "PUBLISHABLE_DECISIONS",
    "EmptyProposalError",
    "GrandfatherReport",
    "PanelReviewError",
    "PanelReviewService",
    "QueueEntry",
    "ReviewStatus",
    "SelfReviewError",
    "UnknownProposalError",
    "UnknownSubjectError",
    "UnreviewableWordingError",
    "VersionMismatchError",
    "WordingProposal",
    "contains_partisan_identity_word",
    "contains_stance_presupposing_word",
    "review_id",
]
