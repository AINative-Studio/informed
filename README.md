# Informed

Interest-Aligned Voter Intelligence.

A voter states their priorities; Informed shows how closely each candidate matches them, with
cited evidence. Nonpartisan by construction, evidence over opinion, privacy first — see
[`prd.md`](prd.md).

## Documents

| File | What it holds |
| --- | --- |
| [`prd.md`](prd.md) | Product requirements, architecture, API, trust and privacy rules |
| [`datamodel.md`](datamodel.md) | ZeroDB data model: tables, ids, vectors, graph, events |
| [`backlog.md`](backlog.md) | 18 epics, 110 user stories with acceptance criteria |
| [`CLAUDE.md`](CLAUDE.md) | How we work: AI-native XP with TDD and BDD |

## Layout

```
backend/     FastAPI + Pydantic v2 API
  app/           application code
  tests/
    features/    Gherkin .feature files
    step_defs/   pytest-bdd step definitions
      common_steps.py   shared step library
frontend/    React + TypeScript installable PWA
  src/           application code and Vitest unit tests
  tests/
    features/    Gherkin .feature files
    steps/       Playwright + Cucumber step definitions
      common.steps.ts   shared step library
.github/workflows/ci.yml
```

## Adding a feature file

Both sides share a step library, so a new feature usually writes little or no
new step code.

**Backend.** Put the Gherkin in `backend/tests/features/<name>.feature` and add
`backend/tests/step_defs/test_<name>_steps.py` containing
`scenarios("../features/<name>.feature")`. The shared steps, fixtures and
synthetic data builders in
[`common_steps.py`](backend/tests/step_defs/common_steps.py) are registered as a
pytest plugin by `tests/conftest.py`, so they resolve with no import. Write only
the steps unique to your feature, and promote one to the shared library once a
second feature needs it.

**Frontend.** Put the Gherkin in `frontend/tests/features/<name>.feature` and add
`frontend/tests/steps/<name>.steps.ts` if it needs new steps. Cucumber loads
every file under `tests/steps/`, so the shared steps and the `world` (browser,
page and per-scenario state) in
[`common.steps.ts`](frontend/tests/steps/common.steps.ts) are already available —
drive the browser through `world.page` rather than launching your own.

**Pending steps.** The PRD's acceptance criteria are committed before the code
that satisfies them, so a step waiting on a later story declares itself pending:
call `pending(request, "<story id>", "<what>")` on the backend, or
`return PENDING` on the frontend. Those scenarios are collected and reported —
backend as `XFAIL`, frontend as `pending` — without failing CI, while a step that
is undefined or actually broken still turns the build red.

## Backend

Requires Python 3.12 or newer.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                 # unit tests and pytest-bdd features
pytest --cov=app       # with coverage
ruff check . && ruff format --check .
uvicorn app.main:app --reload   # http://127.0.0.1:8000/health
```

## Frontend

Requires Node 22 or newer.

```bash
cd frontend
npm install
npx playwright install --with-deps chromium   # once, for the BDD suite

npm test          # Vitest + React Testing Library
npm run test:bdd  # builds, serves, then runs Playwright + Cucumber
npm run lint      # tsc --noEmit
npm run dev       # http://127.0.0.1:5173
```

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs the backend and frontend suites as
parallel jobs on every push and pull request. The aggregate `CI` job fails if any suite fails,
is cancelled or is skipped — make that the required status check in branch protection so a red
build blocks merge.

## Contributing

Follow the loop in [`CLAUDE.md`](CLAUDE.md): one story at a time, a branch per story, Gherkin
first, then red, green, refactor. No production code without a failing test first.
