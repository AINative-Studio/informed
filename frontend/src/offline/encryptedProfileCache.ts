/**
 * The encrypted, client-side offline cache for a voter's own profile (story
 * 7.7 — "view my profile offline").
 *
 * ## Why this exists, and what "the profile" means here
 *
 * `App.tsx` has no router and no account system of its own; the profile this
 * story caches is exactly what `ProfileEditor.tsx` already loads through
 * `loadProfile` (account mode) — the voter's tiered interests. That is
 * political-opinion data. `CLAUDE.md`'s non-negotiable — "Voter profiles are
 * political-opinion data. Encrypt them per user, keep them separate from
 * every other table, and never log, share or use them for targeting" — was
 * written about the server-side `voter_profiles` table (see datamodel.md),
 * but the same category of data now also sits in this browser's storage the
 * moment it is cached for offline viewing, so this module is held to the
 * same bar: the thing actually persisted is ciphertext, not plaintext with a
 * promise around it.
 *
 * ## Why Web Crypto + IndexedDB, not "the OS disk is already encrypted"
 *
 * Full-disk encryption protects a *stolen device*, powered off. It does
 * nothing once the device is unlocked — which is the state this app runs
 * in — against: another script or extension on a shared/public computer
 * reading `localStorage`/IndexedDB directly through the same origin's
 * storage (if it could reach this origin) or, more realistically for this
 * threat model, a person with physical access to an *unlocked* shared
 * device opening devtools and reading whatever this app wrote to storage.
 * Disk encryption is also not something this application controls or can
 * verify is even turned on. Encrypting the cached payload ourselves, with a
 * key this app controls, is the only part of that picture this code can
 * actually make true.
 *
 * ## The scheme, and its honest threat model
 *
 * AES-256-GCM (`crypto.subtle`, no new dependency — every evergreen browser
 * ships it) with a **non-extractable** `CryptoKey`:
 *
 *   1. On first use, generate a random 256-bit AES-GCM key with
 *      `extractable: false` and store the `CryptoKey` object itself (not its
 *      bytes) in IndexedDB. IndexedDB is specified to structured-clone
 *      `CryptoKey` objects while preserving their non-extractable flag —
 *      this is precisely IndexedDB's documented use case for Web Crypto:
 *      "store a key so it can be used but never read out". No code in this
 *      module, or anywhere else in this process, can ever get the raw key
 *      bytes back out of that object; `exportKey('raw', key)` rejects.
 *   2. Every write generates a fresh random 96-bit IV (required for
 *      AES-GCM; reusing an IV under the same key breaks confidentiality) and
 *      calls `crypto.subtle.encrypt`. The ciphertext, IV and a plaintext
 *      timestamp (needed to label the UI's "cached as of ..." notice without
 *      decrypting) go into one IndexedDB record.
 *   3. Reading calls `crypto.subtle.decrypt` with the same stored key.
 *
 * **What this defends against:** another script, browser extension, or a
 * person with devtools open on this device reading the raw storage and
 * finding readable topic/stance/note text — exactly the scenario a
 * "profile cached in cleartext localStorage" would hand them for free.
 *
 * **What this does NOT defend against, and no client-side scheme can:** a
 * sufficiently privileged attacker with full access to this unlocked
 * device — one who can run arbitrary JavaScript in this same origin (an XSS
 * on this exact page) can call `crypto.subtle.decrypt` themselves using the
 * same non-extractable key, because non-extractable only stops a key's
 * *bytes* from leaving the Web Crypto API — it does not stop code running in
 * this origin from *using* the key to decrypt, which is the whole point of
 * caching the profile for the voter's own offline use in the first place.
 * This is not a gap specific to this implementation; it is the ceiling of
 * what client-side-only encryption can ever promise, and it is why
 * datamodel.md's server-side scheme additionally needs a root key the
 * browser never holds. This module's key also never leaves this one
 * browser profile: there is no sync, export or escrow of it anywhere, so
 * losing the device (once locked) or clearing site data makes the cached
 * copy permanently unreadable — a property, not a bug, for data this
 * sensitive.
 *
 * IndexedDB over `localStorage`: `localStorage` is synchronous,
 * string-only, and has no first-class way to hold a non-extractable
 * `CryptoKey` at all (it can only hold strings) — the key would have to be
 * exported to be stored there, which defeats the entire "stored so it can
 * be used but never read out" property this scheme depends on. IndexedDB is
 * the only standard browser storage that can hold the key object itself.
 */
import type { LoadedProfile } from '../api/profiles';

const DB_NAME = 'informed-offline';
const DB_VERSION = 1;
const KEY_STORE = 'keys';
const CACHE_STORE = 'profile-cache';
const DEVICE_KEY_ID = 'device-key';
const CACHE_RECORD_ID = 'current';

interface StoredKeyRecord {
  id: string;
  key: CryptoKey;
}

interface StoredCacheRecord {
  id: string;
  iv: Uint8Array;
  ciphertext: ArrayBuffer;
  cachedAt: string;
}

