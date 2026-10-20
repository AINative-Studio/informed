/**
 * Steps for the frontend evidence-drawer feature (story 7.5).
 *
 * Builds on `candidate_detail.steps.ts`'s pattern of stubbing `/v1/ballot`,
 * `/v1/ballot/cards` and `/v1/candidates/{id}/alignment` so a scored
 * candidate's detail page is reachable, then adds a stub for story 5.4's
 * `GET /v1/bills/{bill_id}/text` so the drawer's "archived source" link has
 * something real to open.
 */
import { Given, Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { visit, world } from './common.steps';

const ADDRESS = '1 Synthetic Way, Example, CA 90000';
const DISTRICT_ID = 'ocd-division/country:us/state:ca/cd:19';
const ELECTION_ID = 'us-2026-11-03-general';
const CANDIDACY_ID = `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000001`;
const BILL_ID = 'us-house:119:hr-1';

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

function alignmentWith(citation: Record<string, unknown>) {
  return {
    candidacy_id: CANDIDACY_ID,
    person_id: 'person:ZZ000001',
    score: 74.5,
    confidence_badge: 'high',
    evidence_count: 12,
    scored_interest_count: 1,
    dealbreaker_warnings: [],
    reasons: [
      {
        stance_id: 'topic:prescription-drug-pricing:lower-costs',
        direction: 'raised',
        s_value: 0.6,
        confidence: 0.7,
        evidence_count: 12,
        citations: [citation],
      },
    ],
    methodology_version: '2026.1',
  };
}

const CITATION_WITH_BILL = {
  roll_call_id: 'us-house:119-2:452',
  position: 'yea',
  vote_type: 'final_passage',
  voted_at: '2026-03-01T00:00:00Z',
  bill_id: BILL_ID,
  is_bundled: false,
  bundled_reason: null,
};

const CITATION_WITHOUT_BILL = {
  roll_call_id: 'us-senate:119-2:12',
  position: 'yea',
  vote_type: 'cloture',
  voted_at: '2026-02-01T00:00:00Z',
  bill_id: null,
  is_bundled: false,
  bundled_reason: null,
};

const BILL_TEXT = {
  bill_id: BILL_ID,
  file_id: 'file:synthetic-1',
  url: 'https://files.example.test/synthetic-archive.pdf?signature=fake',
  expires_in_seconds: 300,
  sha256: 'synthetic-sha-256',
  size_bytes: 1024,
  content_type: 'application/pdf',
  source_url: 'https://www.congress.gov/bill/119th-congress/house-bill/1/text',
  captured_at: '2026-03-02T00:00:00Z',
  version_type: 'Engrossed in House',
  version_date: '2026-03-01T00:00:00Z',
  format_type: 'pdf',
};

async function stubBallotWithRaces(): Promise<void> {
  await world.page.route('**/v1/ballot?*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(BALLOT_WITH_RACES) });
  });
}

async function stubBallotCards(): Promise<void> {
  await world.page.route('**/v1/ballot/cards*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(HOUSE_CARDS) });
  });
}

async function stubAlignment(body: unknown): Promise<void> {
  await world.page.route('**/v1/candidates/**/alignment*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

async function stubBillText(): Promise<void> {
  await world.page.route('**/v1/bills/**/text*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(BILL_TEXT) });
  });
}

Given('a voter opens the Informed app with a scored candidate whose citation has an archived bill', async () => {
  await visit();
  await stubBallotWithRaces();
  await stubBallotCards();
  await stubAlignment(alignmentWith(CITATION_WITH_BILL));
  await stubBillText();
  await world.page.getByLabel(/address/i).fill(ADDRESS);
  await world.page.getByRole('button', { name: /representatives/i }).click();
});

Given('a voter opens the Informed app with a scored candidate whose citation has no bill', async () => {
  await visit();
  await stubBallotWithRaces();
  await stubBallotCards();
  await stubAlignment(alignmentWith(CITATION_WITHOUT_BILL));
  await stubBillText();
  await world.page.getByLabel(/address/i).fill(ADDRESS);
  await world.page.getByRole('button', { name: /representatives/i }).click();
});

// "they open that candidate's detail page" is already defined by
// candidate_detail.steps.ts (story 7.3) — Cucumber's registry is shared
// across every step file matched by cucumber.cjs, and a duplicate step
// definition is a Cucumber error (AMBIGUOUS), not a silent override. Reused
// here rather than redefined, same posture `ballot_view.steps.ts`'s own note
// on this documents for its sibling features.

When('they open the evidence drawer for that citation', async () => {
  const detail = world.page.getByRole('region', { name: /candidate detail/i });
  await detail.getByRole('button', { name: /roll call/i }).first().click();
});

Then(
  "the drawer shows the roll-call number, the date, the vote type and the candidate's position",
  async () => {
    const drawer = world.page.getByRole('dialog');
    await expect(drawer.getByText(/us-house:119-2:452/)).toBeVisible();
    await expect(drawer.getByText(/2026-03-01|march 1, 2026/i)).toBeVisible();
    await expect(drawer.getByText(/final passage/i)).toBeVisible();
    await expect(drawer.getByText(/yea/i)).toBeVisible();
  },
);

Then('the drawer shows a link to the bill', async () => {
  const drawer = world.page.getByRole('dialog');
  const link = drawer.getByRole('link', { name: /bill/i });
  await expect(link).toBeVisible();
  expect(await link.getAttribute('href')).toContain('congress.gov');
});

Then('the drawer shows a link to the archived copy of the bill text', async () => {
  const drawer = world.page.getByRole('dialog');
  const link = drawer.getByRole('link', { name: /archived/i });
  await expect(link).toBeVisible();
  expect(await link.getAttribute('href')).toBe(BILL_TEXT.url);
});

When('they close the drawer', async () => {
  const drawer = world.page.getByRole('dialog');
  await drawer.getByRole('button', { name: /close/i }).click();
});

When('they press Escape', async () => {
  await world.page.keyboard.press('Escape');
});

Then('the drawer is no longer visible', async () => {
  await expect(world.page.getByRole('dialog')).toHaveCount(0);
});

Then('focus returns to the link that opened it', async () => {
  const detail = world.page.getByRole('region', { name: /candidate detail/i });
  const opener = detail.getByRole('button', { name: /roll call/i }).first();
  await expect(opener).toBeFocused();
});

Then('no evidence drawer control is offered for that citation', async () => {
  const detail = world.page.getByRole('region', { name: /candidate detail/i });
  await expect(detail.getByText(/us-senate:119-2:12/)).toBeVisible();
  await expect(detail.getByRole('button', { name: /roll call/i })).toHaveCount(0);
});
