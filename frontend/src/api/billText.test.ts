import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  type BillText,
  BillTextError,
  fetchBillText,
  isLikelyExpired,
  versionLabelFor,
} from './billText';

/**
 * Story 5.4: the `/v1/bills/{bill_id}/text` client. What matters here is that
 * a voter can verify what they are shown — the sha256 and the
 * www.congress.gov source url survive the client untouched — and that the
 * short-lived URL is treated as short-lived rather than cached into a dead
 * link.
 *
 * The shape under test is a real one: these field values are H.R. 3424's
 * actual archived Referred-in-Senate XML, as the live backend returned it on
 * 2026-10-19 (see `backend/scripts/verify_bill_text_live.py`).
 */

const TEXT: BillText = {
  bill_id: 'us-house:119:hr-3424',
  file_id: '583d2231-76fc-478f-b2ed-9ffd11f5deeb',
  url: 'https://files.zerodb.example/583d2231?signature=abc',
  expires_in_seconds: 300,
  sha256: '5ea87a21f43bbfd91dc9cc3d34ad67a4566aa6af02d6f22ee65f016e4637ba5f',
  size_bytes: 4233,
  content_type: 'application/xml',
  source_url: 'https://www.congress.gov/119/bills/hr3424/BILLS-119hr3424rfs.xml',
  captured_at: '2026-10-19T12:00:00+00:00',
  version_type: 'Referred in Senate',
  version_date: '2025-09-09T04:00:00Z',
  format_type: 'Formatted XML',
};

describe('fetchBillText', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('fetches the archived text for a bill id, colons encoded in the path', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => TEXT,
    });

    const text = await fetchBillText('us-house:119:hr-3424');

    expect(text).toEqual(TEXT);
    const [url] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain('/v1/bills/us-house%3A119%3Ahr-3424/text');
  });

  it('passes the sha256 and the primary source url through untouched', async () => {
    // The whole point of the endpoint: a voter checks the archive against
    // congress.gov themselves. A client that reshaped either field would
    // break that without breaking anything visible.
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => TEXT,
    });

    const text = await fetchBillText('us-house:119:hr-3424');

    expect(text.sha256).toBe('5ea87a21f43bbfd91dc9cc3d34ad67a4566aa6af02d6f22ee65f016e4637ba5f');
    expect(text.source_url).toBe(
      'https://www.congress.gov/119/bills/hr3424/BILLS-119hr3424rfs.xml',
    );
  });

  it('never sends a profile, user or voter identity on the request', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => TEXT,
    });

    await fetchBillText('us-house:119:hr-3424');

    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).not.toMatch(/profile|user_id|voter/i);
    expect(JSON.stringify(init ?? {})).not.toMatch(/profile|user_id|voter/i);
  });

  it('raises BillTextError with a 404-specific message when nothing is archived', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: 'no archived text is available for that bill' }),
    });

    try {
      await fetchBillText('us-house:119:hr-9999');
      expect.unreachable('should have thrown');
    } catch (error) {
      expect(error).toBeInstanceOf(BillTextError);
      expect((error as BillTextError).status).toBe(404);
      expect((error as BillTextError).message).toMatch(/not archived/i);
    }
  });

  it('raises a generic message for a non-404 error', async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'boom' }),
    });

    await expect(fetchBillText('us-house:119:hr-3424')).rejects.toThrow(/could not open/i);
  });
});

describe('versionLabelFor', () => {
  it('names the version and its date, so a voter can tell which text this is', () => {
    expect(versionLabelFor(TEXT)).toBe('Referred in Senate (2025-09-09)');
  });

  it('names the version alone when the source published no date', () => {
    // Real case: Congress.gov publishes H.R. 1's enrolled text with a null
    // date. See `app.ingest.bill_text.select_text_version`.
    expect(versionLabelFor({ ...TEXT, version_type: 'Enrolled Bill', version_date: null })).toBe(
      'Enrolled Bill',
    );
  });

  it('says nothing rather than implying a version the archive does not record', () => {
    expect(versionLabelFor({ ...TEXT, version_type: null })).toBeNull();
  });
});

describe('isLikelyExpired', () => {
  it('is false immediately after fetching', () => {
    expect(isLikelyExpired(TEXT, 1_000_000, 1_000_000)).toBe(false);
  });

  it('is false partway through the window', () => {
    expect(isLikelyExpired(TEXT, 1_000_000, 1_000_000 + 100_000)).toBe(false);
  });

  it('is true a second before the window actually closes, erring early', () => {
    // 300s window, so 299s elapsed is already treated as expired: one extra
    // request costs less than handing the browser a dead download link.
    expect(isLikelyExpired(TEXT, 1_000_000, 1_000_000 + 299_000)).toBe(true);
  });

  it('is true well past the window', () => {
    expect(isLikelyExpired(TEXT, 1_000_000, 1_000_000 + 600_000)).toBe(true);
  });
});
