/**
 * The `GET /v1/bills/{bill_id}/text` client (story 5.4).
 *
 * Mirrors `backend/app/bills/models.py`'s `BillTextResponse`.
 *
 * Story 5.4: *"As a voter, I want to open the exact bill text a vote was on,
 * so that I can verify the evidence myself."* The backend archives the
 * document and returns a short-lived presigned URL plus the sha256 of the
 * archived bytes and the www.congress.gov document they were captured from.
 * This client is what the UI calls when a voter asks to open that text.
 *
 * Two things this module deliberately does NOT do:
 *
 *  1. **It does not cache the URL.** The URL expires (the response says in how
 *     many seconds), and a citation link that outlives the page view is a link
 *     that can be shared past its access check. `fetchBillText` is called when
 *     the voter asks to open the text, not on render — see
 *     `isLikelyExpired`, which is there so a UI holding a response across a
 *     long-lived view can tell it needs a fresh one rather than handing the
 *     browser a dead link.
 *  2. **It does not fetch the document itself.** The documents reach megabytes
 *     (H.R. 1's enacted text is 2,787,974 bytes, measured live), so the right
 *     move for a UI is to hand `url` to the browser — a link or a new tab —
 *     and let it stream and render, not to pull it through application code.
 *
 * No voter identity: `fetchBillText` takes a `billId` and nothing else, the
 * same shape and for the same reason as `summaryCard.ts`. The backend
 * endpoint declares no profile parameter either, so there is nothing here a
 * voter's interests could travel in.
 */

import { apiUrl } from './ballot';

/** Mirrors `backend/app/bills/models.py`'s `BillTextResponse`. */
export interface BillText {
  bill_id: string;
  /** ZeroDB Files id of the archived document. */
  file_id: string;
  /** Short-lived presigned URL that serves the archived bytes. */
  url: string;
  /** How long `url` stays valid, in seconds. At most 3600. */
  expires_in_seconds: number;
  /**
   * sha256 of the archived bytes as measured at capture. A reader who
   * downloads what `url` serves can hash it and compare — that check is what
   * makes this document citable evidence rather than a convenient copy.
   */
  sha256: string;
  size_bytes: number | null;
  content_type: string | null;
  /** The www.congress.gov document the bytes were captured from. */
  source_url: string | null;
  captured_at: string | null;
  /**
   * Which published text version this is — "Introduced in House",
   * "Engrossed in House", "Public Law", and so on.
   *
   * The archived version is the latest the source publishes, which is **not
   * necessarily the version a particular roll call voted on**: a May vote on
   * passage is a vote on the engrossed text, not on a public law enacted in
   * July. Show this to the voter rather than presenting the archive as "the"
   * text of a specific vote.
   */
  version_type: string | null;
  version_date: string | null;
  format_type: string | null;
}

/** Raised for any non-2xx answer, carrying a message fit to show a voter. */
export class BillTextError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'BillTextError';
    this.status = status;
  }
}

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';

/**
 * Fetch the presigned URL and provenance for one bill's archived text.
 *
 * Call this when the voter asks to open the text, not on render: the URL it
 * returns is short-lived by design.
 */
export async function fetchBillText(billId: string, signal?: AbortSignal): Promise<BillText> {
  const url = apiUrl(
    API_BASE_URL,
    `/v1/bills/${encodeURIComponent(billId)}/text`,
    window.location.origin,
  );

  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });

  if (!response.ok) {
    throw new BillTextError(messageFor(response.status), response.status);
  }
  return (await response.json()) as BillText;
}

/**
 * A short label naming which text version this is, for showing next to the
 * link a voter is about to open.
 *
 * Returns null when the archive records no version — in which case a UI should
 * say nothing about the version rather than implying one.
 */
export function versionLabelFor(text: BillText): string | null {
  if (!text.version_type) return null;
  if (!text.version_date) return text.version_type;
  const date = text.version_date.slice(0, 10);
  return `${text.version_type} (${date})`;
}

/**
 * Whether a response fetched `fetchedAtMs` ago has probably expired.
 *
 * `nowMs` is injected (default `Date.now()`) so this is directly testable.
 * "Probably" is honest: only the server knows for certain, and the point of
 * this helper is to let a UI re-fetch BEFORE handing the browser a dead link,
 * so it errs early — a full second of margin, which costs one extra request
 * and saves a broken download.
 */
export function isLikelyExpired(
  text: BillText,
  fetchedAtMs: number,
  nowMs: number = Date.now(),
): boolean {
  return nowMs - fetchedAtMs >= (text.expires_in_seconds - 1) * 1000;
}

function messageFor(status: number): string {
  if (status === 404) {
    return "We have not archived the text of that bill yet, so there's nothing to open.";
  }
  return 'We could not open that bill text just now. Please try again.';
}
