/**
 * The `/v1/ballot` client (story 1.5).
 *
 * The one place the frontend knows the ballot endpoint's URL and payload shape.
 * The types mirror backend/app/ballot/models.py; when epic 2 adds `races` to
 * that payload, this file is where the addition lands.
 *
 * Privacy (prd.md, and story 2.3's "the address is never stored"): the address
 * is a query parameter and nothing else. It is not persisted, not put in
 * localStorage, and not carried in the returned data — `Ballot` has nowhere to
 * put it, which is deliberate.
 */

/** `districts` row id pattern is an OCD-ID (datamodel.md). */
export interface DistrictSummary {
  id: string;
  name: string;
  state: string;
}

/** A sitting member of Congress representing this voter. */
export interface Officeholder {
  person_id: string;
  name: string;
  /** `bodies` row id: `us-house` or `us-senate` (datamodel.md). */
  body_id: string;
  party: string | null;
  district_id?: string | null;
}

export interface Ballot {
  district: DistrictSummary;
  officeholders: Officeholder[];
}

export const HOUSE_BODY_ID = 'us-house';
export const SENATE_BODY_ID = 'us-senate';

/**
 * Where the API lives. `VITE_API_BASE_URL` per .env.example; empty by default
 * so the request is same-origin, which is what a deployed build wants and what
 * the BDD suite's route interception matches.
 *
 * A trailing slash is stripped, because `http://localhost:8000/` is a natural
 * way to write a base URL and would otherwise concatenate into
 * `http://localhost:8000//v1/ballot` — a double slash most servers 404 on, and
 * a failure that would only show up in a deploy whose config happened to be
 * written that way.
 */
const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';

/**
 * Build an API URL from a base and a root-relative path.
 *
 * Exported so the trailing-slash cases are directly testable: the base comes
 * from per-environment deploy config, which this code never sees, so the one
 * place to be sure about it is here.
 */
export function apiUrl(base: string, path: string, origin: string): URL {
  return new URL(`${base.replace(/\/+$/, '')}${path}`, origin);
}

/** Raised for any non-2xx answer, carrying a message fit to show a voter. */
export class BallotError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'BallotError';
    this.status = status;
  }
}

/**
 * Fetch the district and sitting officeholders for an address.
 *
 * Error messages are chosen per status rather than passed through blindly: the
 * backend's `detail` is written for a voter for the cases below, but a 500's
 * detail is not, and must not be rendered into the page.
 */
export async function fetchBallot(address: string, signal?: AbortSignal): Promise<Ballot> {
  const url = apiUrl(API_BASE_URL, '/v1/ballot', window.location.origin);
  url.searchParams.set('address', address);

  const response = await fetch(url.toString(), {
    signal,
    headers: { accept: 'application/json' },
  });

  if (!response.ok) {
    throw new BallotError(messageFor(response.status), response.status);
  }
  return (await response.json()) as Ballot;
}

function messageFor(status: number): string {
  if (status === 404) return "We don't have district data for that address yet.";
  if (status === 422) return 'That address could not be read. Please check it and try again.';
  return 'We could not look up your representatives just now. Please try again.';
}
