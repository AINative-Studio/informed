/**
 * Steps for the frontend candidate-detail feature (story 7.3).
 *
 * Builds on `ballot_view.steps.ts`'s pattern of stubbing `/v1/ballot` and
 * `/v1/ballot/cards` so a candidate card is on screen, then adds a stub for
 * `GET /v1/candidates/{id}/alignment?profile_id=` (stories 6.4-6.7) and
 * drives the "View details" affordance `BallotView` exposes per candidate.
 *
 * Stubbed at the network boundary, same posture as every other feature here:
 * this feature's job is to prove the browser renders the alignment payload it
 * gets back, not to re-prove the scoring endpoint itself.
 */
import { Given, Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { visit, world } from './common.steps';

const ADDRESS = '1 Synthetic Way, Example, CA 90000';
const DISTRICT_ID = 'ocd-division/country:us/state:ca/cd:19';
const ELECTION_ID = 'us-2026-11-03-general';
const CANDIDACY_ID = `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000001`;

const BALLOT_WITH_RACES = {
  district: { id: DISTRICT_ID, name: 'California 19th Congressional District', state: 'CA' },
  officeholders: [
    { person_id: 'person:ZZ000001', name: 'Rep. Synthetic Example', body_id: 'us-house', party: 'Independent' },
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
          candidacy_id: CANDIDACY_ID,
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
      candidacy_id: CANDIDACY_ID,
      person_id: 'person:ZZ000001',
      person_name: 'Rep. Synthetic Example',
      party: 'Independent',
      district: 'California 19th Congressional District',
      photo_url: null,
      is_incumbent: true,
      verdict_counts: null,
      say_do_gap: 0.2,
      positions_summary: [
        { stance_id: 'topic:prescription-drug-pricing:lower-costs', s_value: 0.6, confidence: 0.7, evidence_count: 12 },
      ],
      confidence_badge: 'high',
      methodology_version: '2026.1',
      updated_at: '2026-10-19T00:00:00Z',
    },
  ],
};

const ALIGNMENT_WITH_REASONS = {
  candidacy_id: CANDIDACY_ID,
  person_id: 'person:ZZ000001',
  score: 74.5,
  confidence_badge: 'high',
  evidence_count: 12,
  scored_interest_count: 2,
  dealbreaker_warnings: [],
  reasons: [
    {
      stance_id: 'topic:prescription-drug-pricing:lower-costs',
      direction: 'raised',
      s_value: 0.6,
      confidence: 0.7,
      evidence_count: 12,
      citations: [
        {
          roll_call_id: 'us-house:119-2:452',
          position: 'yea',
          vote_type: 'final_passage',
          voted_at: '2026-03-01T00:00:00Z',
          bill_id: 'us-house:119:hr-1',
          is_bundled: false,
          bundled_reason: null,
        },
      ],
    },
    {
      stance_id: 'topic:climate:carbon-pricing',
      direction: 'lowered',
      s_value: -0.4,
      confidence: 0.5,
      evidence_count: 3,
      citations: [
        {
          roll_call_id: 'us-house:119-2:460',
          position: 'nay',
          vote_type: 'final_passage',
          voted_at: '2026-04-01T00:00:00Z',
          bill_id: 'us-house:119:hr-8',
          is_bundled: false,
          bundled_reason: null,
        },
      ],
    },
  ],
  methodology_version: '2026.1',
};

const ALIGNMENT_NO_POSITIONS = {
  candidacy_id: CANDIDACY_ID,
  person_id: 'person:ZZ000001',
  score: null,
  confidence_badge: 'insufficient',
  evidence_count: 0,
  scored_interest_count: 0,
  dealbreaker_warnings: [],
  reasons: [],
  methodology_version: '2026.1',
};

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
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(HOUSE_CARDS),
    });
  });
}

