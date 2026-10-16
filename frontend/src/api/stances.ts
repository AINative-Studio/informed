/**
 * The `GET /v1/topics/{topic_id}/stances` client (story 4.1).
 *
 * Mirrors `backend/app/stances/models.py`. Each call returns a fresh random
 * order (story 3.2's server-side half of the randomization contract); this
 * module does not cache or re-sort anything, because the client's half of
 * that contract — holding one order stable for the rest of the onboarding
 * session — belongs to the component that calls this once per topic and
 * keeps the result in state. See `QuickOnboarding.tsx`.
 */

import { apiUrl } from './ballot';

/** One stance card. Never pre-selected, never flagged as a default. */
export interface StanceOption {
  id: string;
  label: string;
  description: string;
}

export interface StanceOptionsResponse {
  topic_id: string;
  options: StanceOption[];
}

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';

export class StancesError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'StancesError';
    this.status = status;
  }
}

/** Fetch one subtopic's stance options, freshly randomized by the server. */
export async function fetchStances(
  topicId: string,
  signal?: AbortSignal,
): Promise<StanceOptionsResponse> {
  const url = apiUrl(API_BASE_URL, `/v1/topics/${encodeURIComponent(topicId)}/stances`, window.location.origin);
  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });
  if (!response.ok) {
    throw new StancesError(
      'We could not load stance options for that topic just now. Please try again.',
      response.status,
    );
  }
  return (await response.json()) as StanceOptionsResponse;
}
