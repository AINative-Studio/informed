/**
 * The side-by-side compare view (story 7.4).
 *
 * "As a voter, I want to compare candidates in a race side by side, so that
 * I can see the differences on what matters to me." Acceptance: supports
 * 2-4 candidates; same rows across candidates; colors carry text labels too.
 *
 * Reads the same `GET /v1/candidates/{id}/alignment?profile_id=` endpoint
 * `CandidateDetailPage.tsx` (story 7.3) already reads for one candidate at a
 * time — one query per selected candidacy here, through the same
 * `fetchAlignment` client (`src/api/alignment.ts`) — and lays the results
 * out as a real `<table>`: one row per interest, one column per candidate.
 *
 * "Same rows across candidates" means the UNION of every selected
 * candidate's `stance_id`s, not the intersection: a stance only one
 * candidate has a measured position on is still a row, with every OTHER
 * candidate's cell in that row showing an honest "No measured position yet"
 * rather than being blank, a fabricated zero, or the row being dropped
 * entirely. This mirrors `CandidateDetailPage.tsx`'s own posture for a
 * single candidate with nothing measured — see its module docstring — just
 * applied per cell instead of to the whole page: "no evidence" and
 * "evidence that happens to be neutral" are different claims, and this view
 * never collapses one into the other.
 *
 * "Colors carry text labels too" (accessibility, not decoration)
 * ==================================================================
 * Every cell that encodes direction (agrees/disagrees/neutral) carries BOTH
 * a `data-direction` attribute (which CSS a later styling pass can hook for
 * color) AND the plain text label itself as real DOM text content — never
 * color or a bare icon alone. `DirectionCell` below is the one place this is
 * enforced: it always renders a `<span>` with the direction word, so a
 * colorblind voter or a screen reader gets the identical information a
 * sighted voter relying on color would. `CompareView.test.tsx` and
 * `side_by_side_compare.feature` both assert this directly: every element
 * carrying `data-direction` must have accompanying text content naming that
 * direction, not just a CSS class.
 *
 * Why a real `<table>`, not a styled grid of `<div>`s
 * ======================================================
 * A sighted voter scanning a comparison wants to read down a column and
 * across a row; a screen-reader user needs the same structure announced:
 * which candidate a cell belongs to, and which interest a row is about.
 * `<th scope="col">` per candidate column and `<th scope="row">` per
 * interest row give an assistive technology exactly that association for
 * free, which no amount of ARIA on a `<div>` grid replicates as reliably.
 *
 * Nonpartisan language (prd.md's non-negotiable; see `BallotView.tsx`'s own
 * `SortToggle`/story 7.6 precedent for the same discipline): this view shows
 * only the measured data — agreement direction, evidence count, a score
 * number when one exists — and never ranks, highlights a "best" column, or
 * uses words like "recommended", "best match" or "winner" anywhere in its
 * copy.
 *
 * No router in this app (see `App.tsx`'s own docstring): `BallotView.tsx`
 * decides when a voter has selected enough candidates to offer a "Compare"
 * control, and whatever screen owns navigation (`BallotPage.tsx`) decides
 * what renders when that control is used — this component just takes a
 * list of candidacy ids and a profile id, same posture every other screen
 * here already takes.
 */
import { useQueries } from '@tanstack/react-query';

import { type AlignmentReason, type CandidateAlignment, fetchAlignment } from '../api/alignment';

const MIN_CANDIDATES = 2;
const MAX_CANDIDATES = 4;

export function CompareView({
  candidacyIds,
  namesByCandidacyId,
  profileId,
}: {
  candidacyIds: string[];
  /** A readable name per candidacy id, for column headers. Falls back to the id itself if missing. */
  namesByCandidacyId: Record<string, string>;
  profileId: string;
}) {
  if (candidacyIds.length < MIN_CANDIDATES) {
    return (
      <section aria-label="Compare candidates">
        <p role="alert">Select at least two candidates to compare.</p>
      </section>
    );
  }
  if (candidacyIds.length > MAX_CANDIDATES) {
    return (
      <section aria-label="Compare candidates">
        <p role="alert">You can compare up to four candidates at a time.</p>
      </section>
    );
  }

  return (
    <section aria-label="Compare candidates">
      <h2>Compare candidates</h2>
      <p>This shows only the measured data for the candidates you selected, side by side.</p>
      <CompareTable candidacyIds={candidacyIds} namesByCandidacyId={namesByCandidacyId} profileId={profileId} />
    </section>
  );
}

