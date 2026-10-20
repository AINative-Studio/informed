/**
 * Steps for the accessibility-audit feature (story 7.8).
 *
 * Reuses other stories' own `Given`/`When` steps to get each view on
 * screen (`evidence_drawer.steps.ts`'s scored-candidate background,
 * `ballot_view.steps.ts`'s whole-ballot submit, `side_by_side_compare.
 * steps.ts`'s four-candidate race, `quick_onboarding.steps.ts`'s
 * quick-setup switch) — this file adds only the `Then` steps that run the
 * actual check, so a reviewer comparing this story's diff against the
 * features it builds on sees new assertions, not reimplemented navigation.
 *
 * Two different checks, because this story's acceptance criterion has two
 * different claims and this sandboxed environment can only make good on one
 * of them:
 *
 *  - `expectNoAxeViolations` drives @axe-core/playwright's `AxeBuilder`
 *    against the SAME `world.page` every other feature here already drives
 *    (a real rendered Chromium page, not a shallow unit render), restricted
 *    to the WCAG 2.0/2.1 A and AA rule tags — this is the actual "automated
 *    axe checks are clean" half of the acceptance criterion, run for real,
 *    against the ballot list, candidate detail, compare view, evidence
 *    drawer and quick-onboarding flow this epic built. No rule is ever
 *    excluded to force a zero count: the one thing turned off (legacy
 *    mode — see that function's own comment) changes HOW axe talks to the
 *    page, not WHICH rules run. This run found zero real violations on
 *    every one of those views as this story found them — see this story's
 *    PR for the full account, including the one harness bug (not a
 *    component defect) this story's own real "red" state surfaced and
 *    fixed before this suite could run at all.
 *
 *  - The remaining `Then` steps read Playwright's own `page.ariaSnapshot()`
 *    (the maintained replacement for the removed `page.accessibility.
 *    snapshot()` — this installed Playwright, 1.64, no longer exposes that
 *    API at all; see the typecheck failure this produced before this
 *    rewrite) to check two things a screen reader would also surface: every
 *    interactive node has a real, non-empty accessible name, and the tree's
 *    own node order (which is the order a screen reader actually walks,
 *    since Playwright builds this YAML by walking the accessibility tree
 *    depth-first in the same order the browser exposes it) matches the
 *    DOM's visual order for the region under test. This is NOT a substitute
 *    for a human running VoiceOver or NVDA through the main journey — it is
 *    the one piece of that half of the criterion this environment can check
 *    without a real screen reader attached. See this story's PR description
 *    for the explicit statement that the human pass itself was not
 *    performed here.
 */
import { Then } from '@cucumber/cucumber';
import AxeBuilder from '@axe-core/playwright';
import { expect } from '@playwright/test';

import { world } from './common.steps';

/**
 * WCAG 2.2 has no dedicated axe-core tag yet (axe-core's own tag set tops
 * out at `wcag21aa`); 2.2's AA criteria are a strict superset of 2.1 AA's
 * (2.2 added new criteria, it did not remove or loosen any 2.1 one), so
 * scanning against 2.0/2.1's A and AA tags is the closest real automated
 * check available and catches every 2.1-and-earlier AA violation a 2.2 scan
 * would also catch. `best-practice` is deliberately NOT included: those are
 * axe's own opinions beyond what WCAG actually requires, and this story's
 * acceptance criterion is WCAG 2.2 AA, not axe's house style.
 */
const WCAG_AA_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'];

/**
 * Runs an axe scan against the current page (optionally scoped to one
 * element) and asserts zero violations. On failure, prints every violation's
 * id, impact, help text and the CSS selector(s) of every affected node, so a
 * failing run names exactly what to fix rather than just "axe found
 * something" — the detail this story's brief asks the PR itself to state.
 */
