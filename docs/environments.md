# Environments and secrets

Story: `1.6-environments-and-secrets` (Epic 1, 2 pts, Must, Iteration 1).

> As an operator, I want separate dev, staging and prod ZeroDB projects with
> per-environment API keys, so that test data never touches production.

This doc is the pattern every story and every deployment should follow. It
does not contain any secret value — real credentials live in the places
described below, never in this repo.

## The three environments

| Environment | ZeroDB project | Who writes to it | Purpose |
| --- | --- | --- | --- |
| `dev` | `informed-dev` | Developers, local runs, feature branches | Throwaway data, iteration, exploratory testing |
| `staging` | `informed-staging` | CI, PR previews, story 1.5's walking-skeleton deploy | Pre-prod validation with realistic (never real-voter) data |
| `prod` | `informed-prod` | The deployed production app only | Real users. No test, seed, or synthetic voter data ever lands here |

**The rule this story exists to enforce:** test and development data must
never touch the `informed-prod` ZeroDB project, and prod credentials must
never be available to a dev or CI job that isn't deploying to prod. Each
environment gets its own ZeroDB project id *and* its own API key — never a
shared key scoped broadly enough to touch more than one environment's data.

This also protects the privacy rule in `prd.md` (`CLAUDE.md` → Non-negotiables):
voter profiles are political-opinion data, encrypted per user and never put
in fixtures or test output. Separate projects mean a dev/staging mistake
(a bad fixture, a verbose log, a debugging query) structurally cannot leak
into or alongside real voter data in prod.

## Current state (as of this story)

Today the repo only has a single shared ZeroDB project (name `informed`),
whose credentials live in a local, gitignored `local.env` at the repo root.
That file is **not read or modified by this story** — its contents are
already excluded from git (`.gitignore` already has `.env`, `local.env`,
`*.local.env`; see "What's already in place" below).

**Provisioning status: not yet done, tracked as an open follow-up.**

The ZeroDB MCP tools available to this session (`zerodb_create_project`,
`zerodb_list_projects`, etc.) connect to a shared, org-wide AINative ZeroDB
account used by many unrelated apps/tenants (ZeroInvoice, ZeroPipeline,
OpenCapStack, and others were visible in a `zerodb_list_projects` call made
while writing this doc). There is no reliable way from this session to:

1. confirm which AINative workspace should own `informed-dev` /
   `informed-staging` / `informed-prod`, or
2. scope a newly created project into the same workspace as the existing
   `informed` project referenced by `local.env` (whose contents this story
   deliberately does not read).

Creating three new top-level projects under the wrong workspace would be
worse than not creating them — it would scatter the app's data across
projects nobody can find or administer. So this story documents the target
pattern and env-var contract (below and in `.env.example`) and leaves actual
provisioning as a follow-up for whoever holds AINative org/workspace admin
access for Informed. A quick attempt at `zerodb_admin_list_projects` from
this session also returned an error, confirming this session doesn't have
admin-level visibility into the correct org either.

**Follow-up (tracked, not blocking this story):**
- [ ] Provision `informed-dev`, `informed-staging`, `informed-prod` as three
      ZeroDB projects under the Informed AINative workspace.
- [ ] Mint one API key per project, each scoped to only that project.
- [ ] Load the three key sets into GitHub Actions environments (below) and
      update each developer's local `local.env`.
- [ ] Once done, record the three `ZERODB_PROJECT_ID` values (not the keys)
      in the table above, replacing the project names with ids if useful for
      debugging.

## Where real secrets live

No real ZeroDB workspace id, project id, or API key is ever committed to this
repo, in any form, for any environment. Real values live in exactly two
places:

### 1. Local development — gitignored env files

- `local.env` at the repo root (already gitignored) holds one developer's
  working credentials, today pointed at the single shared `informed`
  project. Once `informed-dev` exists, point `local.env` at it instead of a
  shared project.
- `.env.example` (committed, this story) documents every variable name and a
  placeholder value — never a real one. Bootstrap a new machine with:

  ```bash
  cp .env.example local.env
  # then edit local.env and fill in the real dev values, requested from
  # whoever holds them (see "requesting access" below)
  ```
