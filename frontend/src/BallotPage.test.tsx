import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BallotPage } from './BallotPage';
import type { Ballot } from './api/ballot';
import type { BallotCardsResponse } from './api/ballotCards';
import { CURRENT_ELECTION_ID } from './api/ballotCards';

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

  describe('the whole ballot, with scores and confidence (story 7.1)', () => {
    const DISTRICT_ID = 'ocd-division/country:us/state:ca/cd:19';
    const SENATE_DIVISION_ID = 'ocd-division/country:us/state:ca';

    const HOUSE_RACE_CANDIDACY_ID = `${CURRENT_ELECTION_ID}:${DISTRICT_ID}:person:ZZ000001`;
    const SENATE_ONE_CANDIDACY_ID = `${CURRENT_ELECTION_ID}:${SENATE_DIVISION_ID}:person:ZZ000002`;
    const SENATE_TWO_CANDIDACY_ID = `${CURRENT_ELECTION_ID}:${SENATE_DIVISION_ID}:person:ZZ000003`;

    const BALLOT_WITH_RACES: Ballot = {
      ...BALLOT,
      races: [
        {
          id: 'us-house:ocd-division/country:us/state:ca/cd:19',
          body_id: 'us-house',
          office_name: 'U.S. House of Representatives',
          district_id: DISTRICT_ID,
          district_name: 'California 19th Congressional District',
          election_id: null, // real live state today — see api/ballotCards.ts
          candidates: [
            {
              person_id: 'person:ZZ000001',
              name: 'Rep. Synthetic Example',
              party: 'Independent',
              is_incumbent: true,
              candidacy_id: HOUSE_RACE_CANDIDACY_ID,
            },
          ],
        },
        {
          id: 'us-senate:ocd-division/country:us/state:ca',
          body_id: 'us-senate',
          office_name: 'U.S. Senate',
          district_id: SENATE_DIVISION_ID,
          district_name: 'CA',
          election_id: null,
          candidates: [
            {
              person_id: 'person:ZZ000002',
              name: 'Sen. Synthetic One',
              party: null,
              is_incumbent: true,
              candidacy_id: SENATE_ONE_CANDIDACY_ID,
            },
            {
              person_id: 'person:ZZ000003',
              name: 'Sen. Synthetic Two',
              party: null,
              is_incumbent: true,
              candidacy_id: SENATE_TWO_CANDIDACY_ID,
            },
          ],
        },
      ],
    };

    const HOUSE_CARDS: BallotCardsResponse = {
      election_id: CURRENT_ELECTION_ID,
      district_id: DISTRICT_ID,
      cards: [
        {
          candidacy_id: HOUSE_RACE_CANDIDACY_ID,
          person_id: 'person:ZZ000001',
          person_name: 'Rep. Synthetic Example',
          party: 'Independent',
          district: 'California 19th Congressional District',
          photo_url: null,
          is_incumbent: true,
          verdict_counts: null,
          say_do_gap: 0.25,
          positions_summary: [
            { stance_id: 'topic:stance:for', s_value: 0.6, confidence: 0.7, evidence_count: 12 },
          ],
          confidence_badge: 'high',
          methodology_version: '2026.1',
          updated_at: '2026-10-19T00:00:00Z',
        },
      ],
    };

    const SENATE_CARDS: BallotCardsResponse = {
      election_id: CURRENT_ELECTION_ID,
      district_id: SENATE_DIVISION_ID,
      cards: [
        {
          candidacy_id: SENATE_TWO_CANDIDACY_ID,
          person_id: 'person:ZZ000003',
          person_name: 'Sen. Synthetic Two',
          party: null,
          district: 'CA',
          photo_url: null,
          is_incumbent: true,
          verdict_counts: null,
          say_do_gap: null,
          positions_summary: null,
          confidence_badge: 'insufficient',
          methodology_version: null,
          updated_at: '2026-10-19T00:00:00Z',
        },
        {
          candidacy_id: SENATE_ONE_CANDIDACY_ID,
          person_id: 'person:ZZ000002',
          person_name: 'Sen. Synthetic One',
          party: null,
          district: 'CA',
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

    /** Routes `/v1/ballot` to the race fixture and `/v1/ballot/cards` by district. */
    function respondWithRacesAndCards(): void {
      fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/v1/ballot/cards')) {
          const parsed = new URL(url, 'http://localhost');
          const districtId = parsed.searchParams.get('district_id');
          const body = districtId === SENATE_DIVISION_ID ? SENATE_CARDS : HOUSE_CARDS;
          return new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        }
        if (url.includes('/v1/ballot')) {
          return new Response(JSON.stringify(BALLOT_WITH_RACES), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        }
        throw new Error(`unexpected fetch in ballot-view test: ${url}`);
      });
    }

    it('shows each race with its candidates once the ballot and cards have loaded', async () => {
      respondWithRacesAndCards();
      renderWithQuery(<BallotPage />);
      await submitAddress();

      const ballot = await screen.findByRole('region', { name: /your ballot/i });
      expect(within(ballot).getByText('U.S. House of Representatives')).toBeInTheDocument();
      expect(within(ballot).getByText('U.S. Senate')).toBeInTheDocument();
      expect(await within(ballot).findByText('Rep. Synthetic Example')).toBeInTheDocument();
      expect(await within(ballot).findByText('Sen. Synthetic One')).toBeInTheDocument();
      expect(await within(ballot).findByText('Sen. Synthetic Two')).toBeInTheDocument();
    });

    it('shows a loading state for a race while its cards are being fetched', async () => {
      // Every /v1/ballot/cards call is held open deliberately, so the
      // loading state is observed rather than raced against an
      // immediately-resolved mock. Each is resolved with the fixture that
      // matches its own district, same as respondWithRacesAndCards above.
      const resolvers: Array<() => void> = [];
      fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/v1/ballot/cards')) {
          const parsed = new URL(url, 'http://localhost');
          const districtId = parsed.searchParams.get('district_id');
          const body = districtId === SENATE_DIVISION_ID ? SENATE_CARDS : HOUSE_CARDS;
          return new Promise<Response>((resolve) => {
            resolvers.push(() =>
              resolve(
                new Response(JSON.stringify(body), {
                  status: 200,
                  headers: { 'content-type': 'application/json' },
                }),
              ),
            );
          });
        }
        return new Response(JSON.stringify(BALLOT_WITH_RACES), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      });
      renderWithQuery(<BallotPage />);
      await submitAddress();

      const ballot = await screen.findByRole('region', { name: /your ballot/i });
      expect(within(ballot).getAllByText(/loading/i).length).toBeGreaterThan(0);

      resolvers.forEach((resolve) => resolve());
      await within(ballot).findByText('Rep. Synthetic Example');
      await within(ballot).findByText('Sen. Synthetic One');
    });

    it('labels every candidate card with its incumbency', async () => {
      respondWithRacesAndCards();
      renderWithQuery(<BallotPage />);
      await submitAddress();
      const ballot = await screen.findByRole('region', { name: /your ballot/i });

      await within(ballot).findByText('Rep. Synthetic Example');
      const incumbentLabels = within(ballot).getAllByText(/incumbent/i);
      expect(incumbentLabels.length).toBeGreaterThan(0);
    });

    it('keeps the candidates in ballot order (name order), never sorted by score', async () => {
      respondWithRacesAndCards();
      renderWithQuery(<BallotPage />);
      await submitAddress();
      const ballot = await screen.findByRole('region', { name: /your ballot/i });

      const senateList = await within(ballot).findByRole('list', { name: /u\.s\. senate/i });
      await within(senateList).findByText('Sen. Synthetic One');
      const names = within(senateList)
        .getAllByRole('listitem')
        .map((item) => item.textContent ?? '');
      // The backend already returns cards in stable name order
      // (CandidateCardReader.cards_for_race); this pins that the UI renders
      // them in the order the API gave, rather than re-sorting by say_do_gap
      // or confidence — which would read as a ranking, forbidden by prd.md.
      expect(names.findIndex((text) => text.includes('Sen. Synthetic One'))).toBeLessThan(
        names.findIndex((text) => text.includes('Sen. Synthetic Two')),
      );
    });

    it('shows the confidence badge and say/do gap when the card has them', async () => {
      respondWithRacesAndCards();
      renderWithQuery(<BallotPage />);
      await submitAddress();
      const ballot = await screen.findByRole('region', { name: /your ballot/i });

      await within(ballot).findByText('Rep. Synthetic Example');
      expect(within(ballot).getByText(/high confidence/i)).toBeInTheDocument();
    });

    it('shows an honest insufficient-evidence state rather than a fabricated score', async () => {
      respondWithRacesAndCards();
      renderWithQuery(<BallotPage />);
      await submitAddress();
      const ballot = await screen.findByRole('region', { name: /your ballot/i });

      await within(ballot).findByText('Sen. Synthetic One');
      expect(within(ballot).getAllByText(/insufficient/i).length).toBeGreaterThan(0);
    });

    it('never recommends a candidate anywhere in the ballot view', async () => {
      respondWithRacesAndCards();
      renderWithQuery(<BallotPage />);
      await submitAddress();
      const ballot = await screen.findByRole('region', { name: /your ballot/i });
      await within(ballot).findByText('Rep. Synthetic Example');
      await within(ballot).findByText('Sen. Synthetic One');

      expect(ballot.textContent ?? '').not.toMatch(/vote for|recommend|endorse|best match/i);
    });

    it('shows a race with no cards yet as an honest empty state, not an error', async () => {
      fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/v1/ballot/cards')) {
          return new Response(
            JSON.stringify({ election_id: CURRENT_ELECTION_ID, district_id: 'x', cards: [] }),
            { status: 200, headers: { 'content-type': 'application/json' } },
          );
        }
        return new Response(JSON.stringify(BALLOT_WITH_RACES), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      });
      renderWithQuery(<BallotPage />);
      await submitAddress();
      const ballot = await screen.findByRole('region', { name: /your ballot/i });

      expect(await within(ballot).findAllByText(/not available yet/i)).not.toHaveLength(0);
      expect(within(ballot).queryByRole('alert')).not.toBeInTheDocument();
    });
  });
});
