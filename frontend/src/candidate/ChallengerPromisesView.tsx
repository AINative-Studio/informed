/**
 * The challenger promises view: promises grouped by the voter's own
 * interests, with any prior office linked (story 9.6).
 *
 * "As a voter, I want to see a challenger's promises even though they have
 * no voting record, so that I can still compare them." Acceptance: promises
 * are grouped by my interests; a prior record in another office is linked
 * if one exists.
 *
 * Reads `GET /v1/candidates/{id}/promises?profile_id=` (story 9.6) through
 * `src/api/promises.ts` and renders one group per matching interest, exactly
 * the structure `CandidateDetailPage.tsx` (story 7.3) already established
 * for its own `AlignmentReason[]` — same `stance_id` grouping key, same
 * "show the id itself, honestly" posture for naming an interest (see that
 * component's module docstring for why no endpoint resolves a bare stance id
 * back to its wording outside onboarding, and why inventing a label here
 * would be worse than showing the id).
 *
 * This is deliberately a SEPARATE component from `CandidateDetailPage`
 * rather than a tab or a merge into it, for the reason
 * `backend/app/promises/models.py`'s module docstring gives on the backend
 * side: a promise is unscored evidence and a position is a scored one, and
 * folding them into one screen risks a voter reading a promise as if it
 * carried the same weight as a measured vote. Whatever page renders a
 * candidate decides when to show which — see `BallotPage.tsx`'s own
 * docstring on why there is no router in this app and navigation is plain
 * component state.
 *
 * No router here either, same posture as `CandidateDetailPage`: this
 * component takes `candidacyId` and `profileId` as plain props and lets its
 * caller decide how a voter gets to it.
 *
 * Why there is no score, confidence badge or ranking anywhere in this file
 * ====================================================================
 * `ChallengerPromises` carries none, by construction (see that model's own
 * docstring) — there is nothing to render that this component would have to
 * deliberately omit. prd.md's "never a single 'vote for X' output" and "no
 * ranking across candidates" hold here more by absence of ingredients than
 * by restraint.
 *
 * The evidence drawer, reused rather than rebuilt
 * =================================================
 * Each promise's citation (its source url and capture date) opens in the
 * same `Drawer` (`src/ui/Drawer.tsx`) `CandidateDetailPage`'s evidence
 * citations use (story 7.5) — the established focus-trapped, labeled modal
 * pattern, not a second one. There is no archived-bill-text fetch here the
 * way `CandidateDetailPage`'s drawer has: a promise's citation is its
 * campaign source, which this endpoint already returns inline
 * (`source_url`, `captured_at`) with no second round trip needed, unlike a
 * roll call's bill text (a separate presigned-URL endpoint, story 5.4).
 */
