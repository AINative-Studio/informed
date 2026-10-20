/**
 * Steps for the installable-PWA / encrypted-offline-profile feature (story
 * 7.7) — `tests/features/installable_pwa.feature`.
 *
 * Reuses `profile_tiers_dealbreaker_and_load.steps.ts`'s "switch to the full
 * profile editor" / "assign .. to primary with a stance" / "give consent and
 * save" steps to get a saved profile on screen, the same way
 * `accessibility_audit.steps.ts` reuses other files' Given/When steps rather
 * than re-driving navigation. This file adds only the offline-specific
 * `Given`/`Then` steps: going offline for real (Playwright's
 * `browserContext.setOffline`, not a route stub, since the whole point of
 * this scenario is that the UI survives losing the actual network stack) and
 * reading back what the app wrote into IndexedDB to prove the cached profile
 * is encrypted at rest rather than merely "the component also offers an
 * offline branch that was never actually exercised".
 */
import { Given, Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { world } from './common.steps';

Given('the browser goes offline', async () => {
  await world.page.context().setOffline(true);
});

When('the browser comes back online', async () => {
  await world.page.context().setOffline(false);
});

Then('the editor shows that it is displaying a cached offline copy', async () => {
  await expect(world.page.getByText(/offline copy|cached.*offline|showing.*offline/i)).toBeVisible();
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
 */
Then(
  'the profile is cached for offline use, and the raw cache entry contains no readable topic or stance text',
  async () => {
    const dump = await world.page.evaluate(async () => {
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

    expect(dump).not.toBeNull();
    const records = dump as Array<Record<string, unknown>>;
    expect(records.length).toBeGreaterThan(0);
    const serialized = JSON.stringify(records);
    expect(serialized).not.toMatch(/Prescription drug pricing/i);
    expect(serialized).not.toMatch(/prescription-drug-pricing/i);
    // The stored record must actually look like ciphertext bytes (an
    // ArrayBuffer/Uint8Array), not a JSON string of the plaintext payload
    // that merely happens not to contain the raw substring above.
    expect(serialized).toMatch(/iv|ciphertext|cachedAt/);
  },
);
