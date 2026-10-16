/**
 * The `POST /v1/profiles` client, and the anonymous-mode alternative (story 4.1).
 *
 * Mirrors `backend/app/profiles/router.py`'s `SaveProfileRequest` /
 * `SaveProfileResponse`. Two save paths, matching datamodel.md's documented
 * split exactly:
 *
 *   * `saveProfile` — POSTs to the account-backed, encrypted store. Requires
 *     `consent: true`; the backend refuses anything else with a 422.
 *   * `saveAnonymousProfile` — writes nothing to the network. datamodel.md:
 *     "In anonymous mode no row is written: the same payload lives in the
 *     browser, and `/v1/alignment` accepts it inline." `/v1/alignment` does
 *     not exist yet (it is a later story's endpoint, Epic 6), so today this
 *     is a store with nowhere yet to be read from — see the module docstring
 *     below for exactly what that means and what is deferred.
 */

import { apiUrl } from './ballot';

export interface Interest {
  topic_id: string;
  stance_id: string;
  tier: 'primary' | 'secondary' | 'nice_to_have';
  intensity: 1 | 2 | 3;
  dealbreaker: boolean;
  note?: string | null;
}

export interface InterestProfilePayload {
  interests: Interest[];
  evidence_mode?: 'votes' | 'promises' | 'blend';
  followed_person_ids?: string[];
}

export interface SaveProfileResponse {
  version: number;
  locale: string;
  updated_at: string;
}

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';

export class ProfileSaveError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ProfileSaveError';
    this.status = status;
  }
}

/**
 * Save a profile to the account-backed encrypted store.
 *
 * `consent` must be true — this function does not default it, so a caller
 * cannot accidentally send a consentless save. The backend's own 422 is the
 * backstop if one slips through regardless.
 */
export async function saveProfile(
  userId: string,
  profile: InterestProfilePayload,
  options: { consent: true; locale?: string; jurisdictionIds?: string[] },
  signal?: AbortSignal,
): Promise<SaveProfileResponse> {
  const url = apiUrl(API_BASE_URL, '/v1/profiles', window.location.origin);
  const response = await fetch(url.toString(), {
    method: 'POST',
    signal,
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify({
      user_id: userId,
      consent: options.consent,
      profile,
      locale: options.locale ?? 'en-US',
      jurisdiction_ids: options.jurisdictionIds ?? [],
    }),
  });
  if (!response.ok) {
    throw new ProfileSaveError(await messageFor(response), response.status);
  }
  return (await response.json()) as SaveProfileResponse;
}

async function messageFor(response: Response): Promise<string> {
  if (response.status === 422) {
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') return body.detail;
    } catch {
      // Fall through to the generic message below.
    }
    return 'That profile could not be saved. Please check your selections and try again.';
  }
  return 'We could not save your profile just now. Please try again.';
}

// --- Anonymous mode: the client-side half of datamodel.md's documented split

/**
 * `localStorage` key for the anonymous profile.
 *
 * One fixed key, not per-voter: anonymous mode has no voter id to key by —
 * that is the point of it — and ui-prd.md's anonymous mode is "a real,
 * equally-functional path", a single current profile living in this
 * browser, not a multi-profile store.
 */
export const ANONYMOUS_PROFILE_STORAGE_KEY = 'informed:anonymous-profile:v1';

/**
 * Save a profile anonymously: in this browser only, never over the network.
 *
 * This is deliberately not an HTTP call. datamodel.md: "In anonymous mode no
 * row is written: the same payload lives in the browser, and `/v1/alignment`
 * accepts it inline." `/v1/alignment` is Epic 6's endpoint and does not exist
 * yet, so there is nothing to send this payload to today — it would be
 * accepted by nothing. What this function guarantees, which is this story's
 * job, is that choosing anonymous mode never silently drops the voter's
 * selections: the profile is written to `localStorage` synchronously, in the
 * same shape `saveProfile` would have sent, so a later story that builds
 * `/v1/alignment` can read it back with no shape migration.
 *
 * Throws if `localStorage` is unavailable (private browsing in some
 * browsers, storage quota) rather than silently discarding the profile —
 * the one failure mode ui-prd.md's "real, equally-functional path" promise
 * cannot tolerate.
 */
export function saveAnonymousProfile(profile: InterestProfilePayload): void {
  const record: AnonymousProfileRecord = { profile, saved_at: new Date().toISOString() };
  window.localStorage.setItem(ANONYMOUS_PROFILE_STORAGE_KEY, JSON.stringify(record));
}

export interface AnonymousProfileRecord {
  profile: InterestProfilePayload;
  saved_at: string;
}

/** Read back the anonymous profile saved in this browser, if any. */
export function loadAnonymousProfile(): AnonymousProfileRecord | null {
  const raw = window.localStorage.getItem(ANONYMOUS_PROFILE_STORAGE_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AnonymousProfileRecord;
  } catch {
    // A corrupted value is treated as absent rather than thrown: the voter
    // did not cause this, and surfacing a parse error on their own saved
    // choices would be a worse experience than asking them to redo it.
    return null;
  }
}

/** Clear the anonymous profile from this browser (prd.md's one-tap delete). */
export function clearAnonymousProfile(): void {
  window.localStorage.removeItem(ANONYMOUS_PROFILE_STORAGE_KEY);
}
