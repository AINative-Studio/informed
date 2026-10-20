/**
 * Steps for the frontend tiers/dealbreaker/load feature (stories 4.2, 4.3, 4.4).
 *
 * Every `/v1/...` call the editor makes is intercepted with Playwright's
 * `route` — the preview server serves static files only, and the backend's own
 * BDD suite (`backend/tests/features/profile_tiers_dealbreaker_and_load.feature`)
 * already proves those endpoints against ZeroDB. What is left to prove here is
 * that the browser drives the tier-editing screen correctly against whatever
 * they return.
 *
 * The route handlers themselves live in `anonymous_mode.steps.ts` and are
 * shared (see `watchAndStub` there). They moved when stories 14.2 and 14.4
 * added `/v1/profiles/consent`, the export and the delete to the same screen:
 * Cucumber keeps one step registry, both features open the editor through the
 * same Gherkin phrase, and two near-duplicate stubs would drift until the one
 * that fell behind made its feature fail for an unrelated reason.
 */
import { Given, Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { setStoredProfile, watchAndStub } from './anonymous_mode.steps';
import { world } from './common.steps';

Given('they switch to the full profile editor', async () => {
  await watchAndStub();
  await world.page.getByRole('button', { name: /edit my full profile/i }).click();
  // Waits for the topic ROW specifically (the fieldset `role="group"` named
  // by its legend), not just any text node matching this string. Story 7.7
  // added scenarios that revisit the editor after already having saved
  // "Prescription drug pricing" as primary, at which point the plain text
  // also appears a second time in the profile summary list below — a bare
  // `getByText` became ambiguous (Playwright's strict mode) the moment both
  // were on screen at once, even though the topic row was always there
  // first in every scenario written before that story.
  await world.page.getByRole('group', { name: 'Prescription drug pricing' }).waitFor();
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
    setStoredProfile({
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
    });
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
