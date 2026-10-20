/**
 * Steps for the frontend challenger-promises feature (story 9.6).
 *
 * Builds on `candidate_detail.steps.ts`'s pattern of stubbing `/v1/ballot`
 * and `/v1/ballot/cards` so a candidate card is on screen, with the
 * candidate marked `is_incumbent: false` so `BallotPage`'s "View details"
 * routes to `ChallengerPromisesView` rather than `CandidateDetailPage` (see
 * `BallotView.tsx`'s `onViewDetails` docstring for that branch). Adds a stub
 * for `GET /v1/candidates/{id}/promises?profile_id=` (story 9.6).
 *
 * Stubbed at the network boundary, same posture as every other feature here:
 * this feature's job is to prove the browser renders the promises payload it
 * gets back, not to re-prove the grouping endpoint itself.
 */
import { Given, Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { visit, world } from './common.steps';

const ADDRESS = '1 Synthetic Way, Example, CA 90000';
const DISTRICT_ID = 'ocd-division/country:us/state:ca/cd:19';
const ELECTION_ID = 'us-2026-11-03-general';
const CANDIDACY_ID = `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000009`;

const BALLOT_WITH_CHALLENGER = {
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
          candidacy_id: `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000001`,
        },
        {
          person_id: 'person:ZZ000009',
          name: 'Synthetic Challenger',
          party: 'Other',
          is_incumbent: false,
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
      candidacy_id: `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000001`,
      person_id: 'person:ZZ000001',
      person_name: 'Rep. Synthetic Example',
      party: 'Independent',
      district: 'California 19th Congressional District',
      photo_url: null,
      is_incumbent: true,
      verdict_counts: null,
      say_do_gap: 0.2,
      positions_summary: [],
      confidence_badge: 'high',
      methodology_version: '2026.1',
      updated_at: '2026-10-19T00:00:00Z',
    },
    {
      candidacy_id: CANDIDACY_ID,
      person_id: 'person:ZZ000009',
      person_name: 'Synthetic Challenger',
      party: 'Other',
      district: 'California 19th Congressional District',
      photo_url: null,
      is_incumbent: false,
      verdict_counts: null,
      say_do_gap: null,
      positions_summary: [],
      confidence_badge: 'insufficient',
      methodology_version: '2026.1',
      updated_at: '2026-10-19T00:00:00Z',
    },
  ],
};

const PROMISES_WITH_GROUPS = {
  candidacy_id: CANDIDACY_ID,
  person_id: 'person:ZZ000009',
  groups: [
    {
      stance_id: 'topic:prescription-drug-pricing:lower-costs',
      promises: [
        {
          promise_id: 'prm:0001',
          quote_span: 'I will vote to cap insulin co-pays at $35/month',
          actor: 'the candidate',
          action: 'vote to cap',
          object: 'insulin co-pays at $35/month',
          direction: 'support',
          testable: true,
          source_url: 'https://challenger.example.test/promises',
          captured_at: '2026-09-01T00:00:00Z',
        },
      ],
    },
    {
      stance_id: 'topic:climate:carbon-pricing',
      promises: [
        {
          promise_id: 'prm:0002',
          quote_span: "I'll fight for a cleaner future",
          actor: 'the candidate',
          action: null,
          object: null,
          direction: 'unclear',
          testable: false,
          source_url: 'https://challenger.example.test/debate',
          captured_at: '2026-08-15T00:00:00Z',
        },
      ],
    },
  ],
  unmatched_count: 0,
  prior_offices: [],
};

const PROMISES_WITH_UNMATCHED = {
  ...PROMISES_WITH_GROUPS,
  unmatched_count: 1,
};

const PROMISES_WITH_PRIOR_OFFICE = {
  ...PROMISES_WITH_GROUPS,
  prior_offices: [
    {
      body_id: 'us-house',
      district_id: 'ocd-division/country:us/state:ca/sldl:4',
      party: 'Other',
      start: '2019-01-03',
      end: '2023-01-03',
    },
  ],
};

const PROMISES_EMPTY = {
  candidacy_id: CANDIDACY_ID,
  person_id: 'person:ZZ000009',
  groups: [],
  unmatched_count: 0,
  prior_offices: [],
};

