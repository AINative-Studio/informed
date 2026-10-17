import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  type CandidateSummaryCard,
  SummaryCardError,
  fetchSummaryCard,
  shareTextFor,
  shareUrlFor,
} from './summaryCard';

/**
 * Story 14.6: the `/v1/candidates/{id}/summary-card` client and the two
 * share-text/share-url helpers. The central guarantee under test is privacy:
 * nothing here ever reads from, or constructs a request shape that could
 * carry, a voter's saved interests — `fetchSummaryCard` takes one argument
 * (`candidacyId`), full stop.
 */

const CARD: CandidateSummaryCard = {
  candidacy_id: 'election:district:person',
  person_name: 'Pat Rivera',
  party: 'IND',
  district: 'Test District 9',
  photo_url: null,
  verdict_counts: null,
  say_do_gap: null,
  positions_summary: null,
  updated_at: '2026-10-17T00:00:00Z',
};

describe('fetchSummaryCard', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('fetches the card for a candidacy id', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => CARD,
    });

    const card = await fetchSummaryCard('election:district:person');

    expect(card).toEqual(CARD);
    const [url] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain('/v1/candidates/election%3Adistrict%3Aperson/summary-card');
  });

  it('never sends a profile, user or voter identity on the request', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => CARD,
    });

    await fetchSummaryCard('election:district:person');

    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).not.toMatch(/profile|user_id|voter/i);
    expect(JSON.stringify(init ?? {})).not.toMatch(/profile|user_id|voter/i);
  });

  it('raises SummaryCardError with a 404-specific message', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: 'not found' }),
    });

    await expect(fetchSummaryCard('does-not-exist')).rejects.toThrow(SummaryCardError);
    try {
      await fetchSummaryCard('does-not-exist');
    } catch (error) {
      expect(error).toBeInstanceOf(SummaryCardError);
      expect((error as SummaryCardError).status).toBe(404);
    }
  });

  it('raises a generic message for a non-404 error', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'boom' }),
    });

    await expect(fetchSummaryCard('x')).rejects.toThrow(/could not load/i);
  });
});

describe('shareTextFor', () => {
  it('builds a plain-text summary with the party in parens', () => {
    expect(shareTextFor(CARD)).toBe(
      'Pat Rivera (IND) — Test District 9. Candidate summary from Informed: ' +
        'nonpartisan, evidence-based, and about the candidate only.',
    );
  });

  it('omits the parens when party is unknown', () => {
    const noParty: CandidateSummaryCard = { ...CARD, party: null };
    expect(shareTextFor(noParty)).toBe(
      'Pat Rivera — Test District 9. Candidate summary from Informed: ' +
        'nonpartisan, evidence-based, and about the candidate only.',
    );
  });

  it('never contains anything that looks like a voter field', () => {
    expect(shareTextFor(CARD)).not.toMatch(/profile|interest|user_id|voter/i);
  });
});

describe('shareUrlFor', () => {
  it('builds a shareable URL from the candidacy id and an explicit origin', () => {
    expect(shareUrlFor('election:district:person', 'https://informed.example')).toBe(
      'https://informed.example/candidates/election%3Adistrict%3Aperson/summary-card',
    );
  });
});