function CompareTable({
  candidacyIds,
  namesByCandidacyId,
  profileId,
}: {
  candidacyIds: string[];
  namesByCandidacyId: Record<string, string>;
  profileId: string;
}) {
  const queries = useQueries({
    queries: candidacyIds.map((candidacyId) => ({
      queryKey: ['alignment', candidacyId, profileId],
      queryFn: ({ signal }: { signal: AbortSignal }) => fetchAlignment({ candidacyId, profileId }, signal),
    })),
  });

  if (queries.some((query) => query.isPending)) {
    return <p>Loading each candidate&apos;s score breakdown…</p>;
  }
  if (queries.every((query) => query.isError)) {
    return <p role="alert">We could not load these candidates&apos; score breakdowns just now. Please try again.</p>;
  }

  // Alignment data keyed by candidacy id, in the order the voter selected
  // them — a candidate whose own query failed simply has no entry, and
  // every row still renders an honest empty cell for them rather than the
  // whole table failing because one of several requests did not come back.
  const alignmentByCandidacy = new Map<string, CandidateAlignment>();
  queries.forEach((query, index) => {
    if (query.isSuccess && query.data) {
      alignmentByCandidacy.set(candidacyIds[index], query.data);
    }
  });

  const stanceIds = unionOfStanceIds(candidacyIds, alignmentByCandidacy);

  return (
    <table>
      <thead>
        <tr>
          <th scope="col">Interest</th>
          {candidacyIds.map((candidacyId) => (
            <th scope="col" key={candidacyId}>
              {namesByCandidacyId[candidacyId] ?? candidacyId}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {stanceIds.map((stanceId) => (
          <tr key={stanceId}>
            <th scope="row">{stanceId}</th>
            {candidacyIds.map((candidacyId) => (
              <td key={candidacyId}>
                <CompareCell reason={reasonFor(alignmentByCandidacy.get(candidacyId), stanceId)} />
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * The union of every selected candidate's `stance_id`s, in first-seen order
 * across the candidates in the order the voter selected them — so the row
 * order is stable and not arbitrary, but never ranks or reorders by score.
 */
function unionOfStanceIds(
  candidacyIds: string[],
  alignmentByCandidacy: Map<string, CandidateAlignment>,
): string[] {
  const seen = new Set<string>();
  const ordered: string[] = [];
  for (const candidacyId of candidacyIds) {
    const alignment = alignmentByCandidacy.get(candidacyId);
    for (const reason of alignment?.reasons ?? []) {
      if (!seen.has(reason.stance_id)) {
        seen.add(reason.stance_id);
        ordered.push(reason.stance_id);
      }
    }
  }
  return ordered;
}

function reasonFor(alignment: CandidateAlignment | undefined, stanceId: string): AlignmentReason | undefined {
  return alignment?.reasons.find((reason) => reason.stance_id === stanceId);
}

/**
 * One cell: a candidate's direction, evidence count and links for one
 * interest, or an honest "no measured position" when this candidate has
 * never been measured on this particular stance — never a blank cell and
 * never a fabricated zero (this module's own docstring).
 */
function CompareCell({ reason }: { reason: AlignmentReason | undefined }) {
  if (!reason) {
    return <span>No measured position yet.</span>;
  }
  return (
    <>
      <DirectionCell sValue={reason.s_value} />
      {' · '}
      <span>
        {reason.evidence_count} {reason.evidence_count === 1 ? 'piece' : 'pieces'} of evidence
      </span>
    </>
  );
}

/**
 * Agrees / disagrees / measured as neutral, from the sign of `s_value` —
 * the same reading `CandidateDetailPage.tsx`'s `DirectionLabel` uses for one
 * candidate at a time (see that component's own docstring on why the sign
 * of `s_value`, not `AlignmentReason.direction`, is the right read here).
 *
 * `data-direction` names the direction for any later CSS color-coding, but
 * the direction word itself is always real text content in the same
 * element — never color alone. This is the one piece of this view a test
 * can assert directly: an element with `data-direction` always contains its
 * own label as readable text.
 */
function DirectionCell({ sValue }: { sValue: number }) {
  if (sValue > 0) {
    return <span data-direction="agrees">Agrees</span>;
  }
  if (sValue < 0) {
    return <span data-direction="disagrees">Disagrees</span>;
  }
  return <span data-direction="neutral">Measured as neutral</span>;
}
