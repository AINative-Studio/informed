/**
 * The `GET /v1/candidates/{candidacy_id}/summary-card` client (story 14.6).
 *
 * Mirrors `backend/app/summary_card/models.py`'s `CandidateSummaryCard`.
 *
 * Privacy: this function takes a `candidacyId` and nothing else — no
 * profile, no user id, no voter identity of any kind. That mirrors the
 * backend endpoint's own signature (`app.summary_card.router.get_summary_card`
 * takes only the path parameter), so a caller cannot construct a request
 * that carries a voter's interests even by accident: there is no parameter
 * here to put one in.
 */

import { apiUrl } from './ballot';

/** Mirrors `backend/app/summary_card/models.py`'s `VerdictCounts`. */
export interface VerdictCounts {
  kept: number;
  partial: number;
  broken: number;
  no_opportunity: number;
  absent: number;
  not_testable: number;
}

/**
 * A shareable summary of one candidacy, candidate data only.
 *
 * `verdict_counts`, `say_do_gap` and `positions_summary` are `null` until
 * later epics (6, 7, 11) compute them — see the backend model's docstring
 * for why `null` rather than a fabricated zero or summary.
 */
export interface CandidateSummaryCard {
  candidacy_id: string;
  person_name: string;
  party: string | null;
  district: string;
  photo_url: string | null;
  verdict_counts: VerdictCounts | null;
  say_do_gap: number | null;
  positions_summary: string | null;
  updated_at: string;
}

/** Raised for any non-2xx answer, carrying a message fit to show a voter. */
export class SummaryCardError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'SummaryCardError';
    this.status = status;
  }
}

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';

/**
 * Fetch the shareable summary card for one candidacy.
 *
 * Takes only `candidacyId`. There is deliberately no second parameter for a
 * voter's profile or identity — see this module's docstring.
 */
export async function fetchSummaryCard(
  candidacyId: string,
  signal?: AbortSignal,
): Promise<CandidateSummaryCard> {
  const url = apiUrl(
    API_BASE_URL,
    `/v1/candidates/${encodeURIComponent(candidacyId)}/summary-card`,
    window.location.origin,
  );

  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });

  if (!response.ok) {
    throw new SummaryCardError(messageFor(response.status), response.status);
  }
  return (await response.json()) as CandidateSummaryCard;
}

/**
 * A plain-text summary of a card, fit to paste into a message or social post.
 *
 * Built entirely from the card's own fields — nothing else is ever in scope
 * here, so there is nothing a voter's interests could leak through even if
 * this function tried.
 */
export function shareTextFor(card: CandidateSummaryCard): string {
  const party = card.party ? ` (${card.party})` : '';
  return `${card.person_name}${party} — ${card.district}. Candidate summary from Informed: nonpartisan, evidence-based, and about the candidate only.`;
}

/**
 * A shareable URL for a candidacy's summary card page.
 *
 * `origin` is injected (default `window.location.origin`) so this is
 * directly testable without touching the real browser location.
 */
export function shareUrlFor(candidacyId: string, origin: string = window.location.origin): string {
  return `${origin}/candidates/${encodeURIComponent(candidacyId)}/summary-card`;
}

function messageFor(status: number): string {
  if (status === 404) return "We don't have a summary card for that candidate yet.";
  return 'We could not load this candidate summary just now. Please try again.';
}
