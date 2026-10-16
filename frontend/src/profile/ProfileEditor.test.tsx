import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { TopicTreeResponse } from '../api/topics';
import { ProfileEditor } from './ProfileEditor';

/**
 * Unit tests for the full tier-editing profile screen (stories 4.2, 4.3, 4.4).
 *
 * Covers what `QuickOnboarding` deliberately does not (see that component's
 * docstring): choosing ANY number of topics across all three tiers within
 * the 1-5 primary / up to-8 secondary / unlimited nice-to-have limits, with
 * inline validation errors; marking a dealbreaker, restricted to primary;
 * and loading a previously-saved profile back on return.
 *
 * `fetch` is stubbed per-URL, the same pattern `QuickOnboarding.test.tsx`
 * established for this story's sibling component.
 */

const TOPICS: TopicTreeResponse = {
  locale: 'en-US',
  taxonomy_version: 1,
  topics: [
    {
      id: 'en-US:healthcare:v1',
      name: 'Healthcare',
      subtopics: [
        { id: 'en-US:prescription-drug-pricing:v1', name: 'Prescription drug pricing' },
        { id: 'en-US:medicare:v1', name: 'Medicare' },
      ],
    },
    {
      id: 'en-US:technology-and-telecommunications:v1',
      name: 'Technology and telecommunications',
      subtopics: [{ id: 'en-US:broadband-access:v1', name: 'Broadband access' }],
    },
    {
      id: 'en-US:environment-and-public-lands:v1',
      name: 'Environment and public lands',
      subtopics: [{ id: 'en-US:public-lands-and-conservation:v1', name: 'Public lands and conservation' }],
    },
  ],
};

const WEIGHTS = {
  methodology_version: '2026.1',
  weights: { primary: 3.0, secondary: 1.5, nice_to_have: 0.5 },
};

function stancesFor(topicId: string) {
  return {
    topic_id: topicId,
    options: [
      { id: `${topicId}:option-a`, label: 'Option A', description: 'Describes option A.' },
      { id: `${topicId}:option-b`, label: 'Option B', description: 'Describes option B.' },
    ],
  };
}

const fetchMock = vi.fn();
let loadedProfileResponse: unknown = null;

