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

// --- Stories 14.2, 14.3, 14.4 ---------------------------------------------

/**
 * The consent offer, anonymous mode, export and delete, in the editor.
 *
 * The fetch stub in `beforeEach` above is extended per-describe here rather
 * than globally, so the existing stories' tests keep asserting against exactly
 * the endpoints they knew about: a test that passes only because an unrelated
 * endpoint got stubbed is not evidence of anything.
 */

const CONSENT_OFFER = {
  consent_version: '2026.1',
  summary:
    'Informed is asking you to save your political priorities to your account. Your political ' +
    'opinions are special-category personal data.',
  what_is_stored: [
    'The issues you choose, encrypted with a key held for you alone.',
    'Nothing else about your politics.',
  ],
  anonymous_alternative:
    'You do not have to agree. Anonymous mode gives you the same full app with your priorities ' +
    'kept in this browser only.',
  your_rights: [
    'Export: download everything stored for you as JSON.',
    'Delete: permanently erase your profile and destroy its encryption key.',
  ],
  lawful_basis: 'gdpr-9-2-a-explicit-consent',
};

const EXPORT_DOCUMENT = {
  exported_at: '2026-10-17T00:00:00Z',
  version: 2,
  updated_at: '2026-10-16T00:00:00Z',
  locale: 'en-US',
  jurisdiction_ids: [],
  profile: { interests: [], evidence_mode: 'blend', followed_person_ids: [] },
  consent: [
    {
      consent_version: '2026.1',
      action: 'granted',
      lawful_basis: 'gdpr-9-2-a-explicit-consent',
      recorded_at: '2026-10-16T00:00:00Z',
    },
  ],
};

/** Request bodies/urls seen by the stub, so a test can assert what was NOT sent. */
interface SeenRequest {
  method: string;
  pathname: string;
  body: Record<string, unknown> | null;
}

let seen: SeenRequest[] = [];
let deleteResponse = {
  deleted: true,
  key_shredded: true,
  consent_withdrawal_recorded: true,
  elapsed_seconds: 0.31,
};

function stubPrivacyEndpoints() {
  seen = [];
  fetchMock.mockImplementation(async (input: string | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    const method = init?.method ?? 'GET';
    seen.push({
      method,
      pathname: url.pathname,
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : null,
    });
    if (url.pathname === '/v1/topics') return jsonResponse(TOPICS);
    if (url.pathname === '/v1/profiles/methodology') return jsonResponse(WEIGHTS);
    if (url.pathname === '/v1/profiles/consent') return jsonResponse(CONSENT_OFFER);
    const stanceMatch = url.pathname.match(/^\/v1\/topics\/(.+)\/stances$/);
    if (stanceMatch) return jsonResponse(stancesFor(decodeURIComponent(stanceMatch[1])));
    if (url.pathname.endsWith('/export')) return jsonResponse(EXPORT_DOCUMENT);
    if (url.pathname === '/v1/profiles' && method === 'POST') {
      return jsonResponse({ version: 1, locale: 'en-US', updated_at: '2026-10-16T00:00:00Z' });
    }
    const idMatch = url.pathname.match(/^\/v1\/profiles\/(?!methodology|consent$)([^/]+)$/);
    if (idMatch && method === 'DELETE') return jsonResponse(deleteResponse);
    if (idMatch && method === 'GET') return jsonResponse(loadedProfileResponse);
    throw new Error(`unexpected fetch: ${method} ${url.pathname}`);
  });
}

/** Writes that would leave a trace on the server. GETs of public reference
 *  data (topics, stances, weights, the consent text) carry no identity. */
function writeRequests(): SeenRequest[] {
  return seen.filter((request) => request.method !== 'GET');
}

