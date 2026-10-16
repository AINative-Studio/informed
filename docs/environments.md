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

### `scripts/backfill_panel_review.py` — run it after both seeds

The panel review workflow (story 3.3) gates unreviewed wording out of the
`/v1/panel/published/*` reads. Those reads fail **closed**, so on a project
whose ledger is empty they serve nothing at all. This script is what populates
the ledger for wording that was published before the workflow existed:

```sh
set -a; . local.env; set +a
python backend/scripts/backfill_panel_review.py
```

It writes one `panel_reviews` row per stored `issue_topics` and `stances` row —
281 against the current corpus — each with `decision = grandfathered`. Expect
roughly ten minutes: paced for the same 60 req/min ceiling, and each row is a
find-then-write. It is idempotent, so a second run reports every row as already
covered and writes nothing; a re-run after a real panel decision does not
downgrade it.

**`grandfathered` is not approval, and that is the point.** prd.md requires
stance cards and the taxonomy to be reviewed by a cross-partisan advisory panel
before each release, and lists the panel's own composition as an open question —
so nothing in the live corpus has ever been reviewed. Marking it `approved`
would put a false claim in the ledger whose whole value is that its claims are
true; leaving it unmarked would blank the taxonomy picker and every stance card.
`grandfathered` says what is true: **served, and still owed a review.**
`reviewer_id` on every row is the literal `informed:story-3.3-backfill` rather
than a person's name, and `GET /v1/panel/queue` reports all 281 rows as awaiting
a real review until a named panel member decides on one.

**Do not delete these rows.** They are what keeps the gated reads serving, and
they are the record that this corpus predates the review workflow. Deleting them
un-publishes the taxonomy for any caller on the gated paths.

`backend/scripts/verify_panel_review_live.py` checks the whole workflow against
the live project: that every live row carries a decision, that not one of them
claims a panel approval, that the gated reads still serve the corpus, and then —
on a **synthetic** card it creates and deletes — that unreviewed wording is
withheld, that approving a proposal bumps `wording_version` while keeping the
row's id, that a reviewer cannot approve their own proposal, and that the ledger
is append-only. Unlike the three seed verifiers, this one **does** clean up: the
grandfathering decisions over the real corpus stay, and the synthetic card plus
every ledger row about it is deleted in a `finally`.

**`/v1/panel/*` must not be routed from the public edge.** There is no
authentication anywhere in this codebase yet, so `reviewer_id` is a body field
that is recorded and not verified. A forged one on
`POST /v1/panel/proposals/{id}/decision` lets a caller publish editorial wording
to every voter. Until an auth story lands, keep this prefix off the public
router — see `backend/app/panel_review/router.py`'s module docstring.

### Public corrections: the two operational jobs (story 12.4)

`POST /v1/corrections` is public and unauthenticated by design — prd.md
requires a correction form on every claim, and one behind a login is one nobody
uses. It creates two standing operational obligations:

  1. triage within 72 hours — **still run by hand**, deliberately (see below);
  2. erase contact emails 90 days after resolution — **now scheduled**, as of
     Epic 16's scheduled-job runner (`backend/app/scheduling/`). See "The
     scheduled-job runner" below for what changed and what still needs a
     human to finish.

**1. Triage within 72 hours.** prd.md commits to it, every correction stores its
own `sla_due_at`, and the queue of missed commitments is one command:

```sh
set -a; . local.env; set +a
python backend/scripts/triage_correction.py --past-sla   # what the SLA has been missed on
python backend/scripts/triage_correction.py --queue      # everything still open
python backend/scripts/triage_correction.py --show cor:…  # one report, in full
python backend/scripts/triage_correction.py --id cor:… --status triaged
```

