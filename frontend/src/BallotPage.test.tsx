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
});