describe('ProfileEditor — consent at onboarding (story 14.2)', () => {
  beforeEach(() => {
    stubPrivacyEndpoints();
  });

  it('shows the consent wording served by the backend, not wording of its own', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/v1/profiles/consent'),
      expect.anything(),
    );
    expect(await screen.findByText(/special-category personal data/i)).toBeInTheDocument();
    expect(screen.getByText(/encrypted with a key held for you alone/i)).toBeInTheDocument();
  });

  it('offers anonymous mode as an alternative, from the same payload as the ask', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    expect(await screen.findByText(/kept in this browser only/i)).toBeInTheDocument();
  });

  it('names the rights the consent promises (export and delete)', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    expect(await screen.findByText(/download everything stored for you as JSON/i)).toBeInTheDocument();
  });

  it('refuses to save without consent, and echoes the displayed version once given', async () => {
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    const { user } = await setTier('Prescription drug pricing', 'Primary');
    const row = screen.getByRole('group', { name: 'Prescription drug pricing' });
    await user.selectOptions(within(row).getByLabelText(/stance/i), 'Option A');

    const save = screen.getByRole('button', { name: /save my profile/i });
    expect(save).toBeDisabled();

    await user.click(await screen.findByRole('checkbox', { name: /consent/i }));
    expect(save).toBeEnabled();
    await user.click(save);

    await waitFor(() => {
      expect(seen.some((request) => request.pathname === '/v1/profiles' && request.method === 'POST')).toBe(true);
    });
    const post = seen.find((request) => request.pathname === '/v1/profiles' && request.method === 'POST');
    expect(post?.body?.consent).toBe(true);
    // The version the component actually displayed, from the fetched offer —
    // not a constant compiled into the client (story 14.2).
    expect(post?.body?.consent_version).toBe('2026.1');
  });
});

describe('ProfileEditor — anonymous mode (story 14.3)', () => {
  beforeEach(() => {
    stubPrivacyEndpoints();
  });

  it('lets a voter choose anonymous mode instead of an account', async () => {
    const user = userEvent.setup();
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    await user.click(await screen.findByRole('radio', { name: /anonymous/i }));
    expect(screen.getByRole('radio', { name: /anonymous/i })).toBeChecked();
  });

  it('asks for no consent in anonymous mode, because nothing is stored', async () => {
    const user = userEvent.setup();
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    await user.click(await screen.findByRole('radio', { name: /anonymous/i }));
    expect(screen.queryByRole('checkbox', { name: /consent/i })).not.toBeInTheDocument();
  });

  /**
   * Story 14.3's actual acceptance criterion: "no server rows are written".
   * Asserted as "no non-GET request was made at all", which is the strongest
   * check available from the client and one a later refactor cannot quietly
   * break — adding a telemetry POST would fail this test.
   */
  it('completes the whole flow — tiers, a dealbreaker, save — with zero writes to the server', async () => {
    const user = userEvent.setup();
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    await user.click(await screen.findByRole('radio', { name: /anonymous/i }));

    await setTier('Prescription drug pricing', 'Primary');
    const row = screen.getByRole('group', { name: 'Prescription drug pricing' });
    await user.selectOptions(within(row).getByLabelText(/stance/i), 'Option A');
    await user.click(within(row).getByRole('checkbox', { name: /dealbreaker/i }));
    await setTier('Broadband access', 'Secondary');
    const broadband = screen.getByRole('group', { name: 'Broadband access' });
    await user.selectOptions(within(broadband).getByLabelText(/stance/i), 'Option A');

    await user.click(screen.getByRole('button', { name: /save my profile/i }));

    expect(await screen.findByText(/this browser only/i)).toBeInTheDocument();
    expect(writeRequests()).toEqual([]);
  });

  it('keeps an anonymously saved profile in this browser, readable after a remount', async () => {
    const user = userEvent.setup();
    const first = renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    await user.click(await screen.findByRole('radio', { name: /anonymous/i }));
    await setTier('Prescription drug pricing', 'Primary');
    const row = screen.getByRole('group', { name: 'Prescription drug pricing' });
    await user.selectOptions(within(row).getByLabelText(/stance/i), 'Option A');
    await user.click(screen.getByRole('button', { name: /save my profile/i }));
    await screen.findByText(/this browser only/i);
    first.unmount();

    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    await user.click(await screen.findByRole('radio', { name: /anonymous/i }));

    await waitFor(() => {
      const reloaded = screen.getByRole('group', { name: 'Prescription drug pricing' });
      expect(within(reloaded).getByLabelText('Tier')).toHaveValue('primary');
    });
    expect(writeRequests()).toEqual([]);
  });

  it('clears the anonymous profile on request, leaving nothing in this browser', async () => {
    const user = userEvent.setup();
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    await user.click(await screen.findByRole('radio', { name: /anonymous/i }));
    await setTier('Prescription drug pricing', 'Primary');
    const row = screen.getByRole('group', { name: 'Prescription drug pricing' });
    await user.selectOptions(within(row).getByLabelText(/stance/i), 'Option A');
    await user.click(screen.getByRole('button', { name: /save my profile/i }));
    await screen.findByText(/this browser only/i);

    await user.click(screen.getByRole('button', { name: /clear.*(browser|anonymous)/i }));

    expect(window.localStorage.getItem('informed:anonymous-profile:v1')).toBeNull();
    expect(writeRequests()).toEqual([]);
  });
});

