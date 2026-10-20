/**
 * Steps for the frontend user-sortable-ranking feature (story 7.6).
 *
 * Builds on `ballot_view.steps.ts`'s stubs for `/v1/ballot` and
 * `/v1/ballot/cards`, with a race carrying more than one candidate so an
 * order can actually be observed, and one candidate whose card has no score
 * (`say_do_gap: null`) so the "does not crash or treat missing as zero"
 * scenario has something real to assert against.
 */
import { Given, Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { visit, world } from './common.steps';

const DISTRICT_ID = 'ocd-division/country:us/state:ca/cd:19';
const ELECTION_ID = 'us-2026-11-03-general';

const CANDIDACY_LOW = `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000001`;
const CANDIDACY_HIGH = `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000002`;
const CANDIDACY_NO_SCORE = `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000003`;

const BALLOT_WITH_RACE = {
  district: { id: DISTRICT_ID, name: 'California 19th Congressional District', state: 'CA' },
  officeholders: [
    { person_id: 'person:ZZ000001', name: 'Rep. Synthetic Low', body_id: 'us-house', party: 'Independent' },
    { person_id: 'person:ZZ000002', name: 'Rep. Synthetic High', body_id: 'us-house', party: 'Independent' },
  ],
  races: [
    {
      id: 'us-house:ocd-division/country:us/state:ca/cd:19',
      body_id: 'us-house',
      office_name: 'U.S. House of Representatives',
      district_id: DISTRICT_ID,
      district_name: 'California 19th Congressional District',
      election_id: null,
      // Ballot order: Low, then High — deliberately NOT score order, so a
      // test asserting "ballot order" and one asserting "score order" can
      // never pass by coincidence of the fixture's own listing order.
      candidates: [
        { person_id: 'person:ZZ000001', name: 'Rep. Synthetic Low', party: 'Independent', is_incumbent: true, candidacy_id: CANDIDACY_LOW },
        { person_id: 'person:ZZ000002', name: 'Rep. Synthetic High', party: 'Independent', is_incumbent: false, candidacy_id: CANDIDACY_HIGH },
      ],
    },
  ],
};

const BALLOT_WITH_UNSCORED_CANDIDATE = {
  ...BALLOT_WITH_RACE,
  officeholders: [
    ...BALLOT_WITH_RACE.officeholders,
    { person_id: 'person:ZZ000003', name: 'Rep. Synthetic Unscored', body_id: 'us-house', party: null },
  ],
  races: [
    {
      ...BALLOT_WITH_RACE.races[0],
      candidates: [
        ...BALLOT_WITH_RACE.races[0].candidates,
        { person_id: 'person:ZZ000003', name: 'Rep. Synthetic Unscored', party: null, is_incumbent: false, candidacy_id: CANDIDACY_NO_SCORE },
      ],
    },
  ],
};

function cardFor(candidacyId: string, sayDoGap: number | null) {
  return {
    candidacy_id: candidacyId,
    person_id: candidacyId,
    person_name: candidacyId,
    party: null,
    district: 'California 19th Congressional District',
    photo_url: null,
    is_incumbent: false,
    verdict_counts: null,
    say_do_gap: sayDoGap,
    positions_summary:
      sayDoGap === null ? null : [{ stance_id: 'topic:x', s_value: 0.5, confidence: 0.6, evidence_count: 4 }],
    confidence_badge: sayDoGap === null ? 'insufficient' : 'high',
    methodology_version: sayDoGap === null ? null : '2026.1',
    updated_at: '2026-10-19T00:00:00Z',
  };
}

function cardsResponse(cards: ReturnType<typeof cardFor>[]) {
  return { election_id: ELECTION_ID, district_id: DISTRICT_ID, cards };
}

async function stubBallot(body: unknown): Promise<void> {
  await world.page.route('**/v1/ballot?*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

async function stubCards(body: unknown): Promise<void> {
  await world.page.route('**/v1/ballot/cards*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

Given('a voter opens the Informed app with a ballot that has multiple scored candidates in one race', async () => {
  await visit();
  await stubBallot(BALLOT_WITH_RACE);
  // Low's say/do gap is numerically smaller than High's — "High" sorts first
  // by score, "Low" sorts first in ballot order, so the two scenarios can
  // never agree by accident.
  await stubCards(
    cardsResponse([cardFor(CANDIDACY_LOW, 0.1), cardFor(CANDIDACY_HIGH, 0.9)]),
  );
});

Given('a voter opens the Informed app with a ballot where one candidate in a race has no score yet', async () => {
  await visit();
  await stubBallot(BALLOT_WITH_UNSCORED_CANDIDATE);
  await stubCards(
    cardsResponse([
      cardFor(CANDIDACY_LOW, 0.1),
      cardFor(CANDIDACY_HIGH, 0.9),
      cardFor(CANDIDACY_NO_SCORE, null),
    ]),
  );
});

// "they submit their address for the whole-ballot view" is already defined
// by ballot_view.steps.ts (story 7.1) — reused rather than redefined, for
// the same reason `evidence_drawer.steps.ts` reuses
// candidate_detail.steps.ts's "they open that candidate's detail page": a
// duplicate step definition is a Cucumber AMBIGUOUS error, not an override.

function raceList() {
  const ballot = world.page.getByRole('region', { name: /your ballot/i });
  return ballot.getByRole('listitem');
}

Then('the candidates in that race appear in ballot order', async () => {
  const items = raceList();
  await expect(items.first()).toContainText('Rep. Synthetic Low');
  const texts = await items.allTextContents();
  const names = texts.filter((text) => /Rep\. Synthetic (Low|High)/.test(text));
  expect(names[0]).toContain('Low');
  expect(names[1]).toContain('High');
});

When('they turn on sorting by score', async () => {
  await world.page.getByRole('checkbox', { name: /sort.*score|score.*sort/i }).check();
});

When('they turn sorting off again', async () => {
  await world.page.getByRole('checkbox', { name: /sort.*score|score.*sort/i }).uncheck();
});

Then('the candidates in that race appear in score order', async () => {
  const items = raceList();
  const texts = await items.allTextContents();
  const names = texts.filter((text) => /Rep\. Synthetic (Low|High)/.test(text));
  // Higher say/do gap first.
  expect(names[0]).toContain('High');
  expect(names[1]).toContain('Low');
});

Then('every candidate in that race is still shown', async () => {
  const ballot = world.page.getByRole('region', { name: /your ballot/i });
  await expect(ballot.getByText('Rep. Synthetic Low')).toBeVisible();
  await expect(ballot.getByText('Rep. Synthetic High')).toBeVisible();
  await expect(ballot.getByText('Rep. Synthetic Unscored')).toBeVisible();
});

Then('the candidate with no score does not crash or appear scored as zero', async () => {
  const ballot = world.page.getByRole('region', { name: /your ballot/i });
  const unscoredRow = ballot.getByText('Rep. Synthetic Unscored').locator('..');
  await expect(unscoredRow.getByText(/not available yet/i)).toBeVisible();
});

Then('the sort control\'s label does not say recommended, best match, top pick or endorse', async () => {
  const ballot = world.page.getByRole('region', { name: /your ballot/i });
  const control = ballot.getByRole('checkbox', { name: /sort/i });
  const label = await control.getAttribute('aria-label') ?? '';
  const text = (await ballot.textContent()) ?? '';
  expect(label + text).not.toMatch(/recommend|best match|top pick|endorse/i);
});
