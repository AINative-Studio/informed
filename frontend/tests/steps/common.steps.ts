/**
 * Shared BDD step library for the Informed frontend (story 1.7).
 *
 * Cucumber loads every file matched by `import` in cucumber.cjs into one
 * registry, so the steps and the world below are available to every feature
 * file without an import. A feature-specific step file adds only the steps
 * unique to it, and imports `world` / `PENDING` from here.
 *
 * ## Writing a new feature
 *
 * 1. Put the Gherkin under `tests/features/<name>.feature`.
 * 2. If it needs steps nobody has written, add `tests/steps/<name>.steps.ts`.
 * 3. Drive the browser through `world.page` — the Before/After hooks here open
 *    and close it, so a new step file never launches its own browser.
 *
 * ## Pending steps
 *
 * The PRD's acceptance criteria are committed long before the code that
 * satisfies them. A step that returns `PENDING` ('pending') reports as
 * `PENDING`, and Cucumber's `shouldCauseFailure` only treats `PENDING` as a
 * failure when `strict` is on — so cucumber.cjs sets `strict: false`.
 *
 * Crucially that does NOT weaken the suite: `UNDEFINED` (a step nobody wrote)
 * and `FAILED` (a step that is actually broken) cause failure regardless of
 * `strict`. So an unwritten or broken step still turns CI red, while a declared
 * pending step does not. tests/pending-idiom.test.ts proves all three cases.
 */
import { After, Before, Given, Then, When, setDefaultTimeout } from '@cucumber/cucumber';
import { type Browser, type Page, chromium, expect } from '@playwright/test';

setDefaultTimeout(30_000);

/** Return this from a step that is waiting on a story to be implemented. */
export const PENDING = 'pending' as const;

export const baseUrl = process.env.INFORMED_WEB_URL ?? 'http://127.0.0.1:4173';

/**
 * Shared fixtures for every UI step file.
 *
 * This is the frontend half of the acceptance criterion "API and UI steps share
 * fixtures": the browser, the page and the per-scenario state live here, opened
 * once per scenario by the hooks below, so no step file manages its own.
 */
export interface World {
  browser: Browser;
  page: Page;
  /** Carries state between Given, When and Then steps of one scenario. */
  context: ScenarioState;
}

/** Synthetic per-scenario state. Never holds real voter data (see prd.md). */
export interface ScenarioState {
  voter: VoterProfile;
  candidates: Map<string, Candidate>;
  promises: Promise_[];
  question?: string;
}

export interface Interest {
  topic: string;
  tier: Tier;
  stance?: string;
  dealbreaker: boolean;
}

export type Tier = 'primary' | 'secondary' | 'nice-to-have';

/** Tier weights from prd.md -> "Tiered interest profile". */
export const TIER_WEIGHTS: Record<Tier, number> = {
  primary: 3.0,
  secondary: 1.5,
  'nice-to-have': 0.5,
};

export interface VoterProfile {
  interests: Interest[];
}

export interface Vote {
  bill: string;
  position: 'Yea' | 'Nay';
  voteType: 'final-passage' | 'procedural';
}

export interface Candidate {
  name: string;
  overallScore?: number;
  votes: Vote[];
}

/** A synthetic campaign promise and the evidence it is tested against. */
export interface Promise_ {
  text: string;
  sourceUrl: string;
  capturedAt: string;
  rollCallUrl?: string;
}

export const world = {} as World;

/** Weights of a profile's interests, in the order they were chosen. */
export function weightsOf(profile: VoterProfile): number[] {
  return profile.interests.map((interest) => TIER_WEIGHTS[interest.tier]);
}

export function hasPrimary(profile: VoterProfile): boolean {
  return profile.interests.some((interest) => interest.tier === 'primary');
}

export function candidateNamed(name: string): Candidate {
  const existing = world.context.candidates.get(name);
  if (existing) return existing;
  const created: Candidate = { name, votes: [] };
  world.context.candidates.set(name, created);
  return created;
}

export function newScenarioState(): ScenarioState {
  return { voter: { interests: [] }, candidates: new Map(), promises: [] };
}

Before(async () => {
  world.browser = await chromium.launch();
  world.page = await world.browser.newPage();
  world.context = newScenarioState();
});

After(async () => {
  await world.browser?.close();
});

/** Opens the app. Shared by every feature that starts from a page load. */
export async function visit(path = '/'): Promise<void> {
  await world.page.goto(new URL(path, baseUrl).toString(), { waitUntil: 'domcontentloaded' });
}

// --- App shell (story 1.1, implemented) ----------------------------------

Given('a voter opens the Informed app', async () => {
  await visit();
});

