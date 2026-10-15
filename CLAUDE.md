# CLAUDE.md — Informed

Informed is an interest-aligned voter intelligence app: a voter states their priorities, and Informed shows how closely each candidate matches them, with cited evidence. It is built AI-native on AINative Studio.

## Read these first

These three files are the source of truth. Read the relevant parts before planning or writing code, and don't contradict them. If something in them is unclear or wrong, stop and ask instead of guessing.

| File | What it holds | Use it for |
| --- | --- | --- |
| [`prd.md`](prd.md) | Product requirements: principles, features, architecture, API, trust and privacy rules, BDD acceptance criteria, XP release plan | What to build and why; the definition of done |
| [`datamodel.md`](datamodel.md) | ZeroDB data model: NoSQL tables, id patterns, vectors, knowledge graph, files, events, privacy rules | Every table, field, id and query |
| [`backlog.md`](backlog.md) | 18 epics, 110 user stories with points, priority, iteration, acceptance criteria and ZeroDB data | Which story to work on next and when it's done |

## Stack

- **Backend:** Python, FastAPI, Pydantic v2, httpx. Tests: pytest + pytest-bdd.
- **Frontend:** React + TypeScript, TanStack Query, installable PWA. Tests: Vitest + React Testing Library (units), Playwright + Cucumber (BDD).
- **Data:** AINative ZeroDB, reached only through the ZeroDB MCP server. No direct SQL and no separate Postgres. Repositories never call MCP tools directly; they go through the typed ZeroDB client (story 1.2).
- **Agents:** AINative Agent Swarm for ingest, extraction and grounded Q&A.

## How we work: AI-native XP with TDD and BDD

Every story follows the same loop. Don't skip steps.

1. **One story at a time.** Take the next story from `backlog.md` for the current iteration, highest priority first. Don't start a second story until the first is merged.
2. **Branch per story.** Create a branch from an up-to-date `main` named after the story id, e.g. `story/1.1-repo-and-ci`. Never commit straight to `main`.
3. **Gherkin first.** Turn the story's acceptance criteria into a Gherkin `.feature` file (Given / When / Then) before any code. Reuse the scenarios in `prd.md` → "BDD acceptance criteria" where they apply. Backend features go under `backend/tests/features/`; frontend features under `frontend/tests/features/`.
4. **Red.** Write the step definitions and unit tests, run them, and confirm they fail for the right reason. Commit the failing tests.
5. **Green.** Write the smallest amount of code that makes the tests pass.
6. **Refactor.** Clean up with the tests still green.
7. **Merge only when green.** Open a pull request that names the story id and links its feature file. All test suites must pass in CI. A red build blocks the merge.

Rules that apply to every story:

- No production code without a failing test first.
- Keep coverage at 90% or above on scoring and verdict logic.
- Stories over 8 points get split before work starts. A 13-point story is never worked as-is.
- Keep commits small, and mention the story id in each message.

## Data rules (from `datamodel.md`)

- Ids are deterministic strings built from natural keys, e.g. `us-house:119-2:452`. Re-ingesting a record is an idempotent upsert.
- There are no joins or foreign keys. The repository layer validates every reference with Pydantic before a write, and contract tests cover each rule.
- Verdicts, positions and the provenance log are append-only. Mark the live row with `is_current`; never update history in place.
- Per-user alignment scores are computed per request and never stored.

## Non-negotiables (from `prd.md`)

- **Nonpartisan by construction.** One scoring codebase for every candidate and party. No party-specific rules.
- **Evidence over opinion.** Every claim links to a primary source: the promise URL and date, and the roll-call record.
- **Privacy first.** Voter profiles are political-opinion data. Encrypt them per user, keep them separate from every other table, and never log, share or use them for targeting. Never put real voter data in fixtures, logs or test output.
- **The app never recommends who to vote for.** The Q&A agent answers only from the evidence graph and always cites.

## Where we are

Iteration 1 is the walking skeleton: FastAPI + React + ZeroDB, address → district → member list. Start with story 1.1 (repo and CI pipeline) in `backlog.md`.
