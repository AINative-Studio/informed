import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CandidateAlignment } from '../api/alignment';
import { CandidateDetailPage } from './CandidateDetailPage';

/**
 * Unit tests for the candidate detail page (story 7.3).
 *
 * "As a voter, I want a candidate page that breaks the score down interest
 * by interest, so that I see where we agree and disagree." Acceptance: one
 * row per interest, with direction, evidence count and links.
 *
 * `fetch` is stubbed rather than the query hook, matching `BallotPage.test.tsx`'s
 * posture: this proves the component's own TanStack Query wiring and render,
 * not a mocked hook.
 */

const ALIGNMENT: CandidateAlignment = {
  candidacy_id: 'election:district:person-1',
  person_id: 'person:ZZ000001',
  score: 74.5,
  confidence_badge: 'high',
  evidence_count: 15,
  scored_interest_count: 2,
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
    {
      stance_id: 'topic:climate:carbon-pricing',
      direction: 'lowered',
      s_value: -0.4,
      confidence: 0.5,
      evidence_count: 3,
      citations: [
        {
          roll_call_id: 'us-house:119-2:460',
          position: 'nay',
          vote_type: 'final_passage',
          voted_at: '2026-04-01T00:00:00Z',
          bill_id: null,
          is_bundled: false,
          bundled_reason: null,
        },
      ],
    },
  ],
  methodology_version: '2026.1',
};

const NO_POSITIONS: CandidateAlignment = {
  candidacy_id: 'election:district:person-1',
  person_id: 'person:ZZ000001',
  score: null,
  confidence_badge: 'insufficient',
  evidence_count: 0,
  scored_interest_count: 0,
  dealbreaker_warnings: [],
  reasons: [],
  methodology_version: '2026.1',
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

describe('CandidateDetailPage', () => {
  it('renders one row per reason', async () => {
    respondWith(ALIGNMENT);
    renderWithQuery(<CandidateDetailPage candidacyId={ALIGNMENT.candidacy_id} profileId="demo-user-1" />);

    const list = await screen.findByRole('list', { name: /interest-by-interest|breakdown/i });
    expect(list.querySelectorAll('li')).toHaveLength(2);
  });

  it('shows each row direction as agrees or disagrees, matching the s_value sign', async () => {
    respondWith(ALIGNMENT);
    renderWithQuery(<CandidateDetailPage candidacyId={ALIGNMENT.candidacy_id} profileId="demo-user-1" />);

    await screen.findByRole('list', { name: /interest-by-interest|breakdown/i });
    expect(screen.getByText(/agrees/i)).toBeInTheDocument();
    expect(screen.getByText(/disagrees/i)).toBeInTheDocument();
  });

  it('shows each row evidence count', async () => {
    respondWith(ALIGNMENT);
    renderWithQuery(<CandidateDetailPage candidacyId={ALIGNMENT.candidacy_id} profileId="demo-user-1" />);

    await screen.findByRole('list', { name: /interest-by-interest|breakdown/i });
    expect(screen.getByText(/12/)).toBeInTheDocument();
    expect(screen.getByText(/3/)).toBeInTheDocument();
  });

  it('renders at least one real, clickable citation link per row', async () => {
    respondWith(ALIGNMENT);
    renderWithQuery(<CandidateDetailPage candidacyId={ALIGNMENT.candidacy_id} profileId="demo-user-1" />);

    await screen.findByRole('list', { name: /interest-by-interest|breakdown/i });
    const links = screen.getAllByRole('link');
    expect(links.length).toBeGreaterThanOrEqual(2);
    for (const link of links) {
      const href = link.getAttribute('href');
      expect(href).toBeTruthy();
      expect(href).not.toBe('#');
    }
  });

  it('shows an honest empty state when nothing has been measured', async () => {
    respondWith(NO_POSITIONS);
    renderWithQuery(<CandidateDetailPage candidacyId={NO_POSITIONS.candidacy_id} profileId="demo-user-1" />);

    expect(await screen.findByText(/nothing measured|no measured|not available/i)).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: /interest-by-interest|breakdown/i })).not.toBeInTheDocument();
  });

  it('never recommends, endorses or ranks the candidate', async () => {
    respondWith(ALIGNMENT);
    const { container } = renderWithQuery(
      <CandidateDetailPage candidacyId={ALIGNMENT.candidacy_id} profileId="demo-user-1" />,
    );

    await screen.findByRole('list', { name: /interest-by-interest|breakdown/i });
    expect(container.textContent ?? '').not.toMatch(/vote for|recommend|endorse|best match|ranked/i);
  });

  it('shows a readable message when the alignment request fails', async () => {
    respondWith({ detail: 'no stored profile' }, 404);
    renderWithQuery(<CandidateDetailPage candidacyId="x" profileId="demo-user-1" />);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });
});
