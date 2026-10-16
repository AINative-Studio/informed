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

### 3. Voter-profile encryption: `ZERODB_PROFILE_ROOT_KEY` (story 14.1)

Voter interests are special-category political-opinion data under GDPR
(`prd.md` → Privacy). They are never stored in clear text: story 14.1's
`backend/app/profiles/` encrypts each profile's payload with AES-256-GCM
under a **per-user data key**, and wraps that data key under a key derived
from `$ZERODB_PROFILE_ROOT_KEY`. Row ids are `vp:{hmac(user_id)}`, keyed from
the same root, so a database dump cannot be joined back to accounts.

**This variable is a documented stand-in for a real KMS, and the open
question above is still open.** Story 14.1's acceptance criterion reads
"AES-256-GCM per-user data keys **via KMS**", and `datamodel.md`'s open
questions still list "Key management for `voter_profiles`: AINative KMS, or a
cloud KMS?". Neither existed when 14.1 was built, and provisioning a cloud
KMS key is infrastructure work rather than application code — the same
boundary this story drew around ZeroDB project provisioning. So 14.1 shipped
a correct envelope scheme whose root key is an ordinary environment secret:

| Property | Today (env var) | With a real KMS |
| --- | --- | --- |
| Per-user AES-256-GCM data keys | yes | yes |
| Data key random, not derived (so delete can shred it) | yes | yes |
| HMAC row ids | yes | yes |
| Root key never in application memory | **no** | yes |
| Hardware-backed custody, audited key use | **no** | yes |
| Automatic rotation | **no** | yes |

The acknowledged consequence: an attacker who can read this process's
environment **and** the `voter_profile_keys` table can decrypt profiles.
That is a weaker boundary than "via KMS" implies, and it is the one part of
story 14.1 that infrastructure rather than code has to finish.

**Migrating to a real KMS** does not change the stored row shape, the API, or
any caller. Every row records its own `key_ref` (`local-envelope:…` today,
`kms:informed/users/v1` after) precisely so a table mid-migration stays
readable row by row. What changes is the inside of `ProfileRootKey` and
`ProfileKeyring.wrap_key_for` — see `backend/app/profiles/crypto.py`'s module
docstring for the exact list.

Operational rules for this value:

- **One root key per environment**, never shared. A dev key must not be able
  to decrypt a prod profile, for the same reason API keys are not shared.
- **Generate it, never choose it**:
  ```bash
  python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
  ```
  It must decode to exactly 32 bytes; base64 and hex are both accepted.
- **There is no default.** The profile store raises `RootKeyMissingError` and
  refuses to serve rather than falling back, because a shared or constant key
  is not encryption.
- **Rotating it makes every stored profile unreadable** — the wraps were
  sealed under the old root key. A rotation needs a re-wrap migration (read
  each wrap with the old key, re-wrap with the new one) which no story has
  built yet. Treat it as append-once until a real KMS, whose own rotation
  handles this, replaces it.
- It is a GitHub Actions secret per GitHub Environment like the ZeroDB
  values, and belongs in the same "Never" list below.

### Never

- Never put a real `ZERODB_API_KEY`, `ZERODB_PROFILE_ROOT_KEY`, workspace id,
  or project id in: a committed file, a commit message, a GitHub Actions
  workflow YAML literal, a Dockerfile `ENV` instruction, application logs,
  test fixtures, or a PR description.
- Never log a decrypted voter profile, an interest, a stance, a voter's
  free-text note, or any profile key material — at any log level, including
  debug, and including inside an exception message that a 4xx response would
  carry. Story 14.1 asserts this with tests that capture logs across a full
  save/read/delete cycle; a new `logger` call in `backend/app/profiles/` that
  touches plaintext fails CI.
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

### `scripts/bootstrap.py` also seeds permanent reference data

The bootstrap creates the schema **and** writes three fixed rows (story 2.1):
`jurisdictions/us-federal`, `bodies/us-house`, `bodies/us-senate`. They are the
root of the reference tree — story 2.2's legislator terms, 2.3's districts and
2.4's ballot lookup all reference them by id — and the seed is idempotent, so
re-running bootstrap restates them rather than duplicating them.

