import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BallotPage } from './BallotPage';
import type { Ballot } from './api/ballot';

/**
 * Unit tests for the address-entry screen (story 1.5).
 *
 * `fetch` is stubbed rather than the query hook, so the component's own
 * TanStack Query wiring — the thing story 1.5 exists to prove — is under test.
 */

const BALLOT: Ballot = {
  district: {
    id: 'ocd-division/country:us/state:ca/cd:19',
    name: 'California 19th Congressional District',
    state: 'CA',
  },
  officeholders: [
    {
      person_id: 'person:ZZ000001',
      name: 'Rep. Synthetic Example',
      body_id: 'us-house',
      party: 'Independent',
    },
    { person_id: 'person:ZZ000002', name: 'Sen. Synthetic One', body_id: 'us-senate', party: null },
    { person_id: 'person:ZZ000003', name: 'Sen. Synthetic Two', body_id: 'us-senate', party: null },
  ],
};

/** A fresh QueryClient per test: a shared cache would leak between cases. */
function renderWithQuery(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function respondWith(body: unknown, status = 200) {
  fetchMock.mockResolvedValue(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    }),
  );
}

async function submitAddress(address = '1 Synthetic Way, Example, CA 90000') {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/address/i), address);
  await user.click(screen.getByRole('button', { name: /representatives/i }));
  return user;
}