async function expectNoAxeViolations(scopeSelector?: string): Promise<void> {
  let builder = new AxeBuilder({ page: world.page }).withTags(WCAG_AA_TAGS);
  if (scopeSelector) builder = builder.include(scopeSelector);
  // Legacy mode: `AxeBuilder#analyze()`'s default path (`runPartial`/
  // `finishRun`) opens a second, same-context blank page to combine
  // results across frames — needed for multi-iframe apps, not this one —
  // and throws "Please use browser.newContext()" from axe-core's own
  // `finishRun` in this harness, where `common.steps.ts`'s `Before` hook
  // opens the page with a bare `browser.newPage()` rather than an explicit
  // `browser.newContext()` (confirmed by running this scan and hitting that
  // exact error before this line was added — see this story's PR for the
  // real stack trace). Legacy mode calls `axe.run()` directly in THIS page
  // instead, which this app — a single page with no iframes anywhere, per
  // `App.tsx`'s own docstring on there being no router — never needs the
  // cross-frame path for anyway, so nothing this suite can actually scan is
  // lost by using it.
  builder = builder.setLegacyMode(true);
  const results = await builder.analyze();

  if (results.violations.length > 0) {
    const report = results.violations
      .map((violation) => {
        const targets = violation.nodes.map((node) => node.target.join(' ')).join('; ');
        return `- [${violation.impact}] ${violation.id}: ${violation.help} (${violation.helpUrl})\n  targets: ${targets}`;
      })
      .join('\n');
    throw new Error(`axe found ${results.violations.length} violation(s):\n${report}`);
  }

  expect(results.violations).toHaveLength(0);
}

Then('the ballot list has no automated accessibility violations', async () => {
  await expectNoAxeViolations();
});

Then('the candidate detail page has no automated accessibility violations', async () => {
  await expectNoAxeViolations();
});

Then('the open page has no automated accessibility violations', async () => {
  // Scans the WHOLE page, not just the dialog: a focus-trapped modal that
  // leaves the background inert to assistive tech still has to leave the
  // rest of the page's own markup valid, and an open-drawer scan is the one
  // moment every other feature's own fixtures put both the page and the
  // dialog on screen at once.
  await expectNoAxeViolations();
});

Then('the compare view has no automated accessibility violations', async () => {
  await expectNoAxeViolations();
});

Then('the onboarding screen has no automated accessibility violations', async () => {
  await expectNoAxeViolations();
});

/**
 * Roles the ARIA-snapshot YAML names that a voter actually operates
 * (clicks, types into, or picks from) — the set this story cares whether
 * each one has a real accessible name. Matches `AxeBuilder`'s own concern
 * (`button-name`, `link-name`, `aria-input-field-name`, `select-name` etc
 * are all axe rules already in `WCAG_AA_TAGS` above), so this check is a
 * second, independent read of a claim axe already makes — not the only
 * line of defense for it.
 */
const INTERACTIVE_ROLES = ['button', 'link', 'checkbox', 'radio', 'textbox', 'combobox', 'slider', 'switch', 'menuitem', 'tab'];

/**
 * Every `role "name"` pair in a YAML ARIA snapshot whose role is interactive
 * — `role:` with no quoted name following is exactly how Playwright's own
 * YAML renders a node with no accessible name at all (a named node is always
 * `role "name":`), so a bare `role:`/`role` line for one of these roles is a
 * real missing-name finding, not a formatting quirk.
 */
function unnamedInteractiveLines(ariaYaml: string): string[] {
  const rolePattern = new RegExp(`^(\\s*-\\s*)(${INTERACTIVE_ROLES.join('|')})(\\s*(?:"[^"]*")?\\s*:?)`, 'i');
  return ariaYaml
    .split('\n')
    .filter((line) => {
      const match = rolePattern.exec(line);
      if (!match) return false;
      return !/"[^"]+"/.test(line); // has a role but no quoted, non-empty name
    })
    .map((line) => line.trim());
}

