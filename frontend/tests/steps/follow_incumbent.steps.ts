/**
 * Steps for the frontend follow-an-incumbent feature (story 15.1).
 *
 * Builds on the walking-skeleton feature's ballot stub (story 1.5): a voter
 * must see an officeholder before they can follow one, so this file stubs
 * `/v1/ballot` the same way and adds a stub for
 * `/v1/profiles/{user_id}/follow/{person_id}`.
 */
import { Given, Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { world } from './common.steps';

// `Given('a voter opens the Informed app', ...)` and
// `When('they enter their address and submit it', ...)` are already defined
// by common.steps.ts and walking_skeleton.steps.ts respectively — Cucumber's
// registry is shared across every step file matched by cucumber.cjs, so this
// feature reuses both rather than redefining them (a duplicate step
// definition is a Cucumber error, not a silent override).

/** Stubs every `/follow/` call to succeed, echoing back the right list. */
async function stubFollowApiSucceeds(): Promise<void> {
  await world.page.route('**/v1/profiles/*/follow/*', async (route) => {
    const followed = route.request().method() === 'POST' ? ['person:ZZ000001'] : [];
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ followed_person_ids: followed }),
    });
  });
}

/** Stubs every `/follow/` call to fail, as a follow of an unknown person would. */
async function stubFollowApiRejects(): Promise<void> {
  await world.page.route('**/v1/profiles/*/follow/*', async (route) => {
    await route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'not a known person' }),
    });
  });
}

When('they follow {string}', async (name: string) => {
  await stubFollowApiSucceeds();
  await world.page.getByRole('button', { name: new RegExp(`^follow ${name}$`, 'i') }).click();
});

When('they unfollow {string}', async (name: string) => {
  await world.page.getByRole('button', { name: new RegExp(`^unfollow ${name}$`, 'i') }).click();
});

Given('they try to follow {string} but the server rejects it', async (name: string) => {
  await stubFollowApiRejects();
  await world.page.getByRole('button', { name: new RegExp(`^follow ${name}$`, 'i') }).click();
});

Then('the button now offers to unfollow {string}', async (name: string) => {
  await expect(
    world.page.getByRole('button', { name: new RegExp(`^unfollow ${name}$`, 'i') }),
  ).toBeVisible();
});

Then('the button now offers to follow {string}', async (name: string) => {
  await expect(
    world.page.getByRole('button', { name: new RegExp(`^follow ${name}$`, 'i') }),
  ).toBeVisible();
});

Then('the button still offers to follow {string}', async (name: string) => {
  await expect(
    world.page.getByRole('button', { name: new RegExp(`^follow ${name}$`, 'i') }),
  ).toBeVisible();
});

Then('they see a readable error message', async () => {
  await expect(world.page.getByRole('alert').last()).toBeVisible();
});