Triage is a **script, not an endpoint**, and deliberately so: there is no
authentication anywhere in this codebase, and an unauthenticated
`PATCH /v1/corrections/{id}` would let anyone who can reach the API mark an
inconvenient correction `rejected` — on the one feature whose entire purpose is
that mistakes cannot be buried. The mounted surface is the two public
operations only (submit, and the reporter's own status read). Exposing triage
over HTTP needs the auth story first.

The status vocabulary is `received` → `triaged` → `resolved` | `rejected`.
`triaged` is what the 72-hour promise asks for (prd.md says 72 hours to
*triage*, not to fix) and reaching it stops the clock. `resolved` and
`rejected` both close the report and both start the 90-day clock below.

**2. Erase contact emails 90 days after resolution.** datamodel.md's retention
table requires it, and this is the job that keeps the promise:

```sh
set -a; . local.env; set +a
python backend/scripts/sweep_correction_emails.py --dry-run   # count first
python backend/scripts/sweep_correction_emails.py             # then erase
```

It is idempotent, holds no cursor or watermark, and is a pure function of the
stored rows and the clock — so it is safe to run at any cadence. As of Epic
16, it IS scheduled: `app.scheduling.jobs.default_jobs` registers
`sweep_correction_emails` at a 1-day interval, and
`scripts/run_scheduled_jobs.py` — triggered daily by
`.github/workflows/scheduled-jobs.yml` — runs it when due. Daily is the
natural cadence: the window is 90 days, so a day's granularity is well inside
the promise. The script above still works by hand for a one-off dry run or an
out-of-band check; it is simply no longer the only way this happens.

**Triage stays manual, deliberately — not a gap.** Unlike the sweep, triage
needs a human judgment call per report (what it actually is, whether to act)
that a scheduled job cannot make, and `PATCH /v1/corrections/{id}` is
intentionally never exposed over HTTP without auth (see above) — so there is
no safe way to automate it further than `triage_correction.py --past-sla`
already does: surfacing the queue for a human to act on.

**The gap this closes, stated plainly: a scheduler now exists.** Before Epic
16, there was no scheduler, worker, cron or queue in this repository (the same
gap `backend/app/ingest/candidacies.py` documents for weekly candidacy
refreshes, and `backend/app/ingest/house_rollcalls.py` for story 5.6's future
hourly sync) — an address that should have gone on day 90 sat there until
someone ran the sweep by hand. See "The scheduled-job runner" below for the
mechanism, and its one remaining real gap: the GitHub Actions workflow needs
real `ZERODB_*` secrets configured in a `prod` GitHub Environment before it
actually fires on its own, because the three ZeroDB projects and GitHub
Environments this depends on are still **Pending** per this document's own
acceptance-criteria table above.

Neither script ever prints an email address. `--show` is the single deliberate
exception, per-id and opt-in, for a human reading one report on their own
screen; every listing and every sweep summary carries row ids and counts only,
because those are what get pasted into tickets.

`backend/scripts/verify_public_corrections_live.py` checks the whole story
against the live project — the submission round trip, the 72-hour arithmetic,
the token lookup (and that the row id does *not* open it), the status moves, the
SLA queue, the 90-day sweep against a backdated correction, and the full HTTP
surface with no credentials. Unlike the seed verifiers it **cleans up after
itself**: every row it writes is marked `SYNTHETIC TEST CORRECTION` and is
deleted in a `finally`, and the script fails if any marker row survives. Real
corrections are permanent; test ones would sit in the SLA queue forever.

### The scheduled-job runner (Epic 16)

Three stories independently flagged the same gap in almost the same words:
story 12.4's 90-day sweep, story 2.6's weekly candidacy refresh
(`backend/app/ingest/candidacies.py`), and story 5.1's roll-call ingest, ready
for story 5.6's hourly sync. Epic 16 closes it with one scheduler-agnostic
mechanism, not three bespoke ones:

```sh
set -a; . local.env; set +a
python backend/scripts/run_scheduled_jobs.py
```

What it does: `app.scheduling.jobs.default_jobs()` lists every registered job
with its own cadence (`sweep_correction_emails` daily, `load_candidacies`
weekly, `load_house_rollcalls` hourly — registered now, ahead of story 5.6,
at the cadence that story specifies). `app.scheduling.registry.ScheduledJobRunner`
checks each job's last-successful-run timestamp, stored in a new
`scheduled_jobs` ZeroDB table (`app.bootstrap` creates it like every other
infrastructure table), and runs only the ones that are due. One job's failure
is recorded and does not stop the others. A job blocked on a missing
credential (`load_candidacies` needs `FEC_API_KEY`, `load_house_rollcalls`
needs `CONGRESS_GOV_API_KEY` — neither exists in this environment) is
reported `BLOCKED`, distinct from a genuine `FAILED`, so a run log reads
correctly either way.

**This script is what any real scheduler calls — it is not itself a
scheduler.** `.github/workflows/scheduled-jobs.yml` wires a daily GitHub
Actions `schedule:` trigger to it, which is enough cadence to serve every
registered job (the runner, not the trigger, decides what is actually due).
That workflow targets a `prod` GitHub Environment carrying `ZERODB_WORKSPACE_ID`
/ `ZERODB_PROJECT_ID` / `ZERODB_API_KEY` (plus `FEC_API_KEY` /
`CONGRESS_GOV_API_KEY` once those exist), exactly the pattern this document
already specifies above — **and that Environment has not been configured**,
for the same reason the three ZeroDB projects above are still Pending: an
agent session cannot create a GitHub Environment or set its secrets through
the GitHub UI. Until a human with repo admin access does that, the workflow
file is correct and will run the moment the secrets exist, but today a
scheduled tick fails fast on a missing credential rather than silently doing
nothing or faking a green run. `workflow_dispatch` is enabled on it so a human
can trigger a manual run once the secrets are in place, without waiting for
the next scheduled tick.

### If ZeroDB calls 402 with `storage_limit_exceeded`

Measured during story 3.3: the live `informed` project reports **1.7558 GB**
stored against a **0.5 GB** plan limit, and the limit is enforced
**non-deterministically**. A row insert was refused with
`402 storage_limit_exceeded` partway through a run, and minutes later — with
nothing deleted and nothing changed — the identical request shape succeeded, as
did 281 more. Symptoms:

- `POST .../rows` answers `402` with `{"error": "storage_limit_exceeded",
  "current_storage_gb": 1.7558, "storage_limit_gb": 0.5, "action_required":
  "Upgrade your plan to increase storage limit or delete existing data"}`.
- Reads, in-place `PUT`s and `DELETE`s keep working throughout.
- Retrying the same insert later often just works.

What to do: **re-run the script.** Every bulk writer here is idempotent
(`upsert` is find-then-write, and the review ledger's ids are content-addressed),
so a run that died partway is repaired by running it again rather than by
cleaning up. `ZeroDBClient` deliberately does **not** retry a 402 — it is not a
throttle and retrying an over-quota write inside one request achieves nothing —
so the failure is loud rather than silent.

The 1.76 GB is almost certainly not row data: the whole project holds 832 rows
across 8 non-empty tables, which is kilobytes. It is more likely vectors,
indexes or per-project overhead, and
`GET /projects/{id}/database/stats` answers 404 so there is no way to see the
breakdown. Raised in datamodel.md's open questions for AINative.

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

## Rate limiting: `TRUST_PROXY_HEADERS`, and the single-process limit (story 14.5)

Story 14.5 puts every public endpoint behind a per-IP sliding-window limiter
(`backend/app/ratelimit/`). Two operational facts about it, neither of which is
a bug and both of which change what a deployment has to look like.

### `TRUST_PROXY_HEADERS` — off by default, and that default is deliberate

The limiter keys on the client address. Where it reads that address from is a
deployment question, not a code question:

| Deployment shape | `TRUST_PROXY_HEADERS` | Address used |
| --- | --- | --- |
| Directly exposed (uvicorn on a public port) | unset / `false` | The TCP peer |
| Behind exactly one proxy that **overwrites** `X-Forwarded-For` | `true` | Leftmost `X-Forwarded-For` entry |
| Behind a proxy that **appends** to a client-supplied header | unset / `false` | The TCP peer |

Setting it to `true` in the wrong place makes the limiter worse than useless:
any client can send `X-Forwarded-For`, so an app that honours it while
directly exposed hands every scraper an unlimited supply of identities while
still reporting that rate limiting is on. Set it only once the proxy in front
is known to replace the header, and check that it does.

Accepted values are `1`, `true`, `yes` (case-insensitive). Anything else,
including unset, is off.

### The limiter counts inside ONE process

**This is an in-process limiter.** There is no Redis, no Memcached and no
shared cache anywhere in this project (data lives in ZeroDB, over HTTP), so the
counters live in a dict in the serving process. Consequences, for whoever
provisions the hosting target above:

- **Run the backend as a single instance with a single worker** for the limits
  in the table to mean what they say. `uvicorn --workers 4` makes every
  allowance 4x, because each worker counts separately. Horizontal autoscaling
  multiplies it by the instance count.
- **Counters reset on restart**, so a deploy hands every client a fresh
  allowance. Immaterial for a scraping defence measured in hours; it is one
  reason this is abuse mitigation and not an authorization control.

This was the right trade for the deployment that exists (none yet; a single
instance when there is one) and it is not the right trade forever. The
arithmetic is confined to `RateLimitMiddleware.check`, so the migration is to
reimplement that one method against a shared store.

**Follow-up, tracked, not satisfied by story 14.5:**

- [ ] When the backend runs on more than one instance or worker, move the
      counters to a shared store (Redis, or a ZeroDB-backed counter) behind
      `RateLimitMiddleware.check`, and re-check the limits in
      `app/ratelimit/policy.py` against real traffic.
- [ ] **Per-account limits.** backlog.md 14.5 asks for them; they are deferred
      because there is no authentication anywhere in this codebase, and an
      account limit keyed on a caller-stated `user_id` is a limit anyone lifts
      by changing a string. `account_key` is the hook, returns `None`, and is
      called on every request; wiring it is a one-function change the day a
      real auth story lands.
- [ ] **An interactive bot challenge** (hCaptcha, Turnstile or similar). What
      ships today is a correct `429` with `Retry-After`. A real challenge
      needs a frontend flow, a provider dependency, a privacy review against
      prd.md's "no third-party trackers", and an accessible fallback — a
      story of its own, not a sub-task.

### Tuning the limits

They live in one table, `backend/app/ratelimit/policy.py`, each with the
reasoning for its number next to it. The tests pin the *invariants* — every
write stricter than a read, every route covered, a 429 that carries
`Retry-After` — rather than the specific integers, so changing a number is a
one-line diff and does not mean rewriting tests.

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