describe('ProfileEditor — export and delete (story 14.4)', () => {
  beforeEach(() => {
    stubPrivacyEndpoints();
    loadedProfileResponse = {
      interests: [
        {
          topic_id: 'en-US:medicare:v1',
          stance_id: 'en-US:medicare:v1:option-a',
          tier: 'primary',
          intensity: 2,
          dealbreaker: true,
          note: null,
        },
      ],
      evidence_mode: 'blend',
      followed_person_ids: [],
    };
    deleteResponse = {
      deleted: true,
      key_shredded: true,
      consent_withdrawal_recorded: true,
      elapsed_seconds: 0.31,
    };
  });

  it('exports the stored profile as a JSON document the voter can keep', async () => {
    const user = userEvent.setup();
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');

    await user.click(screen.getByRole('button', { name: /export my (profile|data)/i }));

    await waitFor(() => {
      expect(seen.some((request) => request.pathname.endsWith('/export'))).toBe(true);
    });
    const link = await screen.findByRole('link', { name: /download/i });
    expect(link).toHaveAttribute('download');
  });

  it('asks for confirmation before deleting, and sends nothing until confirmed', async () => {
    const user = userEvent.setup();
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');

    await user.click(screen.getByRole('button', { name: /delete my (profile|data)/i }));

    expect(await screen.findByText(/cannot be undone|permanent/i)).toBeInTheDocument();
    expect(seen.some((request) => request.method === 'DELETE')).toBe(false);
  });

  it('deletes and reports both guarantees once confirmed', async () => {
    const user = userEvent.setup();
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');

    await user.click(screen.getByRole('button', { name: /delete my (profile|data)/i }));
    await user.click(await screen.findByRole('button', { name: /yes.*delete|delete permanently/i }));

    expect(await screen.findByText(/permanently deleted/i)).toBeInTheDocument();
    expect(screen.getByText(/key/i)).toBeInTheDocument();
    expect(seen.some((request) => request.method === 'DELETE')).toBe(true);
  });

  it('cancelling the confirmation sends no delete and leaves the profile loaded', async () => {
    const user = userEvent.setup();
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');

    await user.click(screen.getByRole('button', { name: /delete my (profile|data)/i }));
    await user.click(await screen.findByRole('button', { name: /cancel|keep my profile/i }));

    expect(seen.some((request) => request.method === 'DELETE')).toBe(false);
    const medicare = screen.getByRole('group', { name: 'Medicare' });
    expect(within(medicare).getByLabelText('Tier')).toHaveValue('primary');
    expect(within(medicare).getByRole('checkbox', { name: /dealbreaker/i })).toBeChecked();
  });

  it('offers neither export nor delete in anonymous mode, since nothing is stored', async () => {
    const user = userEvent.setup();
    renderWithQuery(<ProfileEditor />);
    await screen.findByText('Prescription drug pricing');
    await user.click(await screen.findByRole('radio', { name: /anonymous/i }));

    expect(screen.queryByRole('button', { name: /export my (profile|data)/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /delete my (profile|data)/i })).not.toBeInTheDocument();
  });
});
