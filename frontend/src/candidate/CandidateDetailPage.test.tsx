import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CandidateAlignment } from '../api/alignment';
import type { BillText } from '../api/billText';
import { CandidateDetailPage, billUrlFor } from './CandidateDetailPage';

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
          bill_id: 'us-house:119:hr-1',
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
          bill_id: 'us-house:119:hr-8',
          is_bundled: false,
          bundled_reason: null,
        },
      ],
    },
  ],
  methodology_version: '2026.1',
};

/** A reason whose only citation is a procedural vote with no bill attached. */
const ALIGNMENT_WITH_NO_BILL_CITATION: CandidateAlignment = {
  candidacy_id: 'election:district:person-1',
  person_id: 'person:ZZ000001',
  score: 60,
  confidence_badge: 'low',
  evidence_count: 1,
  scored_interest_count: 1,
  dealbreaker_warnings: [],
  reasons: [
    {
      stance_id: 'topic:procedure:cloture',
      direction: 'raised',
      s_value: 0.3,
      confidence: 0.4,
      evidence_count: 1,
      citations: [
        {
          roll_call_id: 'us-senate:119-2:12',
          position: 'yea',
          vote_type: 'cloture',
          voted_at: '2026-02-01T00:00:00Z',
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

    const list = await screen.findByRole('list', { name: /interest-by-interest|breakdown/i });
    const rows = within(list).getAllByRole('listitem');
    expect(within(rows[0]).getByText(/^agrees with your stance$/i)).toBeInTheDocument();
    expect(within(rows[1]).getByText(/^disagrees with your stance$/i)).toBeInTheDocument();
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

  it('shows the roll call as plain text, never a fabricated link, when no bill is attached', async () => {
    respondWith(ALIGNMENT_WITH_NO_BILL_CITATION);
    renderWithQuery(
      <CandidateDetailPage
        candidacyId={ALIGNMENT_WITH_NO_BILL_CITATION.candidacy_id}
        profileId="demo-user-1"
      />,
    );

    await screen.findByRole('list', { name: /interest-by-interest|breakdown/i });
    expect(screen.getByText(/us-senate:119-2:12/)).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
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

/**
 * Unit tests for story 7.5's evidence drawer.
 *
 * "As a voter, I want to open any vote or promise behind a score and see the
 * source, so that I can verify it." Acceptance: shows roll-call number,
 * date, question, position, bill link and archived source.
 *
 * `roll_calls.question` (datamodel.md) is not published by any endpoint this
 * frontend can reach — `Citation` (`backend/app/scoring/models.py`) carries
 * no such field, and `extra="forbid"` means the backend response cannot
 * carry one either. The drawer shows `vote_type` (what kind of vote it was)
 * in its place rather than fabricating question text no endpoint provides —
 * the same "show nothing invented" posture `CitationLink`'s own docstring
 * already takes for a missing bill page.
 */
const BILL_TEXT: BillText = {
  bill_id: 'us-house:119:hr-1',
  file_id: 'file:synthetic-1',
  url: 'https://files.example.test/synthetic-archive.pdf?signature=fake',
  expires_in_seconds: 300,
  sha256: 'synthetic-sha-256',
  size_bytes: 1024,
  content_type: 'application/pdf',
  source_url: 'https://www.congress.gov/bill/119th-congress/house-bill/1/text',
  captured_at: '2026-03-02T00:00:00Z',
  version_type: 'Engrossed in House',
  version_date: '2026-03-01T00:00:00Z',
  format_type: 'pdf',
};

describe('CandidateDetailPage evidence drawer', () => {
  function respondSequence(bodies: { body: unknown; status?: number }[]) {
    let call = 0;
    fetchMock.mockImplementation(() => {
      const next = bodies[Math.min(call, bodies.length - 1)];
      call += 1;
      return Promise.resolve(
        new Response(JSON.stringify(next.body), {
          status: next.status ?? 200,
          headers: { 'content-type': 'application/json' },
        }),
      );
    });
  }

  it('opens a dialog with the roll-call number, date, vote type and position when a citation is activated', async () => {
    const user = userEvent.setup();
    respondSequence([{ body: ALIGNMENT }, { body: BILL_TEXT }]);
    renderWithQuery(<CandidateDetailPage candidacyId={ALIGNMENT.candidacy_id} profileId="demo-user-1" />);

    await screen.findByRole('list', { name: /interest-by-interest|breakdown/i });
    await user.click(screen.getAllByRole('button', { name: /roll call/i })[0]);

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/us-house:119-2:452/)).toBeInTheDocument();
    expect(within(dialog).getByText(/2026-03-01/)).toBeInTheDocument();
    expect(within(dialog).getByText(/final passage/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/yea/i)).toBeInTheDocument();
  });

  it('shows a link to the bill and, once the archive loads, a link to the archived source', async () => {
    const user = userEvent.setup();
    respondSequence([{ body: ALIGNMENT }, { body: BILL_TEXT }]);
    renderWithQuery(<CandidateDetailPage candidacyId={ALIGNMENT.candidacy_id} profileId="demo-user-1" />);

    await screen.findByRole('list', { name: /interest-by-interest|breakdown/i });
    await user.click(screen.getAllByRole('button', { name: /roll call/i })[0]);

    const dialog = await screen.findByRole('dialog');
    const billLink = within(dialog).getByRole('link', { name: /bill/i });
    expect(billLink.getAttribute('href')).toBe('https://www.congress.gov/bill/119th-congress/house-bill/1');

    const archiveLink = await within(dialog).findByRole('link', { name: /archived/i });
    expect(archiveLink.getAttribute('href')).toBe(BILL_TEXT.url);
  });

  it('shows an honest message when the archived source fails to load, without a fabricated link', async () => {
    const user = userEvent.setup();
    respondSequence([{ body: ALIGNMENT }, { body: { detail: 'not archived' }, status: 404 }]);
    renderWithQuery(<CandidateDetailPage candidacyId={ALIGNMENT.candidacy_id} profileId="demo-user-1" />);

    await screen.findByRole('list', { name: /interest-by-interest|breakdown/i });
    await user.click(screen.getAllByRole('button', { name: /roll call/i })[0]);

    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByRole('alert')).toBeInTheDocument();
    expect(within(dialog).queryByRole('link', { name: /archived/i })).not.toBeInTheDocument();
  });

  it('offers no drawer control for a citation with no bill attached', async () => {
    respondWith(ALIGNMENT_WITH_NO_BILL_CITATION);
    renderWithQuery(
      <CandidateDetailPage
        candidacyId={ALIGNMENT_WITH_NO_BILL_CITATION.candidacy_id}
        profileId="demo-user-1"
      />,
    );

    await screen.findByRole('list', { name: /interest-by-interest|breakdown/i });
    expect(screen.getByText(/us-senate:119-2:12/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /roll call/i })).not.toBeInTheDocument();
  });

  it('closes the drawer on Escape and returns focus to the control that opened it', async () => {
    const user = userEvent.setup();
    respondSequence([{ body: ALIGNMENT }, { body: BILL_TEXT }]);
    renderWithQuery(<CandidateDetailPage candidacyId={ALIGNMENT.candidacy_id} profileId="demo-user-1" />);

    await screen.findByRole('list', { name: /interest-by-interest|breakdown/i });
    const opener = screen.getAllByRole('button', { name: /roll call/i })[0];
    await user.click(opener);
    await screen.findByRole('dialog');

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});

describe('billUrlFor', () => {
  it('derives the real congress.gov bill page from a House bill id', () => {
    expect(billUrlFor('us-house:119:hr-1')).toBe(
      'https://www.congress.gov/bill/119th-congress/house-bill/1',
    );
  });

  it('derives the real congress.gov bill page from a Senate bill id', () => {
    expect(billUrlFor('us-senate:119:s-42')).toBe(
      'https://www.congress.gov/bill/119th-congress/senate-bill/42',
    );
  });

  it('derives a joint-resolution bill page', () => {
    expect(billUrlFor('us-house:119:hjres-3')).toBe(
      'https://www.congress.gov/bill/119th-congress/house-joint-resolution/3',
    );
  });

  it('returns null for an id that does not match the documented shape', () => {
    expect(billUrlFor('not-a-real-bill-id')).toBeNull();
  });
});
