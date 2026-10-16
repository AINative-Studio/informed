/**
 * Tests the pending-step idiom itself (story 1.7).
 *
 * Two things need proving, and they pull in opposite directions:
 *
 * - a **pending** step must not fail the build, or the PRD's acceptance criteria
 *   could not be committed before the code that satisfies them;
 * - an **undefined** step (nobody wrote it) and a **broken** step must still
 *   fail the build, or the pending idiom would be a hole in CI.
 *
 * Each case runs Cucumber in a child process against a throwaway feature and
 * step file, using the real cucumber.cjs, and asserts on the exit code. That is
 * the only way to observe Cucumber's own pass/fail decision rather than
 * reimplementing it.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { createRequire } from 'node:module';
import { afterAll, describe, expect, it } from 'vitest';

const FEATURE = `Feature: Pending idiom
  Scenario: A step that is not implemented yet
    Given a shared-library scenario
    Then the unimplemented behaviour holds
`;

const PENDING_STEPS = `import { Given, Then } from '@cucumber/cucumber';
Given('a shared-library scenario', () => {});
Then('the unimplemented behaviour holds', () => 'pending');
`;

const BROKEN_STEPS = `import { Given, Then } from '@cucumber/cucumber';
Given('a shared-library scenario', () => {});
Then('the unimplemented behaviour holds', () => {
  throw new Error('this step is genuinely broken, not pending');
});
`;

/** Only the Given, so the Then is UNDEFINED. */
const INCOMPLETE_STEPS = `import { Given } from '@cucumber/cucumber';
Given('a shared-library scenario', () => {});
`;

const tempDirs: string[] = [];

/**
 * `strict` exactly as the project ships it, so these tests verify the real
 * setting instead of a copy that could drift from cucumber.cjs.
 */
const projectConfig = createRequire(import.meta.url)('../cucumber.cjs') as {
  default: { strict: boolean };
};
const projectStrict = projectConfig.default.strict;

/**
 * Runs Cucumber over one throwaway feature plus the given step source.
 *
 * The run uses its own config rather than the project's: cucumber.cjs globs in
 * `tests/steps/**` and `tests/features/**`, which would drag the real suite into
 * this run. Only `strict` is carried over, since that is the setting under test.
 */
function runCucumber(stepSource: string): { status: number; output: string } {
  // The scratch directory has to sit inside the project, not in the OS temp
  // dir: tsx resolves a .ts file's module kind from the nearest package.json,
  // and outside this tree there is none saying `"type": "module"`.
  const dir = mkdtempSync(join(process.cwd(), '.bdd-idiom-'));
  tempDirs.push(dir);
  // Cucumber resolves --config and the paths inside it against the cwd, so
  // every path handed to it below is relative to the project root.
  const rel = relative(process.cwd(), dir);
  const featurePath = join(rel, 'pending_idiom.feature');
  const stepsPath = join(rel, 'pending_idiom.steps.ts');
  const configPath = join(rel, 'cucumber.cjs');
  writeFileSync(featurePath, FEATURE);
  writeFileSync(stepsPath, stepSource);
  writeFileSync(
    configPath,
    `module.exports = { default: { paths: ${JSON.stringify([featurePath])}, ` +
      `import: ${JSON.stringify([stepsPath])}, format: ['summary'], ` +
      `strict: ${projectStrict} } };\n`,
  );

  const result = spawnSync('npx', ['cucumber-js', '--config', configPath], {
    cwd: process.cwd(),
    encoding: 'utf8',
    env: {
      ...process.env,
      NODE_OPTIONS: `${process.env.NODE_OPTIONS ?? ''} --import tsx/esm`.trim(),
    },
  });

  return { status: result.status ?? 1, output: `${result.stdout ?? ''}${result.stderr ?? ''}` };
}

afterAll(() => {
  for (const dir of tempDirs) rmSync(dir, { recursive: true, force: true });
});

describe('the pending-step idiom', () => {
  it('lets a pending step pass, so PRD scenarios can be committed before the code', () => {
    const { status, output } = runCucumber(PENDING_STEPS);

    expect(output).toContain('1 pending');
    expect(status).toBe(0);
  }, 120_000);

  it('still fails the build when a step is genuinely broken', () => {
    const { status, output } = runCucumber(BROKEN_STEPS);

    expect(output).toContain('1 failed');
    expect(status).not.toBe(0);
  }, 120_000);

  it('still fails the build when a step is undefined', () => {
    const { status, output } = runCucumber(INCOMPLETE_STEPS);

    expect(output).toContain('1 undefined');
    expect(status).not.toBe(0);
  }, 120_000);
});
