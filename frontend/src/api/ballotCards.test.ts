import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  BallotCardsError,
  CURRENT_ELECTION_ID,
  type BallotCardsResponse,
  electionIdFor,
  fetchBallotCards,
} from './ballotCards';

/**
 * Story 7.1: the `/v1/ballot/cards` client.
 *
 * Mirrors `backend/app/ballot/models.py`'s `BallotCardsResponse`/
 * `CandidateCardView` (story 7.1's own additions on top of story 7.2's read
 * model). The central guarantees under test: the request never carries a
 * personalized score of any kind, and `electionIdFor` falls back to the one
 * permanently-seeded election id rather than failing when `race.election_id`
 * is null — which it always is today, because `/v1/ballot`'s own `_race()`
 * has not been wired to real `candidacies` rows yet (story 2.6 only loads
 * them; nothing reads them back into this response). See this module's
 * docstring for why that fallback is honest rather than invented.
 */

const RESPONSE: BallotCardsResponse = {
  election_id: CURRENT_ELECTION_ID,
  district_id: 'ocd-division/country:us/state:ca/cd:19',
  cards: [
    {
      candidacy_id: 'election:district:person-1',
      person_id: 'person:ZZ000001',
      person_name: 'Rep. Synthetic Example',
      party: 'Independent',
      district: 'California 19th Congressional District',
      photo_url: null,
      is_incumbent: true,
      verdict_counts: null,
      say_do_gap: null,
      positions_summary: null,
      confidence_badge: 'insufficient',
      methodology_version: null,
      updated_at: '2026-10-19T00:00:00Z',
    },
  ],
};

describe('fetchBallotCards', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('fetches cards for an election and district', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => RESPONSE,
    });

    const result = await fetchBallotCards({
      electionId: CURRENT_ELECTION_ID,
      districtId: RESPONSE.district_id,
    });

    expect(result).toEqual(RESPONSE);
    const [url] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain('/v1/ballot/cards');
    expect(String(url)).toContain(`election_id=${encodeURIComponent(CURRENT_ELECTION_ID)}`);
    expect(String(url)).toContain(`district_id=${encodeURIComponent(RESPONSE.district_id)}`);
  });

  it('never sends a profile, user or voter identity on the request', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => RESPONSE,
    });

    await fetchBallotCards({ electionId: CURRENT_ELECTION_ID, districtId: RESPONSE.district_id });

    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).not.toMatch(/profile|user_id|voter/i);
    expect(JSON.stringify(init ?? {})).not.toMatch(/profile|user_id|voter/i);
  });

  it('raises BallotCardsError with a readable message on a server error', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'boom' }),
    });

    await expect(
      fetchBallotCards({ electionId: CURRENT_ELECTION_ID, districtId: 'x' }),
    ).rejects.toThrow(BallotCardsError);
  });

  it('raises BallotCardsError on a 422 with a readable message', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({ detail: 'missing district_id' }),
    });

    await expect(
      fetchBallotCards({ electionId: CURRENT_ELECTION_ID, districtId: 'x' }),
    ).rejects.toThrow(/could not/i);
  });
});

describe('electionIdFor', () => {
  it('uses the race-provided election id when one exists', () => {
    expect(electionIdFor({ election_id: 'some-other-election' })).toBe('some-other-election');
  });

  it('falls back to the permanently-seeded election id when the race carries none', () => {
    expect(electionIdFor({ election_id: null })).toBe(CURRENT_ELECTION_ID);
  });
});
