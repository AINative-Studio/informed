import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CandidateAlignment } from '../api/alignment';
import { CompareView } from './CompareView';

/**
 * Unit tests for the side-by-side compare view (story 7.4).
 *
 * "As a voter, I want to compare candidates in a race side by side, so that
 * I can see the differences on what matters to me." Acceptance: supports
 * 2-4 candidates; same rows across candidates; colors carry text labels too.
 *
 * `fetch` is stubbed per candidacy id, matching `CandidateDetailPage.test.tsx`'s
 * and `BallotView.test.tsx`'s posture: this proves the component's own
 * TanStack Query wiring and render, not a mocked hook.
 */

const CANDIDACY_A = 'election:district:person-a';
const CANDIDACY_B = 'election:district:person-b';
const CANDIDACY_C = 'election:district:person-c';

const ALIGNMENT_A: CandidateAlignment = {
  candidacy_id: CANDIDACY_A,
  person_id: 'person:A',
  score: 70,
  confidence_badge: 'high',
  evidence_count: 8,
  scored_interest_count: 2,
  dealbreaker_warnings: [],
  reasons: [
    {
      stance_id: 'topic:shared',
      direction: 'raised',
      s_value: 0.6,
      confidence: 0.7,
      evidence_count: 5,
      citations: [
        {
          roll_call_id: 'us-house:119-2:452',
          position: 'yea',
          vote_type: 'final_passage',
          voted_at: '2026-03-01T00:00:00Z',
          bill_id: 'us-house:119:hr-1',
          is_bundled: false,
          bundled_reason: null,
        },
      ],
    },
    {
      stance_id: 'topic:a-only',
      direction: 'lowered',
      s_value: -0.5,
      confidence: 0.6,
      evidence_count: 3,
      citations: [
        {
          roll_call_id: 'us-house:119-2:453',
          position: 'nay',
          vote_type: 'final_passage',
          voted_at: '2026-04-01T00:00:00Z',
          bill_id: 'us-house:119:hr-2',
          is_bundled: false,
          bundled_reason: null,
        },
      ],
    },
  ],
  methodology_version: '2026.1',
};

const ALIGNMENT_B: CandidateAlignment = {
  candidacy_id: CANDIDACY_B,
  person_id: 'person:B',
  score: 40,
  confidence_badge: 'medium',
  evidence_count: 4,
  scored_interest_count: 1,
  dealbreaker_warnings: [],
  reasons: [
    {
      stance_id: 'topic:shared',
      direction: 'lowered',
      s_value: -0.3,
      confidence: 0.5,
      evidence_count: 4,
      citations: [
        {
          roll_call_id: 'us-house:119-2:460',
          position: 'nay',
          vote_type: 'final_passage',
          voted_at: '2026-04-01T00:00:00Z',
          bill_id: 'us-house:119:hr-8',
          is_bundled: false,
          bundled_reason: null,
        },
      ],
    },
  ],
  methodology_version: '2026.1',
};

const ALIGNMENT_C: CandidateAlignment = {
  candidacy_id: CANDIDACY_C,
  person_id: 'person:C',
  score: null,
  confidence_badge: 'insufficient',
  evidence_count: 0,
  scored_interest_count: 0,
  dealbreaker_warnings: [],
  reasons: [
    {
      stance_id: 'topic:shared',
      direction: 'raised',
      s_value: 0,
      confidence: 0.5,
      evidence_count: 2,
      citations: [
        {
          roll_call_id: 'us-house:119-2:470',
          position: 'present',
          vote_type: 'final_passage',
          voted_at: '2026-05-01T00:00:00Z',
          bill_id: 'us-house:119:hr-9',
          is_bundled: false,
          bundled_reason: null,
        },
      ],
    },
  ],
  methodology_version: '2026.1',
};

const ALIGNMENT_BY_CANDIDACY: Record<string, CandidateAlignment> = {
  [CANDIDACY_A]: ALIGNMENT_A,
  [CANDIDACY_B]: ALIGNMENT_B,
  [CANDIDACY_C]: ALIGNMENT_C,
};

const NAMES: Record<string, string> = {
  [CANDIDACY_A]: 'Rep. Synthetic Alpha',
  [CANDIDACY_B]: 'Rep. Synthetic Beta',
  [CANDIDACY_C]: 'Rep. Synthetic Gamma',
};

