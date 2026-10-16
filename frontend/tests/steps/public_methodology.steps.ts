/**
 * Steps for the frontend public-methodology feature (story 12.2).
 *
 * The browser, the page and the per-scenario state come from the shared step
 * library (story 1.7); this file adds only the methodology-page steps.
 *
 * `/v1/methodology` is intercepted with Playwright's `route`, exactly as
 * `walking_skeleton.steps.ts` intercepts `/v1/ballot`: the preview server
 * serves static files only, and `backend/tests/features/public_methodology.
 * feature` plus `scripts/verify_public_methodology_live.py` already prove the
 * endpoint serves this document out of ZeroDB. What is left to prove here is
 * that a reader actually SEES the numbers in a browser.
 *
 * The stub is the live payload. Copied from the real response measured
 * against the live project on 2026-10-16, down to the `published_at` — so if
 * the backend's wire shape changes, these scenarios go red rather than
 * passing against a hand-tuned fixture that no endpoint ever returned.
 */
import { Given, Then } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { visit, world } from './common.steps';

/**
 * The live `/v1/methodology` document, measured 2026-10-16.
 *
 * Published reference data, not voter data: prd.md publishes the methodology
 * deliberately, so there is nothing here CLAUDE.md's "no real voter data in a
 * fixture" rule applies to.
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

async function stubMethodologyApi(): Promise<void> {
  // `**/v1/methodology` without a trailing wildcard, so this does not also
  // swallow `/v1/methodology/versions`: the page under test fetches only the
  // current version, and a route that matched both would hide a wrong URL.
  await world.page.route('**/v1/methodology', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(METHODOLOGY),
    });
  });
}

Given('a reader opens the Informed methodology page', async () => {
  await stubMethodologyApi();
  await visit();
  // Reached by a plain nav button, like every other screen in this app — there
  // is still no router (see App.tsx). What matters for this feature is that
  // getting here required no sign-in and no address, which the
  // "never asked to sign in" scenario asserts directly.
  await world.page.getByRole('button', { name: /how we score/i }).click();
});

Then('they see the methodology version in force', async () => {
  // By role rather than by text: `2026.1` appears twice on purpose — once as
  // the heading and once inside the citation path a verdict will link — so a
  // bare text locator is ambiguous in Playwright's strict mode.
  await expect(
    world.page.getByRole('heading', { name: /methodology version 2026\.1/i }),
  ).toBeVisible();
  await expect(world.page.getByText(/in force/i)).toBeVisible();
});

Then('they see the weight for each interest tier', async () => {
  const weights = world.page.getByRole('table', { name: /tier weight/i });
  await expect(weights).toBeVisible();
  // The real numbers, not merely a table: a page that rendered the tiers with
  // no weights beside them would satisfy a laxer assertion and would be
  // useless to the reader this story exists for.
  await expect(weights.getByText('3')).toBeVisible();
  await expect(weights.getByText('1.5')).toBeVisible();
  await expect(weights.getByText('0.5')).toBeVisible();
});

Then('they see a link to the methodology changelog', async () => {
  const link = world.page.getByRole('link', { name: /changelog/i });
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute('href', METHODOLOGY.changelog_url);
});

Then('they are told which parts of the methodology are not published yet', async () => {
  // Named, not hidden. The backend reports `evidence_blend` and `vote_types`
  // as unpublished because epic 6's scorer does not exist; a page that
  // dropped that on the floor would read as the whole methodology.
  const gaps = world.page.getByRole('list', { name: /not published yet/i });
  await expect(gaps).toBeVisible();
  // Both groups, in reader-facing words rather than as the raw field names
  // (`evidence_blend` means nothing to the journalist this page is for).
  await expect(gaps).toContainText(/voting record/i);
  await expect(gaps).toContainText(/procedural/i);
});

Then('they were never asked to sign in or enter an address', async () => {
  const text = (await world.page.textContent('body')) ?? '';
  expect(text).not.toMatch(/sign in|log in|create an account/i);
  // The methodology region specifically: the app shell's other screens are
  // reachable from the same nav, so this scopes to the page under test rather
  // than asserting the whole app has no address field anywhere.
  const page = world.page.getByRole('region', { name: /how (we|informed) score/i });
  await expect(page.getByLabel(/address/i)).toHaveCount(0);
});

Then('the methodology page never recommends a candidate', async () => {
  // Anchored on something the page definitely rendered, so this cannot pass
  // against a blank screen — the same guard walking_skeleton.steps.ts uses.
  await expect(world.page.getByRole('table', { name: /tier weight/i })).toBeVisible();
  const text = (await world.page.textContent('body')) ?? '';
  expect(text).not.toMatch(/vote for|recommend|endorse|best match/i);
});
