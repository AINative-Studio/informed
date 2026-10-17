import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ANONYMOUS_PROFILE_STORAGE_KEY,
  ProfileLoadError,
  ProfileSaveError,
  clearAnonymousProfile,
  deleteProfile,
  exportProfile,
  fetchConsentOffer,
  fetchTierWeights,
  followPerson,
  loadAnonymousProfile,
  loadProfile,
  saveAnonymousProfile,
  saveProfile,
  unfollowPerson,
} from './profiles';

/**
 * `GET /v1/profiles/{user_id}` and `GET /v1/profiles/methodology` clients
 * (stories 4.2, 4.4).
 *
 * Mirrors `backend/app/profiles/router.py`'s `LoadedProfile` and
 * `TierWeightsResponse`. Follows the fetch-stub style `QuickOnboarding.test.tsx`
 * already established for this API module's save path.
 */

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  // Story 14.3's storage is a real browser-global side effect, so it is reset
  // per test like the fetch stub — otherwise one anonymous-mode test's saved
  // profile is visible to the next one.
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('loadProfile', () => {
  it('GETs /v1/profiles/{user_id} and returns the decoded profile', async () => {
    const profile = {
      interests: [
        {
          topic_id: 'test:t1:v1',
          stance_id: 'test:t1:v1:s1',
          tier: 'primary',
          intensity: 2,
          dealbreaker: true,
          note: null,
        },
      ],
      evidence_mode: 'blend',
      followed_person_ids: [],
    };
    fetchMock.mockResolvedValueOnce(jsonResponse(profile));

    const result = await loadProfile('voter-1');

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/v1/profiles/voter-1'),
      expect.anything(),
    );
    expect(result).toEqual(profile);
  });

  it('returns null when the voter has never saved a profile', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(null));
    const result = await loadProfile('voter-with-no-profile');
    expect(result).toBeNull();
  });

  it('throws ProfileLoadError on a non-2xx response', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'boom' }, 500));
    await expect(loadProfile('voter-1')).rejects.toThrow(ProfileLoadError);
  });
});

describe('fetchTierWeights', () => {
  it('GETs /v1/profiles/methodology and returns the weights and version', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        methodology_version: '2026.1',
        weights: { primary: 3.0, secondary: 1.5, nice_to_have: 0.5 },
      }),
    );

    const result = await fetchTierWeights();

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/v1/profiles/methodology'),
      expect.anything(),
    );
    expect(result.methodology_version).toBe('2026.1');
    expect(result.weights).toEqual({ primary: 3.0, secondary: 1.5, nice_to_have: 0.5 });
  });

  it('throws ProfileLoadError on a non-2xx response', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'boom' }, 500));
    await expect(fetchTierWeights()).rejects.toThrow(ProfileLoadError);
  });
});

describe('followPerson', () => {
  it('POSTs /v1/profiles/{user_id}/follow/{person_id} and returns the updated list', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ followed_person_ids: ['person:B000944'] }),
    );

    const result = await followPerson('voter-1', 'person:B000944');

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/v1/profiles/voter-1/follow/person%3AB000944'),
      expect.objectContaining({ method: 'POST' }),
    );
    expect(result).toEqual({ followed_person_ids: ['person:B000944'] });
  });

  it('throws ProfileSaveError naming the problem on a 404 for an unknown person', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'not a known person' }, 404));
    await expect(followPerson('voter-1', 'person:does-not-exist')).rejects.toThrow(
      ProfileSaveError,
    );
  });

  it('throws ProfileSaveError on a 422 (e.g. no saved profile yet)', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'needs a primary interest' }, 422));
    await expect(followPerson('voter-1', 'person:B000944')).rejects.toThrow(ProfileSaveError);
  });
});

describe('unfollowPerson', () => {
  it('DELETEs /v1/profiles/{user_id}/follow/{person_id} and returns the updated list', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ followed_person_ids: [] }));

    const result = await unfollowPerson('voter-1', 'person:B000944');

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/v1/profiles/voter-1/follow/person%3AB000944'),
      expect.objectContaining({ method: 'DELETE' }),
    );
    expect(result).toEqual({ followed_person_ids: [] });
  });

  it('throws ProfileSaveError on a non-2xx response', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'boom' }, 500));
    await expect(unfollowPerson('voter-1', 'person:B000944')).rejects.toThrow(ProfileSaveError);
  });
});

// --- Stories 14.2 and 14.4: the consent offer, export and delete -----------

describe('fetchConsentOffer', () => {
  const OFFER = {
    consent_version: '2026.1',
    summary: 'Informed is asking you to save your political priorities.',
    what_is_stored: ['The issues you choose.', 'Nothing else about your politics.'],
    anonymous_alternative: 'You do not have to agree. Anonymous mode gives you the same full app.',
    your_rights: ['Export: download everything stored for you as JSON.'],
    lawful_basis: 'gdpr-9-2-a-explicit-consent',
  };

  it('GETs /v1/profiles/consent and returns the wording with its version', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(OFFER));

    const result = await fetchConsentOffer();

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/v1/profiles/consent'),
      expect.anything(),
    );
    expect(result.consent_version).toBe('2026.1');
    expect(result.lawful_basis).toBe('gdpr-9-2-a-explicit-consent');
  });

  /**
   * The point of story 14.2's single-payload design, asserted on the client
   * too: a component that has the ask also has the alternative, so it cannot
   * render a consent-or-nothing screen. See the backend `ConsentOffer`
   * docstring and ui-prd.md 2.2.
   */
  it('carries the anonymous-mode alternative in the same payload as the ask', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(OFFER));
    const result = await fetchConsentOffer();
    expect(result.summary).not.toHaveLength(0);
    expect(result.anonymous_alternative).toContain('Anonymous mode');
  });

  it('requests a specific version when asked, so an old record stays reproducible', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(OFFER));
    await fetchConsentOffer('2026.1');
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('version=2026.1'),
      expect.anything(),
    );
  });

  it('throws ProfileLoadError on a non-2xx response', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'nope' }, 404));
    await expect(fetchConsentOffer('1999.0')).rejects.toThrow(ProfileLoadError);
  });
});

