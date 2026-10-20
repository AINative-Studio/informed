/**
 * The candidate detail page: the score broken down interest by interest
 * (story 7.3).
 *
 * "As a voter, I want a candidate page that breaks the score down interest
 * by interest, so that I see where we agree and disagree." Acceptance: one
 * row per interest, with direction, evidence count and links.
 *
 * Reads `GET /v1/candidates/{id}/alignment?profile_id=` (stories 6.4-6.7)
 * through `src/api/alignment.ts` and renders one row per `AlignmentReason` —
 * prd.md's Feature 3 "comparison view" language, applied to one candidate:
 * "one row per interest, colored by direction, with 'why' links to
 * evidence."
 *
 * No router in this app (see `App.tsx`'s own docstring on why bringing one
 * in is a later story's call, not this one's): this component takes
 * `candidacyId` and `profileId` as plain props, same posture as every other
 * screen here, and whatever renders it decides how a voter gets to it — see
 * `BallotView.tsx`'s "View details" button.
 *
 * What a row calls the interest
 * ==============================
 * The alignment response names an interest only by its `stance_id` — an id
 * the voter chose (`app.scoring.models`'s own docstring: "No interest,
 * stance wording, tier, intensity or note from the voter's profile" ever
 * leaves that endpoint). There is no endpoint today that resolves a bare
 * stance id back to its human label outside the topic-scoped
 * `/v1/topics/{topic_id}/stances` lookup `QuickOnboarding` uses while a
 * voter is choosing one — resolving every reason's id would mean a second
 * round trip per row with no cache key this component can build safely.
 * So each row shows the id itself, honestly, the same posture
 * `BallotView.tsx`'s `positions_summary` rendering already takes with the
 * same field. Giving a row a readable name is a real improvement for a
 * later story to make once that lookup exists, not a gap this one should
 * paper over by inventing a label.
 *
 * Direction, in the terms this story's acceptance criterion actually asks
 * for
 * ===============================================================
 * `AlignmentReason.direction` ("raised"/"lowered") says which way the
 * reason moved the OVERALL SCORE, not whether the candidate's record
 * agrees with the voter's stated stance — those two happen to coincide for
 * every voter-chosen interest (a stance a voter picked is, by construction,
 * the position they want; `s_value` is the candidate's own measured
 * position on the same scale), so "raised" and `s_value > 0` always point
 * the same way for a reason that actually appears in this list. This
 * component reads the sign of `s_value` directly (never `direction`) to
 * decide "agrees"/"disagrees"/"no clear record" — the plainer, more literal
 * reading of prd.md's "colored by direction" for a single candidate's own
 * row, and the one `s_value`'s own documented range (-1 acts against, +1
 * acts for) states outright.
 *
 * Links: real citations, derived the same way the backend derives its own
 * ============================================================================
 * `citations` is structurally non-empty on the backend (see
 * `app.scoring.models.AlignmentReason`'s docstring), but `Citation` itself
 * carries no ready-made URL — only a `roll_call_id` and, when the vote
 * attached to one, a `bill_id`. Rather than invent a format, `billUrlFor`
 * below derives the real, public, keyless `www.congress.gov/bill/...` page
 * from `bill_id`'s own documented shape (`{body}:{congress}:{type}-{number}`,
 * `backend/app/ingest/house_bills.bill_id`) — the exact derivation the
 * backend's own `house_rollcalls.roll_call_item_url` uses for a sibling URL,
 * justified there because the source's `legislationUrl` field (confirmed
 * live, `house_rollcalls.py`) points at this same `congress.gov/bill/...`
 * path. A citation with no `bill_id` (a procedural or nomination vote) has
 * no bill page to derive, and `roll_calls.source_url` is a backend field no
 * endpoint publishes yet — that case names the roll call as plain text
 * instead of a fabricated link, matching prd.md's "evidence over opinion"
 * in the direction that matters here: it would rather show nothing
 * clickable than something that looks like a link and goes nowhere real.
 *
 * The evidence drawer (story 7.5)
 * ================================
 * "As a voter, I want to open any vote or promise behind a score and see
 * the source, so that I can verify it." A citation with a bill attached
 * (see above) is now a button that opens a `Drawer` (`src/ui/Drawer.tsx`)
 * showing the full roll-call detail: the roll-call number and date it
 * already names in its link text, plus `position` and `vote_type` (both
 * already on `Citation` but previously never shown), a link to the bill,
 * and — new in this story — the real archived copy of that bill's text via
 * story 5.4's `GET /v1/bills/{bill_id}/text`, fetched only once the drawer
 * actually opens (same lazy-fetch posture `billText.ts`'s own docstring
 * requires: the presigned URL it returns is short-lived, so it must not be
 * fetched speculatively on render).
 *
 * `roll_calls.question` (datamodel.md) is a real backend field, but no
 * endpoint publishes it — `Citation`'s `extra="forbid"` means the backend
 * response literally cannot carry it, and adding it would be new backend
 * work this story's brief says is likely unnecessary. Rather than fabricate
 * a "question" sentence from nothing, the drawer shows `vote_type` (what
 * *kind* of vote this was) in its place, named plainly as that — the same
 * "show what exists, not what doesn't" posture `CitationLink` already takes
 * for a citation with no bill page.
 *
 * A citation with no `bill_id` has nothing for this drawer to add beyond
 * what `CitationLink` already renders as plain text (no bill page, so no
 * archived bill text either) — so it gets no drawer control at all, rather
 * than a drawer that opens onto mostly empty space.
 */
