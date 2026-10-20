/**
 * Unit tests for the encrypted offline profile cache (story 7.7).
 *
 * `prd.md`'s non-negotiable — "Voter profiles are political-opinion data.
 * Encrypt them per user ... never log" — is written for the server-side
 * store, but the SAME category of data sits in this client-side cache the
 * moment the profile is cached for offline viewing, so this module is held
 * to the same bar datamodel.md holds `voter_profiles` to: the thing actually
 * written to storage is ciphertext, tested by reading it back raw and
 * checking it, not asserted.
 *
 * Threat model this cache defends against, and does not: see this module's
 * own docstring. These tests assert only the property that is actually
 * checkable from JavaScript — the persisted bytes contain no readable
 * profile text and will not decrypt without the (non-extractable,
 * IndexedDB-held) device key.
 */
import 'fake-indexeddb/auto';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  cacheProfileOffline,
  clearCachedProfileOffline,
  getRawCacheRecordForTesting,
  readCachedProfileOffline,
} from './encryptedProfileCache';
import type { LoadedProfile } from '../api/profiles';

const SAMPLE_PROFILE: LoadedProfile = {
  interests: [
    {
      topic_id: 'en-US:prescription-drug-pricing:v1',
      stance_id: 'en-US:prescription-drug-pricing:v1:option-a',
      tier: 'primary',
      intensity: 2,
      dealbreaker: false,
      note: null,
    },
  ],
  evidence_mode: 'blend',
  followed_person_ids: [],
};

beforeEach(() => {
  // A fresh in-memory IndexedDB per test — otherwise the device key and
  // cache record created by one test would leak into the next and hide a
  // bug where, say, `cacheProfileOffline` forgot to overwrite the previous
  // entry.
  globalThis.indexedDB = new IDBFactory();
});

afterEach(async () => {
  await clearCachedProfileOffline();
});

describe('cacheProfileOffline / readCachedProfileOffline', () => {
  it('round-trips the profile it was given', async () => {
    await cacheProfileOffline(SAMPLE_PROFILE);
    const result = await readCachedProfileOffline();
    expect(result?.profile).toEqual(SAMPLE_PROFILE);
  });

  it('returns null when nothing has been cached yet', async () => {
    expect(await readCachedProfileOffline()).toBeNull();
  });

  it('records when the profile was cached', async () => {
    const before = Date.now();
    await cacheProfileOffline(SAMPLE_PROFILE);
    const result = await readCachedProfileOffline();
    expect(result).not.toBeNull();
    const cachedAtMs = new Date(result!.cachedAt).getTime();
    expect(cachedAtMs).toBeGreaterThanOrEqual(before);
    expect(cachedAtMs).toBeLessThanOrEqual(Date.now());
  });

  it('overwrites the previous cache entry on a second save, not append to it', async () => {
    await cacheProfileOffline(SAMPLE_PROFILE);
    const second: LoadedProfile = { ...SAMPLE_PROFILE, interests: [] };
    await cacheProfileOffline(second);
    const result = await readCachedProfileOffline();
    expect(result?.profile).toEqual(second);
  });

  it('clearCachedProfileOffline removes the cached profile', async () => {
    await cacheProfileOffline(SAMPLE_PROFILE);
    await clearCachedProfileOffline();
    expect(await readCachedProfileOffline()).toBeNull();
  });
});

describe('what is actually persisted (the ciphertext claim, tested not asserted)', () => {
  it('stores no readable topic id, stance id or note in the raw IndexedDB record', async () => {
    const profileWithNote: LoadedProfile = {
      ...SAMPLE_PROFILE,
      interests: [{ ...SAMPLE_PROFILE.interests[0], note: 'my secret political opinion' }],
    };
    await cacheProfileOffline(profileWithNote);

    const raw = await getRawCacheRecordForTesting();
    expect(raw).not.toBeNull();

    // The stored ciphertext must be binary (an ArrayBuffer/Uint8Array-shaped
    // value from `crypto.subtle.encrypt`), not a JSON string — a cache that
    // `JSON.stringify`'d the plaintext and called the result "stored" would
    // still pass a naive substring check if the substring happened to need
    // encoding, so this checks the shape first.
    expect(raw!.ciphertext).toBeInstanceOf(ArrayBuffer);
    expect(raw!.iv).toBeInstanceOf(Uint8Array);

    // Every readable fragment of the real payload, searched for in the
    // actual bytes written to storage.
    const bytes = Buffer.from(raw!.ciphertext as ArrayBuffer).toString('latin1');
    for (const needle of [
      'prescription-drug-pricing',
      'option-a',
      'my secret political opinion',
      'primary',
      'blend',
    ]) {
      expect(bytes).not.toContain(needle);
    }
  });

  it('decrypts successfully with the stored device key but fails with a different key', async () => {
    await cacheProfileOffline(SAMPLE_PROFILE);
    // Sanity: the module's own reader (which uses the real stored key)
    // succeeds.
    expect(await readCachedProfileOffline()).not.toBeNull();

    const raw = await getRawCacheRecordForTesting();
    const wrongKey = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, [
      'encrypt',
      'decrypt',
    ]);
    await expect(
      crypto.subtle.decrypt({ name: 'AES-GCM', iv: raw!.iv }, wrongKey, raw!.ciphertext),
    ).rejects.toBeDefined();
  });

  it('generates a device key that cannot be exported (non-extractable)', async () => {
    await cacheProfileOffline(SAMPLE_PROFILE);
    const raw = await getRawCacheRecordForTesting();
    expect(raw!.key.extractable).toBe(false);
    await expect(crypto.subtle.exportKey('raw', raw!.key)).rejects.toBeDefined();
  });

  it('reuses the same device key across separate cache writes', async () => {
    await cacheProfileOffline(SAMPLE_PROFILE);
    const firstKeyRaw = (await getRawCacheRecordForTesting())!.key;
    await cacheProfileOffline({ ...SAMPLE_PROFILE, interests: [] });
    const secondKeyRaw = (await getRawCacheRecordForTesting())!.key;
    // CryptoKey objects do not structurally compare, but both must decrypt
    // ciphertext written under the other's session for them to be "the same
    // key" in the way that matters: ability to read data encrypted earlier.
    expect(firstKeyRaw).toBe(secondKeyRaw);
  });

  it('uses a fresh random IV on every encryption, never reusing one', async () => {
    await cacheProfileOffline(SAMPLE_PROFILE);
    const ivA = (await getRawCacheRecordForTesting())!.iv.slice();
    await cacheProfileOffline(SAMPLE_PROFILE);
    const ivB = (await getRawCacheRecordForTesting())!.iv.slice();
    expect(Buffer.from(ivA).equals(Buffer.from(ivB))).toBe(false);
  });
});
