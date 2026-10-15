/**
 * Cucumber config for the frontend BDD suite (story 1.1).
 * Features live in tests/features; steps drive a real browser via Playwright.
 * TypeScript steps are loaded by tsx, registered through NODE_OPTIONS in the
 * `test:bdd` script (Node >= 20 rejects the deprecated --loader flag).
 */
module.exports = {
  default: {
    paths: ['tests/features/**/*.feature'],
    import: ['tests/steps/**/*.ts'],
    format: ['progress-bar', 'summary'],
    strict: true,
  },
};
