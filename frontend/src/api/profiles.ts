/**
 * The `POST /v1/profiles` client, and the anonymous-mode alternative (story 4.1).
 * Also `GET /v1/profiles/{user_id}` (story 4.4) and `GET /v1/profiles/methodology`
 * (story 4.2).
 *
 * Mirrors `backend/app/profiles/router.py`'s `SaveProfileRequest` /
 * `SaveProfileResponse` / `LoadedProfile` / `TierWeightsResponse`. Three
 * save/load paths, matching datamodel.md's documented split exactly:
 *
 *   * `saveProfile` — POSTs to the account-backed, encrypted store. Requires
 *     `consent: true`; the backend refuses anything else with a 422.
 *   * `saveAnonymousProfile` — writes nothing to the network. datamodel.md:
 *     "In anonymous mode no row is written: the same payload lives in the
 *     browser, and `/v1/alignment` accepts it inline." `/v1/alignment` does
 *     not exist yet (it is a later story's endpoint, Epic 6), so today this
 *     is a store with nowhere yet to be read from — see the module docstring
 *     below for exactly what that means and what is deferred.
 *   * `loadProfile` — GETs the decrypted, saved profile back for a signed-in
 *     voter (story 4.4's "come back to it"), or `null` when nothing has been
 *     saved — not an error, the same answer the backend gives for "never
 *     saved" and "deleted".
 *   * `fetchTierWeights` — GETs the live primary/secondary/nice_to_have
 *     weights from `methodology_versions` (story 4.2), so a tier-editing UI
 *     never hard-codes "3.0 / 1.5 / 0.5" itself.
 *   * `followPerson` / `unfollowPerson` — `POST`/`DELETE
 *     /v1/profiles/{user_id}/follow/{person_id}` (story 15.1). Dedicated
 *     endpoints rather than resubmitting the whole profile through
 *     `saveProfile`: the backend still writes through the same encrypted
 *     `VoterProfileService`, this is just the ergonomic call shape for a
 *     single follow/unfollow action.
 *
 * Stories 14.2 and 14.4 add three more, and one field:
 *
 *   * `fetchConsentOffer` — `GET /v1/profiles/consent` (story 14.2). The
 *     consent wording, its version, and the anonymous-mode alternative, in one
 *     payload. Needs no identity and writes nothing.
 *   * `saveProfile`'s `consentVersion` option — the version the client
 *     actually rendered, echoed back so the stored consent record names
 *     wording this voter really saw. Omitted rather than defaulted when the
 *     caller has none; see that option's own comment.
 *   * `exportProfile` — `GET /v1/profiles/{user_id}/export` (story 14.4).
 *     Everything stored for this voter, decrypted, with its consent history.
 *   * `deleteProfile` — `DELETE /v1/profiles/{user_id}` (story 14.4).
 *     Irreversible; the caller owns the confirmation step.
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

/**
 * Mirrors `backend/app/profiles/consent.ConsentOffer` (story 14.2).
 *
 * The consent wording and the anonymous-mode alternative arrive together, in
 * one object, deliberately — see that module's docstring and ui-prd.md 2.2
 * ("side by side, as an actual choice"). A component that renders the ask has
 * the alternative already in hand, so it cannot accidentally show a
 * consent-or-nothing screen.
 */
export interface ConsentOffer {
  consent_version: string;
  summary: string;
  what_is_stored: string[];
  anonymous_alternative: string;
  your_rights: string[];
  lawful_basis: string;
}

/** One consent event as it appears in an export (story 14.2). */
export interface ConsentRecord {
  consent_version: string;
  action: 'granted' | 'withdrawn';
  lawful_basis: string;
  recorded_at: string;
}

/** Mirrors `backend/app/profiles/router.py`'s `ProfileExportResponse` (14.4). */
export interface ProfileExport {
  exported_at: string;
  version: number;
  updated_at: string | null;
  locale: string;
  jurisdiction_ids: string[];
  profile: {
    interests: Interest[];
    evidence_mode: 'votes' | 'promises' | 'blend';
    followed_person_ids: string[];
    history?: { version: number; changed_at: string }[];
  };
  consent: ConsentRecord[];
}

/** Mirrors `backend/app/profiles/router.py`'s `DeleteProfileResponse` (14.4). */
export interface DeleteProfileResult {
  deleted: boolean;
  key_shredded: boolean;
  consent_withdrawal_recorded: boolean;
  elapsed_seconds: number;
}

