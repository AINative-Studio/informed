/**
 * Cucumber config for the frontend BDD suite (stories 1.1, 1.7).
 * Features live in tests/features; steps drive a real browser via Playwright.
 * TypeScript steps are loaded by tsx, registered through NODE_OPTIONS in the
 * `test:bdd` script (Node >= 20 rejects the deprecated --loader flag).
 *
 * `strict` is off (story 1.7) so the PRD's acceptance criteria can be committed
 * before the code that satisfies them. In Cucumber's `shouldCauseFailure`,
 * AMBIGUOUS, FAILED and UNDEFINED always fail the run; PENDING fails only under
 * `strict`. So turning strict off lets a step that declares itself pending pass,
 * while a step nobody wrote (UNDEFINED) or one that is actually broken (FAILED)
 * still turns CI red. tests/pending-idiom.test.ts asserts all three.
 */
module.exports = {
  default: {
    paths: ['tests/features/**/*.feature'],
    import: ['tests/steps/**/*.ts'],
    format: ['progress-bar', 'summary'],
    strict: false,
  },
};