import { useQuery } from '@tanstack/react-query';
import { useId, useRef, useState } from 'react';

import { type AlignmentReason, type Citation, fetchAlignment } from '../api/alignment';
import { type BillText, BillTextError, fetchBillText, versionLabelFor } from '../api/billText';
import { Drawer } from '../ui/Drawer';

export function CandidateDetailPage({
  candidacyId,
  profileId,
}: {
  candidacyId: string;
  profileId: string;
}) {
  const query = useQuery({
    queryKey: ['alignment', candidacyId, profileId],
    queryFn: ({ signal }) => fetchAlignment({ candidacyId, profileId }, signal),
  });

  return (
    // `aria-label` alone, not `aria-labelledby`: when both are present on one
    // element, `aria-labelledby` wins per the ARIA spec, which would silently
    // make this region's accessible name the heading text below rather than
    // "Candidate detail" — exactly the kind of mismatch between what a test
    // (and a screen reader) is told to look for and what is actually there
    // that BDD exists to catch.
    <section aria-label="Candidate detail">
      <h2>How this candidate matches your priorities</h2>

      {query.isPending ? <p>Loading this candidate&apos;s score breakdown…</p> : null}
      {query.isError ? <p role="alert">{messageFrom(query.error)}</p> : null}
      {query.isSuccess ? <AlignmentBreakdown alignment={query.data} /> : null}
    </section>
  );
}

function AlignmentBreakdown({
  alignment,
}: {
  alignment: { reasons: AlignmentReason[] };
}) {
  const headingId = useId();

  if (alignment.reasons.length === 0) {
    return (
      <p>
        Nothing measured yet for this candidate against your priorities. Check back once more
        votes or positions have been recorded.
      </p>
    );
  }

  return (
    <>
      <h3 id={headingId}>Interest-by-interest breakdown</h3>
      <ul aria-labelledby={headingId}>
        {alignment.reasons.map((reason) => (
          <li key={reason.stance_id}>
            <ReasonRow reason={reason} />
          </li>
        ))}
      </ul>
    </>
  );
}

