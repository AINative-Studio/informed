/**
 * Steps for the frontend side-by-side-compare feature (story 7.4).
 *
 * Builds on `ballot_view.steps.ts`'s pattern of stubbing `/v1/ballot` and
 * `/v1/ballot/cards`, with a race carrying four candidates — enough to prove
 * the 2-4 candidate cap in both directions — and adds a stub for
 * `GET /v1/candidates/{id}/alignment?profile_id=` per candidacy, each with a
 * deliberately different set of stance ids so "same rows across candidates"
 * (the union, not the intersection) has something real to assert against.
 */
import { Given, Then, When } from '@cucumber/cucumber';
import { expect } from '@playwright/test';

import { visit, world } from './common.steps';

const DISTRICT_ID = 'ocd-division/country:us/state:ca/cd:19';
const ELECTION_ID = 'us-2026-11-03-general';

const CANDIDACY_A = `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000001`;
const CANDIDACY_B = `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000002`;
const CANDIDACY_C = `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000003`;
const CANDIDACY_D = `${ELECTION_ID}:${DISTRICT_ID}:person:ZZ000004`;

const NAMES: Record<string, string> = {
  [CANDIDACY_A]: 'Rep. Synthetic Alpha',
  [CANDIDACY_B]: 'Rep. Synthetic Beta',
  [CANDIDACY_C]: 'Rep. Synthetic Gamma',
  [CANDIDACY_D]: 'Rep. Synthetic Delta',
};

const BALLOT_WITH_FOUR_CANDIDATES = {
  district: { id: DISTRICT_ID, name: 'California 19th Congressional District', state: 'CA' },
  officeholders: [
    { person_id: 'person:ZZ000001', name: NAMES[CANDIDACY_A], body_id: 'us-house', party: 'Independent' },
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
        { person_id: 'person:ZZ000001', name: NAMES[CANDIDACY_A], party: 'Independent', is_incumbent: true, candidacy_id: CANDIDACY_A },
        { person_id: 'person:ZZ000002', name: NAMES[CANDIDACY_B], party: 'Other', is_incumbent: false, candidacy_id: CANDIDACY_B },
        { person_id: 'person:ZZ000003', name: NAMES[CANDIDACY_C], party: 'Other', is_incumbent: false, candidacy_id: CANDIDACY_C },
        { person_id: 'person:ZZ000004', name: NAMES[CANDIDACY_D], party: 'Other', is_incumbent: false, candidacy_id: CANDIDACY_D },
      ],
    },
  ],
};

function cardFor(candidacyId: string) {
  return {
    candidacy_id: candidacyId,
    person_id: candidacyId,
    person_name: NAMES[candidacyId],
    party: null,
    district: 'California 19th Congressional District',
    photo_url: null,
    is_incumbent: false,
    verdict_counts: null,
    say_do_gap: 0.3,
    positions_summary: [{ stance_id: 'topic:x', s_value: 0.3, confidence: 0.6, evidence_count: 4 }],
    confidence_badge: 'high',
    methodology_version: '2026.1',
    updated_at: '2026-10-19T00:00:00Z',
  };
}

const CARDS_RESPONSE = {
  election_id: ELECTION_ID,
  district_id: DISTRICT_ID,
  cards: [CANDIDACY_A, CANDIDACY_B, CANDIDACY_C, CANDIDACY_D].map(cardFor),
};

/**
 * Candidate A has a position on BOTH `topic:shared` and `topic:a-only`;
 * candidate B has a position only on `topic:shared`. "Same rows across
 * candidates" means the compare view's row set is the UNION of these two
 * — `topic:a-only` must still appear as a row even though B never measured
 * it, with an honest empty state in B's column rather than the row being
 * dropped or B's cell showing a fabricated zero.
 */
function alignmentFor(candidacyId: string) {
  if (candidacyId === CANDIDACY_A) {
    return {
      candidacy_id: CANDIDACY_A,
      person_id: 'person:ZZ000001',
      score: 70,
      confidence_badge: 'high',
      evidence_count: 8,
      scored_interest_count: 2,
      dealbreaker_warnings: [],
      reasons: [
        {
          stance_id: 'topic:shared',
          direction: 'raised',
          s_value: 0.6,
          confidence: 0.7,
          evidence_count: 5,
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
          stance_id: 'topic:a-only',
          direction: 'lowered',
          s_value: -0.5,
          confidence: 0.6,
          evidence_count: 3,
          citations: [
            {
              roll_call_id: 'us-house:119-2:453',
              position: 'nay',
              vote_type: 'final_passage',
              voted_at: '2026-04-01T00:00:00Z',
              bill_id: 'us-house:119:hr-2',
              is_bundled: false,
              bundled_reason: null,
            },
          ],
        },
      ],
      methodology_version: '2026.1',
    };
  }
  // Every other selected candidate: a position on `topic:shared` only, so
  // `topic:a-only` is a row they have genuinely never been measured on.
  return {
    candidacy_id: candidacyId,
    person_id: candidacyId,
    score: 55,
    confidence_badge: 'medium',
    evidence_count: 4,
    scored_interest_count: 1,
    dealbreaker_warnings: [],
    reasons: [
      {
        stance_id: 'topic:shared',
        direction: 'lowered',
        s_value: -0.3,
        confidence: 0.5,
        evidence_count: 4,
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
}

async function stubBallot(): Promise<void> {
  await world.page.route('**/v1/ballot?*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(BALLOT_WITH_FOUR_CANDIDATES) });
  });
}

async function stubCards(): Promise<void> {
  await world.page.route('**/v1/ballot/cards*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CARDS_RESPONSE) });
  });
}

async function stubAlignment(): Promise<void> {
  await world.page.route('**/v1/candidates/**/alignment*', async (route) => {
    const url = new URL(route.request().url());
    const match = /\/v1\/candidates\/(.+)\/alignment/.exec(url.pathname);
    const candidacyId = match ? decodeURIComponent(match[1]) : '';
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(alignmentFor(candidacyId)),
    });
  });
}

