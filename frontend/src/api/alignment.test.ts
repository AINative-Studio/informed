import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AlignmentError, type CandidateAlignment, fetchAlignment } from './alignment';

/**
 * Story 7.3: the `/v1/candidates/{id}/alignment` client.
 *
 * Mirrors `backend/app/scoring/models.py`'s `CandidateAlignment`. The central
 * guarantees under test: `profile_id` always travels as a query parameter
 * (the backend requires it — see `app.scoring.router`'s docstring on why
 * there is no anonymous alignment), and a non-2xx answer raises
 * `AlignmentError` with a message fit to show a voter rather than the raw
 * `detail` text.
 */

const RESPONSE: CandidateAlignment = {
  candidacy_id: 'election:district:person-1',
  person_id: 'person:ZZ000001',
  score: 74.5,
  confidence_badge: 'high',
  evidence_count: 12,
  scored_interest_count: 1,
  dealbreaker_warnings: [],
  reasons: [
    {
      stance_id: 'topic:prescription-drug-pricing:lower-costs',
      direction: 'raised',
      s_value: 0.6,
      confidence: 0.7,
      evidence_count: 12,
      citations: [
        {
          roll_call_id: 'us-house:119-2:452',
          position: 'yea',
          vote_type: 'final_passage',
          voted_at: '2026-03-01T00:00:00Z',
          bill_id: 'us-bill:119:hr1',
          is_bundled: false,
          bundled_reason: null,
        },
      ],
    },
  ],
  methodology_version: '2026.1',
};

describe('fetchAlignment', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('fetches alignment for a candidacy and profile', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => RESPONSE,
    });

    const result = await fetchAlignment({
      candidacyId: RESPONSE.candidacy_id,
      profileId: 'demo-user-1',
    });

    expect(result).toEqual(RESPONSE);
    const [url] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain(
      `/v1/candidates/${encodeURIComponent(RESPONSE.candidacy_id)}/alignment`,
    );
    expect(String(url)).toContain('profile_id=demo-user-1');
  });

  it('raises AlignmentError with a readable message on a 404 (unknown candidacy or profile)', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: 'no candidacy known' }),
    });

    await expect(
      fetchAlignment({ candidacyId: 'nope', profileId: 'demo-user-1' }),
    ).rejects.toThrow(AlignmentError);
  });

  it('raises AlignmentError on a server error with a message fit for a voter', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'boom: stack trace leaked here' }),
    });

    await expect(
      fetchAlignment({ candidacyId: 'x', profileId: 'demo-user-1' }),
    ).rejects.toThrow(/could not/i);
  });
});
