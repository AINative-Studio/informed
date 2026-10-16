/**
 * Playwright-driven Cucumber steps for tests/features/app_loads.feature (story 1.1).
 *
 * Story 1.7 replaces these ad-hoc steps with the shared BDD step library.
 */
import { After, Before, Given, Then, setDefaultTimeout } from '@cucumber/cucumber';
import { expect } from '@playwright/test';
import { type Browser, type Page, chromium } from '@playwright/test';

setDefaultTimeout(30_000);

const baseUrl = process.env.INFORMED_WEB_URL ?? 'http://127.0.0.1:4173';

let browser: Browser;
let page: Page;

Before(async () => {
  browser = await chromium.launch();
  page = await browser.newPage();
});

After(async () => {
  await browser?.close();
});

Given('a voter opens the Informed app', async () => {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
});

Then('the page shows the Informed name', async () => {
  await expect(page.getByRole('heading', { name: 'Informed', level: 1 })).toBeVisible();
});

Then('the page explains that Informed is nonpartisan', async () => {
  await expect(page.getByText(/nonpartisan/i)).toBeVisible();
});