/** One row: the interest, its direction, its evidence count and its links. */
function ReasonRow({ reason }: { reason: AlignmentReason }) {
  return (
    <>
      <span>{reason.stance_id}</span>
      {' — '}
      <DirectionLabel sValue={reason.s_value} />
      {' · '}
      <span>
        {reason.evidence_count} {reason.evidence_count === 1 ? 'piece' : 'pieces'} of evidence
      </span>
      {' · '}
      <CitationLinks citations={reason.citations} />
    </>
  );
}

/**
 * Agrees / disagrees / no clear record, from the sign of `s_value`.
 *
 * `s_value` is documented as -1 (acted against the voter's stance) to +1
 * (acted for it). Zero is a real, if rare, measured-neutral outcome and is
 * reported as neither — see `app.scoring.formula`'s own distinction between
 * "measured, found neutral" and "not measured at all" for why collapsing a
 * true zero into "agrees" would overstate the record.
 */
function DirectionLabel({ sValue }: { sValue: number }) {
  if (sValue > 0) return <strong>Agrees with your stance</strong>;
  if (sValue < 0) return <strong>Disagrees with your stance</strong>;
  return <strong>Measured as neutral on this stance</strong>;
}

/** The citations for one row, each a real link when a bill page exists for it. */
function CitationLinks({ citations }: { citations: Citation[] }) {
  return (
    <>
      {citations.map((citation, index) => (
        <span key={citation.roll_call_id}>
          {index > 0 ? ', ' : ''}
          <CitationLink citation={citation} />
        </span>
      ))}
    </>
  );
}

function CitationLink({ citation }: { citation: Citation }) {
  const url = citation.bill_id ? billUrlFor(citation.bill_id) : null;
  const openerRef = useRef<HTMLButtonElement>(null);
  const [isOpen, setIsOpen] = useState(false);

  if (!url) {
    // No bill attached (a procedural or nomination vote) — no
    // backend-published browsable resource exists for the bare roll call,
    // and so nothing for a drawer to add beyond this line. See this
    // module's docstring: shown as text, never a fabricated link or an
    // empty drawer.
    return <span>Roll call {citation.roll_call_id} (no bill page available)</span>;
  }

  return (
    <>
      <button type="button" ref={openerRef} onClick={() => setIsOpen(true)}>
        Roll call {citation.roll_call_id}
      </button>
      <Drawer
        isOpen={isOpen}
        onClose={() => setIsOpen(false)}
        label={`Evidence for roll call ${citation.roll_call_id}`}
        returnFocusTo={openerRef}
      >
        <EvidenceDrawerContent citation={citation} billUrl={url} />
      </Drawer>
    </>
  );
}

/**
 * The drawer's body: everything story 7.5's acceptance criterion asks for —
 * roll-call number, date, question, position, bill link and archived
 * source — using the field this backend actually publishes in place of
 * "question" (see this module's docstring).
 */
function EvidenceDrawerContent({ citation, billUrl }: { citation: Citation; billUrl: string }) {
  const headingId = useId();
  const billTextQuery = useQuery({
    queryKey: ['bill-text', citation.bill_id],
    queryFn: ({ signal }) => fetchBillText(citation.bill_id as string, signal),
  });

  return (
    <section aria-labelledby={headingId}>
      <h3 id={headingId}>Roll call {citation.roll_call_id}</h3>
      <dl>
        <dt>Date</dt>
        <dd>{formatDate(citation.voted_at)}</dd>
        <dt>Kind of vote</dt>
        <dd>{voteTypeLabel(citation.vote_type)}</dd>
        <dt>This candidate&apos;s position</dt>
        <dd>{positionLabel(citation.position)}</dd>
        {citation.is_bundled ? (
          <>
            <dt>Bundled vote</dt>
            <dd>{bundledReasonLabel(citation.bundled_reason)}</dd>
          </>
        ) : null}
      </dl>
      <p>
        <a href={billUrl} target="_blank" rel="noreferrer">
          Open the bill
        </a>
      </p>
      <ArchivedSource query={billTextQuery} />
    </section>
  );
}

