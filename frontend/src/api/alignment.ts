/**
 * The `GET /v1/candidates/{id}/alignment` client (story 7.3).
 *
 * Mirrors `backend/app/scoring/models.py`'s `CandidateAlignment`,
 * `AlignmentReason`, `DealbreakerWarning` and `Citation` — stories 6.4-6.7's
 * scored-and-cited alignment response. Story 7.3 is the first UI to read
 * this endpoint; `BallotView`'s cards (story 7.1) read the separate,
 * un-personalized `candidate_cards` summary instead (see
 * `src/api/ballotCards.ts`), so this is a new client rather than a reuse.
 *
 * `profile_id` is REQUIRED on the wire (see `app.scoring.router`'s
 * docstring: "An alignment score is a function of a voter's priorities;
 * there is no 'anonymous' version of it that means anything"), so it is a
 * required parameter here too — there is no way to call this without one.
 */

import { apiUrl } from './ballot';

/** Mirrors `backend/app/scoring/badges.py`'s `ConfidenceBadge`. */
export type ConfidenceBadge = 'high' | 'medium' | 'low' | 'insufficient';

/** Mirrors `backend/app/scoring/models.py`'s `ReasonDirection`. */
export type ReasonDirection = 'raised' | 'lowered';

/** Mirrors `backend/app/scoring/models.py`'s `Citation`. */
export interface Citation {
  roll_call_id: string;
  position: string;
  vote_type: string;
  voted_at: string;
  bill_id: string | null;
  is_bundled: boolean;
  bundled_reason: string | null;
}

/**
 * Mirrors `backend/app/scoring/models.py`'s `AlignmentReason`.
 *
 * `citations` is never empty on a real response — the backend's own model
 * cannot be constructed without at least one (`min_length=1`, no default).
 * Typed as a plain array here rather than a non-empty tuple because nothing
 * in TypeScript's structural type system can carry that guarantee across an
 * untrusted `fetch` boundary; the component that renders a row treats a
 * reason with no renderable link as a data error to note, not crash on.
 */
export interface AlignmentReason {
  stance_id: string;
  direction: ReasonDirection;
  s_value: number;
  confidence: number;
  evidence_count: number;
  citations: Citation[];
}

/** Mirrors `backend/app/scoring/models.py`'s `DealbreakerWarning`. */
export interface DealbreakerWarning {
  stance_id: string;
  s_value: number;
  confidence: number;
  evidence_count: number;
  citations: Citation[];
}

/**
 * Mirrors `backend/app/scoring/models.py`'s `CandidateAlignment`.
 *
 * Carries no interest wording, tier, intensity or note from the voter's
 * profile — see that model's docstring for why, and do not add one here:
 * the backend response itself cannot hold it (`extra="forbid"`).
 */
export interface CandidateAlignment {
  candidacy_id: string;
  person_id: string;
  score: number | null;
  confidence_badge: ConfidenceBadge;
  evidence_count: number;
  scored_interest_count: number;
  dealbreaker_warnings: DealbreakerWarning[];
  reasons: AlignmentReason[];
  methodology_version: string;
}

/** Raised for any non-2xx answer, carrying a message fit to show a voter. */
export class AlignmentError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'AlignmentError';
    this.status = status;
  }
}

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';

/**
 * Fetch one candidate's alignment against one voter's profile.
 *
 * `candidacyId` may itself contain `/` (an OCD-ID district segment) — see
 * `app.scoring.router`'s docstring on why the backend route uses a
 * `:path` converter for it. Encoding each path SEGMENT would still mangle
 * that, so the id is interpolated as-is and left to the browser's own URL
 * parsing, exactly as `ballotCards.ts`'s `district_id` query parameter does
 * for the same id shape (there, via `searchParams.set`, which encodes the
 * value but never reinterprets its slashes as path segments).
 */
export async function fetchAlignment(
  params: { candidacyId: string; profileId: string },
  signal?: AbortSignal,
): Promise<CandidateAlignment> {
  const url = apiUrl(API_BASE_URL, `/v1/candidates/${params.candidacyId}/alignment`, window.location.origin);
  url.searchParams.set('profile_id', params.profileId);

  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });

  if (!response.ok) {
    throw new AlignmentError(messageFor(response.status), response.status);
  }
  return (await response.json()) as CandidateAlignment;
}

function messageFor(status: number): string {
  if (status === 404) {
    return "We don't have a score for this candidate against your profile yet.";
  }
  return 'We could not load this candidate’s score breakdown just now. Please try again.';
}