beforeEach(() => {
  fetchMock.mockReset();
  loadedProfileResponse = null;
  vi.stubGlobal('fetch', fetchMock);
  window.localStorage.clear();
  fetchMock.mockImplementation(async (input: string | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    if (url.pathname === '/v1/topics') {
      return jsonResponse(TOPICS);
    }
    if (url.pathname === '/v1/profiles/methodology') {
      return jsonResponse(WEIGHTS);
    }
    const stanceMatch = url.pathname.match(/^\/v1\/topics\/(.+)\/stances$/);
    if (stanceMatch) {
      return jsonResponse(stancesFor(decodeURIComponent(stanceMatch[1])));
    }
    const loadMatch = url.pathname.match(/^\/v1\/profiles\/(.+)$/);
    if (loadMatch && (!init || init.method === undefined)) {
      return jsonResponse(loadedProfileResponse);
    }
    if (url.pathname === '/v1/profiles' && init?.method === 'POST') {
      return jsonResponse({ version: 1, locale: 'en-US', updated_at: '2026-10-16T00:00:00Z' });
    }
    throw new Error(`unexpected fetch: ${url.pathname}`);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function renderWithQuery(ui: ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

async function setTier(topicName: string, tier: 'Primary' | 'Secondary' | 'Nice-to-have') {
  const user = userEvent.setup();
  const row = screen.getByRole('group', { name: topicName });
  await user.selectOptions(within(row).getByLabelText('Tier'), tier);
  return { user, row };
}

describe('ProfileEditor — tier assignment and limits (story 4.2)', () => {
  it('shows every subtopic with no tier pre-selected', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    for (const select of screen.getAllByRole('combobox')) {
      expect(select).toHaveValue('none');
    }
  });

  it('reads the live tier weights rather than hard-coding them', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/v1/profiles/methodology'),
      expect.anything(),
    );
    // The weights come from the stubbed /v1/profiles/methodology response
    // (3.0 / 1.5 / 0.5), not from a constant in the component — rendered
    // into the instructions paragraph (text split across JSX nodes, so this
    // checks the paragraph's combined text content rather than one node).
    const instructions = screen.getByText(/Assign each topic a tier/).closest('p');
    expect(instructions?.textContent).toContain('weight 3');
    expect(instructions?.textContent).toContain('weight 1.5');
    expect(instructions?.textContent).toContain('weight 0.5');
  });

  it('allows a single primary interest (the minimum of 1)', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    await setTier('Prescription drug pricing', 'Primary');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('picking every available topic as primary (within this fixture\'s 4) shows no error', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    for (const topic of TOPICS.topics.flatMap((topic) => topic.subtopics)) {
      await setTier(topic.name, 'Primary');
    }
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows an inline error naming the primary limit once more than 5 are picked', async () => {
    const user = userEvent.setup();
    // Six rows is more than MAX_PRIMARY (5); this component renders one row
    // per subtopic returned by /v1/topics, so a 6-subtopic selection needs a
    // 6-subtopic fixture — build it inline rather than padding the shared
    // TOPICS fixture every other test in this file also renders against.
    const sixSubtopics = Array.from({ length: 6 }, (_, index) => ({
      id: `en-US:synthetic-${index}:v1`,
      name: `Synthetic topic ${index}`,
    }));
    fetchMock.mockImplementation(async (input: string | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost');
      if (url.pathname === '/v1/topics') {
        return jsonResponse({
          locale: 'en-US',
          taxonomy_version: 1,
          topics: [{ id: 'en-US:synthetic:v1', name: 'Synthetic', subtopics: sixSubtopics }],
        });
      }
      if (url.pathname === '/v1/profiles/methodology') return jsonResponse(WEIGHTS);
      const stanceMatch = url.pathname.match(/^\/v1\/topics\/(.+)\/stances$/);
      if (stanceMatch) return jsonResponse(stancesFor(decodeURIComponent(stanceMatch[1])));
      const loadMatch = url.pathname.match(/^\/v1\/profiles\/(.+)$/);
      if (loadMatch && (!init || init.method === undefined)) return jsonResponse(null);
      throw new Error(`unexpected fetch: ${url.pathname}`);
    });

    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Synthetic topic 0');
    for (const topic of sixSubtopics) {
      const row = screen.getByRole('group', { name: topic.name });
      await user.selectOptions(within(row).getByLabelText('Tier'), 'Primary');
    }

    expect(await screen.findByRole('alert')).toHaveTextContent(/at most 5 primary/i);
    expect(screen.getByRole('button', { name: /save my profile/i })).toBeDisabled();
  });

  it('shows an inline error naming the secondary limit once more than 8 are picked', async () => {
    const user = userEvent.setup();
    const nineSubtopics = Array.from({ length: 9 }, (_, index) => ({
      id: `en-US:synthetic-${index}:v1`,
      name: `Synthetic topic ${index}`,
    }));
    fetchMock.mockImplementation(async (input: string | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost');
      if (url.pathname === '/v1/topics') {
        return jsonResponse({
          locale: 'en-US',
          taxonomy_version: 1,
          topics: [{ id: 'en-US:synthetic:v1', name: 'Synthetic', subtopics: nineSubtopics }],
        });
      }
      if (url.pathname === '/v1/profiles/methodology') return jsonResponse(WEIGHTS);
      const stanceMatch = url.pathname.match(/^\/v1\/topics\/(.+)\/stances$/);
      if (stanceMatch) return jsonResponse(stancesFor(decodeURIComponent(stanceMatch[1])));
      const loadMatch = url.pathname.match(/^\/v1\/profiles\/(.+)$/);
      if (loadMatch && (!init || init.method === undefined)) return jsonResponse(null);
      throw new Error(`unexpected fetch: ${url.pathname}`);
    });

    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Synthetic topic 0');
    for (const topic of nineSubtopics) {
      const row = screen.getByRole('group', { name: topic.name });
      await user.selectOptions(within(row).getByLabelText('Tier'), 'Secondary');
    }

    expect(await screen.findByRole('alert')).toHaveTextContent(/at most 8 secondary/i);
    expect(screen.getByRole('button', { name: /save my profile/i })).toBeDisabled();
  });

  it('allows unlimited nice-to-have interests', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    for (const topic of TOPICS.topics.flatMap((t) => t.subtopics)) {
      await setTier(topic.name, 'Nice-to-have');
    }
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('requires a stance once a tier is chosen before the profile can be saved', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    await setTier('Prescription drug pricing', 'Primary');
    expect(screen.getByRole('button', { name: /save my profile/i })).toBeDisabled();
  });

  it('blocks saving when there is no primary interest at all', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    await setTier('Broadband access', 'Secondary');
    expect(screen.getByRole('button', { name: /save my profile/i })).toBeDisabled();
    expect(screen.getByText(/at least 1 primary interest/i)).toBeInTheDocument();
  });
});

describe('ProfileEditor — dealbreaker (story 4.3)', () => {
  it('offers a dealbreaker checkbox only for a primary-tier topic', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    const { row } = await setTier('Prescription drug pricing', 'Primary');
    expect(within(row).getByRole('checkbox', { name: /dealbreaker/i })).toBeInTheDocument();
  });

  it('does not offer a dealbreaker checkbox for a secondary-tier topic', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    const { row } = await setTier('Broadband access', 'Secondary');
    expect(within(row).queryByRole('checkbox', { name: /dealbreaker/i })).not.toBeInTheDocument();
  });

  it('does not offer a dealbreaker checkbox for a nice-to-have topic', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    const { row } = await setTier('Public lands and conservation', 'Nice-to-have');
    expect(within(row).queryByRole('checkbox', { name: /dealbreaker/i })).not.toBeInTheDocument();
  });

  it('clears the dealbreaker flag if a topic is moved out of primary', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    const { row, user } = await setTier('Prescription drug pricing', 'Primary');
    await user.click(within(row).getByRole('checkbox', { name: /dealbreaker/i }));
    await user.selectOptions(within(row).getByLabelText('Tier'), 'Secondary');
    expect(within(row).queryByRole('checkbox', { name: /dealbreaker/i })).not.toBeInTheDocument();
  });

  it('shows a dealbreaker interest in the profile summary', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    const { row, user } = await setTier('Prescription drug pricing', 'Primary');
    await user.selectOptions(within(row).getByLabelText(/stance/i), 'Option A');
    await user.click(within(row).getByRole('checkbox', { name: /dealbreaker/i }));

    const summary = await screen.findByRole('region', { name: /profile summary/i });
    expect(within(summary).getByText(/prescription drug pricing/i)).toBeInTheDocument();
    expect(within(summary).getByText(/dealbreaker/i)).toBeInTheDocument();
  });
});

