import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Race } from '../api/ballot';
import { BallotView, sortCardsByScore } from './BallotView';

/**
 * Unit tests for `BallotView`'s story 7.6 sort control.
 *
 * "As a voter, I want to sort candidates by score if I choose, so that I
 * control how results are presented." Acceptance: default order is ballot
 * order; the sort is a user action; there is no "recommended" label.
 *
 * `fetch` is stubbed (matching `CandidateDetailPage.test.tsx`'s posture)
 * rather than the query hook, so this proves the component's own TanStack
 * Query wiring and render, not a mocked hook.
 */

const DISTRICT_ID = 'ocd-division/country:us/state:ca/cd:19';

const RACE: Race = {
  id: 'us-house:ocd-division/country:us/state:ca/cd:19',
  body_id: 'us-house',
  office_name: 'U.S. House of Representatives',
  district_id: DISTRICT_ID,
  district_name: 'California 19th Congressional District',
  election_id: null,
  candidates: [
    {
      person_id: 'person:ZZ000001',
      name: 'Rep. Synthetic Low',
      party: 'Independent',
      is_incumbent: true,
      candidacy_id: 'election:district:person-low',
    },
    {
      person_id: 'person:ZZ000002',
      name: 'Rep. Synthetic High',
      party: 'Independent',
      is_incumbent: false,
      candidacy_id: 'election:district:person-high',
    },
    {
      person_id: 'person:ZZ000003',
      name: 'Rep. Synthetic Unscored',
      party: null,
      is_incumbent: false,
      candidacy_id: 'election:district:person-unscored',
    },
  ],
};

const CARDS_RESPONSE = {
  election_id: 'us-2026-11-03-general',
  district_id: DISTRICT_ID,
  cards: [
    {
      candidacy_id: 'election:district:person-low',
      person_id: 'person:ZZ000001',
      person_name: 'Rep. Synthetic Low',
      party: 'Independent',
      district: 'California 19th Congressional District',
      photo_url: null,
      is_incumbent: true,
      verdict_counts: null,
      say_do_gap: 0.1,
      positions_summary: [{ stance_id: 'topic:x', s_value: 0.5, confidence: 0.6, evidence_count: 4 }],
      confidence_badge: 'high',
      methodology_version: '2026.1',
      updated_at: '2026-10-19T00:00:00Z',
    },
    {
      candidacy_id: 'election:district:person-high',
      person_id: 'person:ZZ000002',
      person_name: 'Rep. Synthetic High',
      party: 'Independent',
      district: 'California 19th Congressional District',
      photo_url: null,
      is_incumbent: false,
      verdict_counts: null,
      say_do_gap: 0.9,
      positions_summary: [{ stance_id: 'topic:x', s_value: 0.9, confidence: 0.8, evidence_count: 9 }],
      confidence_badge: 'high',
      methodology_version: '2026.1',
      updated_at: '2026-10-19T00:00:00Z',
    },
    {
      candidacy_id: 'election:district:person-unscored',
      person_id: 'person:ZZ000003',
      person_name: 'Rep. Synthetic Unscored',
      party: null,
      district: 'California 19th Congressional District',
      photo_url: null,
      is_incumbent: false,
      verdict_counts: null,
      say_do_gap: null,
      positions_summary: null,
      confidence_badge: 'insufficient',
      methodology_version: null,
      updated_at: '2026-10-19T00:00:00Z',
    },
  ],
};

