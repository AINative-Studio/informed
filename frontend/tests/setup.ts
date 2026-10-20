import '@testing-library/jest-dom/vitest';

// jsdom (story 1.1's test environment) implements neither IndexedDB nor
// `crypto.subtle` — confirmed directly against the installed jsdom 25 before
// adding this: `typeof window.crypto.subtle === 'undefined'` and
// `typeof window.indexedDB === 'undefined'` in a bare jsdom window. Story
// 7.7's offline profile cache (`src/offline/encryptedProfileCache.ts`) needs
// both for real — it is exactly the module under test, not an implementation
// detail — so the unit suite polyfills them here rather than mocking the
// module's own calls to them, which would test nothing but the mocks.
//
// `fake-indexeddb` is the standard, actively-maintained in-memory IndexedDB
// implementation used for this exact purpose; Node's own `crypto.webcrypto`
// is the real Web Crypto implementation (not a shim), so AES-GCM encryption
// in this suite is the same cryptographic code path a browser runs, not a
// stand-in that could hide a scheme that only looks correct.
import 'fake-indexeddb/auto';
import { webcrypto } from 'node:crypto';

if (typeof globalThis.crypto.subtle === 'undefined') {
  Object.defineProperty(globalThis.crypto, 'subtle', { value: webcrypto.subtle });
}