describe('BallotPage', () => {
  it('shows an address input before anything is submitted', () => {
    renderWithQuery(<BallotPage />);
    expect(screen.getByLabelText(/address/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /representatives/i })).toBeInTheDocument();
  });

  it('does not call the API until the voter submits', () => {
    renderWithQuery(<BallotPage />);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('renders one House member and two senators for a submitted address', async () => {
    respondWith(BALLOT);
    renderWithQuery(<BallotPage />);
    await submitAddress();

    expect(await screen.findByText('Rep. Synthetic Example')).toBeInTheDocument();
    const house = await screen.findByRole('list', { name: /house/i });
    const senate = await screen.findByRole('list', { name: /senate/i });
    expect(house.querySelectorAll('li')).toHaveLength(1);
    expect(senate.querySelectorAll('li')).toHaveLength(2);
  });

  it('names the district it resolved the address to', async () => {
    respondWith(BALLOT);
    renderWithQuery(<BallotPage />);
    await submitAddress();

    expect(
      await screen.findByText(/California 19th Congressional District/),
    ).toBeInTheDocument();
  });

  it('asks for an address instead of calling the API when the field is empty', async () => {
    renderWithQuery(<BallotPage />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /representatives/i }));

    expect(await screen.findByText(/enter your address/i)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('sends the address as a query parameter to /v1/ballot', async () => {
    respondWith(BALLOT);
    renderWithQuery(<BallotPage />);
    await submitAddress('99 Example Rd');

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = new URL(String(fetchMock.mock.calls[0][0]), 'http://localhost');
    expect(url.pathname).toBe('/v1/ballot');
    expect(url.searchParams.get('address')).toBe('99 Example Rd');
  });

  it('shows a readable message when the district is not in ZeroDB yet', async () => {
    respondWith({ detail: 'No district found for that address' }, 404);
    renderWithQuery(<BallotPage />);
    await submitAddress();

    expect(await screen.findByRole('alert')).toHaveTextContent(/district/i);
  });

  it('shows a readable message when the API is unreachable', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    renderWithQuery(<BallotPage />);
    await submitAddress();

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('never recommends a candidate', async () => {
    respondWith(BALLOT);
    renderWithQuery(<BallotPage />);
    await submitAddress();
    await screen.findByText('Rep. Synthetic Example');

    expect(document.body.textContent ?? '').not.toMatch(/vote for|recommend|endorse|best match/i);
  });

  it('does not keep the submitted address on screen after it is used', async () => {
    // prd.md, privacy first: the address is a lookup key, not a stored fact.
    // The input keeps what the voter typed (they may want to correct it), but
    // the results must not repeat it back as part of the answer.
    respondWith(BALLOT);
    renderWithQuery(<BallotPage />);
    await submitAddress('123 Private Lane');
    await screen.findByText('Rep. Synthetic Example');

    const results = screen.getByRole('region', { name: /representatives/i });
    expect(results.textContent ?? '').not.toContain('123 Private Lane');
  });

  describe('following an officeholder (story 15.1)', () => {
    it('shows a Follow button next to each officeholder', async () => {
      respondWith(BALLOT);
      renderWithQuery(<BallotPage />);
      await submitAddress();
      await screen.findByText('Rep. Synthetic Example');

      expect(screen.getAllByRole('button', { name: /^follow /i })).toHaveLength(3);
    });

    it('calls the follow endpoint and flips to Unfollow on success', async () => {
      respondWith(BALLOT);
      renderWithQuery(<BallotPage />);
      await submitAddress();
      await screen.findByText('Rep. Synthetic Example');

      fetchMock.mockResolvedValueOnce(
        new Response(JSON.stringify({ followed_person_ids: ['person:ZZ000001'] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      );
      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: /follow rep\. synthetic example/i }));

      const call = await waitFor(() => {
        const match = fetchMock.mock.calls.find(([url]) =>
          String(url).includes('/follow/person%3AZZ000001'),
        );
        expect(match).toBeDefined();
        return match;
      });
      expect(call?.[1]).toMatchObject({ method: 'POST' });

      expect(
        await screen.findByRole('button', { name: /unfollow rep\. synthetic example/i }),
      ).toBeInTheDocument();
    });

    it('calls the unfollow endpoint when an already-followed officeholder is toggled', async () => {
      respondWith(BALLOT);
      renderWithQuery(<BallotPage />);
      await submitAddress();
      await screen.findByText('Rep. Synthetic Example');

      fetchMock.mockResolvedValueOnce(
        new Response(JSON.stringify({ followed_person_ids: ['person:ZZ000001'] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      );
      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: /follow rep\. synthetic example/i }));
      await screen.findByRole('button', { name: /unfollow rep\. synthetic example/i });

      fetchMock.mockResolvedValueOnce(
        new Response(JSON.stringify({ followed_person_ids: [] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      );
      await user.click(screen.getByRole('button', { name: /unfollow rep\. synthetic example/i }));

      const call = await waitFor(() => {
        const match = fetchMock.mock.calls.find(
          ([url, init]) =>
            String(url).includes('/follow/person%3AZZ000001') &&
            (init as RequestInit | undefined)?.method === 'DELETE',
        );
        expect(match).toBeDefined();
        return match;
      });
      expect(call).toBeDefined();

      expect(
        await screen.findByRole('button', { name: /follow rep\. synthetic example/i }),
      ).toBeInTheDocument();
    });

    it('shows a readable message and does not flip state when a follow fails', async () => {
      respondWith(BALLOT);
      renderWithQuery(<BallotPage />);
      await submitAddress();
      await screen.findByText('Rep. Synthetic Example');

      fetchMock.mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: 'not a known person' }), {
          status: 404,
          headers: { 'content-type': 'application/json' },
        }),
      );
      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: /follow rep\. synthetic example/i }));

      expect(await screen.findByRole('alert')).toBeInTheDocument();
      expect(
        screen.getByRole('button', { name: /follow rep\. synthetic example/i }),
      ).toBeInTheDocument();
    });

    it('never recommends who to follow', async () => {
      respondWith(BALLOT);
      renderWithQuery(<BallotPage />);
      await submitAddress();
      await screen.findByText('Rep. Synthetic Example');

      expect(document.body.textContent ?? '').not.toMatch(/vote for|recommend|endorse|best match/i);
    });
  });

  describe('sharing a candidate summary card (story 14.6)', () => {
    const BALLOT_WITH_CANDIDACY: Ballot = {
      ...BALLOT,
      races: [
        {
          id: 'us-house:ocd-division/country:us/state:ca/cd:19',
          body_id: 'us-house',
          office_name: 'U.S. House of Representatives',
          district_id: 'ocd-division/country:us/state:ca/cd:19',
          district_name: 'California 19th Congressional District',
          election_id: 'election',
          candidates: [
            {
              person_id: 'person:ZZ000001',
              name: 'Rep. Synthetic Example',
              party: 'Independent',
              is_incumbent: true,
              candidacy_id: 'election:district:person:ZZ000001',
            },
          ],
        },
      ],
    };

    it('shows a Share button for a candidate who has a candidacy_id', async () => {
      respondWith(BALLOT_WITH_CANDIDACY);
      renderWithQuery(<BallotPage />);
      await submitAddress();
      await screen.findByText('Rep. Synthetic Example');

      expect(
        screen.getByRole('button', { name: /share rep\. synthetic example/i }),
      ).toBeInTheDocument();
    });

    it('shows no Share button when no candidacy_id is known yet', async () => {
      respondWith(BALLOT);
      renderWithQuery(<BallotPage />);
      await submitAddress();
      await screen.findByText('Rep. Synthetic Example');

      expect(screen.queryByRole('button', { name: /^share /i })).not.toBeInTheDocument();
    });

    it('builds a shareable summary containing only candidate data, never the voter profile', async () => {
      respondWith(BALLOT_WITH_CANDIDACY);
      renderWithQuery(<BallotPage />);
      await submitAddress();
      await screen.findByText('Rep. Synthetic Example');

      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: /share rep\. synthetic example/i }));

      const shareText = await screen.findByText(/candidate summary from informed/i);
      expect(shareText.textContent ?? '').toMatch(/Rep\. Synthetic Example/);
      expect(shareText.textContent ?? '').toMatch(/Independent/);
      // Nothing resembling a voter's own data ever appears in the share text.
      expect(shareText.textContent ?? '').not.toMatch(/profile|interest|user_id|voter/i);
    });

    it('the Share button calls the summary-card endpoint with no profile parameter', async () => {
      respondWith(BALLOT_WITH_CANDIDACY);
      renderWithQuery(<BallotPage />);
      await submitAddress();
      await screen.findByText('Rep. Synthetic Example');

      fetchMock.mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            candidacy_id: 'election:district:person:ZZ000001',
            person_name: 'Rep. Synthetic Example',
            party: 'Independent',
            district: 'California 19th Congressional District',
            photo_url: null,
            verdict_counts: null,
            say_do_gap: null,
            positions_summary: null,
            updated_at: '2026-10-17T00:00:00Z',
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
      );
      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: /share rep\. synthetic example/i }));

      const call = await waitFor(() => {
        const match = fetchMock.mock.calls.find(([url]) =>
          String(url).includes('/summary-card'),
        );
        expect(match).toBeDefined();
        return match;
      });
      const [url, init] = call ?? [];
      expect(String(url)).not.toMatch(/profile|user_id|voter/i);
      expect(JSON.stringify(init ?? {})).not.toMatch(/profile|user_id|voter/i);
    });
  });
});
