import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  type ChallengerPromises,
  ChallengerPromisesError,
  fetchChallengerPromises,
} from './promises';

/**
 * Story 9.6: the `/v1/candidates/{id}/promises` client.
 *
 * Mirrors `backend/app/promises/models.py`'s `ChallengerPromises`. Same
 * central guarantees as `alignment.test.ts`: `profile_id` always travels as
 * a query parameter, the candidacy id is never URI-encoded (it may itself
 * contain an OCD-ID's slashes), and a non-2xx answer raises
 * `ChallengerPromisesError` with a voter-safe message.
 */

const RESPONSE: ChallengerPromises = {
  candidacy_id: 'election:district:challenger-1',
  person_id: 'person:ZZ000002',
  groups: [
    {
      stance_id: 'topic:prescription-drug-pricing:lower-costs',
      promises: [
        {
          promise_id: 'prm:0001',
          quote_span: 'I will vote to cap insulin co-pays at $35/month',
          actor: 'the candidate',
          action: 'vote to cap',
          object: 'insulin co-pays at $35/month',
          direction: 'support',
          testable: true,
          source_url: 'https://challenger.example.test/promises',
          captured_at: '2026-09-01T00:00:00Z',
        },
      ],
    },
  ],
  unmatched_count: 0,
  prior_offices: [],
};

describe('fetchChallengerPromises', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('fetches promises for a candidacy and profile', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => RESPONSE,
    });

    const result = await fetchChallengerPromises({
      candidacyId: RESPONSE.candidacy_id,
      profileId: 'demo-user-1',
    });

    expect(result).toEqual(RESPONSE);
    const [url] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    // Not URI-encoded, for the same reason `alignment.test.ts` asserts this:
    // the backend route uses a `:path` converter because a real candidacy id
    // contains slashes (an OCD-ID district segment).
    expect(String(url)).toContain(`/v1/candidates/${RESPONSE.candidacy_id}/promises`);
    expect(String(url)).toContain('profile_id=demo-user-1');
  });

  it('raises ChallengerPromisesError with a readable message on a 404', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: 'no candidacy known' }),
    });

    await expect(
      fetchChallengerPromises({ candidacyId: 'nope', profileId: 'demo-user-1' }),
    ).rejects.toThrow(ChallengerPromisesError);
  });

  it('raises ChallengerPromisesError on a server error with a message fit for a voter', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'boom: stack trace leaked here' }),
    });

    await expect(
      fetchChallengerPromises({ candidacyId: 'x', profileId: 'demo-user-1' }),
    ).rejects.toThrow(/could not/i);
  });
});
