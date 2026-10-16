import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MethodologyPage } from './MethodologyPage';

/**
 * The public methodology page (story 12.2).
 *
 * `fetch` is stubbed rather than the module mocked, so these exercise the real
 * `fetchMethodology` — including the assertion that matters most on this page:
 * the request carries no credentials. prd.md requires the methodology to be
 * public, and a page that happened to work only for a signed-in reader would
 * satisfy every other test here.
 *
 * The payload is the live `/v1/methodology` document, measured against the
 * live project on 2026-10-16. Published reference data; no voter data is
 * reachable from this endpoint at all.
 */
const METHODOLOGY = {
  version: '2026.1',
  published_at: '2026-10-16T21:07:34Z',
  weights: { tiers: { primary: 3.0, secondary: 1.5, nice_to_have: 0.5 } },
  changelog_url:
    'https://github.com/AINative-Studio/informed/blob/main/docs/methodology-changelog.md#20261--the-first-published-methodology',
  is_current: true,
  path: '/v1/methodology/2026.1',
  weights_not_yet_published: ['evidence_blend', 'vote_types'],
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MethodologyPage />
    </QueryClientProvider>,
  );
}

/** The last `fetch` call's init, for the credential assertions. */
let lastInit: RequestInit | undefined;
let lastUrl: string | undefined;

function stubFetch(body: unknown, status = 200) {
  const stub = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    lastUrl = String(url);
    lastInit = init;
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', stub);
  return stub;
}

beforeEach(() => {
  lastInit = undefined;
  lastUrl = undefined;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('MethodologyPage', () => {
  it('names the methodology version in force', async () => {
    stubFetch(METHODOLOGY);
    renderPage();
    expect(
      await screen.findByRole('heading', { name: /methodology version 2026\.1/i }),
    ).toBeInTheDocument();
  });

  it('shows the real weight for every tier', async () => {
    stubFetch(METHODOLOGY);
    renderPage();
    const table = await screen.findByRole('table', { name: /tier weight/i });
    // The numbers themselves. A page that rendered the tier names with no
    // weights would pass a laxer assertion and be useless to a reader who
    // came here to find out how much a primary interest counts.
    expect(table).toHaveTextContent('3');
    expect(table).toHaveTextContent('1.5');
    expect(table).toHaveTextContent('0.5');
    expect(table).toHaveTextContent(/primary/i);
    expect(table).toHaveTextContent(/secondary/i);
    expect(table).toHaveTextContent(/nice to have/i);
  });

  it('links the changelog at the URL the API published', async () => {
    stubFetch(METHODOLOGY);
    renderPage();
    const link = await screen.findByRole('link', { name: /changelog/i });
    expect(link).toHaveAttribute('href', METHODOLOGY.changelog_url);
  });

  it('names the parts of the methodology that are not published yet', async () => {
    stubFetch(METHODOLOGY);
    renderPage();
    const gaps = await screen.findByRole('list', { name: /not published yet/i });
    // Rendered in plain words, not as the raw field names: a reader who came
    // to understand the method is not helped by `evidence_blend`.
    expect(gaps).toHaveTextContent(/voting record/i);
    expect(gaps).toHaveTextContent(/procedural/i);
    expect(gaps).not.toHaveTextContent('evidence_blend');
  });

  it('does not render a zero weight for an unpublished group', async () => {
    stubFetch(METHODOLOGY);
    renderPage();
    const table = await screen.findByRole('table', { name: /tier weight/i });
    // Zero is a published decision; "not set yet" is a different statement.
    // The gap belongs in the prose list, never as a 0 row in the table.
    expect(table).not.toHaveTextContent(/evidence/i);
    expect(table.querySelectorAll('tbody tr')).toHaveLength(3);
  });

  it('publishes the citation path a verdict will link', async () => {
    stubFetch(METHODOLOGY);
    renderPage();
    // The forward-looking half of "every verdict links to its methodology
    // version": epic 11's verdicts do not exist, but the address they will
    // cite is already shown here and fetchable.
    expect(await screen.findByText('/v1/methodology/2026.1')).toBeInTheDocument();
  });

  it('requests the public endpoint with no credentials', async () => {
    stubFetch(METHODOLOGY);
    renderPage();
    await screen.findByRole('table', { name: /tier weight/i });

    expect(lastUrl).toMatch(/\/v1\/methodology$/);
    const headers = new Headers(lastInit?.headers);
    expect(headers.has('authorization')).toBe(false);
    // Not `include`: a credentialed cross-origin read would make the public
    // page depend on a session that a journalist does not have.
    expect(lastInit?.credentials).not.toBe('include');
  });

  it('never tells the reader who to vote for', async () => {
    stubFetch(METHODOLOGY);
    renderPage();
    await screen.findByRole('table', { name: /tier weight/i });
    expect(document.body.textContent ?? '').not.toMatch(
      /vote for|recommend|endorse|best match/i,
    );
  });

  it('says the weights apply to every candidate and party', async () => {
    stubFetch(METHODOLOGY);
    renderPage();
    // prd.md's first non-negotiable, stated on the page a skeptical reader
    // opens to check exactly that claim.
    expect(await screen.findByText(/every candidate and every party/i)).toBeInTheDocument();
  });

  it('shows a reader-facing message when the endpoint fails', async () => {
    stubFetch({ detail: 'boom' }, 503);
    renderPage();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/could not load the methodology/i);
    // The backend's own detail is not written for a reader and is not shown.
    expect(alert).not.toHaveTextContent('boom');
  });

  it('marks a superseded version as no longer in force', async () => {
    stubFetch({ ...METHODOLOGY, is_current: false });
    renderPage();
    expect(await screen.findByText(/superseded/i)).toBeInTheDocument();
  });

  it('renders without a gap section once every group is published', async () => {
    stubFetch({
      ...METHODOLOGY,
      weights: {
        tiers: METHODOLOGY.weights.tiers,
        evidence_blend: { votes: 0.7, promises: 0.3 },
        vote_types: { final_passage: 1.0, procedural: 0.25 },
      },
      weights_not_yet_published: [],
    });
    renderPage();
    await screen.findByRole('table', { name: /tier weight/i });
    // Forward-looking: when epic 6 publishes the remaining groups, the "not
    // published yet" section disappears on its own rather than needing an
    // edit here.
    expect(screen.queryByRole('list', { name: /not published yet/i })).not.toBeInTheDocument();
  });
});
