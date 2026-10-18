# Informed

Interest-Aligned Voter Intelligence.

A voter states their priorities; Informed shows how closely each candidate matches them, with
cited evidence. Nonpartisan by construction, evidence over opinion, privacy first — see
[`prd.md`](prd.md).

## Status

Iteration 1's walking skeleton is done, and work is well into Epics 2–4 and 9/12–16 — see the
[issue tracker](../../issues) for the full backlog as GitHub issues (one per user story, labeled
by epic and priority) and what's open vs. closed. Most of what's left is blocked on a few things
any contributor can help unblock:

- A **Congress.gov API key** (free, via [api.data.gov](https://api.data.gov/signup/)) — unblocks
  House/Senate roll-call ingest and everything downstream of real vote data (Epics 5, 6, 8, 10, 11).
- An **OpenFEC API key** (free, same signup) — unblocks the full weekly candidacy refresh (story 2.6).
- Real **authentication** — every profile-touching endpoint currently takes a bare `user_id` with
  no verification. This is the most important open gap; see `docs/environments.md` for where it's
  flagged.

## Documents

| File | What it holds |
| --- | --- |
| [`prd.md`](prd.md) | Product requirements, architecture, API, trust and privacy rules |
| [`datamodel.md`](datamodel.md) | ZeroDB data model: tables, ids, vectors, graph, events |
| [`backlog.md`](backlog.md) | 18 epics, 110 user stories with acceptance criteria |
| [`CLAUDE.md`](CLAUDE.md) | How we work: AI-native XP with TDD and BDD |
| [`docs/environments.md`](docs/environments.md) | Dev/staging/prod setup, secrets, and every known operational gap |
| [`docs/observability.md`](docs/observability.md) | Metrics, logging, and what dashboards to build once real infra exists |
| [`docs/methodology-changelog.md`](docs/methodology-changelog.md) | Plain-language history of the scoring methodology's weights |
| [`docs/spikes/`](docs/spikes) | Written answers from timeboxed research spikes (data sources, ZeroDB limits) |

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

**Requires Python 3.12** — not newer. CI pins 3.12, and the test suite's `pytest-bdd` plugin
fails to import under 3.14 (`ImportError: No module named 'tests.step_defs'`). If your system
`python3` is newer, install 3.12 alongside it (`brew install python@3.12` on macOS) and point the
venv at it explicitly.

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                 # unit tests and pytest-bdd features — no credentials needed
pytest --cov=app       # with coverage
ruff check . && ruff format --check .
uvicorn app.main:app --reload   # http://127.0.0.1:8000/health
```

The committed test suite runs entirely against an in-memory ZeroDB fake — no live credentials
are needed to develop or run `pytest`. To exercise a module's `scripts/verify_*_live.py` script
against the real ZeroDB project (optional, only needed if you're touching a story that writes
live data), copy `.env.example` to `local.env`, fill in real `ZERODB_*` values from whoever holds
the project's credentials, and `source` it before running the script. See
[`docs/environments.md`](docs/environments.md) for the full setup and secret-handling story.

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

Pick an open [issue](../../issues) — each is one backlog story with its own acceptance criteria,
epic label, and MoSCoW priority label. Then follow the loop in [`CLAUDE.md`](CLAUDE.md): one
story at a time, a branch per story (`story/<id>-<short-name>`), Gherkin first, then red, green,
refactor. No production code without a failing test first. Open a PR naming the story id; CI
must be green before merge.

A story's PR should say plainly which parts of its acceptance criteria are genuinely verified
(ideally against the real ZeroDB project, via a `scripts/verify_*_live.py` script) versus
honestly blocked on something outside the PR's control (a missing API key, an upstream story
that hasn't landed yet). Don't fabricate data or a passing result to make a criterion look met —
every prior story in this backlog has held that line, including by writing zero rows when the
honest alternative was writing wrong ones.

## License

MIT — see [`LICENSE`](LICENSE).