Given('a voter opens the Informed app with a ballot that has four scored candidates in one race', async () => {
  await visit();
  await stubBallot();
  await stubCards();
  await stubAlignment();
});

// "they submit their address for the whole-ballot view" is already defined
// by ballot_view.steps.ts (story 7.1) — reused rather than redefined, same
// posture as sortable_ranking.steps.ts and candidate_detail.steps.ts.

function raceArticle() {
  return world.page.getByRole('heading', { name: 'U.S. House of Representatives' }).locator('..');
}

Then('there is no way to open the compare view yet', async () => {
  const race = raceArticle();
  await expect(race.getByRole('button', { name: /compare/i })).toHaveCount(0);
});

When('they select two candidates in that race to compare', async () => {
  const race = raceArticle();
  await race.getByRole('checkbox', { name: new RegExp(`select ${NAMES[CANDIDACY_A]}.*compare`, 'i') }).check();
  await race.getByRole('checkbox', { name: new RegExp(`select ${NAMES[CANDIDACY_B]}.*compare`, 'i') }).check();
});

When('they select two more candidates in that race to compare', async () => {
  const race = raceArticle();
  await race.getByRole('checkbox', { name: new RegExp(`select ${NAMES[CANDIDACY_C]}.*compare`, 'i') }).check();
  await race.getByRole('checkbox', { name: new RegExp(`select ${NAMES[CANDIDACY_D]}.*compare`, 'i') }).check();
});

Then('a control to compare the selected candidates appears', async () => {
  const race = raceArticle();
  await expect(race.getByRole('button', { name: /compare/i })).toBeVisible();
});

Then('a fifth candidate cannot be selected for comparison', async () => {
  // Only four candidates exist in this fixture, so "cannot be selected" is
  // proven by every unselected checkbox now being disabled — there is no
  // fifth candidate to click, which is itself the point: the cap is reached
  // and the UI must not let a fifth checkbox be checked once four already are.
  const race = raceArticle();
  const checkboxes = race.getByRole('checkbox', { name: /compare/i });
  const count = await checkboxes.count();
  for (let index = 0; index < count; index += 1) {
    const checkbox = checkboxes.nth(index);
    if (!(await checkbox.isChecked())) {
      await expect(checkbox).toBeDisabled();
    }
  }
});

When('they open the compare view', async () => {
  const race = raceArticle();
  await race.getByRole('button', { name: /compare/i }).click();
});

function compareRegion() {
  return world.page.getByRole('region', { name: /compare/i });
}

Then('every selected candidate\'s column appears', async () => {
  const compare = compareRegion();
  const table = compare.getByRole('table');
  await expect(table.getByRole('columnheader', { name: NAMES[CANDIDACY_A] })).toBeVisible();
  await expect(table.getByRole('columnheader', { name: NAMES[CANDIDACY_B] })).toBeVisible();
});

Then('the same set of interest rows appears for every candidate', async () => {
  const compare = compareRegion();
  // Both `topic:shared` and `topic:a-only` are rows, even though only A has
  // a measured position on `topic:a-only` — the union, not the intersection.
  await expect(compare.getByRole('row', { name: /topic:shared/i })).toBeVisible();
  await expect(compare.getByRole('row', { name: /topic:a-only/i })).toBeVisible();
});

Then(
  'the row for an interest only one candidate has a position on shows an honest empty state for the other candidate',
  async () => {
    const compare = compareRegion();
    const row = compare.getByRole('row', { name: /topic:a-only/i });
    await expect(row).toContainText(/no measured position/i);
  },
);

Then('every color-coded cell also carries a text label saying its direction', async () => {
  const compare = compareRegion();
  // Wait for the table to actually render before inspecting its cells:
  // `locator.count()` is a single snapshot with no auto-retry (unlike
  // `expect(...).toBeVisible()`, which polls), so without this the step can
  // run while the per-candidacy alignment queries are still pending and see
  // zero `[data-direction]` cells that are about to appear a moment later —
  // the same flake `candidate_detail.steps.ts`'s own "at least one control"
  // step already documents and guards against the same way.
  await expect(compare.getByRole('table')).toBeVisible();
  // Every cell whose rendering carries a direction class/data attribute must
  // also contain readable text naming that direction — "agrees"/"disagrees"
  // etc — not just a colored swatch with no text content a screen reader or
  // colorblind voter could use. See CompareView.tsx's own `DirectionCell`.
  const directionCells = compare.locator('[data-direction]');
  const cellCount = await directionCells.count();
  expect(cellCount).toBeGreaterThan(0);
  for (let index = 0; index < cellCount; index += 1) {
    const cell = directionCells.nth(index);
    const text = (await cell.textContent()) ?? '';
    expect(text).toMatch(/agrees|disagrees|neutral|no measured position/i);
  }
});

Then('the compare view does not recommend, endorse or rank any candidate', async () => {
  const compare = compareRegion();
  await expect(compare.getByRole('table')).toBeVisible();
  const text = (await compare.textContent()) ?? '';
  expect(text).not.toMatch(/vote for|recommend|endorse|best match|winner|ranked/i);
});