- Any additional per-environment override file a developer creates locally
  (`.env.staging`, `.env.prod`, `.env.dev`) is gitignored too — see the
  `.gitignore` update in this story. These are for a developer who needs to
  point their local client at staging/prod read-only for debugging; they are
  never used to *run* staging/prod workloads from a laptop.

### 2. CI/CD — GitHub Actions encrypted secrets, scoped per environment

This repo is hosted on GitHub, so the recommended secret manager for CI is
**GitHub Actions' own encrypted secrets, scoped with GitHub Environments**
(Settings → Environments in the repo), one GitHub Environment per
deployment target:

- `dev` environment → secrets `ZERODB_WORKSPACE_ID`, `ZERODB_PROJECT_ID`,
  `ZERODB_API_KEY` for `informed-dev`.
- `staging` environment → the same three secret names, values scoped to
  `informed-staging`. Required for any workflow that deploys the walking
  skeleton (story 1.5) to staging.
- `prod` environment → the same three secret names, values scoped to
  `informed-prod`. Configure this GitHub Environment with required reviewers
  / protection rules once prod deploys exist, so no workflow can push to prod
  without an approval gate.

A workflow job declares `environment: staging` (or `dev` / `prod`) and reads
`${{ secrets.ZERODB_API_KEY }}` etc. — GitHub injects the right value for
that job's environment, and a job targeting `dev` has no access to the
`prod` secrets at all. This is what gives us "per-environment API keys" from
the acceptance criteria without inventing new infrastructure.

If/when AINative provides an org-level secret manager or KMS (`prd.md`'s
privacy section calls for voter profiles to be "encrypted per user"; the
mechanism for that — AINative's own KMS vs. a cloud KMS such as AWS
Secrets Manager/GCP Secret Manager — is still an open architectural
question, separate from this story) GitHub Actions secrets can pull from it
via an OIDC-federated fetch step instead of storing the value directly in
GitHub. That migration does not change the env var contract in
`.env.example`, only where the value is fetched from before the job runs.

### Never

- Never put a real `ZERODB_API_KEY`, workspace id, or project id in: a
  committed file, a commit message, a GitHub Actions workflow YAML literal,
  a Dockerfile `ENV` instruction, application logs, test fixtures, or a PR
  description.
- Never share a single API key across more than one environment.
- Never let a `dev`-scoped key have permissions on `informed-staging` or
  `informed-prod` — request environment-scoped keys at creation time (see
  the follow-up checklist above).

## Bootstrapping each environment locally

1. Clone the repo.
2. `cp .env.example local.env`.
3. Ask an existing operator (or check your own AINative workspace access)
   for the **dev** workspace id, project id, and API key. Only ask for dev
   credentials unless your task specifically requires staging/prod access.
4. Fill those three `ZERODB_*` values into `local.env`, leave `APP_ENV=dev`.
5. Run the backend/frontend per their own setup docs (story 1.1) — they load
   `local.env` automatically; never export real secrets into shell history
   or into `~/.zshrc`/`~/.bashrc`.
6. Before committing anything, run a quick local secret scan (see below) —
   CI should also run one, but don't rely on that alone.

### If ZeroDB calls 403 with `API_KEY_PROJECT_MISMATCH`

A key's **name** on AINative's platform and the **project it is actually
bound to** are independent — nothing stops a key named `informed-developer`
from having been minted against a totally unrelated project (this happened
during story 1.3/1.6 cleanup: a key named `informed-developer` was bound to
an unrelated project called "Ocean Production", not to the `informed`
project at all). Symptoms:

- `GET /api/v1/projects/{informed_id}` can still return `200` with the
  correct project details — that lookup does not appear to enforce
  key-to-project scoping.
- Every data-plane call (`.../database/tables`, `.../rows`, etc.) 403s with
  `{"error_code": "API_KEY_PROJECT_MISMATCH"}`, regardless of retries.
- The key still works fine against *other* projects, so it isn't revoked
  or expired — it's just scoped to the wrong one.

