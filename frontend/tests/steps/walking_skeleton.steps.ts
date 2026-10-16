/**
 * Steps for the frontend walking-skeleton feature (story 1.5).
 *
 * The browser, the page and the per-scenario state come from the shared step
 * library (story 1.7); this file adds only the address-entry steps.
 *
 * `/v1/ballot` is intercepted with Playwright's `route`, because the preview
 * server serves static files only and has no backend behind it. That is the
 * right seam for this feature: backend/tests/features/walking_skeleton.feature
 * already proves the endpoint reads the delegation out of ZeroDB, so what is
 * left to prove here is that the browser renders the payload it gets.
 */
import { Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { world } from './common.steps';

/** The payload shape the backend returns (see backend/app/ballot/models.py). */
const BALLOT = {
  district: {
    id: 'ocd-division/country:us/state:ca/cd:19',
    name: 'California 19th Congressional District',
    state: 'CA',
  },
  officeholders: [
    {
      person_id: 'person:ZZ000001',
      name: 'Rep. Synthetic Example',
      body_id: 'us-house',
      party: 'Independent',
    },
    { person_id: 'person:ZZ000002', name: 'Sen. Synthetic One', body_id: 'us-senate', party: null },
    { person_id: 'person:ZZ000003', name: 'Sen. Synthetic Two', body_id: 'us-senate', party: null },
  ],
};

/** A synthetic address. prd.md: no real voter data in a fixture, ever. */
const ADDRESS = '1 Synthetic Way, Example, CA 90000';

/** Intercepts `/v1/ballot` so the static preview build has an API to talk to. */
async function stubBallotApi(): Promise<void> {
  await world.page.route('**/v1/ballot*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(BALLOT),
    });
  });
}

When('they enter their address and submit it', async () => {
  await stubBallotApi();
  await world.page.getByLabel(/address/i).fill(ADDRESS);
  await world.page.getByRole('button', { name: /representatives/i }).click();
});

When('they submit the address form without entering an address', async () => {
  await world.page.getByRole('button', { name: /representatives/i }).click();
});

Then('they see their House district', async () => {
  await expect(
    world.page.getByText(/California 19th Congressional District/),
  ).toBeVisible();
});

Then('they see {int} House member', async (count: number) => {
  await expect(
    world.page.getByRole('list', { name: /house/i }).getByRole('listitem'),
  ).toHaveCount(count);
});

Then('they see {int} senators', async (count: number) => {
  await expect(
    world.page.getByRole('list', { name: /senate/i }).getByRole('listitem'),
  ).toHaveCount(count);
});

Then('they are asked to enter an address', async () => {
  await expect(world.page.getByText(/enter your address/i)).toBeVisible();
});

Then('the page never recommends a candidate', async () => {
  await expect(world.page.getByText('Rep. Synthetic Example')).toBeVisible();
  const text = (await world.page.textContent('body')) ?? '';
  expect(text).not.toMatch(/vote for|recommend|endorse|best match/i);
});
