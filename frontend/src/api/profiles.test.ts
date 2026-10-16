import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ProfileLoadError,
  ProfileSaveError,
  fetchTierWeights,
  followPerson,
  loadProfile,
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