function renderWithQuery(ui: ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input: string | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const match = /\/v1\/candidates\/(.+)\/alignment/.exec(url);
    const candidacyId = match ? decodeURIComponent(match[1]).split('?')[0] : '';
    const body = ALIGNMENT_BY_CANDIDACY[candidacyId];
    if (!body) {
      return new Response(JSON.stringify({ detail: 'not found' }), { status: 404 });
    }
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('CompareView', () => {
  it('renders a column for every selected candidate', async () => {
    renderWithQuery(
      <CompareView
        candidacyIds={[CANDIDACY_A, CANDIDACY_B]}
        namesByCandidacyId={NAMES}
        profileId="demo-user-1"
      />,
    );

    const table = await screen.findByRole('table');
    expect(within(table).getByRole('columnheader', { name: NAMES[CANDIDACY_A] })).toBeInTheDocument();
    expect(within(table).getByRole('columnheader', { name: NAMES[CANDIDACY_B] })).toBeInTheDocument();
  });

  it('uses the union of every candidate\'s stance ids as its row set', async () => {
    renderWithQuery(
      <CompareView
        candidacyIds={[CANDIDACY_A, CANDIDACY_B]}
        namesByCandidacyId={NAMES}
        profileId="demo-user-1"
      />,
    );

    await screen.findByRole('table');
    // `topic:a-only` belongs to A alone, but must still be a row: the row
    // set is the union, not the intersection of what each candidate has.
    expect(screen.getByRole('row', { name: /topic:shared/ })).toBeInTheDocument();
    expect(screen.getByRole('row', { name: /topic:a-only/ })).toBeInTheDocument();
  });

  it('shows an honest empty state, not a fabricated value, for a candidate with no position on a row', async () => {
    renderWithQuery(
      <CompareView
        candidacyIds={[CANDIDACY_A, CANDIDACY_B]}
        namesByCandidacyId={NAMES}
        profileId="demo-user-1"
      />,
    );

    await screen.findByRole('table');
    const row = screen.getByRole('row', { name: /topic:a-only/ });
    expect(within(row).getByText(/no measured position/i)).toBeInTheDocument();
  });

  it('never shows direction by color alone: every direction cell also carries a text label', async () => {
    renderWithQuery(
      <CompareView
        candidacyIds={[CANDIDACY_A, CANDIDACY_B, CANDIDACY_C]}
        namesByCandidacyId={NAMES}
        profileId="demo-user-1"
      />,
    );

    await screen.findByRole('table');
    const cells = document.querySelectorAll('[data-direction]');
    expect(cells.length).toBeGreaterThan(0);
    for (const cell of Array.from(cells)) {
      expect(cell.textContent ?? '').toMatch(/agrees|disagrees|neutral/i);
    }
  });

  it('renders a true semantic measured-neutral distinctly from an unmeasured row', async () => {
    renderWithQuery(
      <CompareView
        candidacyIds={[CANDIDACY_A, CANDIDACY_C]}
        namesByCandidacyId={NAMES}
        profileId="demo-user-1"
      />,
    );

    await screen.findByRole('table');
    // C's s_value is exactly 0 on topic:shared — a real measured-neutral
    // outcome, not "no measured position" (CandidateDetailPage.tsx's own
    // DirectionLabel makes this same distinction for a single candidate).
    const row = screen.getByRole('row', { name: /topic:shared/ });
    expect(within(row).getByText(/neutral/i)).toBeInTheDocument();
    expect(within(row).queryByText(/no measured position/i)).not.toBeInTheDocument();
  });

  it('never uses recommending or ranking language', async () => {
    renderWithQuery(
      <CompareView
        candidacyIds={[CANDIDACY_A, CANDIDACY_B]}
        namesByCandidacyId={NAMES}
        profileId="demo-user-1"
      />,
    );

    const table = await screen.findByRole('table');
    expect(table.textContent ?? '').not.toMatch(/vote for|recommend|endorse|best match|winner|ranked/i);
  });

  it('requires at least two candidates', () => {
    renderWithQuery(
      <CompareView candidacyIds={[CANDIDACY_A]} namesByCandidacyId={NAMES} profileId="demo-user-1" />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(/at least two/i);
  });

  it('caps at four candidates', () => {
    renderWithQuery(
      <CompareView
        candidacyIds={[CANDIDACY_A, CANDIDACY_B, CANDIDACY_C, 'd', 'e']}
        namesByCandidacyId={NAMES}
        profileId="demo-user-1"
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent(/up to four/i);
  });
});