describe('saveProfile', () => {
  const PROFILE = {
    interests: [
      {
        topic_id: 'test:t1:v1',
        stance_id: 'test:t1:v1:s1',
        tier: 'primary' as const,
        intensity: 2 as const,
        dealbreaker: false,
      },
    ],
  };

  it('echoes back the consent_version the client actually displayed (story 14.2)', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ version: 1, locale: 'en-US', updated_at: '2026-10-17T00:00:00Z' }),
    );

    await saveProfile('voter-1', PROFILE, { consent: true, consentVersion: '2026.1' });

    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect(body.consent).toBe(true);
    expect(body.consent_version).toBe('2026.1');
  });

  /**
   * The backend defaults `consent_version` so story 4.1's callers keep
   * working, so omitting it here must not send `undefined` (which
   * `JSON.stringify` drops) *and* must not invent a version this client never
   * showed. Leaving the key out entirely is the one honest option.
   */
  it('omits consent_version entirely when the caller has none to report', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ version: 1, locale: 'en-US', updated_at: '2026-10-17T00:00:00Z' }),
    );

    await saveProfile('voter-1', PROFILE, { consent: true });

    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect('consent_version' in body).toBe(false);
  });

  it('surfaces the backend 422 detail when the consent version is unknown', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "consent version '1999.0' is not one this release can show." }, 422),
    );

    await expect(
      saveProfile('voter-1', PROFILE, { consent: true, consentVersion: '1999.0' }),
    ).rejects.toThrow(/not one this release can show/);
  });
});

describe('exportProfile', () => {
  const EXPORT = {
    exported_at: '2026-10-17T00:00:00Z',
    version: 2,
    updated_at: '2026-10-16T00:00:00Z',
    locale: 'en-US',
    jurisdiction_ids: [],
    profile: { interests: [], evidence_mode: 'blend', followed_person_ids: [] },
    consent: [
      {
        consent_version: '2026.1',
        action: 'granted',
        lawful_basis: 'gdpr-9-2-a-explicit-consent',
        recorded_at: '2026-10-16T00:00:00Z',
      },
    ],
  };

  it('GETs /v1/profiles/{user_id}/export and returns the document with its consent history', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(EXPORT));

    const result = await exportProfile('voter-1');

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/v1/profiles/voter-1/export'),
      expect.anything(),
    );
    expect(result.version).toBe(2);
    expect(result.consent[0].action).toBe('granted');
  });

  /**
   * A 404 means "nothing stored" — the backend's deliberate difference from
   * the load endpoint's null-with-200. The message must name anonymous mode,
   * so a voter who never had an account does not read it as data loss.
   */
  it('throws ProfileLoadError naming anonymous mode when nothing is stored', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: 'there is no stored profile to export.' }, 404),
    );
    await expect(exportProfile('voter-with-nothing')).rejects.toThrow(/anonymous mode/i);
  });

  it('throws ProfileLoadError on any other non-2xx response', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'boom' }, 500));
    await expect(exportProfile('voter-1')).rejects.toThrow(ProfileLoadError);
  });
});

describe('deleteProfile', () => {
  it('DELETEs /v1/profiles/{user_id} and returns both named guarantees', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        deleted: true,
        key_shredded: true,
        consent_withdrawal_recorded: true,
        elapsed_seconds: 0.42,
      }),
    );

    const result = await deleteProfile('voter-1');

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/v1/profiles/voter-1'),
      expect.objectContaining({ method: 'DELETE' }),
    );
    expect(result.deleted).toBe(true);
    expect(result.key_shredded).toBe(true);
    expect(result.elapsed_seconds).toBeLessThan(60);
  });

  it('throws ProfileSaveError on a non-2xx response', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'boom' }, 500));
    await expect(deleteProfile('voter-1')).rejects.toThrow(ProfileSaveError);
  });
});

describe('anonymous profile storage (story 14.3)', () => {
  it('round-trips a profile through this browser only, with no network call', () => {
    const profile = {
      interests: [
        {
          topic_id: 'test:t1:v1',
          stance_id: 'test:t1:v1:s1',
          tier: 'primary' as const,
          intensity: 2 as const,
          dealbreaker: true,
        },
      ],
      evidence_mode: 'blend' as const,
    };

    saveAnonymousProfile(profile);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(loadAnonymousProfile()?.profile).toEqual(profile);
  });

  it('clears to nothing, leaving no trace in this browser', () => {
    saveAnonymousProfile({ interests: [] });
    clearAnonymousProfile();
    expect(loadAnonymousProfile()).toBeNull();
    expect(window.localStorage.getItem(ANONYMOUS_PROFILE_STORAGE_KEY)).toBeNull();
  });

  it('treats a corrupted stored value as absent rather than throwing at the voter', () => {
    window.localStorage.setItem(ANONYMOUS_PROFILE_STORAGE_KEY, '{not json');
    expect(loadAnonymousProfile()).toBeNull();
  });
});