/** Mirrors `backend/app/profiles/router.py`'s `LoadedProfile`. */
export interface LoadedProfile {
  interests: Interest[];
  evidence_mode: 'votes' | 'promises' | 'blend';
  followed_person_ids: string[];
}

/** Mirrors `backend/app/profiles/router.py`'s `TierWeightsResponse`. */
export interface TierWeightsResponse {
  methodology_version: string;
  weights: Record<'primary' | 'secondary' | 'nice_to_have', number>;
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
  options: {
    consent: true;
    /**
     * The version of the consent wording this client actually rendered, read
     * from `fetchConsentOffer` and echoed back (story 14.2). Omitted — not
     * defaulted here — when the caller has no version to report: the backend
     * defaults the field so story 4.1's callers keep working, and a client
     * that invented a version string would put wording it never showed into
     * an audit record. See `app.profiles.consent`'s module docstring.
     */
    consentVersion?: string;
    locale?: string;
    jurisdictionIds?: string[];
  },
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
      ...(options.consentVersion === undefined
        ? {}
        : { consent_version: options.consentVersion }),
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

/** Raised for any non-2xx answer from a load-side endpoint (story 4.4, 4.2). */
export class ProfileLoadError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ProfileLoadError';
    this.status = status;
  }
}

/**
 * Load a voter's saved profile, or `null` if they have never saved one.
 *
 * `null` is the backend's documented answer for "never saved" (a 200 whose
 * body is `null`, not a 404) — see `app.profiles.router.load_profile`'s
 * docstring — so this function returns `null` for that case rather than
 * throwing, and a caller does not need to special-case a 404.
 */
export async function loadProfile(userId: string, signal?: AbortSignal): Promise<LoadedProfile | null> {
  const url = apiUrl(API_BASE_URL, `/v1/profiles/${encodeURIComponent(userId)}`, window.location.origin);
  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });
  if (!response.ok) {
    throw new ProfileLoadError(
      'We could not load your saved profile just now. Please try again.',
      response.status,
    );
  }
  return (await response.json()) as LoadedProfile | null;
}

/**
 * The live tier weights (story 4.2), read from `methodology_versions` rather
 * than hard-coded here — see `app.profiles.methodology`'s module docstring
 * on the backend for why.
 */
export async function fetchTierWeights(signal?: AbortSignal): Promise<TierWeightsResponse> {
  const url = apiUrl(API_BASE_URL, '/v1/profiles/methodology', window.location.origin);
  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });
  if (!response.ok) {
    throw new ProfileLoadError(
      'We could not load the current tier weights just now. Please try again.',
      response.status,
    );
  }
  return (await response.json()) as TierWeightsResponse;
}

/**
 * The consent wording a voter is asked to agree to (story 14.2).
 *
 * `GET /v1/profiles/consent`. Needs no identity and writes nothing — an
 * anonymous voter reads this to decide whether to have an account at all.
 *
 * Pass `version` to re-display the exact wording an old consent record names;
 * omit it for the current version. An unknown version is a 404 from the
 * backend rather than a silent fallback, deliberately: quietly showing
 * different text than was asked for is how a voter ends up reading wording
 * they never agreed to.
 */
export async function fetchConsentOffer(
  version?: string,
  signal?: AbortSignal,
): Promise<ConsentOffer> {
  const url = apiUrl(API_BASE_URL, '/v1/profiles/consent', window.location.origin);
  if (version !== undefined) url.searchParams.set('version', version);
  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });
  if (!response.ok) {
    throw new ProfileLoadError(
      'We could not load the consent information just now. Please try again — ' +
        'nothing has been saved.',
      response.status,
    );
  }
  return (await response.json()) as ConsentOffer;
}

/**
 * Export everything stored for one voter, as one JSON document (story 14.4).
 *
 * `GET /v1/profiles/{user_id}/export`. Returns the voter's own decrypted
 * interests plus the append-only consent history, so the audit trail travels
 * with the data rather than needing a second request.
 *
 * A 404 means nothing is stored — the backend's deliberate difference from
 * `loadProfile`'s `null`-with-200 (a load asks "is there a profile to edit",
 * where no is an ordinary answer; an export asks for a document, and an empty
 * document is not one). The message for that case names anonymous mode, so a
 * voter who never had an account does not read it as data loss.
 */