import { useId, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import {
  type ChallengerPromises,
  type PriorOffice,
  type PromiseGroup,
  type PromiseItem,
  fetchChallengerPromises,
} from '../api/promises';
import { Drawer } from '../ui/Drawer';

export function ChallengerPromisesView({
  candidacyId,
  profileId,
}: {
  candidacyId: string;
  profileId: string;
}) {
  const query = useQuery({
    queryKey: ['challenger-promises', candidacyId, profileId],
    queryFn: ({ signal }) => fetchChallengerPromises({ candidacyId, profileId }, signal),
  });

  return (
    // `aria-label` alone, not `aria-labelledby` — same reason
    // `CandidateDetailPage`'s own top-level section gives: both present on
    // one element would make `aria-labelledby` win per the ARIA spec, which
    // would silently point this region's accessible name at the heading text
    // below instead.
    <section aria-label="Challenger promises">
      <h2>What this candidate has promised</h2>
      <p>
        This candidate has no voting record yet. Here is what they have said they would do,
        grouped by the priorities you chose.
      </p>

      {query.isPending ? <p>Loading this candidate&apos;s promises…</p> : null}
      {query.isError ? <p role="alert">{messageFrom(query.error)}</p> : null}
      {query.isSuccess ? <PromisesBody promises={query.data} /> : null}
    </section>
  );
}

function PromisesBody({ promises }: { promises: ChallengerPromises }) {
  return (
    <>
      <PriorOfficeNotice offices={promises.prior_offices} />
      <PromiseGroups groups={promises.groups} unmatchedCount={promises.unmatched_count} />
    </>
  );
}

/**
 * The "prior record in another office" half of the acceptance criterion.
 *
 * Shows a plain sentence per prior office this person's own `people.terms[]`
 * record carries (`backend/app/promises/service.py`'s `_prior_offices_for` —
 * never fabricated, never inferred). Renders nothing at all when the list is
 * empty, rather than a "no prior office" line for every one of the many
 * challengers who have genuinely never held office — that would be the
 * dominant case and the loudest line on the page for no information gained.
 */
function PriorOfficeNotice({ offices }: { offices: PriorOffice[] }) {
  if (offices.length === 0) return null;

  return (
    <section aria-label="Prior elected office">
      <h3>Previously served</h3>
      <ul>
        {offices.map((office) => (
          <li key={`${office.body_id}:${office.start ?? ''}`}>
            {priorOfficeLabel(office)}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** A plain-language sentence for one prior term, naming the body and dates when known. */
function priorOfficeLabel(office: PriorOffice): string {
  const body = bodyLabel(office.body_id);
  const dates = datesLabel(office.start, office.end);
  return dates ? `${body}${dates}` : body;
}

/** A readable name for a `body_id`, falling back to the id itself when unrecognized. */
function bodyLabel(bodyId: string): string {
  const labels: Record<string, string> = {
    'us-house': 'U.S. House of Representatives',
    'us-senate': 'U.S. Senate',
  };
  return labels[bodyId] ?? bodyId;
}

function datesLabel(start: string | null, end: string | null): string {
  if (!start && !end) return '';
  const startYear = yearOf(start);
  const endYear = end ? yearOf(end) : 'present';
  if (!startYear) return '';
  return ` (${startYear}–${endYear})`;
}

function yearOf(iso: string | null): string | null {
  if (!iso) return null;
  const match = /^(\d{4})/.exec(iso);
  return match ? match[1] : null;
}

/**
 * The "grouped by my interests" half of the acceptance criterion.
 *
 * One `<ul>` per matching interest, same structure
 * `CandidateDetailPage.AlignmentBreakdown` uses for its own reasons list.
 * `unmatchedCount` is shown as a plain count, never hidden — see
 * `ChallengerPromises.unmatched_count`'s backend docstring for why a promise
 * outside the voter's interests must still be acknowledged to exist.
 */
function PromiseGroups({
  groups,
  unmatchedCount,
}: {
  groups: PromiseGroup[];
  unmatchedCount: number;
}) {
  const headingId = useId();

  if (groups.length === 0) {
    return (
      <p>
        This candidate has no promises on file yet that match the priorities you chose. Check
        back once more of their campaign material has been archived.
      </p>
    );
  }

  return (
    <>
      <h3 id={headingId}>Promises about your priorities</h3>
      <div aria-labelledby={headingId}>
        {groups.map((group) => (
          <section key={group.stance_id} aria-label={`Promises about ${group.stance_id}`}>
            <h4>{group.stance_id}</h4>
            <ul>
              {group.promises.map((promise) => (
                <li key={promise.promise_id}>
                  <PromiseRow promise={promise} />
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
      {unmatchedCount > 0 ? (
        <p>
          {unmatchedCount} more {unmatchedCount === 1 ? 'promise' : 'promises'} from this
          candidate do not match one of the priorities you chose.
        </p>
      ) : null}
    </>
  );
}

/** One promise: its quote, whether it is testable, and a control to open its source. */
function PromiseRow({ promise }: { promise: PromiseItem }) {
  const openerRef = useRef<HTMLButtonElement>(null);
  const [isOpen, setIsOpen] = useState(false);

  return (
    <>
      <blockquote>&ldquo;{promise.quote_span}&rdquo;</blockquote>
      {' — '}
      <TestableLabel testable={promise.testable} />
      {' · '}
      <button type="button" ref={openerRef} onClick={() => setIsOpen(true)}>
        View source
      </button>
      <Drawer
        isOpen={isOpen}
        onClose={() => setIsOpen(false)}
        label={`Source for promise ${promise.promise_id}`}
        returnFocusTo={openerRef}
      >
        <PromiseSourceContent promise={promise} />
      </Drawer>
    </>
  );
}

/**
 * Testable / not testable, in prd.md's own words.
 *
 * prd.md: "vague rhetoric is tagged non-testable and shown but not scored."
 * Shown for every promise, not just the testable ones, so a voter can tell
 * the two kinds apart without guessing from the wording alone.
 */
function TestableLabel({ testable }: { testable: boolean }) {
  return testable ? <span>A specific, checkable commitment</span> : <span>Not testable</span>;
}

/** The drawer's body: the promise's commitment shape and its archived source. */
function PromiseSourceContent({ promise }: { promise: PromiseItem }) {
  const headingId = useId();

  return (
    <section aria-labelledby={headingId}>
      <h3 id={headingId}>Source</h3>
      <dl>
        {promise.actor ? (
          <>
            <dt>Who</dt>
            <dd>{promise.actor}</dd>
          </>
        ) : null}
        {promise.action ? (
          <>
            <dt>What they said they would do</dt>
            <dd>
              {promise.action}
              {promise.object ? ` ${promise.object}` : ''}
            </dd>
          </>
        ) : null}
        <dt>Archived</dt>
        <dd>{formatDate(promise.captured_at)}</dd>
      </dl>
      <p>
        <a href={promise.source_url} target="_blank" rel="noreferrer">
          Open the source
        </a>
      </p>
    </section>
  );
}

/**
 * `captured_at` as a plain calendar date, in UTC — same fix and same reason
 * `CandidateDetailPage.formatDate` documents: `toLocaleDateString` without
 * `timeZone: 'UTC'` can shift a UTC-midnight timestamp back a day for a
 * voter behind UTC.
 */
function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC' });
}

function messageFrom(error: unknown): string {
  if (error instanceof Error && error.name === 'ChallengerPromisesError') return error.message;
  return 'We could not load this candidate’s promises just now. Please try again.';
}
