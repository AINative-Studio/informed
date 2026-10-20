/**
 * Steps for the frontend whole-ballot feature (story 7.1).
 *
 * Builds on the walking-skeleton feature's `/v1/ballot` stub (story 1.5) and
 * `common.steps.ts`'s shared `world`: `When('they enter their address and
 * submit it', ...)` is already defined by `walking_skeleton.steps.ts`, but
 * that stub's fixture carries no `races`, so this file stubs `/v1/ballot`
 * again with a `races`-carrying payload before the first navigation — the
 * same "intercept the network boundary" posture every feature here takes,
 * matching `follow_incumbent.steps.ts`'s reuse of a shared step while adding
 * its own stub.
 *
 * `/v1/ballot/cards` is stubbed per district, mirroring
 * `backend/app/ballot/models.py`'s `BallotCardsResponse`.
 */
import { Given, Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { visit, world } from './common.steps';

/** A synthetic address. prd.md: no real voter data in a fixture, ever. */
const ADDRESS = '1 Synthetic Way, Example, CA 90000';

const DISTRICT_ID = 'ocd-division/country:us/state:ca/cd:19';
const SENATE_DIVISION_ID = 'ocd-division/country:us/state:ca';
const ELECTION_ID = 'us-2026-11-03-general';

const HOUSE_CANDIDACY_ID = `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000001`;
const SENATE_ONE_CANDIDACY_ID = `${ELECTION_ID}:${SENATE_DIVISION_ID}:person:ZZ000002`;
const SENATE_TWO_CANDIDACY_ID = `${ELECTION_ID}:${SENATE_DIVISION_ID}:person:ZZ000003`;

const BALLOT_WITH_RACES = {
  district: {
    id: DISTRICT_ID,
    name: 'California 19th Congressional District',
    state: 'CA',
  },
  officeholders: [
    { person_id: 'person:ZZ000001', name: 'Rep. Synthetic Example', body_id: 'us-house', party: 'Independent' },
    { person_id: 'person:ZZ000002', name: 'Sen. Synthetic One', body_id: 'us-senate', party: null },
    { person_id: 'person:ZZ000003', name: 'Sen. Synthetic Two', body_id: 'us-senate', party: null },
  ],
  races: [
    {
      id: 'us-house:ocd-division/country:us/state:ca/cd:19',
      body_id: 'us-house',
      office_name: 'U.S. House of Representatives',
      district_id: DISTRICT_ID,
      district_name: 'California 19th Congressional District',
      election_id: null,
      candidates: [
        {
          person_id: 'person:ZZ000001',
          name: 'Rep. Synthetic Example',
          party: 'Independent',
          is_incumbent: true,
          candidacy_id: HOUSE_CANDIDACY_ID,
        },
      ],
    },
    {
      id: 'us-senate:ocd-division/country:us/state:ca',
      body_id: 'us-senate',
      office_name: 'U.S. Senate',
      district_id: SENATE_DIVISION_ID,
      district_name: 'CA',
      election_id: null,
      candidates: [
        {
          person_id: 'person:ZZ000002',
          name: 'Sen. Synthetic One',
          party: null,
          is_incumbent: true,
          candidacy_id: SENATE_ONE_CANDIDACY_ID,
        },
        {
          person_id: 'person:ZZ000003',
          name: 'Sen. Synthetic Two',
          party: null,
          is_incumbent: true,
          candidacy_id: SENATE_TWO_CANDIDACY_ID,
        },
      ],
    },
  ],
};

const HOUSE_CARDS = {
  election_id: ELECTION_ID,
  district_id: DISTRICT_ID,
  cards: [
    {
      candidacy_id: HOUSE_CANDIDACY_ID,
      person_id: 'person:ZZ000001',
      person_name: 'Rep. Synthetic Example',
      party: 'Independent',
      district: 'California 19th Congressional District',
      photo_url: null,
      is_incumbent: true,
      verdict_counts: null,
      say_do_gap: 0.2,
      positions_summary: [
        { stance_id: 'topic:stance:for', s_value: 0.6, confidence: 0.7, evidence_count: 12 },
      ],
      confidence_badge: 'high',
      methodology_version: '2026.1',
      updated_at: '2026-10-19T00:00:00Z',
    },
  ],
};

const SENATE_CARDS = {
  election_id: ELECTION_ID,
  district_id: SENATE_DIVISION_ID,
  cards: [
    {
      candidacy_id: SENATE_ONE_CANDIDACY_ID,
      person_id: 'person:ZZ000002',
      person_name: 'Sen. Synthetic One',
      party: null,
      district: 'CA',
      photo_url: null,
      is_incumbent: true,
      verdict_counts: null,
      say_do_gap: null,
      positions_summary: null,
      confidence_badge: 'insufficient',
      methodology_version: null,
      updated_at: '2026-10-19T00:00:00Z',
    },
    {
      candidacy_id: SENATE_TWO_CANDIDACY_ID,
      person_id: 'person:ZZ000003',
      person_name: 'Sen. Synthetic Two',
      party: null,
      district: 'CA',
      photo_url: null,
      is_incumbent: true,
      verdict_counts: null,
      say_do_gap: null,
      positions_summary: null,
      confidence_badge: 'insufficient',
      methodology_version: null,
      updated_at: '2026-10-19T00:00:00Z',
    },
  ],
};

/** Overrides the shared walking-skeleton stub with a races-carrying payload. */
async function stubBallotWithRaces(): Promise<void> {
  await world.page.route('**/v1/ballot?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(BALLOT_WITH_RACES),
    });
  });
}