export async function exportProfile(userId: string, signal?: AbortSignal): Promise<ProfileExport> {
  const url = apiUrl(
    API_BASE_URL,
    `/v1/profiles/${encodeURIComponent(userId)}/export`,
    window.location.origin,
  );
  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });
  if (!response.ok) {
    throw new ProfileLoadError(
      response.status === 404
        ? 'There is nothing stored for you to export. If you use Informed in ' +
          'anonymous mode your profile lives only in this browser and was ' +
          'never sent to us.'
        : 'We could not export your profile just now. Please try again.',
      response.status,
    );
  }
  return (await response.json()) as ProfileExport;
}

/**
 * Permanently delete one voter's profile and shred its key (story 14.4).
 *
 * `DELETE /v1/profiles/{user_id}`. Irreversible: the data key is 32 random
 * bytes that were never derivable from anything, so once its wrap is gone no
 * retained ciphertext can be decrypted again by anyone, Informed's operators
 * included. **A caller MUST confirm with the voter before calling this** — see
 * `ProfileEditor`'s delete flow, which is where that confirmation lives.
 *
 * Idempotent on the backend, and a 200 (not a 404) for a voter who never had a
 * profile: "it is gone" is the true answer in both cases.
 */
export async function deleteProfile(
  userId: string,
  signal?: AbortSignal,
): Promise<DeleteProfileResult> {
  const url = apiUrl(
    API_BASE_URL,
    `/v1/profiles/${encodeURIComponent(userId)}`,
    window.location.origin,
  );
  const response = await fetch(url.toString(), {
    method: 'DELETE',
    signal,
    headers: { accept: 'application/json' },
  });
  if (!response.ok) {
    throw new ProfileSaveError(
      'We could not delete your profile just now. Nothing has been deleted — ' +
        'please try again.',
      response.status,
    );
  }
  return (await response.json()) as DeleteProfileResult;
}

/** Mirrors `backend/app/profiles/router.py`'s `FollowResponse`. */
export interface FollowResponse {
  followed_person_ids: string[];
}

/**
 * Follow an incumbent: `POST /v1/profiles/{user_id}/follow/{person_id}`.
 *
 * Reuses `ProfileSaveError` (not a new error type) because the failure
 * modes are the same shape as a profile save: a 404 means `person_id` is not
 * a real, currently-loaded person (the backend's reference check, story
 * 1.4's philosophy applied to a field inside the encrypted payload), a 422
 * means this voter has no identity or no profile yet to attach a follow to.
 * Idempotent on the backend: following someone already followed succeeds
 * again rather than erroring.
 */
export async function followPerson(
  userId: string,
  personId: string,
  signal?: AbortSignal,
): Promise<FollowResponse> {
  const url = apiUrl(
    API_BASE_URL,
    `/v1/profiles/${encodeURIComponent(userId)}/follow/${encodeURIComponent(personId)}`,
    window.location.origin,
  );
  const response = await fetch(url.toString(), {
    method: 'POST',
    signal,
    headers: { accept: 'application/json' },
  });
  if (!response.ok) {
    throw new ProfileSaveError(await messageForFollow(response), response.status);
  }
  return (await response.json()) as FollowResponse;
}

/**
 * Unfollow an incumbent: `DELETE /v1/profiles/{user_id}/follow/{person_id}`.
 *
 * No existence check on the backend — unfollowing a person_id that is no
 * longer a real person, or was never followed, always succeeds, so the only
 * realistic failure here is a network or server problem.
 */
export async function unfollowPerson(
  userId: string,
  personId: string,
  signal?: AbortSignal,
): Promise<FollowResponse> {
  const url = apiUrl(
    API_BASE_URL,
    `/v1/profiles/${encodeURIComponent(userId)}/follow/${encodeURIComponent(personId)}`,
    window.location.origin,
  );
  const response = await fetch(url.toString(), {
    method: 'DELETE',
    signal,
    headers: { accept: 'application/json' },
  });
  if (!response.ok) {
    throw new ProfileSaveError(await messageForFollow(response), response.status);
  }
  return (await response.json()) as FollowResponse;
}

async function messageForFollow(response: Response): Promise<string> {
  if (response.status === 404) {
    return 'That person could not be found, so following them was not saved.';
  }
  if (response.status === 422) {
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') return body.detail;
    } catch {
      // Fall through to the generic message below.
    }
    return 'That could not be saved. Please check your profile and try again.';
  }
  return 'We could not update who you follow just now. Please try again.';
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
