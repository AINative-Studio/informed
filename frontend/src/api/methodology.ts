/**
 * The `/v1/methodology` client (story 12.2).
 *
 * Mirrors `backend/app/methodology/models.py`. The one place the frontend
 * knows the methodology endpoint's URL and payload shape.
 *
 * No credentials, ever. prd.md requires the methodology page to be public, and
 * `fetch` here sends no Authorization header and no cookies — the request is
 * identical whether or not anyone is signed in, which is the property the page
 * is claiming when it says "anyone can check this".
 *
 * Nothing on this path touches a voter's profile. `PublishedMethodology` has
 * nowhere to put one, which is deliberate: the backend's response model
 * forbids extra fields for the same reason (see that module's docstring).
 */

import { apiUrl } from './ballot';

/** One methodology version, as published. Mirrors `PublishedMethodology`. */
export interface PublishedMethodology {
  /** The version id, e.g. `2026.1` — what a verdict or score cites. */
  version: string;
  published_at: string;
  /**
   * Published weights by group. `tiers` carries the interest tier weights;
   * groups listed in `weights_not_yet_published` are absent rather than zero.
   */
  weights: Record<string, Record<string, number>>;
  changelog_url: string;
  is_current: boolean;
  /** This version's canonical public path, e.g. `/v1/methodology/2026.1`. */
  path: string;
  /**
   * Documented weight groups this version does not carry yet. Today:
   * `evidence_blend` and `vote_types`, which belong to the alignment scorer
   * that is not built. Rendered rather than ignored — see MethodologyPage.
   */
  weights_not_yet_published: string[];
}

export interface MethodologyVersionsResponse {
  versions: PublishedMethodology[];
}

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';

export class MethodologyError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'MethodologyError';
    this.status = status;
  }
}

/**
 * The order tiers are shown in, heaviest first.
 *
 * Declared here rather than derived from `Object.keys(weights.tiers)`, because
 * a JSON object's key order is whatever the server serialized and a page that
 * silently reordered the tiers between reloads would be confusing. The labels
 * are the display names for the stored keys.
 */
export const TIER_DISPLAY_ORDER: ReadonlyArray<{ key: string; label: string }> = [
  { key: 'primary', label: 'Primary' },
  { key: 'secondary', label: 'Secondary' },
  { key: 'nice_to_have', label: 'Nice to have' },
];

/**
 * Human-readable names for datamodel.md's weight groups.
 *
 * Used for the "not published yet" list: `evidence_blend` is not a phrase to
 * show a reader who came here to understand the method.
 */
export const WEIGHT_GROUP_LABELS: Record<string, string> = {
  tiers: 'interest tier weights',
  evidence_blend: 'how a voting record is balanced against stated promises',
  vote_types: 'how a final-passage vote is weighed against a procedural one',
};

/** A reader-facing name for a weight group, falling back to the raw key. */
export function weightGroupLabel(group: string): string {
  return WEIGHT_GROUP_LABELS[group] ?? group;
}

/** Fetch the methodology currently in force. Public; sends no credentials. */
export async function fetchMethodology(signal?: AbortSignal): Promise<PublishedMethodology> {
  const url = apiUrl(API_BASE_URL, '/v1/methodology', window.location.origin);
  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });
  if (!response.ok) {
    throw new MethodologyError(
      'We could not load the methodology just now. Please try again.',
      response.status,
    );
  }
  return (await response.json()) as PublishedMethodology;
}

/** Fetch every published version, newest first — the changelog list. */
export async function fetchMethodologyVersions(
  signal?: AbortSignal,
): Promise<MethodologyVersionsResponse> {
  const url = apiUrl(API_BASE_URL, '/v1/methodology/versions', window.location.origin);
  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });
  if (!response.ok) {
    throw new MethodologyError(
      'We could not load the methodology history just now. Please try again.',
      response.status,
    );
  }
  return (await response.json()) as MethodologyVersionsResponse;
}
