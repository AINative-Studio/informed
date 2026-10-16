import { describe, expect, it } from 'vitest';

import { apiUrl } from './ballot';

/**
 * URL building for `/v1/ballot` (story 1.5).
 *
 * `VITE_API_BASE_URL` is set per environment in deploy config this code never
 * sees, so every shape it could plausibly take is pinned here rather than
 * discovered in a broken deploy.
 */
describe('apiUrl', () => {
  const ORIGIN = 'http://127.0.0.1:4173';

  it('is same-origin when no base URL is configured', () => {
    expect(apiUrl('', '/v1/ballot', ORIGIN).toString()).toBe(`${ORIGIN}/v1/ballot`);
  });

  it('uses an absolute base URL when one is configured', () => {
    expect(apiUrl('http://localhost:8000', '/v1/ballot', ORIGIN).toString()).toBe(
      'http://localhost:8000/v1/ballot',
    );
  });

  it('does not produce a double slash when the base URL has a trailing one', () => {
    // `http://localhost:8000/` is a natural way to write a base URL, and the
    // naive concatenation gives `//v1/ballot`, which most servers 404 on.
    expect(apiUrl('http://localhost:8000/', '/v1/ballot', ORIGIN).toString()).toBe(
      'http://localhost:8000/v1/ballot',
    );
  });

  it('tolerates several trailing slashes', () => {
    expect(apiUrl('https://api.informed.example///', '/v1/ballot', ORIGIN).toString()).toBe(
      'https://api.informed.example/v1/ballot',
    );
  });

  it('keeps a path prefix on the base URL', () => {
    // An API served under a path, e.g. behind a shared gateway.
    expect(apiUrl('https://informed.example/api', '/v1/ballot', ORIGIN).toString()).toBe(
      'https://informed.example/api/v1/ballot',
    );
  });
});