function renderWithQuery(ui: ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(
    new Response(JSON.stringify(CARDS_RESPONSE), { status: 200, headers: { 'content-type': 'application/json' } }),
  );
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function namesInOrder(race: HTMLElement): string[] {
  const items = within(race).getAllByRole('listitem');
  return items.map((item) => item.textContent ?? '');
}

describe('BallotView sort control', () => {
  it('renders candidates in ballot order by default', async () => {
    renderWithQuery(<BallotView races={[RACE]} />);

    const race = await screen.findByRole('heading', { name: RACE.office_name });
    const article = race.closest('article') as HTMLElement;
    await screen.findByText('Rep. Synthetic Low');

    const names = namesInOrder(article);
    expect(names[0]).toContain('Rep. Synthetic Low');
    expect(names[1]).toContain('Rep. Synthetic High');
    expect(names[2]).toContain('Rep. Synthetic Unscored');
  });

  it('has a sort control that is off by default and never labeled as a recommendation', async () => {
    renderWithQuery(<BallotView races={[RACE]} />);
    await screen.findByText('Rep. Synthetic Low');

    const control = screen.getByRole('checkbox', { name: /sort/i });
    expect(control).not.toBeChecked();

    const label = control.getAttribute('aria-label') ?? control.closest('label')?.textContent ?? '';
    expect(label).not.toMatch(/recommend|best match|top pick|endorse/i);
  });

  it('re-orders candidates by score once the voter turns sorting on', async () => {
    const user = userEvent.setup();
    renderWithQuery(<BallotView races={[RACE]} />);
    await screen.findByText('Rep. Synthetic Low');

    const article = screen.getByRole('heading', { name: RACE.office_name }).closest('article') as HTMLElement;
    await user.click(within(article).getByRole('checkbox', { name: /sort/i }));

    const names = namesInOrder(article);
    expect(names[0]).toContain('Rep. Synthetic High');
    expect(names[1]).toContain('Rep. Synthetic Low');
  });

  it('turning sorting back off restores ballot order', async () => {
    const user = userEvent.setup();
    renderWithQuery(<BallotView races={[RACE]} />);
    await screen.findByText('Rep. Synthetic Low');

    const article = screen.getByRole('heading', { name: RACE.office_name }).closest('article') as HTMLElement;
    const control = within(article).getByRole('checkbox', { name: /sort/i });
    await user.click(control);
    await user.click(control);

    const names = namesInOrder(article);
    expect(names[0]).toContain('Rep. Synthetic Low');
    expect(names[1]).toContain('Rep. Synthetic High');
  });

  it('keeps an unscored candidate visible and after scored ones when sorted, without crashing', async () => {
    const user = userEvent.setup();
    renderWithQuery(<BallotView races={[RACE]} />);
    await screen.findByText('Rep. Synthetic Low');

    const article = screen.getByRole('heading', { name: RACE.office_name }).closest('article') as HTMLElement;
    await user.click(within(article).getByRole('checkbox', { name: /sort/i }));

    expect(within(article).getByText('Rep. Synthetic Unscored')).toBeInTheDocument();
    const names = namesInOrder(article);
    expect(names.at(-1)).toContain('Rep. Synthetic Unscored');
  });
});

describe('BallotView compare selection (story 7.4)', () => {
  it('offers no compare control until two candidates are selected', async () => {
    renderWithQuery(<BallotView races={[RACE]} />);
    await screen.findByText('Rep. Synthetic Low');

    const article = screen.getByRole('heading', { name: RACE.office_name }).closest('article') as HTMLElement;
    expect(within(article).queryByRole('button', { name: /compare/i })).not.toBeInTheDocument();
  });

  it('shows a compare control once two candidates in the same race are selected', async () => {
    const user = userEvent.setup();
    renderWithQuery(<BallotView races={[RACE]} onCompare={vi.fn()} />);
    await screen.findByText('Rep. Synthetic Low');

    const article = screen.getByRole('heading', { name: RACE.office_name }).closest('article') as HTMLElement;
    const checkboxes = within(article).getAllByRole('checkbox', { name: /select.*compare/i });
    await user.click(checkboxes[0]);
    await user.click(checkboxes[1]);

    expect(within(article).getByRole('button', { name: /compare/i })).toBeInTheDocument();
  });

  it('calls onCompare with the selected candidacy ids when the compare control is used', async () => {
    const user = userEvent.setup();
    const onCompare = vi.fn();
    renderWithQuery(<BallotView races={[RACE]} onCompare={onCompare} />);
    await screen.findByText('Rep. Synthetic Low');

    const article = screen.getByRole('heading', { name: RACE.office_name }).closest('article') as HTMLElement;
    const checkboxes = within(article).getAllByRole('checkbox', { name: /select.*compare/i });
    await user.click(checkboxes[0]);
    await user.click(checkboxes[1]);
    await user.click(within(article).getByRole('button', { name: /compare/i }));

    expect(onCompare).toHaveBeenCalledWith([
      'election:district:person-low',
      'election:district:person-high',
    ]);
  });

  it('disables further selection once four candidates are selected', async () => {
    const user = userEvent.setup();
    const FOUR_CANDIDATE_RACE: Race = {
      ...RACE,
      candidates: [
        ...RACE.candidates,
        {
          person_id: 'person:ZZ000004',
          name: 'Rep. Synthetic Fourth',
          party: null,
          is_incumbent: false,
          candidacy_id: 'election:district:person-fourth',
        },
        {
          person_id: 'person:ZZ000005',
          name: 'Rep. Synthetic Fifth',
          party: null,
          is_incumbent: false,
          candidacy_id: 'election:district:person-fifth',
        },
      ],
    };
    renderWithQuery(<BallotView races={[FOUR_CANDIDATE_RACE]} />);
    await screen.findByText('Rep. Synthetic Low');

    const article = screen.getByRole('heading', { name: RACE.office_name }).closest('article') as HTMLElement;
    const checkboxes = within(article).getAllByRole('checkbox', { name: /select.*compare/i });
    await user.click(checkboxes[0]);
    await user.click(checkboxes[1]);
    await user.click(checkboxes[2]);
    await user.click(checkboxes[3]);

    expect(checkboxes[4]).toBeDisabled();
  });

  it('never labels the compare affordance with recommending language', async () => {
    const user = userEvent.setup();
    renderWithQuery(<BallotView races={[RACE]} onCompare={vi.fn()} />);
    await screen.findByText('Rep. Synthetic Low');

    const article = screen.getByRole('heading', { name: RACE.office_name }).closest('article') as HTMLElement;
    const checkboxes = within(article).getAllByRole('checkbox', { name: /select.*compare/i });
    await user.click(checkboxes[0]);
    await user.click(checkboxes[1]);

    const text = article.textContent ?? '';
    expect(text).not.toMatch(/recommend|best match|top pick|endorse|winner/i);
  });
});

describe('sortCardsByScore', () => {
  it('sorts by descending say_do_gap', () => {
    const items = [
      { key: 'a', score: 0.1 },
      { key: 'b', score: 0.9 },
      { key: 'c', score: 0.5 },
    ];
    expect(sortCardsByScore(items, (item) => item.score).map((item) => item.key)).toEqual(['b', 'c', 'a']);
  });

  it('places every null-score item after every scored item, each keeping its original relative order', () => {
    const items = [
      { key: 'a', score: null },
      { key: 'b', score: 0.9 },
      { key: 'c', score: null },
      { key: 'd', score: 0.1 },
    ];
    expect(sortCardsByScore(items, (item) => item.score).map((item) => item.key)).toEqual([
      'b',
      'd',
      'a',
      'c',
    ]);
  });

  it('does not mutate the input array', () => {
    const items = [{ key: 'a', score: 0.1 }, { key: 'b', score: 0.9 }];
    const copy = [...items];
    sortCardsByScore(items, (item) => item.score);
    expect(items).toEqual(copy);
  });
});
