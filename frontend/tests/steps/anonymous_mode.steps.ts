/**
 * Steps for anonymous mode, export and delete (stories 14.3, 14.4, plus the
 * UI half of 14.2) — `tests/features/anonymous_mode.feature`.
 *
 * ## Why these scenarios are worth driving in a real browser
 *
 * The unit tests in `src/profile/ProfileEditor.test.tsx` already assert the
 * same behaviours against a stubbed `fetch` in jsdom. These add the one thing
 * jsdom cannot give: a real browser's real network stack. Story 14.3's
 * acceptance criterion is "no server rows are written", and the only way to
 * check that claim without trusting the component's own code paths is to watch
 * every request the page actually issues. `page.on('request')` does exactly
 * that — it sees requests a mocked module boundary would hide, including ones
 * made by a `<script>`, a service worker, an image, or a future telemetry
 * call nobody remembered to route through the API module.
 *
 * ## What counts as a write
 *
 * Any non-GET request to any URL, plus any request at all to a profile write
 * endpoint. Public reference GETs (topics, stances, tier weights, the consent
 * text) are not writes and are deliberately allowed: they describe the
 * taxonomy, not the voter, carry no identity, and an anonymous voter who
 * could not read them would have a degraded path rather than ui-prd.md's
 * "real, equally-functional" one. See the feature file's own header.
 *
 * ## Routing
 *
 * The preview server serves static files only, so every `/v1/...` call is
 * fulfilled by Playwright `route` handlers, the same way
 * `quick_onboarding.steps.ts` and `profile_tiers_dealbreaker_and_load.steps.ts`
 * do. The backend's own BDD suite
 * (`backend/tests/features/consent_export_delete.feature`) proves those
 * endpoints against real ZeroDB; what is left to prove here is that the
 * browser drives the screen correctly against whatever they return.
 */
import { Before, Then, When } from '@cucumber/cucumber';
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

/** Synthetic consent wording. Shaped like the real `ConsentOffer`, never real
 *  legal text — prd.md: no real voter data, and no real wording either, in a
 *  fixture that nobody will keep in step with the shipped version. */
const CONSENT_OFFER = {
  consent_version: '2026.1',
  summary:
    'Informed is asking you to save your political priorities to your account. Your political ' +
    'opinions are special-category personal data.',
  what_is_stored: ['The issues you choose, encrypted with a key held for you alone.'],
  anonymous_alternative:
    'You do not have to agree. Anonymous mode gives you the same full app with your priorities ' +
    'kept on this device only.',
  your_rights: ['Export: download everything stored for you as JSON.'],
  lawful_basis: 'gdpr-9-2-a-explicit-consent',
};

const EXPORT_DOCUMENT = {
  exported_at: '2026-10-17T00:00:00Z',
  version: 1,
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

function stancesFor(topicId: string) {
  return {
    topic_id: topicId,
    options: [
      { id: `${topicId}:option-a`, label: 'Option A', description: 'Describes option A.' },
      { id: `${topicId}:option-b`, label: 'Option B', description: 'Describes option B.' },
    ],
  };
}

/** Set by the "already saved a profile" Given; drives the load stub. */
let storedProfile: unknown = null;

/**
 * Let another step file seed the profile this stub serves.
 *
 * `profile_tiers_dealbreaker_and_load.steps.ts` owns the Gherkin phrase
 * "they have already saved a profile with ... as a primary dealbreaker",
 * which both features use, and Cucumber keeps one registry — so that file
 * stays the definition site and hands the fixture here, where the routes are.
 */
export function setStoredProfile(profile: unknown): void {
  storedProfile = profile;
}
/** Every request the page issued that could have changed server state. */
let writesSeen: string[] = [];
/** Every DELETE to a profile endpoint, tracked separately for 14.4's steps. */
let deletesSeen: string[] = [];

// Reset before any step body runs, so a Given that sets `storedProfile`
// survives into the route stubs a later When registers — the same ordering
// `profile_tiers_dealbreaker_and_load.steps.ts` documents for its own fixture.
Before(() => {
  storedProfile = null;
  writesSeen = [];
  deletesSeen = [];
});

/**
 * Watch every request the page makes, then stub the API.
 *
 * The listener is attached BEFORE any route handler, so a request is recorded
 * whether or not something later fulfils it. Recording inside the route
 * handlers instead would miss exactly the case these scenarios exist to catch:
 * a write to a URL nobody wrote a handler for.
 */
export async function watchAndStub(): Promise<void> {
  world.page.on('request', (request) => {
    const method = request.method();
    const url = request.url();
    if (method !== 'GET') writesSeen.push(`${method} ${url}`);
    if (method === 'DELETE' && url.includes('/v1/profiles/')) deletesSeen.push(url);
  });

  await world.page.route('**/v1/topics', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(TOPICS) });
  });
  await world.page.route('**/v1/profiles/methodology', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(WEIGHTS) });
  });
  await world.page.route('**/v1/profiles/consent*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(CONSENT_OFFER),
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
    if (route.request().method() === 'POST') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ version: 1, locale: 'en-US', updated_at: '2026-10-17T00:00:00Z' }),
      });
      return;
    }
    await route.continue();
  });
  await world.page.route('**/v1/profiles/*/export', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(EXPORT_DOCUMENT),
    });
  });
  // `GET`/`DELETE /v1/profiles/{user_id}`. A regex rather than a glob so the
  // literal `methodology` and `consent` segments are excluded by pattern
  // rather than by registration order — the trap
  // `profile_tiers_dealbreaker_and_load.steps.ts` documents, now with one
  // more literal to exclude.
  await world.page.route(/\/v1\/profiles\/(?!methodology$|consent$)[^/]+$/, async (route) => {
    if (route.request().method() === 'DELETE') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          deleted: true,
          key_shredded: true,
          consent_withdrawal_recorded: true,
          elapsed_seconds: 0.3,
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(storedProfile),
    });
  });
}

