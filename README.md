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
frontend/    React + TypeScript installable PWA
  src/           application code and Vitest unit tests
  tests/
    features/    Gherkin .feature files
    steps/       Playwright + Cucumber step definitions
.github/workflows/ci.yml
```

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
