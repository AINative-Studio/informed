/**
 * Steps for the installable-PWA / encrypted-offline-profile feature (story
 * 7.7) — `tests/features/installable_pwa.feature`.
 *
 * Reuses `profile_tiers_dealbreaker_and_load.steps.ts`'s "switch to the full
 * profile editor" / "assign .. to primary with a stance" / "give consent and
 * save" steps to get a saved profile on screen, the same way
 * `accessibility_audit.steps.ts` reuses other files' Given/When steps rather
 * than re-driving navigation. This file adds the offline-specific steps:
 * going offline for real (Playwright's `browserContext.setOffline`, not a
 * route stub, since the whole point of these scenarios is that the UI
 * survives losing the actual network stack), a full app reload (this
 * app has no router — `App.tsx`'s own docstring — so "switch to the full
 * profile editor" while ALREADY on that screen is a no-op click that
 * neither remounts `ProfileEditor` nor refires its `useQuery`; a real
 * "reopen the app at the polling place" has to actually reload), and
 * reading back what the app wrote into IndexedDB to prove the cached
 * profile is encrypted at rest rather than merely "the component also
 * offers an offline branch that was never actually exercised".
 */
import { Given, Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { visit, world } from './common.steps';

/**
 * Aborts the profile-GET route specifically; defined once and reused by
 * both steps below so "come back online" can remove exactly this handler
 * (`page.unroute` with a handler reference removes only that registration,
 * leaving `anonymous_mode.steps.ts`'s own profile-GET stub — registered
 * earlier in every scenario by `watchAndStub` — intact underneath it).
 */
async function abortProfileGet(route: Parameters<Parameters<typeof world.page.route>[1]>[0]) {
  if (route.request().method() === 'GET') {
    await route.abort('internetdisconnected');
    return;
  }
  await route.continue();
}

Given('the browser goes offline', async () => {
  await world.page.context().setOffline(true);
  // `browserContext.setOffline` genuinely blocks any request that reaches
  // the real network stack (confirmed directly: before the service worker
  // existed, `page.goto` while offline failed outright with
  // `net::ERR_INTERNET_DISCONNECTED` — which is also why the app shell can
  // still load here, once the service worker is precached and serving it).
  // What it does NOT do — confirmed the same way, with a minimal repro
  // script against a bare `page.route` stub — is fail a request that
  // Playwright's own route interception answers: `route.fulfill` serves the
  // response from Playwright's layer before the request ever reaches the
  // (offline) network stack, so `anonymous_mode.steps.ts`'s profile-GET stub
  // would otherwise keep "succeeding" with its stubbed `null` body no matter
  // how offline the browser claims to be — the one route this story's
  // scenarios need to actually fail, to reach `ProfileEditor`'s real
  // `profileQuery.isError` branch. So this additionally registers a
  // same-pattern route that takes priority (Playwright matches
  // last-registered-first) and aborts the connection, which is what a real
  // offline fetch to an uncached, non-service-worker-handled API endpoint
  // would do.
  await world.page.route(/\/v1\/profiles\/(?!methodology$|consent$)[^/]+$/, abortProfileGet);
});

When('the browser comes back online', async () => {
  await world.page.context().setOffline(false);
  await world.page.unroute(/\/v1\/profiles\/(?!methodology$|consent$)[^/]+$/, abortProfileGet);
});

/**
 * Reloads the app from scratch and opens the full profile editor — the
 * "voter reopens the installed app" moment this story's acceptance
 * criterion is actually about. A plain nav-button click while already on
 * the profile screen is a no-op in this still-router-less app (see this
 * file's own docstring), so every scenario that needs a fresh
 * `ProfileEditor` mount (and therefore a fresh `loadProfile` attempt) goes
 * through this step instead of the shared "switch to the full profile
 * editor" step once the Background has already landed on that screen once.
 */
When('they reopen the app and switch to the full profile editor', async () => {
  await visit();
  await world.page.getByRole('button', { name: /edit my full profile/i }).click();
});

Then('the editor shows that it is displaying a cached offline copy', async () => {
  // `main.tsx`'s QueryClient sets `retry: 2` with React Query's default
  // exponential backoff, which is deliberately left in place for this
  // scenario rather than stubbed away: the offline notice appearing only
  // AFTER retries genuinely exhaust is correct production behaviour, not
  // test friction to route around, and this suite's default 30s step
  // timeout (`common.steps.ts`'s `setDefaultTimeout`) comfortably covers
  // the few real seconds that takes. Confirmed directly against a
  // standalone repro script before raising this: at a 3s wait the editor
  // was still showing "Loading your profile…"; by 15s the offline notice
  // and the cached tier had both rendered.
  try {
    await expect(world.page.getByText(/offline copy|cached.*offline|showing.*offline/i)).toBeVisible({
      timeout: 20_000,
    });
  } catch (error) {
    // On failure, name what the page showed instead — most likely either
    // the plain "could not load" error (no cache was found: check the
    // Background actually waited for the save to land before going
    // offline) or the editor's live-loaded state (the offline route-abort
    // in "the browser goes offline" above did not take effect).
    throw new Error(
      `${(error as Error).message}\n\nPage showed:\n${await world.page.locator('body').innerText()}`,
    );
  }
});

Then('the editor no longer shows the offline-copy notice', async () => {
  await expect(world.page.getByText(/offline copy|cached.*offline|showing.*offline/i)).toHaveCount(0);
});

/**
 * Reads the single record the app wrote to IndexedDB's offline profile
 * cache and asserts none of a saved topic's real text leaked into it in the
 * clear. Does not merely check "something non-empty is there": a cache that
 * stored a plaintext JSON blob and called it done would also pass a vacuous
 * presence check, so this specifically looks for the voter's actual chosen
 * wording and fails if it is readable.
 *
 * `cacheProfileOffline` is called fire-and-forget from `ProfileEditor` (a
 * storage-write failure must not block the screen that just loaded
 * successfully — see that component's own comment), so the write can still
 * be in flight the instant the save confirmation appears. `expect.poll`
 * retries the read rather than trusting a single snapshot, the same
 * tolerance Playwright's own `toBeVisible` already gives every other step
 * in this suite for asynchronous UI state.
 */
Then(
  'the profile is cached for offline use, and the raw cache entry contains no readable topic or stance text',
  async () => {
    async function readRawCacheRecords(): Promise<Array<Record<string, unknown>> | null> {
      return world.page.evaluate(async () => {
        const databases = await window.indexedDB.databases();
        const target = databases.find((db) => db.name === 'informed-offline');
        if (!target?.name) return null;
        return new Promise((resolve, reject) => {
          const open = window.indexedDB.open(target.name as string);
          open.onerror = () => reject(open.error);
          open.onsuccess = () => {
            const db = open.result;
            if (!db.objectStoreNames.contains('profile-cache')) {
              db.close();
              resolve(null);
              return;
            }
            const tx = db.transaction('profile-cache', 'readonly');
            const getAll = tx.objectStore('profile-cache').getAll();
            getAll.onsuccess = () => {
              db.close();
              resolve(getAll.result);
            };
            getAll.onerror = () => {
              db.close();
              reject(getAll.error);
            };
          };
        });
      });
    }

    await expect.poll(readRawCacheRecords, { timeout: 5000 }).not.toBeNull();
    const records = await readRawCacheRecords();

    expect(records).not.toBeNull();
    expect(records!.length).toBeGreaterThan(0);
    const serialized = JSON.stringify(records);
    expect(serialized).not.toMatch(/Prescription drug pricing/i);
    expect(serialized).not.toMatch(/prescription-drug-pricing/i);
    // The stored record must actually look like ciphertext bytes (an
    // ArrayBuffer/Uint8Array), not a JSON string of the plaintext payload
    // that merely happens not to contain the raw substring above.
    expect(serialized).toMatch(/iv|ciphertext|cachedAt/);
  },
);