async function stubAlignment(body: unknown): Promise<void> {
  // `**` between `/candidates/` and `/alignment`, not `*`: a real candidacy
  // id contains slashes (an OCD-ID district segment embedded in it — see
  // `backend/app/scoring/router.py`'s own docstring on why its route uses a
  // `:path` converter for exactly this reason), and Playwright's glob `*`
  // does not match across `/`. A single `*` here silently never matches the
  // real request and the test would hang on a real network call instead of
  // hitting this stub — found by running this suite and seeing the request
  // actually leave the page.
  await world.page.route('**/v1/candidates/**/alignment*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });
}

Given('a voter opens the Informed app with a scored candidate', async () => {
  await visit();
  await stubBallotWithRaces();
  await stubBallotCards();
  await stubAlignment(ALIGNMENT_WITH_REASONS);
  await world.page.getByLabel(/address/i).fill(ADDRESS);
  await world.page.getByRole('button', { name: /representatives/i }).click();
});

Given('a voter opens the Informed app with a candidate who has no measured positions', async () => {
  await visit();
  await stubBallotWithRaces();
  await stubBallotCards();
  await stubAlignment(ALIGNMENT_NO_POSITIONS);
  await world.page.getByLabel(/address/i).fill(ADDRESS);
  await world.page.getByRole('button', { name: /representatives/i }).click();
});

When("they open that candidate's detail page", async () => {
  const ballot = world.page.getByRole('region', { name: /your ballot/i });
  await ballot.getByRole('button', { name: /view details|details/i }).first().click();
});

Then('the detail page shows a row for each reason behind the score', async () => {
  const detail = world.page.getByRole('region', { name: /candidate detail/i });
  const rows = detail.getByRole('listitem');
  await expect(rows).toHaveCount(2);
});

Then("each row shows whether the candidate's record agrees or disagrees with that interest", async () => {
  const detail = world.page.getByRole('region', { name: /candidate detail/i });
  await expect(detail.getByText(/agrees/i).first()).toBeVisible();
  await expect(detail.getByText(/disagrees/i).first()).toBeVisible();
});

Then('each row shows how many pieces of evidence it rests on', async () => {
  const detail = world.page.getByRole('region', { name: /candidate detail/i });
  await expect(detail.getByText(/12.*evidence|evidence.*12/i).first()).toBeVisible();
  await expect(detail.getByText(/3.*evidence|evidence.*3/i).first()).toBeVisible();
});

Then('every row has at least one link a voter can actually open', async () => {
  const detail = world.page.getByRole('region', { name: /candidate detail/i });
  // Wait for the breakdown to actually render before counting links:
  // `locator.count()` is a single snapshot with no auto-retry (unlike
  // `expect(...).toBeVisible()`, which polls), so without this the step can
  // run before the alignment query resolves and see zero links that are
  // about to appear a moment later — found by this step flaking against the
  // real async render.
  await expect(detail.getByRole('listitem').first()).toBeVisible();
  const links = detail.getByRole('link');
  const count = await links.count();
  expect(count).toBeGreaterThanOrEqual(2);
  for (let index = 0; index < count; index += 1) {
    const href = await links.nth(index).getAttribute('href');
    expect(href).toBeTruthy();
    expect(href).not.toBe('#');
  }
});

Then('the detail page says there is nothing measured yet', async () => {
  const detail = world.page.getByRole('region', { name: /candidate detail/i });
  await expect(detail.getByText(/nothing measured|no measured|not available/i)).toBeVisible();
});

Then('no interest row is shown', async () => {
  const detail = world.page.getByRole('region', { name: /candidate detail/i });
  await expect(detail.getByRole('listitem')).toHaveCount(0);
});

Then('the detail page does not recommend, endorse or rank the candidate', async () => {
  const detail = world.page.getByRole('region', { name: /candidate detail/i });
  const text = (await detail.textContent()) ?? '';
  expect(text).not.toMatch(/vote for|recommend|endorse|best match|ranked/i);
});
