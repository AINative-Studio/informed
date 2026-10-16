/**
 * Steps for the frontend tiers/dealbreaker/load feature (stories 4.2, 4.3, 4.4).
 *
 * `/v1/topics`, `/v1/topics/{id}/stances`, `/v1/profiles/methodology`,
 * `/v1/profiles` and `/v1/profiles/{user_id}` are intercepted with
 * Playwright's `route`, exactly as `quick_onboarding.steps.ts` intercepts
 * its own three endpoints: the preview server serves static files only, and
 * the backend's own BDD suite
 * (`backend/tests/features/profile_tiers_dealbreaker_and_load.feature`)
 * already proves those endpoints against ZeroDB. What is left to prove here
 * is that the browser drives the tier-editing screen correctly against
 * whatever those endpoints return.
 */
import { Before, Given, Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { world } from './common.steps';

const TOPICS = {
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

/** Mutated by the "already saved a profile" Given to control the load stub. */
let storedProfileResponse: unknown = null;

// Reset per scenario, BEFORE any step body runs — including a "Given they
// have already saved a profile..." step, which must survive into the route
// stubs that "switch to the full profile editor" registers afterwards.
// Resetting inside `stubProfileEditorApis` itself (which runs on that later
// step) would wipe out a value this hook already reset and an earlier Given
// had set, which is exactly the bug this ordering avoids.
Before(() => {
  storedProfileResponse = null;
});

async function stubProfileEditorApis(): Promise<void> {
  await world.page.route('**/v1/topics', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(TOPICS) });
  });
  await world.page.route('**/v1/profiles/methodology', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(WEIGHTS) });
  });
  await world.page.route('**/v1/topics/*/stances', async (route) => {
    const url = new URL(route.request().url());
    const topicId = decodeURIComponent(url.pathname.split('/')[3] ?? '');
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(stancesFor(topicId)),
    });
  });
  await world.page.route('**/v1/profiles', async (route) => {
    if (route.request().method() === 'POST') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ version: 1, locale: 'en-US', updated_at: '2026-10-16T00:00:00Z' }),
      });
      return;
    }
    await route.continue();
  });
  // GET /v1/profiles/{user_id} — the load endpoint. A glob route is matched
  // most-recently-registered-first in Playwright, and `**/v1/profiles/*`
  // would otherwise also catch `**/v1/profiles/methodology` (registered
  // above) since `methodology` matches `*` too — so this uses a regex that
  // excludes that one literal segment, rather than relying on route order.
  await world.page.route(/\/v1\/profiles\/(?!methodology$)[^/]+$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(storedProfileResponse),
    });
  });
}

Given('they switch to the full profile editor', async () => {
  await stubProfileEditorApis();
  await world.page.getByRole('button', { name: /edit my full profile/i }).click();
  await world.page.getByText('Prescription drug pricing').waitFor();
});

Then('none of the tier choices is pre-selected', async () => {
  for (const select of await world.page.getByLabel('Tier').all()) {
    await expect(select).toHaveValue('none');
  }
});

Then(
  'the editor states the primary weight as {float} and the secondary weight as {float}',
  async (primary: number, secondary: number) => {
    const text = (await world.page.textContent('body')) ?? '';
    expect(text).toContain(`weight ${primary}`);
    expect(text).toContain(`weight ${secondary}`);
  },
);

async function assignTier(topicName: string, tier: 'Primary' | 'Secondary' | 'Nice-to-have') {
  const row = world.page.getByRole('group', { name: topicName });
  await row.getByLabel('Tier').selectOption(tier);
}

When('they assign {string} to primary with a stance', async (topicName: string) => {
  await assignTier(topicName, 'Primary');
  const row = world.page.getByRole('group', { name: topicName });
  await row.getByLabel(/stance/i).selectOption('Option A');
});

When('they assign {string} to secondary', async (topicName: string) => {
  await assignTier(topicName, 'Secondary');
});

Then('the save button is disabled until consent is given', async () => {
  const saveButton = world.page.getByRole('button', { name: /save my profile/i });
  await expect(saveButton).toBeDisabled();
  await world.page.getByLabel(/consent/i).check();
  await expect(saveButton).toBeEnabled();
});

Then('a dealbreaker checkbox is shown for that topic', async () => {
  await expect(world.page.getByRole('checkbox', { name: /dealbreaker/i })).toBeVisible();
});

Then('no dealbreaker checkbox is shown for that topic', async () => {
  await expect(world.page.getByRole('checkbox', { name: /dealbreaker/i })).toHaveCount(0);
});

When('they mark it as a dealbreaker', async () => {
  await world.page.getByRole('checkbox', { name: /dealbreaker/i }).check();
});

Then('the profile summary shows that topic as a dealbreaker', async () => {
  const summary = world.page.getByRole('region', { name: /profile summary/i });
  await expect(summary.getByText(/dealbreaker/i)).toBeVisible();
});

When('they give consent and save', async () => {
  await world.page.getByLabel(/consent/i).check();
  await world.page.getByRole('button', { name: /save my profile/i }).click();
});

Then('they see a confirmation that their profile was saved', async () => {
  await expect(world.page.getByText(/saved/i)).toBeVisible();
});

Given(
  'they have already saved a profile with {string} as a primary dealbreaker',
  async (topicName: string) => {
    const topicId =
      topicName === 'Medicare' ? 'en-US:medicare:v1' : 'en-US:prescription-drug-pricing:v1';
    storedProfileResponse = {
      interests: [
        {
          topic_id: topicId,
          stance_id: `${topicId}:option-a`,
          tier: 'primary',
          intensity: 2,
          dealbreaker: true,
          note: null,
        },
      ],
      evidence_mode: 'blend',
      followed_person_ids: [],
    };
  },
);

Then(
  '{string} already shows as primary with the dealbreaker checked',
  async (topicName: string) => {
    const row = world.page.getByRole('group', { name: topicName });
    await expect(row.getByLabel('Tier')).toHaveValue('primary');
    await expect(row.getByRole('checkbox', { name: /dealbreaker/i })).toBeChecked();
  },
);
