import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ANONYMOUS_PROFILE_STORAGE_KEY, loadAnonymousProfile } from '../api/profiles';
import type { TopicTreeResponse } from '../api/topics';
import { QuickOnboarding } from './QuickOnboarding';

/**
 * Unit tests for the quick-onboarding flow (story 4.1).
 *
 * `fetch` is stubbed per-URL rather than globally mocked once, because this
 * flow calls three different endpoints (topics, stances-per-topic, save) in
 * sequence — the same pattern `BallotPage.test.tsx` uses for one endpoint,
 * extended to route on the request URL.
 */

const TOPICS: TopicTreeResponse = {
  locale: 'en-US',
  taxonomy_version: 1,
  topics: [
    {
      id: 'en-US:healthcare:v1',
      name: 'Healthcare',
      subtopics: [{ id: 'en-US:prescription-drug-pricing:v1', name: 'Prescription drug pricing' }],
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
    {
      id: 'en-US:elections-and-voting:v1',
      name: 'Elections and voting administration',
      subtopics: [],
    },
  ],
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

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  window.localStorage.clear();
  fetchMock.mockImplementation(async (input: string | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    if (url.pathname === '/v1/topics') {
      return jsonResponse(TOPICS);
    }
    const stanceMatch = url.pathname.match(/^\/v1\/topics\/(.+)\/stances$/);
    if (stanceMatch) {
      return jsonResponse(stancesFor(decodeURIComponent(stanceMatch[1])));
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

/** Drives the flow through picking 3 topics (the quick path's fixed count). */
async function pickThreeTopics() {
  const user = userEvent.setup();
  await screen.findByText('Healthcare');
  await user.click(screen.getByLabelText('Healthcare'));
  await user.click(screen.getByLabelText('Technology and telecommunications'));
  await user.click(screen.getByLabelText('Environment and public lands'));
  return user;
}

describe('QuickOnboarding — topic picking', () => {
  it('shows every top-level topic with none pre-selected', async () => {
    renderWithQuery(<QuickOnboarding />);
    await screen.findByText('Healthcare');
    for (const checkbox of screen.getAllByRole('checkbox')) {
      expect(checkbox).not.toBeChecked();
    }
  });

  it('tracks how many of 3 have been picked', async () => {
    renderWithQuery(<QuickOnboarding />);
    const user = await pickThreeTopics();
    expect(screen.getByText('3 of 3 picked')).toBeInTheDocument();
    void user;
  });

  it('blocks a 4th primary topic rather than silently swapping one out', async () => {
    renderWithQuery(<QuickOnboarding />);
    const user = await pickThreeTopics();
    const fourth = screen.getByLabelText('Elections and voting administration');
    expect(fourth).toBeDisabled();
    expect(
      screen.getByText(/you have picked 3 topics, the quick setup's limit/i),
    ).toBeInTheDocument();
    void user;
  });

  it('does not let the voter proceed before picking 3', async () => {
    renderWithQuery(<QuickOnboarding />);
    await screen.findByText('Healthcare');
    expect(screen.getByRole('button', { name: /next: choose your stance/i })).toBeDisabled();
  });
});

describe('QuickOnboarding — stance picking', () => {
  async function toStanceStep() {
    const user = userEvent.setup();
    renderWithQuery(<QuickOnboarding />);
    await pickThreeTopics();
    await user.click(screen.getByRole('button', { name: /next: choose your stance/i }));
    return user;
  }

  it('shows stance options for each chosen topic with nothing pre-selected', async () => {
    await toStanceStep();
    await screen.findAllByText('Option A');
    for (const radio of screen.getAllByRole('radio')) {
      expect(radio).not.toBeChecked();
    }
  });

  it('requires a stance for every topic before proceeding', async () => {
    const user = await toStanceStep();
    await screen.findAllByText('Option A');
    expect(screen.getByRole('button', { name: /next: save my profile/i })).toBeDisabled();

    // Pick one option per topic (one radio group per topic, first option each).
    const fieldsets = screen.getAllByRole('group');
    for (const fieldset of fieldsets) {
      const firstRadio = within(fieldset).getAllByRole('radio')[0];
      await user.click(firstRadio);
    }
    expect(screen.getByRole('button', { name: /next: save my profile/i })).toBeEnabled();
  });
});

describe('QuickOnboarding — consent gate and save', () => {
  async function toConsentStep() {
    const user = userEvent.setup();
    renderWithQuery(<QuickOnboarding />);
    await pickThreeTopics();
    await user.click(screen.getByRole('button', { name: /next: choose your stance/i }));
    await screen.findAllByText('Option A');
    const fieldsets = screen.getAllByRole('group');
    for (const fieldset of fieldsets) {
      const firstRadio = within(fieldset).getAllByRole('radio')[0];
      await user.click(firstRadio);
    }
    await user.click(screen.getByRole('button', { name: /next: save my profile/i }));
    return user;
  }

  it('shows the consent language and the anonymous-mode alternative side by side', async () => {
    await toConsentStep();
    expect(screen.getByRole('button', { name: /i consent — save to my account/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /use anonymous mode instead/i })).toBeInTheDocument();
  });

  it('never shows a score anywhere in the flow', async () => {
    await toConsentStep();
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/\bscore\b.*\d/i);
  });

  it('POSTs consent: true and the 3 picked interests on account save', async () => {
    const user = await toConsentStep();
    await user.click(screen.getByRole('button', { name: /i consent — save to my account/i }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/v1/profiles'),
        expect.objectContaining({ method: 'POST' }),
      ),
    );
    const call = fetchMock.mock.calls.find((args: unknown[]) =>
      String(args[0]).includes('/v1/profiles'),
    )!;
    const init = call[1] as RequestInit;
    const body = JSON.parse(init.body as string);
    expect(body.consent).toBe(true);
    expect(body.profile.interests).toHaveLength(3);
    expect(body.profile.interests.every((i: { tier: string }) => i.tier === 'primary')).toBe(true);

    expect(await screen.findByText(/saved to your account/i)).toBeInTheDocument();
  });

  it('writes nothing to the network in anonymous mode, and stores the profile locally', async () => {
    const user = await toConsentStep();
    fetchMock.mockClear();
    await user.click(screen.getByRole('button', { name: /use anonymous mode instead/i }));

    expect(await screen.findByText(/saved in this browser only/i)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();

    const stored = loadAnonymousProfile();
    expect(stored).not.toBeNull();
    expect(stored?.profile.interests).toHaveLength(3);
    expect(window.localStorage.getItem(ANONYMOUS_PROFILE_STORAGE_KEY)).not.toBeNull();
  });

  it('shows a readable error and does not advance when the save fails', async () => {
    const user = await toConsentStep();
    fetchMock.mockImplementationOnce(async () =>
      jsonResponse({ detail: 'consent is required before a profile can be saved.' }, 422),
    );
    await user.click(screen.getByRole('button', { name: /i consent — save to my account/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/consent/i);
    expect(screen.queryByText(/saved to your account/i)).not.toBeInTheDocument();
  });
});
