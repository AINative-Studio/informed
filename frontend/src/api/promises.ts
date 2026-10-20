/**
 * The `GET /v1/candidates/{id}/promises` client (story 9.6).
 *
 * Mirrors `backend/app/promises/models.py`'s `ChallengerPromises`,
 * `PromiseGroup`, `PromiseItem` and `PriorOffice` — the promise-side
 * counterpart to `src/api/alignment.ts`'s vote-based response, for a
 * candidate with no voting record to be scored from. See that backend
 * module's docstring for why this is its own endpoint rather than a widened
 * `/alignment`.
 *
 * `profile_id` is REQUIRED on the wire, for the same reason `alignment.ts`
 * documents: the grouping key IS the voter's own stated interests, so there
 * is no meaningful anonymous version of this response either.
 */

import { apiUrl } from './ballot';

/** Mirrors `backend/app/repositories/models.py`'s `PromiseDirection`. */
export type PromiseDirection = 'support' | 'oppose' | 'unclear';

/** Mirrors `backend/app/promises/models.py`'s `PromiseItem`. */
export interface PromiseItem {
  promise_id: string;
  quote_span: string;
  actor: string | null;
  action: string | null;
  object: string | null;
  direction: PromiseDirection;
  testable: boolean;
  source_url: string;
  captured_at: string;
}

/**
 * Mirrors `backend/app/promises/models.py`'s `PromiseGroup`.
 *
 * `promises` is never empty on a real response — the backend's own model
 * cannot be constructed without at least one (`min_length=1`). Typed as a
 * plain array here for the same reason `alignment.ts`'s `AlignmentReason.
 * citations` is: nothing in TypeScript's structural type system can carry
 * that guarantee across an untrusted `fetch` boundary.
 */
export interface PromiseGroup {
  stance_id: string;
  promises: PromiseItem[];
}

/** Mirrors `backend/app/promises/models.py`'s `PriorOffice`. */
export interface PriorOffice {
  body_id: string;
  district_id: string | null;
  party: string | null;
  start: string | null;
  end: string | null;
}

/**
 * Mirrors `backend/app/promises/models.py`'s `ChallengerPromises`.
 *
 * Carries no interest wording, tier, intensity or note from the voter's
 * profile — see that model's docstring for why, and do not add one here:
 * the backend response itself cannot hold it (`extra="forbid"`).
 */
export interface ChallengerPromises {
  candidacy_id: string;
  person_id: string;
  groups: PromiseGroup[];
  unmatched_count: number;
  prior_offices: PriorOffice[];
}

/** Raised for any non-2xx answer, carrying a message fit to show a voter. */
export class ChallengerPromisesError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ChallengerPromisesError';
    this.status = status;
  }
}

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';

/**
 * Fetch one candidate's promises, grouped by one voter's own interests.
 *
 * `candidacyId` may itself contain `/` (an OCD-ID district segment) — same
 * posture as `alignment.ts`'s `fetchAlignment`, which documents why the id
 * is interpolated as-is rather than encoded segment by segment.
 */
export async function fetchChallengerPromises(
  params: { candidacyId: string; profileId: string },
  signal?: AbortSignal,
): Promise<ChallengerPromises> {
  const url = apiUrl(API_BASE_URL, `/v1/candidates/${params.candidacyId}/promises`, window.location.origin);
  url.searchParams.set('profile_id', params.profileId);

  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });

  if (!response.ok) {
    throw new ChallengerPromisesError(messageFor(response.status), response.status);
  }
  return (await response.json()) as ChallengerPromises;
}

function messageFor(status: number): string {
  if (status === 404) {
    return "We don't have promises for this candidate against your profile yet.";
  }
  return 'We could not load this candidate’s promises just now. Please try again.';
}
