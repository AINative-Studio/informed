import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ChallengerPromises } from '../api/promises';
import { ChallengerPromisesView } from './ChallengerPromisesView';

/**
 * Unit tests for the challenger promises view (story 9.6).
 *
 * "As a voter, I want to see a challenger's promises even though they have
 * no voting record, so that I can still compare them." Acceptance: promises
 * are grouped by my interests; a prior record in another office is linked
 * if one exists.
 *
 * `fetch` is stubbed rather than the query hook, matching
 * `CandidateDetailPage.test.tsx`'s posture: this proves the component's own
 * TanStack Query wiring and render, not a mocked hook.
 */

const PROMISES: ChallengerPromises = {
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
    {
      stance_id: 'topic:climate:carbon-pricing',
      promises: [
        {
          promise_id: 'prm:0002',
          quote_span: "I'll fight for a cleaner future",
          actor: 'the candidate',
          action: null,
          object: null,
          direction: 'unclear',
          testable: false,
          source_url: 'https://challenger.example.test/debate',
          captured_at: '2026-08-15T00:00:00Z',
        },
      ],
    },
  ],
  unmatched_count: 2,
  prior_offices: [],
};

const PROMISES_WITH_PRIOR_OFFICE: ChallengerPromises = {
  ...PROMISES,
  prior_offices: [
    {
      body_id: 'us-house',
      district_id: 'ocd-division/country:us/state:zz/cd:3',
      party: 'Synthetic',
      start: '2019-01-03',
      end: '2023-01-03',
    },
  ],
};

const NO_PROMISES: ChallengerPromises = {
  candidacy_id: 'election:district:challenger-2',
  person_id: 'person:ZZ000003',
  groups: [],
  unmatched_count: 0,
  prior_offices: [],
};

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

describe('ChallengerPromisesView', () => {
  it('shows a group for each of the voter matching interests', async () => {
    respondWith(PROMISES);
    renderWithQuery(
      <ChallengerPromisesView candidacyId={PROMISES.candidacy_id} profileId="demo-user-1" />,
    );

    expect(
      await screen.findByRole('region', { name: /promises about topic:prescription-drug-pricing/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('region', { name: /promises about topic:climate:carbon-pricing/i }),
    ).toBeInTheDocument();
  });

  it('shows each promise verbatim quote inside its group', async () => {
    respondWith(PROMISES);
    renderWithQuery(
      <ChallengerPromisesView candidacyId={PROMISES.candidacy_id} profileId="demo-user-1" />,
    );

    expect(
      await screen.findByText(/I will vote to cap insulin co-pays at \$35\/month/i),
    ).toBeInTheDocument();
  });

  it('labels a non-testable promise as not testable', async () => {
    respondWith(PROMISES);
    renderWithQuery(
      <ChallengerPromisesView candidacyId={PROMISES.candidacy_id} profileId="demo-user-1" />,
    );

    const climateGroup = await screen.findByRole('region', {
      name: /promises about topic:climate:carbon-pricing/i,
    });
    expect(within(climateGroup).getByText(/not testable/i)).toBeInTheDocument();
  });

  it('labels a testable promise as a specific, checkable commitment', async () => {
    respondWith(PROMISES);
    renderWithQuery(
      <ChallengerPromisesView candidacyId={PROMISES.candidacy_id} profileId="demo-user-1" />,
    );

    const drugGroup = await screen.findByRole('region', {
      name: /promises about topic:prescription-drug-pricing/i,
    });
    expect(within(drugGroup).getByText(/specific, checkable commitment/i)).toBeInTheDocument();
  });

  it('reports promises outside the voter interests as a plain count, not hidden', async () => {
    respondWith(PROMISES);
    renderWithQuery(
      <ChallengerPromisesView candidacyId={PROMISES.candidacy_id} profileId="demo-user-1" />,
    );

    expect(
      await screen.findByText(/2 more promises.*do not match one of the priorities/i),
    ).toBeInTheDocument();
  });

  it('opens a drawer with the source link when a promise source control is activated', async () => {
    const user = userEvent.setup();
    respondWith(PROMISES);
    renderWithQuery(
      <ChallengerPromisesView candidacyId={PROMISES.candidacy_id} profileId="demo-user-1" />,
    );

    await screen.findByText(/I will vote to cap insulin co-pays/i);
    await user.click(screen.getAllByRole('button', { name: /view source/i })[0]);

    const dialog = await screen.findByRole('dialog');
    const link = within(dialog).getByRole('link', { name: /open the source/i });
    expect(link.getAttribute('href')).toBe('https://challenger.example.test/promises');
    expect(within(dialog).getByText(/2026-09-01|September 1, 2026/)).toBeInTheDocument();
  });

  it('shows a prior office when the challenger previously served in one', async () => {
    respondWith(PROMISES_WITH_PRIOR_OFFICE);
    renderWithQuery(
      <ChallengerPromisesView
        candidacyId={PROMISES_WITH_PRIOR_OFFICE.candidacy_id}
        profileId="demo-user-1"
      />,
    );

    const priorOffice = await screen.findByRole('region', { name: /prior elected office/i });
    expect(within(priorOffice).getByText(/U\.S\. House of Representatives/i)).toBeInTheDocument();
    expect(within(priorOffice).getByText(/2019.*2023/)).toBeInTheDocument();
  });

  it('shows no prior-office section when the challenger has never held office', async () => {
    respondWith(PROMISES);
    renderWithQuery(
      <ChallengerPromisesView candidacyId={PROMISES.candidacy_id} profileId="demo-user-1" />,
    );

    await screen.findByText(/I will vote to cap insulin co-pays/i);
    expect(screen.queryByRole('region', { name: /prior elected office/i })).not.toBeInTheDocument();
  });

  it('shows an honest empty state when the candidate has no matching promises', async () => {
    respondWith(NO_PROMISES);
    renderWithQuery(
      <ChallengerPromisesView candidacyId={NO_PROMISES.candidacy_id} profileId="demo-user-1" />,
    );

    expect(await screen.findByText(/no promises on file yet/i)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /promises about your priorities/i })).not.toBeInTheDocument();
  });

  it('never recommends, endorses or ranks the candidate', async () => {
    respondWith(PROMISES);
    const { container } = renderWithQuery(
      <ChallengerPromisesView candidacyId={PROMISES.candidacy_id} profileId="demo-user-1" />,
    );

    await screen.findByText(/I will vote to cap insulin co-pays/i);
    expect(container.textContent ?? '').not.toMatch(/vote for|recommend|endorse|best match|ranked/i);
  });

  it('shows a readable message when the promises request fails', async () => {
    respondWith({ detail: 'no stored profile' }, 404);
    renderWithQuery(<ChallengerPromisesView candidacyId="x" profileId="demo-user-1" />);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });
});