Then('the editor offers anonymous mode as an alternative to saving to an account', async () => {
  await expect(world.page.getByRole('radio', { name: /anonymous/i })).toBeVisible();
  await expect(world.page.getByRole('radio', { name: /account/i })).toBeVisible();
});

/**
 * A secondary interest WITH a stance.
 *
 * `profile_tiers_dealbreaker_and_load.steps.ts` owns "they assign X to
 * secondary", which deliberately leaves the stance unset — its own feature
 * only checks that a secondary topic offers no dealbreaker checkbox and never
 * saves afterwards. These scenarios do save, and the editor requires a stance
 * on every chosen interest, so they need the fuller phrasing rather than a
 * silent change to the other feature's step.
 */
When('they assign {string} to secondary with a stance', async (topicName: string) => {
  const row = world.page.getByRole('group', { name: topicName });
  await row.getByLabel('Tier').selectOption('Secondary');
  await row.getByLabel(/stance/i).selectOption('Option A');
});

When('they choose anonymous mode', async () => {
  await world.page.getByRole('radio', { name: /anonymous/i }).check();
});

When('they save anonymously', async () => {
  await world.page.getByRole('button', { name: /save my profile/i }).click();
});

Then(
  'they see a confirmation that their profile was saved in this browser only',
  async () => {
    await expect(world.page.getByText(/Saved in this browser only/i)).toBeVisible();
  },
);

/**
 * Story 14.3's acceptance criterion, as an assertion over the real network.
 *
 * Empty, not "contains no POST to /v1/profiles": any non-GET request at all
 * fails this, so adding a telemetry write — to any host — would turn the
 * scenario red rather than slipping past a narrower check.
 */
Then('no request that could write to the server was made', async () => {
  expect(writesSeen).toEqual([]);
});

When('they reload the page and switch to the full profile editor', async () => {
  await world.page.reload({ waitUntil: 'domcontentloaded' });
  // The route handlers and the request listener are registered on the page,
  // not the navigation, so they survive a reload and need no re-registration.
  await world.page.getByRole('button', { name: /edit my full profile/i }).click();
  await world.page.getByText('Prescription drug pricing').waitFor();
});

Then('{string} already shows as primary', async (topicName: string) => {
  const row = world.page.getByRole('group', { name: topicName });
  await expect(row.getByLabel('Tier')).toHaveValue('primary');
});

Then('no account consent checkbox is required before saving', async () => {
  // In anonymous mode the consent control is absent entirely — not merely
  // unchecked — because nothing is stored for there to be a lawful basis for.
  await expect(world.page.getByRole('checkbox', { name: /consent/i })).toHaveCount(0);
  await expect(world.page.getByRole('button', { name: /save my profile/i })).toBeEnabled();
});

When('they clear their anonymous profile', async () => {
  await world.page.getByRole('button', { name: /clear my profile from this browser/i }).click();
});

Then('nothing of their profile remains in this browser', async () => {
  const stored = await world.page.evaluate(() =>
    window.localStorage.getItem('informed:anonymous-profile:v1'),
  );
  expect(stored).toBeNull();
});

// --- Story 14.4: export and delete -----------------------------------------

When('they export their profile', async () => {
  await world.page.getByRole('button', { name: /export my profile/i }).click();
});

Then('they are offered their profile as a JSON download', async () => {
  const link = world.page.getByRole('link', { name: /download my profile as JSON/i });
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute('download', /\.json$/);
});

When('they ask to delete their profile', async () => {
  await world.page.getByRole('button', { name: /^delete my profile$/i }).click();
});

Then('they are warned the deletion is permanent and asked to confirm', async () => {
  const confirmation = world.page.getByRole('group', { name: /confirm deletion/i });
  await expect(confirmation.getByText(/cannot be undone/i)).toBeVisible();
  await expect(confirmation.getByRole('button', { name: /yes, delete my profile/i })).toBeVisible();
  await expect(confirmation.getByRole('button', { name: /cancel/i })).toBeVisible();
});

Then('no delete request has been sent yet', async () => {
  expect(deletesSeen).toEqual([]);
});

When('they confirm the deletion', async () => {
  await world.page.getByRole('button', { name: /yes, delete my profile/i }).click();
});

Then('they are told their profile and its key were permanently deleted', async () => {
  const status = world.page.getByRole('status');
  await expect(status).toContainText(/permanently deleted/i);
  await expect(status).toContainText(/encryption key/i);
});

When('they cancel the deletion', async () => {
  await world.page.getByRole('button', { name: /cancel — keep my profile/i }).click();
});