describe('ProfileEditor — save and load (story 4.4)', () => {
  async function pickOnePrimaryWithStance() {
    const user = userEvent.setup();
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    const row = screen.getByRole('group', { name: 'Prescription drug pricing' });
    await user.selectOptions(within(row).getByLabelText('Tier'), 'Primary');
    await user.selectOptions(within(row).getByLabelText(/stance/i), 'Option A');
    return { user, row };
  }

  it('saves the tiered profile with consent and reports success', async () => {
    const { user } = await pickOnePrimaryWithStance();
    await user.click(screen.getByLabelText(/consent/i));
    await user.click(screen.getByRole('button', { name: /save my profile/i }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/v1/profiles'),
        expect.objectContaining({ method: 'POST' }),
      ),
    );
    const call = fetchMock.mock.calls.find(
      (args: unknown[]) => String(args[0]).endsWith('/v1/profiles') && (args[1] as RequestInit)?.method === 'POST',
    )!;
    const body = JSON.parse((call[1] as RequestInit).body as string);
    expect(body.consent).toBe(true);
    expect(body.profile.interests).toHaveLength(1);
    expect(body.profile.interests[0].tier).toBe('primary');

    expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  });

  it('loads a previously-saved profile on mount and pre-fills the tiers', async () => {
    loadedProfileResponse = {
      interests: [
        {
          topic_id: 'en-US:medicare:v1',
          stance_id: 'en-US:medicare:v1:option-b',
          tier: 'primary',
          intensity: 2,
          dealbreaker: true,
          note: null,
        },
        {
          topic_id: 'en-US:broadband-access:v1',
          stance_id: 'en-US:broadband-access:v1:option-a',
          tier: 'secondary',
          intensity: 2,
          dealbreaker: false,
          note: null,
        },
      ],
      evidence_mode: 'blend',
      followed_person_ids: [],
    };

    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');

    const medicareRow = await screen.findByRole('group', { name: 'Medicare' });
    expect(within(medicareRow).getByLabelText('Tier')).toHaveValue('primary');
    expect(within(medicareRow).getByRole('checkbox', { name: /dealbreaker/i })).toBeChecked();

    const broadbandRow = screen.getByRole('group', { name: 'Broadband access' });
    expect(within(broadbandRow).getByLabelText('Tier')).toHaveValue('secondary');

    const summary = screen.getByRole('region', { name: /profile summary/i });
    expect(within(summary).getByText(/medicare/i)).toBeInTheDocument();
    expect(within(summary).getByText(/broadband access/i)).toBeInTheDocument();
  });

  it('shows no profile and an empty editor when nothing was ever saved', async () => {
    loadedProfileResponse = null;
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    for (const select of screen.getAllByRole('combobox')) {
      expect(select).toHaveValue('none');
    }
  });
});