Then('the page shows the Informed name', async () => {
  await expect(world.page.getByRole('heading', { name: 'Informed', level: 1 })).toBeVisible();
});

Then('the page explains that Informed is nonpartisan', async () => {
  await expect(world.page.getByText(/nonpartisan/i)).toBeVisible();
});

// --- Tiered interest profile (epic 3) ------------------------------------

Given('a new voter on the onboarding screen', async () => {
  await visit();
  world.context.voter = { interests: [] };
});

When(
  'they choose {string} as a primary interest with stance {string}',
  (topic: string, stance: string) => {
    world.context.voter.interests.push({ topic, tier: 'primary', stance, dealbreaker: false });
  },
);

When('they choose {string} as a secondary interest', (topic: string) => {
  world.context.voter.interests.push({ topic, tier: 'secondary', dealbreaker: false });
});

When('they choose {string} as a nice-to-have interest', (topic: string) => {
  world.context.voter.interests.push({ topic, tier: 'nice-to-have', dealbreaker: false });
});

Then(
  'their profile is saved with weights {float}, {float} and {float}',
  (first: number, second: number, third: number) => {
    // The tier weights are asserted locally; the onboarding UI that saves the
    // profile is story 3.2.
    expect(weightsOf(world.context.voter)).toEqual([first, second, third]);
    return PENDING; // story 3.2 — onboarding screen that saves the profile
  },
);

Then('no candidate score is shown until at least one primary interest exists', () => {
  return PENDING; // story 4.2 — score gating on a primary interest
});

Given('a voter marks {string} as a dealbreaker', (topic: string) => {
  world.context.voter = {
    interests: [
      { topic, tier: 'primary', stance: 'lower out-of-pocket costs', dealbreaker: true },
    ],
  };
});

Given(
  'candidate {word} scores {float} overall but acted against that stance on {int} final-passage votes',
  (name: string, score: number, count: number) => {
    const candidate = candidateNamed(name);
    candidate.overallScore = score;
    for (let index = 0; index < count; index += 1) {
      candidate.votes.push({
        bill: `synthetic-bill-${index}`,
        position: 'Nay',
        voteType: 'final-passage',
      });
    }
  },
);

When('the voter views the ballot', () => {
  return PENDING; // story 4.4 — ballot view
});

Then('candidate {word} shows a dealbreaker warning above the score', (_name: string) => {
  return PENDING; // story 4.5 — dealbreaker warning on the ballot
});

// --- Promise vs. Record (epic 7) -----------------------------------------

Given(
  "incumbent {word}'s {int} campaign site committed to {string}",
  (name: string, year: number, promise: string) => {
    candidateNamed(name);
    world.context.promises.push({
      text: promise,
      sourceUrl: 'https://web.archive.org/synthetic/campaign-page',
      capturedAt: `${year}-09-01T00:00:00Z`,
    });
  },
);

Given(
  '{word} voted {word} on the final-passage roll call of a bill {}',
  (name: string, position: string, description: string) => {
    candidateNamed(name).votes.push({
      bill: description,
      position: position as Vote['position'],
      voteType: 'final-passage',
    });
    const latest = world.context.promises.at(-1);
    if (latest) latest.rollCallUrl = 'https://clerk.house.gov/synthetic/roll-call';
  },
);

When("the voter opens {word}'s Promise vs. Record", (_name: string) => {
  return PENDING; // story 7.5 — Promise vs. Record view
});

Then('the promise shows verdict {string}', (_verdict: string) => {
  return PENDING; // story 7.5 — verdict badge in the UI
});

Then('it links the archived campaign page and the roll-call record', () => {
  return PENDING; // story 7.5 — citation links rendered beside the verdict
});

Given('a promise maps only to a bill passed by voice vote', () => {
  // A voice vote leaves no roll call, so there is no recorded position to show.
  world.context.promises.push({
    text: 'synthetic promise',
    sourceUrl: 'https://web.archive.org/synthetic/campaign-page',
    capturedAt: '2024-09-01T00:00:00Z',
  });
});

Then('the verdict is {string} and the UI says {string}', (_verdict: string, _message: string) => {
  expect(world.context.promises.at(-1)?.rollCallUrl).toBeUndefined();
  return PENDING; // story 7.5 — "no recorded vote" copy in the UI
});

// --- Agent answers are grounded (epic 14) --------------------------------

When('a voter asks {string}', (question: string) => {
  world.context.question = question;
});

Then("the agent explains it doesn't recommend candidates", () => {
  return PENDING; // story 14.4 — Q&A panel showing the refusal
});

Then("offers the voter's alignment comparison instead", () => {
  return PENDING; // story 14.4 — alignment comparison offered in its place
});