**Do not delete those three rows.** Unlike the `zzz-live-check-*` rows the
`verify_*_live.py` scripts write and then clean up, these are production
reference data in every environment, dev included. A `us-federal` row in the
live project is the seed's output, not leftover test debris. If one is deleted,
re-run `python backend/scripts/bootstrap.py` to put it back.

Row-seeding lives in `backend/app/seed.py`, separate from the schema work in
`backend/app/bootstrap.py`; `backend/scripts/verify_seed_live.py` checks the
three rows against the live project (and is itself the one verify script that
deliberately leaves what it wrote in place).

### `scripts/seed_taxonomy.py` — a second, separate permanent seed

The issue taxonomy (story 3.1) is **not** written by the bootstrap. Run it once
per environment, after the bootstrap has created the tables:

```sh
set -a; . local.env; set +a
python backend/scripts/seed_taxonomy.py
```

It writes 80 `issue_topics` rows — 20 top-level topics and 60 subtopics, all
`locale = en-US` and `taxonomy_version = 1` — idempotently, so re-running it
restates them rather than duplicating them. Expect it to take a couple of
minutes: the writes are paced for the live project's 60 req/min ceiling, which
is also why it is a separate command rather than a step inside every bootstrap.

**Do not delete those rows either.** They are the vocabulary the whole product
is measured against: every voter interest, stance card (story 3.2), topic
embedding (story 3.4) and alignment score cites a topic id such as
`en-US:healthcare:v1`. If one is deleted, re-run the script to put it back.

The taxonomy itself lives in `backend/app/seed_taxonomy.py` (one module, so a
wording change reviewed by the advisory panel never touches the reference-tree
seed), and `backend/scripts/verify_issue_taxonomy_live.py` checks the stored
tree against the live project — counts, parent links, locale and version, the
PUT-not-POST idempotency mechanism, and that no stored label presupposes a
political stance. Like `verify_seed_live.py`, it leaves what it wrote in place.

### `scripts/seed_stances.py` — a third permanent seed, after the taxonomy

The balanced stance cards (story 3.2) are not written by the bootstrap either.
Run them **after** `scripts/seed_taxonomy.py`, which is a hard prerequisite:

```sh
set -a; . local.env; set +a
python backend/scripts/seed_taxonomy.py   # if it has not been run here
python backend/scripts/seed_stances.py
```

It writes 201 `stances` rows — three or four cards for each of the taxonomy's
60 subtopics, all at `wording_version = 1` — idempotently. Expect several
minutes: the writes are paced for the same 60 req/min ceiling, which is why
this is a third separate command rather than another bootstrap step.

`Stance.topic_id` is a declared reference, so each card's write resolves its
subtopic out of `issue_topics` before storing it. Against a project with no
taxonomy the first card is refused and the script exits **2** with a message
naming the missing seed — deliberately, because a stance pointing at a topic
that does not exist is unusable and 201 orphans is worse than stopping at one.

**Do not delete these rows.** They are the menu of positions a voter can hold:
a voter's encrypted profile stores `stance_id`s from this corpus (see
datamodel.md's voter-profile document), as do `candidate_positions` and every
promise classified under a stance, so deleting one orphans a saved profile and
every score computed from it. If one is deleted, re-run the script.

The corpus lives in `backend/app/seed_stances.py` — one module, like the
taxonomy, so an advisory-panel wording change touches nothing else — and
`backend/scripts/verify_stance_cards_live.py` checks the stored cards against
the live project: at least three per subtopic, direction codes spanning both
sides of each subtopic's axis with no repeat, `wording_version` 1 throughout,
no `default`/`preselected`/`display_order` key in the raw stored rows, the
PUT-not-POST idempotency mechanism, that no stored label or description names a
political side, and that a live read through `StanceService` comes back in a
varying order. Like the two seeds above, it leaves what it wrote in place.

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