/** The archived copy of the bill's text — story 5.4's presigned URL, not the live source. */
function ArchivedSource({
  query,
}: {
  query: { isPending: boolean; isError: boolean; error: unknown; isSuccess: boolean; data?: BillText };
}) {
  if (query.isPending) return <p>Loading the archived copy of this bill&apos;s text…</p>;
  if (query.isError) {
    return (
      <p role="alert">
        {query.error instanceof BillTextError
          ? query.error.message
          : 'We could not open the archived bill text just now. Please try again.'}
      </p>
    );
  }
  if (!query.isSuccess || !query.data) return null;
  const text = query.data;
  const version = versionLabelFor(text);
  return (
    <p>
      <a href={text.url} target="_blank" rel="noreferrer">
        Open the archived source{version ? ` (${version})` : ''}
      </a>
    </p>
  );
}

function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
}

/** A readable label for `Citation.vote_type` — datamodel.md's enum, spelled out. */
function voteTypeLabel(voteType: string): string {
  const labels: Record<string, string> = {
    final_passage: 'Final passage',
    amendment: 'Amendment',
    cloture: 'Cloture',
    motion_to_recommit: 'Motion to recommit',
    procedural: 'Procedural',
    other: 'Other',
  };
  return labels[voteType] ?? voteType;
}

/** A readable label for `Citation.position` — datamodel.md's enum, spelled out. */
function positionLabel(position: string): string {
  const labels: Record<string, string> = {
    yea: 'Yea',
    nay: 'Nay',
    present: 'Present',
    not_voting: 'Not voting',
  };
  return labels[position] ?? position;
}

/** `bundled_reason`'s two documented codes, in plain language (datamodel.md). */
function bundledReasonLabel(reason: string | null): string {
  if (reason === 'omnibus_marker') return 'The measure itself was titled as an omnibus bill.';
  if (reason === 'measure_count_exceeds_threshold') {
    return 'This one vote disposed of several separate measures at once.';
  }
  return 'Marked as bundled.';
}

/**
 * The real, public, keyless `www.congress.gov/bill/...` page for a bill id.
 *
 * Derives it from `bill_id`'s own documented shape
 * (`{body}:{congress}:{type}-{number}`, `backend/app/ingest/house_bills.bill_id`
 * and `senate_bills`' sibling), the same way the backend derives
 * `house_rollcalls.roll_call_item_url` from a roll call's own congress/
 * session/number — not a guess, because the House source's own
 * `legislationUrl` field (confirmed live, `house_rollcalls.py`) points at
 * exactly this path shape for exactly this reason: to let a reader open the
 * bill a vote was on.
 *
 * Exported for direct unit testing (`CandidateDetailPage.test.tsx` covers it
 * indirectly through rendering; a malformed id returns `null` rather than a
 * broken link, since a citation whose `bill_id` does not match the
 * documented shape is a data problem to show honestly, not to link into a
 * 404).
 */
export function billUrlFor(billId: string): string | null {
  const match = /^[^:]+:(\d+):([a-z]+)-(\d+)$/i.exec(billId);
  if (!match) return null;
  const [, congress, billType, number] = match;
  return `https://www.congress.gov/bill/${congress}th-congress/${billTypeSlug(billType)}/${number}`;
}

/** congress.gov's own path segment for each bill-type prefix this codebase mints. */
function billTypeSlug(billType: string): string {
  const slugs: Record<string, string> = {
    hr: 'house-bill',
    s: 'senate-bill',
    hjres: 'house-joint-resolution',
    sjres: 'senate-joint-resolution',
    hconres: 'house-concurrent-resolution',
    sconres: 'senate-concurrent-resolution',
    hres: 'house-resolution',
    sres: 'senate-resolution',
  };
  return slugs[billType.toLowerCase()] ?? billType.toLowerCase();
}

function messageFrom(error: unknown): string {
  if (error instanceof Error && error.name === 'AlignmentError') return error.message;
  return 'We could not load this candidate’s score breakdown just now. Please try again.';
}