**Fix:** mint a new key explicitly scoped to the right project id, rather
than trusting a key's name:

```bash
curl -X POST "https://api.ainative.studio/api/v1/api-keys" \
  -H "Authorization: Bearer $EXISTING_WORKING_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"informed-app-key","project_id":"<the real project id>"}'
```

The response's `project_id`/`project_name` fields confirm the actual
binding — check those, not the key's own `name`, before trusting it.
Verify with a data-plane call (not just `GET /projects/{id}`) before
relying on it:

```bash
curl -H "Authorization: Bearer $NEW_KEY" \
  "https://api.ainative.studio/api/v1/projects/<project_id>/database/tables"
```

A `200` with an empty or real table list confirms correct scoping.

### Quick local secret scan

Run this before pushing if you've touched anything env-related:

```bash
git grep -nE "sk_[A-Za-z0-9]{16,}|zdb_[A-Za-z0-9]{16,}" -- . ':!*.lock'
```

No output means nothing that looks like a live key is tracked. This was run
as part of closing out story 1.6; see the PR for the result.

## Summary of acceptance criteria status

| Acceptance criterion | Status |
| --- | --- |
| Three ZeroDB projects exist | **Pending** — documented above as a follow-up; this session's ZeroDB access is a shared org-wide account without a clear path to scope new projects into Informed's workspace. Pattern, naming (`informed-dev`/`informed-staging`/`informed-prod`), and env-var contract are fully specified so provisioning is a mechanical step once workspace access is confirmed. |
| Keys stored in the secret manager | **Documented** — GitHub Actions encrypted secrets, one GitHub Environment per deployment target (`dev`/`staging`/`prod`), plus gitignored local env files for local dev. Actual key minting depends on the projects above existing. |
| No keys appear in the repo | **Done** — `.env.example` has placeholders only; `local.env`/`.env`/`*.local.env`/per-env override files are gitignored; a repo-wide scan for live-looking key patterns found nothing in tracked or staged files before this PR was opened. |

## Deployment targets: none provisioned yet (noted by story 1.5)

Story 1.5's second acceptance criterion is "deployed to staging", and it could
**not** be met, because there is no staging environment to deploy to. Checked
while closing out story 1.5, in this repo and its CI config:

- `.github/workflows/` contains `ci.yml` and nothing else — no deploy job, no
  `environment:` declaration, no release workflow.
- There is no `Dockerfile`, `docker-compose.yml`, `fly.toml`, `railway.json`,
  `vercel.json`, Procfile, Helm chart or Terraform anywhere in the tree. The
  backend has no container or process definition; the frontend has a Vite build
  and a local `vite preview`, used only by the BDD runner.
- The three ZeroDB projects a staging deploy would need are themselves still
  **Pending** (the row above), so even a hand-rolled deploy would have no
  staging data layer to point at.

Story 1.5 deliberately did **not** invent one. Standing up a hosting target,
choosing a platform, and wiring deploy credentials is infrastructure work with
its own decisions (where it runs, who pays, what the domain is, how secrets
reach it) and its own security review. Bolting a guessed-at pipeline onto a
walking-skeleton story would commit the project to all of that as a side
effect, and a pipeline that deploys to nothing is worse than none: it reports
green.

**Follow-up, tracked, not satisfied by story 1.5:**

- [ ] Choose and provision a hosting target for the FastAPI backend and the
      React build (per environment).
- [ ] Add a deploy workflow with a GitHub Environment per target, reading the
      per-environment ZeroDB secrets named above.
- [ ] Provision the three ZeroDB projects (the row above) so staging has its
      own isolated data layer.
- [ ] Then re-check story 1.5's "deployed to staging" criterion against a real
      staging URL, and record it here.

Until those exist, "deployed to staging" cannot be claimed for any story, and
no story should claim it. What story 1.5 *did* prove, against the live
"informed" ZeroDB project rather than a fake, is the full React → FastAPI →
ZeroDB path: see `backend/scripts/verify_ballot_live.py` and story 1.5's PR for
the run output.
