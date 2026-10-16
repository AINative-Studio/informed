/**
 * Steps for the frontend quick-onboarding feature (story 4.1).
 *
 * `/v1/topics`, `/v1/topics/{id}/stances` and `/v1/profiles` are intercepted
 * with Playwright's `route`, exactly as `walking_skeleton.steps.ts`
 * intercepts `/v1/ballot`: the preview server serves static files only, and
 * the backend's own BDD suite (`backend/tests/features/quick_onboarding.
 * feature`) already proves those endpoints against ZeroDB. What is left to
 * prove here is that the browser drives the three-step flow correctly
 * against whatever those endpoints return.
 */
import { Given, Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { world } from './common.steps';

const TOPICS = {
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

async function stubOnboardingApis(): Promise<void> {
  await world.page.route('**/v1/topics', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(TOPICS),
    });
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
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ version: 1, locale: 'en-US', updated_at: '2026-10-16T00:00:00Z' }),
    });
  });
}

const THREE_TOPIC_NAMES = ['Healthcare', 'Technology and telecommunications', 'Environment and public lands'];

async function pickThreeTopics(): Promise<void> {
  for (const name of THREE_TOPIC_NAMES) {
    await world.page.getByLabel(name).click();
  }
}

async function pickAStanceForEachTopic(): Promise<void> {
  // Each topic's stance options load asynchronously (a separate `fetch` per
  // topic). Waiting for the 3rd topic's fieldset is enough to know all three
  // have rendered, since `QuickOnboarding` lists them in selection order.
  await world.page.getByRole('group').nth(2).waitFor();
  const groups = await world.page.getByRole('group').all();
  for (const group of groups) {
    await group.getByRole('radio').first().click();
  }
}

Given('they switch to the quick-setup screen', async () => {
  await stubOnboardingApis();
  await world.page.getByRole('button', { name: /set up my interests/i }).click();
  await world.page.getByText('Healthcare').waitFor();
});

Then('none of the topic choices is pre-selected', async () => {
  for (const checkbox of await world.page.getByRole('checkbox').all()) {
    await expect(checkbox).not.toBeChecked();
  }
});

When('they pick {int} of the 3 required topics', async (count: number) => {
  for (const name of THREE_TOPIC_NAMES.slice(0, count)) {
    await world.page.getByLabel(name).click();
  }
});

Then('the picker shows {string}', async (text: string) => {
  await expect(world.page.getByText(text)).toBeVisible();
});

When('they pick 3 primary topics', async () => {
  await pickThreeTopics();
});

Then('a 4th topic choice is disabled with an explanation shown', async () => {
  await expect(world.page.getByLabel('Elections and voting administration')).toBeDisabled();
  await expect(world.page.getByText(/quick setup's limit/i)).toBeVisible();
});

When('they pick 3 primary topics and move to the stance step', async () => {
  await pickThreeTopics();
  await world.page.getByRole('button', { name: /next: choose your stance/i }).click();
});

Then('none of the stance choices for any topic is pre-selected', async () => {
  await world.page.getByRole('group').nth(2).waitFor();
  for (const radio of await world.page.getByRole('radio').all()) {
    await expect(radio).not.toBeChecked();
  }
});

When('they pick 3 primary topics, a stance for each, and move to the consent step', async () => {
  await pickThreeTopics();
  await world.page.getByRole('button', { name: /next: choose your stance/i }).click();
  await pickAStanceForEachTopic();
  await world.page.getByRole('button', { name: /next: save my profile/i }).click();
});

Then('the consent screen offers saving to an account', async () => {
  await expect(
    world.page.getByRole('button', { name: /i consent — save to my account/i }),
  ).toBeVisible();
});

Then('the consent screen offers staying anonymous, as an equal choice', async () => {
  await expect(
    world.page.getByRole('button', { name: /use anonymous mode instead/i }),
  ).toBeVisible();
});

Then('the onboarding screen shows no candidate score anywhere', async () => {
  // Concrete and checkable today, unlike the shared pending step of the same
  // name in interest_profile.feature (story 4.2's job, once scoring exists):
  // this flow's own screen renders at this step, so "no score anywhere" can
  // actually be asserted against real DOM text rather than deferred.
  const text = (await world.page.textContent('body')) ?? '';
  expect(text).not.toMatch(/\bscore\b.*\d/i);
});

When('they pick 3 primary topics, a stance for each, and choose anonymous mode', async () => {
  await pickThreeTopics();
  await world.page.getByRole('button', { name: /next: choose your stance/i }).click();
  await pickAStanceForEachTopic();
  await world.page.getByRole('button', { name: /next: save my profile/i }).click();
  await world.page.getByRole('button', { name: /use anonymous mode instead/i }).click();
});

Then('they see a confirmation that their profile is saved in this browser only', async () => {
  await expect(world.page.getByText(/saved in this browser only/i)).toBeVisible();
});