async function stubBallotCards(): Promise<void> {
  await world.page.route('**/v1/ballot/cards*', async (route) => {
    const url = new URL(route.request().url());
    const districtId = url.searchParams.get('district_id');
    const body = districtId === SENATE_DIVISION_ID ? SENATE_CARDS : HOUSE_CARDS;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });
}

// Both the Given and the When below are distinct from
// common.steps.ts's/walking_skeleton.steps.ts's own ("a voter opens the
// Informed app", "they enter their address and submit it") — Cucumber's
// registry is shared across every step file matched by cucumber.cjs, and a
// duplicate step definition is a Cucumber error, not a silent override (see
// follow_incumbent.steps.ts's own note on this).
//
// This feature cannot reuse the shared "they enter their address and submit
// it" step: Playwright's `page.route` matches most-recently-registered-first,
// and that step registers its own `/v1/ballot` stub (an officeholders-only
// payload with no `races`) at submit time — AFTER this feature's Background
// would have run, so it would always win and this feature could never see
// its own races-carrying payload. Submitting the form directly here, after
// stubbing, keeps this feature's stub the only (and therefore the winning)
// handler for both endpoints.
Given('a voter opens the Informed app with a ballot that has races and cards', async () => {
  await visit();
  await stubBallotWithRaces();
  await stubBallotCards();
});

When('they submit their address for the whole-ballot view', async () => {
  await world.page.getByLabel(/address/i).fill(ADDRESS);
  await world.page.getByRole('button', { name: /representatives/i }).click();
});

Then('they see the whole ballot section', async () => {
  await expect(world.page.getByRole('region', { name: /your ballot/i })).toBeVisible();
});

Then('the House race and the Senate race both appear', async () => {
  const ballot = world.page.getByRole('region', { name: /your ballot/i });
  await expect(ballot.getByText('U.S. House of Representatives')).toBeVisible();
  await expect(ballot.getByText('U.S. Senate')).toBeVisible();
  await expect(ballot.getByText('Rep. Synthetic Example')).toBeVisible();
  await expect(ballot.getByText('Sen. Synthetic One')).toBeVisible();
});

Then('every candidate in the ballot section is labeled as incumbent or challenger', async () => {
  const ballot = world.page.getByRole('region', { name: /your ballot/i });
  await expect(ballot.getByText('Rep. Synthetic Example')).toBeVisible();
  const labels = await ballot.getByText(/^(incumbent|challenger)$/i).count();
  expect(labels).toBeGreaterThan(0);
});

Then('the incumbent\'s card shows {string}', async (label: string) => {
  const ballot = world.page.getByRole('region', { name: /your ballot/i });
  await expect(ballot.getByText(label)).toBeVisible();
});

Then("a senator's card says scores are not available yet", async () => {
  const ballot = world.page.getByRole('region', { name: /your ballot/i });
  await expect(ballot.getByText(/not available yet/i).first()).toBeVisible();
});

Then('no error is shown for that candidate', async () => {
  const ballot = world.page.getByRole('region', { name: /your ballot/i });
  await expect(ballot.getByRole('alert')).toHaveCount(0);
});

Then('the ballot section does not recommend, endorse or rank any candidate', async () => {
  const ballot = world.page.getByRole('region', { name: /your ballot/i });
  await expect(ballot.getByText('Rep. Synthetic Example')).toBeVisible();
  const text = (await ballot.textContent()) ?? '';
  expect(text).not.toMatch(/vote for|recommend|endorse|best match|ranked/i);
});