async function stubBallotWithChallenger(): Promise<void> {
  await world.page.route('**/v1/ballot?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(BALLOT_WITH_CHALLENGER),
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

async function stubPromises(body: unknown): Promise<void> {
  // `**` between `/candidates/` and `/promises`, not `*` — same reason
  // `candidate_detail.steps.ts`'s `stubAlignment` uses `**`: a real
  // candidacy id contains slashes (an OCD-ID district segment), and
  // Playwright's glob `*` does not match across `/`.
  await world.page.route('**/v1/candidates/**/promises*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });
}

async function openChallengerDetails(): Promise<void> {
  const ballot = world.page.getByRole('region', { name: /your ballot/i });
  await ballot.getByRole('button', { name: /view details for synthetic challenger/i }).click();
}

Given('a voter opens the Informed app with a challenger who has promises', async () => {
  await visit();
  await stubBallotWithChallenger();
  await stubBallotCards();
  await stubPromises(PROMISES_WITH_GROUPS);
  await world.page.getByLabel(/address/i).fill(ADDRESS);
  await world.page.getByRole('button', { name: /representatives/i }).click();
});

Given('a voter opens the Informed app with a challenger who has no promises yet', async () => {
  await visit();
  await stubBallotWithChallenger();
  await stubBallotCards();
  await stubPromises(PROMISES_EMPTY);
  await world.page.getByLabel(/address/i).fill(ADDRESS);
  await world.page.getByRole('button', { name: /representatives/i }).click();
});

Given('that challenger has a promise outside the voter\'s interests', async () => {
  await stubPromises(PROMISES_WITH_UNMATCHED);
});

Given('that challenger has a non-testable promise', async () => {
  // Already true of the seeded fixture (the climate promise is non-testable)
  // — this step names the fact the scenario depends on rather than changing
  // the stub, matching the "Background already set this up" posture
  // `candidate_detail.steps.ts` uses for scenarios that reuse its Background.
});

Given('that challenger previously served in another office', async () => {
  await stubPromises(PROMISES_WITH_PRIOR_OFFICE);
});

Given('that challenger has never held elected office', async () => {
  // Already true of the Background's seeded fixture (`prior_offices: []`).
});

When("they open that challenger's promises view", async () => {
  await openChallengerDetails();
});

Then('the view shows a group for each of the voter\'s matching interests', async () => {
  await expect(
    world.page.getByRole('region', { name: /promises about topic:prescription-drug-pricing/i }),
  ).toBeVisible();
  await expect(
    world.page.getByRole('region', { name: /promises about topic:climate:carbon-pricing/i }),
  ).toBeVisible();
});

Then('each promise in a group shows its verbatim quote', async () => {
  await expect(
    world.page.getByText(/I will vote to cap insulin co-pays at \$35\/month/i),
  ).toBeVisible();
});

Then('the view says that promise does not match one of the voter\'s interests', async () => {
  await expect(world.page.getByText(/do not match one of the priorities you chose/i)).toBeVisible();
});

Then('that promise is labeled not testable', async () => {
  const climateGroup = world.page.getByRole('region', { name: /promises about topic:climate:carbon-pricing/i });
  await expect(climateGroup.getByText(/not testable/i)).toBeVisible();
});

Then("the view names the challenger's prior office", async () => {
  const priorOffice = world.page.getByRole('region', { name: /prior elected office/i });
  await expect(priorOffice).toBeVisible();
  // A plain notice, not a fabricated link to nowhere -- see this file's
  // header comment and `ChallengerPromisesView.tsx`'s `PriorOfficeNotice`.
  await expect(priorOffice.getByRole('link')).toHaveCount(0);
  await expect(priorOffice.getByText(/U\.S\. House of Representatives/i)).toBeVisible();
});

Then('the view names no prior office', async () => {
  await expect(world.page.getByRole('region', { name: /prior elected office/i })).toHaveCount(0);
});

Then('the view says there are no promises on file yet', async () => {
  await expect(world.page.getByText(/no promises on file yet/i)).toBeVisible();
});

Then('no promise group is shown', async () => {
  await expect(world.page.getByRole('heading', { name: /promises about your priorities/i })).toHaveCount(0);
});

Then('the view does not recommend, endorse or rank the candidate', async () => {
  const view = world.page.getByRole('region', { name: /challenger promises/i });
  const text = (await view.textContent()) ?? '';
  expect(text).not.toMatch(/vote for|recommend|endorse|best match|ranked/i);
});