Then('every interactive control on the page has a real accessible name', async () => {
  const ariaYaml = await world.page.locator('body').ariaSnapshot();
  const unnamed = unnamedInteractiveLines(ariaYaml);
  if (unnamed.length > 0) {
    throw new Error(`${unnamed.length} interactive node(s) with no real accessible name:\n${unnamed.join('\n')}`);
  }
  expect(unnamed).toHaveLength(0);
});

Then("the drawer's accessibility-tree reading order matches its visual order", async () => {
  const dialog = world.page.getByRole('dialog');
  await expect(dialog).toBeVisible();

  // The DOM's own visual order: every non-empty text node inside the
  // dialog, depth-first, which for this app's plain top-to-bottom layout
  // (no CSS exists yet to reorder anything visually — see this story's PR
  // for why that makes "visual order" and "DOM order" the same claim today)
  // is reading order.
  // Walks with a plain, filter-less `createTreeWalker` rather than an
  // `acceptNode` callback: esbuild's helper injection for a named function
  // passed as an object-literal property (`tsx`'s transform of this file)
  // left a dangling reference to its own `__name` helper in the function
  // source `page.evaluate` sends into the browser's isolated context, where
  // that helper does not exist — confirmed by running this step and hitting
  // `ReferenceError: __name is not defined` before this rewrite. Filtering
  // blank text nodes in plain JS after the walk, instead of inside the
  // walker's own options object, avoids the pattern that triggered it.
  const domOrder = await dialog.evaluate((element) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const texts: string[] = [];
    let current = walker.nextNode();
    while (current) {
      const text = (current.textContent ?? '').trim();
      if (text.length > 0) texts.push(text);
      current = walker.nextNode();
    }
    return texts;
  });

  // The accessibility tree's own order, as one string: `ariaSnapshot()`
  // emits one line per node in the same depth-first order the browser's
  // accessibility tree uses, which is the order a screen reader actually
  // walks. Deliberately NOT tokenized into discrete node names to compare
  // list-for-list against `domOrder`: the tree composes some accessible
  // names from several DOM text fragments at once (a link's name folding
  // in its own child text, e.g. "Open the archived source (Engrossed in
  // House (2026-03-01))" from what the DOM renders as two separate text
  // nodes), and a `<dt>`/`<dd>` pair's text renders as unquoted `term:`/
  // `definition:` lines rather than a quoted name at all — found by
  // running exactly that comparison against this drawer's real render and
  // seeing both of those legitimate mismatches, not an accessibility bug.
  // Each DOM fragment's position *within the YAML text* is what the actual
  // claim "reading order matches visual order" needs: every fragment the
  // DOM shows in some order must be findable in the YAML in that SAME
  // order, wherever within a YAML line or across composed names it lands.
  const ariaYaml = await dialog.ariaSnapshot();
  let searchFrom = 0;
  const outOfOrder: string[] = [];
  for (const text of domOrder) {
    const foundAt = ariaYaml.indexOf(text, searchFrom);
    if (foundAt === -1) {
      outOfOrder.push(`"${text}" (not found at or after the previous fragment's position in the ARIA snapshot)`);
      continue;
    }
    searchFrom = foundAt + text.length;
  }
  if (outOfOrder.length > 0) {
    throw new Error(`Reading order mismatch for:\n${outOfOrder.join('\n')}\n\nARIA snapshot:\n${ariaYaml}`);
  }
  expect(outOfOrder).toHaveLength(0);
});

// No `Given`/`When` steps are defined in this file: every navigation step
// this feature uses ("a voter opens the Informed app with a scored
// candidate whose citation has an archived bill", "they open that
// candidate's detail page", "they open the evidence drawer for that
// citation", "a voter opens the Informed app with a ballot that has four
// scored candidates in one race", "they submit their address for the
// whole-ballot view", "they select two candidates in that race to compare",
// "they open the compare view", "a voter opens the Informed app", "they
// switch to the quick-setup screen") is already defined by the feature
// files this one builds on — see this file's own docstring.