export interface CachedProfileResult {
  profile: LoadedProfile;
  cachedAt: string;
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = window.indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(KEY_STORE)) {
        db.createObjectStore(KEY_STORE, { keyPath: 'id' });
      }
      if (!db.objectStoreNames.contains(CACHE_STORE)) {
        db.createObjectStore(CACHE_STORE, { keyPath: 'id' });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('Could not open the offline cache.'));
  });
}

function runTransaction<T>(
  db: IDBDatabase,
  storeName: string,
  mode: IDBTransactionMode,
  run: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, mode);
    const store = tx.objectStore(storeName);
    const request = run(store);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('Offline cache operation failed.'));
  });
}

/**
 * The one AES-GCM key this browser profile uses for the offline cache,
 * created on first use and reused thereafter. Non-extractable, per this
 * module's own docstring — there is no `exportKey` call anywhere in this
 * file, and the generated key is never passed to one.
 */
async function getOrCreateDeviceKey(db: IDBDatabase): Promise<CryptoKey> {
  const existing = await runTransaction<StoredKeyRecord | undefined>(
    db,
    KEY_STORE,
    'readonly',
    (store) => store.get(DEVICE_KEY_ID),
  );
  if (existing) return existing.key;

  const key = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, false, [
    'encrypt',
    'decrypt',
  ]);
  await runTransaction(db, KEY_STORE, 'readwrite', (store) =>
    store.put({ id: DEVICE_KEY_ID, key } satisfies StoredKeyRecord),
  );
  return key;
}

/**
 * Encrypt `profile` and write it as this browser's one offline profile
 * cache entry, replacing (not appending to) whatever was cached before.
 *
 * Called by `ProfileEditor` after every successful account-mode
 * `loadProfile`/`saveProfile`, so the cache always reflects the most
 * recently known-good copy of this voter's own profile — never a copy of
 * someone else's, since this module has no identity concept of its own and
 * is only ever invoked with the current, already-authenticated voter's data.
 */
export async function cacheProfileOffline(profile: LoadedProfile): Promise<void> {
  const db = await openDatabase();
  try {
    const key = await getOrCreateDeviceKey(db);
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const plaintext = new TextEncoder().encode(JSON.stringify(profile));
    const ciphertext = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, plaintext);
    const record: StoredCacheRecord = {
      id: CACHE_RECORD_ID,
      iv,
      ciphertext,
      cachedAt: new Date().toISOString(),
    };
    await runTransaction(db, CACHE_STORE, 'readwrite', (store) => store.put(record));
  } finally {
    db.close();
  }
}

/**
 * Decrypt and return the cached profile, or `null` if nothing has been
 * cached yet in this browser. Never throws for "nothing cached" — the same
 * "absence is not an error" shape `loadProfile` already uses for the
 * network path, so a caller does not need two different empty-state checks.
 */
export async function readCachedProfileOffline(): Promise<CachedProfileResult | null> {
  const db = await openDatabase();
  try {
    const [keyRecord, cacheRecord] = await Promise.all([
      runTransaction<StoredKeyRecord | undefined>(db, KEY_STORE, 'readonly', (store) =>
        store.get(DEVICE_KEY_ID),
      ),
      runTransaction<StoredCacheRecord | undefined>(db, CACHE_STORE, 'readonly', (store) =>
        store.get(CACHE_RECORD_ID),
      ),
    ]);
    if (!keyRecord || !cacheRecord) return null;

    const plaintext = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: cacheRecord.iv },
      keyRecord.key,
      cacheRecord.ciphertext,
    );
    const profile = JSON.parse(new TextDecoder().decode(plaintext)) as LoadedProfile;
    return { profile, cachedAt: cacheRecord.cachedAt };
  } finally {
    db.close();
  }
}

/** Remove the cached profile (not the device key) from this browser. */
export async function clearCachedProfileOffline(): Promise<void> {
  const db = await openDatabase();
  try {
    await runTransaction(db, CACHE_STORE, 'readwrite', (store) => store.delete(CACHE_RECORD_ID));
  } finally {
    db.close();
  }
}

/**
 * Test-only escape hatch: the raw stored key and cache record, exactly as
 * IndexedDB holds them, so a test can assert what was actually persisted
 * (ciphertext shape, non-extractability, IV freshness) rather than trusting
 * this module's own read path to tell the truth about itself. Not used by
 * any production code path — `ProfileEditor` and every other caller use only
 * `cacheProfileOffline` / `readCachedProfileOffline` / `clearCachedProfileOffline`.
 */
export async function getRawCacheRecordForTesting(): Promise<
  { key: CryptoKey; iv: Uint8Array; ciphertext: ArrayBuffer } | null
> {
  const db = await openDatabase();
  try {
    const [keyRecord, cacheRecord] = await Promise.all([
      runTransaction<StoredKeyRecord | undefined>(db, KEY_STORE, 'readonly', (store) =>
        store.get(DEVICE_KEY_ID),
      ),
      runTransaction<StoredCacheRecord | undefined>(db, CACHE_STORE, 'readonly', (store) =>
        store.get(CACHE_RECORD_ID),
      ),
    ]);
    if (!keyRecord || !cacheRecord) return null;
    return { key: keyRecord.key, iv: cacheRecord.iv, ciphertext: cacheRecord.ciphertext };
  } finally {
    db.close();
  }
}
