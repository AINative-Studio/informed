/**
 * The `GET /v1/ballot/cards` client (story 7.1).
 *
 * Mirrors `backend/app/ballot/models.py`'s `BallotCardsResponse` and
 * `CandidateCardView` — story 7.1's own additions on top of story 7.2's
 * `candidate_cards` read model. See that module's docstring for the full
 * reasoning; the short version is: every candidate on one race's ballot, each
 * with their incumbency flag and a confidence badge, in a stable name order
 * that is never a ranking.
 *
 * Privacy: this function takes an election id and a district id and nothing
 * else — no profile, no user id, no voter identity of any kind. The backend
 * endpoint's own signature carries the identical guarantee (a
 * `candidate_cards` row holds no voter reference at all), so there is no
 * parameter here to carry one in even by accident.
 *
 * Why `electionIdFor` exists, and why it is honest rather than invented
 * =======================================================================
 * `GET /v1/ballot`'s own `Race.election_id` is `null` for every race today.
 * Story 2.6 loads real `candidacies` rows (and a real `elections` row,
 * `us-2026-11-03-general`) into ZeroDB, but nothing in `app.ballot.service`
 * was changed to read them back into `/v1/ballot`'s response — that wiring
 * is a gap in story 2.6's own scope, not something story 7.1 can fix inside
 * a different story's already-merged, already-tested module. `CURRENT_ELECTION_ID`
 * is not a guess: it is the exact id `backend/app/seed_elections.py` seeds as
 * permanent federal reference data (2 U.S.C. 7 fixes the date by statute),
 * the same constant the backend's own live-verification scripts use. Falling
 * back to it when a race carries no election id of its own is using the one
 * real election this deployment actually tracks, not fabricating one.
 */

import { apiUrl } from './ballot';

/** Mirrors `backend/app/repositories/models.py`'s `VerdictCounts`. */
export interface VerdictCounts {
  kept: number;
  partial: number;
  broken: number;
  no_opportunity: number;
  absent: number;
  not_testable: number;
}

/** Mirrors `backend/app/repositories/models.py`'s `PositionSummaryEntry`. */
export interface PositionSummaryEntry {
  stance_id: string;
  s_value: number;
  confidence: number;
  evidence_count: number;
}

/** Mirrors `backend/app/scoring/badges.py`'s `ConfidenceBadge`. */
export type ConfidenceBadge = 'high' | 'medium' | 'low' | 'insufficient';

/**
 * One candidate's card, as the ballot view renders it. Mirrors
 * `backend/app/ballot/models.py`'s `CandidateCardView`.
 *
 * Deliberately carries no personalized score: `say_do_gap` is the one
 * alignment-shaped number here, and it is the same number for every voter.
 * A per-voter score belongs to `GET /v1/candidates/{id}/alignment?profile_id=`.
 */
export interface CandidateCardView {
  candidacy_id: string;
  person_id: string;
  person_name: string;
  party: string | null;
  district: string;
  photo_url: string | null;
  is_incumbent: boolean;
  verdict_counts: VerdictCounts | null;
  say_do_gap: number | null;
  positions_summary: PositionSummaryEntry[] | null;
  confidence_badge: ConfidenceBadge;
  methodology_version: string | null;
  updated_at: string;
}

/** Mirrors `backend/app/ballot/models.py`'s `BallotCardsResponse`. */
export interface BallotCardsResponse {
  election_id: string;
  district_id: string;
  cards: CandidateCardView[];
}

/**
 * The one federal general election this deployment tracks today —
 * `backend/app/seed_elections.py`'s permanent reference row, not a guess.
 * See this module's docstring for why `electionIdFor` falls back to it.
 */
export const CURRENT_ELECTION_ID = 'us-2026-11-03-general';

/**
 * The election id to ask `/v1/ballot/cards` for, given one race from
 * `/v1/ballot`. Prefers the race's own `election_id` so this stops needing
 * the fallback the day `/v1/ballot` is wired to real candidacies (story
 * 2.6's own remaining gap), with no change needed here.
 */
export function electionIdFor(race: { election_id: string | null }): string {
  return race.election_id ?? CURRENT_ELECTION_ID;
}

/** Raised for any non-2xx answer, carrying a message fit to show a voter. */
export class BallotCardsError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'BallotCardsError';
    this.status = status;
  }
}

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';

/**
 * Fetch every built card for one race.
 *
 * An empty `cards` array is a legitimate answer (no card has been rebuilt
 * for this race yet), not an error — the caller should render that as
 * "nothing computed here yet", not retry or show an alert.
 */
export async function fetchBallotCards(
  params: { electionId: string; districtId: string },
  signal?: AbortSignal,
): Promise<BallotCardsResponse> {
  const url = apiUrl(API_BASE_URL, '/v1/ballot/cards', window.location.origin);
  url.searchParams.set('election_id', params.electionId);
  url.searchParams.set('district_id', params.districtId);

  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });

  if (!response.ok) {
    throw new BallotCardsError(messageFor(response.status), response.status);
  }
  return (await response.json()) as BallotCardsResponse;
}

function messageFor(status: number): string {
  if (status === 422) return 'That race could not be read. Please try again.';
  return 'We could not load this race just now. Please try again.';
}
