# Informed — Product Backlog

As of Oct 15, 2026 · Companion to the Informed PRD and `informed-data-model.md`

The backlog has **18 epics and 110 user stories, totalling 496 story points**. Of those, 412 points make up the MVP (iterations 1–8) and 331 points are Must-have. Points use the Fibonacci scale (1, 2, 3, 5, 8, 13), sized relative to a 3-point reference story: the ballot lookup endpoint (2.4).

## How to read this backlog

- **Points** measure relative effort, complexity and uncertainty, not hours. Any 13-point story must be split before sprint planning, as XP keeps stories small.
- **Priority** uses MoSCoW: Must for the MVP, Should if capacity allows, Could for later.
- **Iteration** maps to the PRD's XP release plan. 9 means Phase 2–3 and later.
- **ZeroDB data** names the tables, namespaces or primitives from the data model that the story touches.
- Every story is written test-first: its acceptance criteria become a Gherkin scenario before any code.

## Summary by epic

| Epic | Name | Stories | Points | Iterations |
| --- | --- | ---: | ---: | --- |
| E1 | Platform foundation | 7 | 26 | 1 |
| E2 | Jurisdictions, districts and candidates | 7 | 27 | 1 |
| E3 | Issue taxonomy and stance cards | 5 | 20 | 2 |
| E4 | Interest Profile | 8 | 32 | 2 |
| E5 | US House ingest | 8 | 34 | 3 |
| E6 | Alignment scoring | 8 | 33 | 4 |
| E7 | Ballot and compare UI | 8 | 35 | 4 |
| E8 | US Senate ingest and vote typing | 5 | 18 | 5 |
| E9 | Promise collection and extraction | 6 | 24 | 6 |
| E10 | Promise-to-vote mapping and human QA | 5 | 27 | 6-7 |
| E11 | Promise vs. Record verdicts | 6 | 28 | 7 |
| E12 | Trust and neutrality | 5 | 24 | 7-8 |
| E13 | Evidence graph and grounded Q&A agent | 6 | 23 | 8 |
| E14 | Privacy and security | 7 | 30 | 2, 8 |
| E15 | Follow and alerts | 4 | 13 | 8 |
| E16 | Data integrity and observability | 5 | 18 | 1-8 |
| E17 | Global expansion adapters | 6 | 55 | 9+ |
| E18 | Model distillation (Focustem) | 4 | 29 | 9+ |
| | **Total** | **110** | **496** | |

## Points by iteration

| Iteration | Stories | Points |
| --- | ---: | ---: |
| 1 | 13 | 44 |
| 2 | 10 | 42 |
| 3 | 10 | 45 |
| 4 | 15 | 59 |
| 5 | 11 | 41 |
| 6 | 7 | 34 |
| 7 | 13 | 63 |
| 8 | 21 | 84 |
| 9+ (Phase 2–3) | 10 | 84 |

The MVP iterations average 52 points each. Treat this as a draft plan: measure real velocity in iterations 1–2, then let the planning game move Should and Could stories to fit.

**Point distribution:** 1 pts × 2, 2 pts × 15, 3 pts × 35, 5 pts × 40, 8 pts × 15, 13 pts × 3

## Epics and user stories

### E1 — Platform foundation (26 pts)

Walking skeleton: monorepo, CI, ZeroDB MCP client, table bootstrap, repository layer. Iterations: 1.

#### 1.1 Repo and CI pipeline · **3 pts** · Must · Iteration 1

As a developer, I want a monorepo with the FastAPI and React apps and a CI pipeline that runs every test suite on each push, so that no untested change reaches main.

- **Acceptance:** Pytest, pytest-bdd, Vitest and Playwright run on push; a red build blocks merge; pipeline under 10 minutes.
- **ZeroDB data:** —

#### 1.2 Typed ZeroDB MCP client · **5 pts** · Must · Iteration 1

As a backend developer, I want a typed Python wrapper around the ZeroDB MCP server, so that repositories never call MCP tools directly.

- **Acceptance:** Covers tables, vectors, graph, files and events; retries with backoff; MCP errors mapped to domain exceptions; contract tests run against an in-memory fake.
- **ZeroDB data:** All primitives

#### 1.3 Idempotent ZeroDB bootstrap · **5 pts** · Must · Iteration 1

As a developer, I want one command that creates all NoSQL tables, vector namespaces, HNSW indexes and the graph ontology, so that any environment can be built from code.

- **Acceptance:** Running it twice changes nothing; the schema version is recorded; it creates 22 tables, 4 namespaces and 1 ontology.
- **ZeroDB data:** All 22 tables; 4 namespaces; ontology

#### 1.4 Repository layer with natural-key ids · **5 pts** · Must · Iteration 1

As a backend developer, I want a base repository that upserts by deterministic id and checks references before writing, so that NoSQL data stays consistent without foreign keys.

- **Acceptance:** Given a row referencing a missing id, the write is rejected; re-ingesting the same record produces no duplicate.
- **ZeroDB data:** All tables

#### 1.5 Walking skeleton end to end · **3 pts** · Must · Iteration 1

As a voter, I want to enter my address and see my federal representatives, so that the team proves the React → FastAPI → ZeroDB path works.

- **Acceptance:** Address input returns 1 House member and 2 senators from ZeroDB; deployed to staging.
- **ZeroDB data:** people, districts

#### 1.6 Environments and secrets · **2 pts** · Must · Iteration 1

As an operator, I want separate dev, staging and prod ZeroDB projects with per-environment API keys, so that test data never touches production.

- **Acceptance:** Three ZeroDB projects exist; keys are stored in the secret manager; no keys appear in the repo.
- **ZeroDB data:** Projects

#### 1.7 Shared BDD step library · **3 pts** · Must · Iteration 1

As a developer, I want a shared Gherkin step library for API and UI features, so that acceptance tests are written before code with little friction.

- **Acceptance:** PRD Gherkin features run as pending; API and UI steps share fixtures.
- **ZeroDB data:** —

### E2 — Jurisdictions, districts and candidates (27 pts)

Resolve an address to districts, officeholders and ballot candidates. Iterations: 1.

#### 2.1 Seed US federal jurisdiction · **1 pts** · Must · Iteration 1

As the system, I want the US federal jurisdiction and its two chambers seeded, so that ingest has a home.

- **Acceptance:** Rows `us-federal`, `us-house` and `us-senate` exist after bootstrap.
- **ZeroDB data:** jurisdictions, bodies

#### 2.2 Load legislators · **5 pts** · Must · Iteration 1

As the system, I want every current and recent member of Congress loaded from congress-legislators with their terms, so that votes resolve to people.

- **Acceptance:** All 535 sitting members are present with bioguide ids; `terms[]` covers the 118th and 119th Congress; the load is idempotent.
- **ZeroDB data:** people

#### 2.3 Address to district · **5 pts** · Must · Iteration 1

As a voter, I want my address resolved to my congressional district, so that I see the right races.

- **Acceptance:** The Census Geocoder returns an OCD-ID; an invalid address gives a helpful error; the address is never stored.
- **ZeroDB data:** districts

#### 2.4 Ballot lookup endpoint · **3 pts** · Must · Iteration 1

As a voter, I want `GET /v1/ballot?address=` to return my districts, races and candidates, so that the app knows what is on my ballot.

- **Acceptance:** Response lists races with incumbents flagged; p95 latency under 800 ms.
- **ZeroDB data:** districts, elections, candidacies

#### 2.5 Spike: challenger candidate source · **2 pts** · Must · Iteration 1

As the team, we want to evaluate FEC candidate data against state election offices for challenger lists, so that we pick a reliable source.

- **Acceptance:** A written recommendation with coverage and freshness for 3 sample states.
- **ZeroDB data:** —

#### 2.6 Load elections and candidacies · **8 pts** · Must · Iteration 2

As a voter, I want challengers listed alongside incumbents, so that I can compare everyone on my ballot.

- **Acceptance:** Upcoming federal general-election candidacies are loaded from the chosen source and refreshed weekly.
- **ZeroDB data:** elections, candidacies, people

#### 2.7 Redistricting support · **3 pts** · Should · Iteration 3

As the system, I want districts to carry `valid_from` and `valid_to`, so that votes and ballots map to the right map after redistricting.

- **Acceptance:** A lookup with an election date returns the district valid on that date.
- **ZeroDB data:** districts

### E3 — Issue taxonomy and stance cards (20 pts)

Neutral, panel-reviewed topics and stances that every score is measured against. Iterations: 2.

#### 3.1 Issue taxonomy v1 · **3 pts** · Must · Iteration 2

As a voter, I want a clear list of issue topics and subtopics, so that I can find what I care about.

- **Acceptance:** About 20 topics and 60 subtopics in en-US; parent and child tree; `taxonomy_version` = 1.
- **ZeroDB data:** issue_topics

#### 3.2 Balanced stance cards · **5 pts** · Must · Iteration 2

As a voter, I want stance options written neutrally from across the spectrum in random order, so that the app does not steer me.

- **Acceptance:** Each subtopic has at least 3 stances; display order is randomized per session; no stance is preselected.
- **ZeroDB data:** stances

#### 3.3 Panel review workflow · **5 pts** · Must · Iteration 3

As an advisory panel member, I want to review and approve stance wording before release, so that the wording stays neutral.

- **Acceptance:** Unapproved wording cannot be published; every edit bumps `wording_version`; the review history is visible.
- **ZeroDB data:** stances

#### 3.4 Embed topics and stances · **2 pts** · Must · Iteration 2

As the system, I want topics and stances embedded, so that free text and promises can be matched to them.

- **Acceptance:** Both namespaces are populated; embeddings are rebuilt when a version changes.
- **ZeroDB data:** vectors: topics, stances

#### 3.5 Free-text interest mapping · **5 pts** · Should · Iteration 3

As a voter, I want to describe a concern in my own words and get topic suggestions, so that I can still use the app if I don't know the taxonomy.

- **Acceptance:** The top 3 suggestions appear; nothing is saved until the voter confirms; the text itself is not stored.
- **ZeroDB data:** vectors: topics

### E4 — Interest Profile (32 pts)

Voter states primary, secondary and nice-to-have interests. Iterations: 2.

#### 4.1 Quick onboarding · **5 pts** · Must · Iteration 2

As a busy voter, I want to pick 3 primary topics and my stance on each in under 2 minutes, so that I get value fast.

- **Acceptance:** Median completion under 2 minutes in usability tests; no score is shown until at least 1 primary interest exists.
- **ZeroDB data:** voter_profiles

#### 4.2 Three tiers with limits · **3 pts** · Must · Iteration 2

As a voter, I want to sort interests into primary, secondary and nice-to-have, so that scores reflect what matters most to me.

- **Acceptance:** Allows 1–5 primary and up to 8 secondary; weights load from `methodology_versions`; violations show inline errors.
- **ZeroDB data:** voter_profiles, methodology_versions

#### 4.3 Dealbreaker flag · **2 pts** · Must · Iteration 2

As a voter, I want to mark a primary interest as a dealbreaker, so that a candidate who acts against it is flagged regardless of their average.

- **Acceptance:** The flag is allowed only on primary interests; it is shown in the profile summary.
- **ZeroDB data:** voter_profiles

#### 4.4 Save and load profile · **3 pts** · Must · Iteration 2

As a signed-in voter, I want my profile saved, so that I can come back to it.

- **Acceptance:** Uses the encrypted store (14.1); a round trip returns an identical profile.
- **ZeroDB data:** voter_profiles

#### 4.5 Drag-and-drop re-tiering · **5 pts** · Should · Iteration 4

As a voter, I want to drag interests between tiers and see scores update immediately, so that I can explore trade-offs.

- **Acceptance:** Rescoring completes in under 300 ms; works by keyboard and touch.
- **ZeroDB data:** voter_profiles

#### 4.6 Profile versioning · **3 pts** · Could · Iteration 5

As a voter, I want to see how my scores change if I change my priorities, so that I understand what drives them.

- **Acceptance:** Each save bumps `version`; I can compare with the previous version if history is opted in.
- **ZeroDB data:** voter_profiles

#### 4.7 Advanced taxonomy browser · **3 pts** · Should · Iteration 4

As an engaged citizen, I want to browse the full taxonomy, so that I can set detailed interests.

- **Acceptance:** Search and filter work; all topics are reachable within 3 clicks.
- **ZeroDB data:** issue_topics, stances

#### 4.8 Guided agent interview · **8 pts** · Could · Iteration 8

As a voter unsure of my priorities, I want an agent to interview me and propose a profile, so that I can set up without reading a taxonomy.

- **Acceptance:** The agent asks questions only and never suggests what I should care about; I confirm every interest it proposes.
- **ZeroDB data:** voter_profiles, memory

### E5 — US House ingest (34 pts)

House roll calls, member votes and bills from Congress.gov, with provenance. Iterations: 3.

#### 5.1 House roll-call ingest · **5 pts** · Must · Iteration 3

As the system, I want House roll calls ingested from the Congress.gov house-vote API, so that votes are available to score.

- **Acceptance:** Every roll call since the 118th Congress is ingested with `source_url`, `content_hash` and a provenance entry; ids follow `us-house:{congress}-{session}:{roll}`.
- **ZeroDB data:** roll_calls, provenance_log

#### 5.2 Member positions · **5 pts** · Must · Iteration 3

As the system, I want each member's position stored with denormalized roll-call fields, so that scoring reads one table.

- **Acceptance:** `member_votes` rows carry voted_at, vote_type, bill_id, topic_ids and is_bundled; the drift check passes.
- **ZeroDB data:** member_votes

#### 5.3 Bills with sponsors · **5 pts** · Must · Iteration 3

As the system, I want bills ingested with sponsors, cosponsors and subjects, so that sponsorship counts as evidence.

- **Acceptance:** `bills.sponsors[]` is populated; CRS subjects are stored; idempotent on re-run.
- **ZeroDB data:** bills

#### 5.4 Archive bill text · **3 pts** · Should · Iteration 3

As a voter, I want to open the exact bill text a vote was on, so that I can verify the evidence myself.

- **Acceptance:** Text is stored in Files with its sha256; the UI opens it via a presigned URL.
- **ZeroDB data:** files, bills

#### 5.5 Bill-to-stance classifier v0 · **8 pts** · Must · Iteration 3

As the system, I want bills tagged with topics and with whether they advance or oppose each stance, so that votes translate into stance positions.

- **Acceptance:** At least 85% precision on a 200-bill labeled set; low-confidence tags go to QA.
- **ZeroDB data:** bills, graph: advances/opposes

#### 5.6 Hourly incremental sync · **3 pts** · Must · Iteration 4

As the system, I want new votes picked up hourly while the House is in session, so that records stay current.

- **Acceptance:** A new roll call appears within 2 hours of publication; reruns are no-ops.
- **ZeroDB data:** roll_calls, member_votes

#### 5.7 Roll-call ingested event · **2 pts** · Must · Iteration 4

As a downstream consumer, I want a `rollcall.ingested` event for each new roll call, so that positions and alerts update without polling.

- **Acceptance:** The event carries roll_call_id, bill_id and topic_ids; it is emitted exactly once per roll call.
- **ZeroDB data:** events

#### 5.8 Backfill 118th and 119th Congress · **3 pts** · Must · Iteration 3

As a voter, I want an incumbent's full current-era record, so that the score isn't based on a few weeks of votes.

- **Acceptance:** Backfill completes; row counts match the source totals.
- **ZeroDB data:** roll_calls, member_votes, bills

### E6 — Alignment scoring (33 pts)

Candidate positions per stance and per-request alignment scores. Iterations: 4.

#### 6.1 Methodology versions · **3 pts** · Must · Iteration 4

As a methodology owner, I want weights for tiers, evidence blend and vote types versioned, so that every score is reproducible.

- **Acceptance:** Exactly one current version exists; publishing a version emits `methodology.published`.
- **ZeroDB data:** methodology_versions, events

#### 6.2 Candidate positions from votes · **8 pts** · Must · Iteration 4

As the system, I want each incumbent's position (−1 to +1) per stance computed from their votes, weighted by vote type, so that alignment has a basis.

- **Acceptance:** Final passage outweighs procedural and bundled votes; results are append-only with `is_current`; recomputed on `rollcall.ingested`.
- **ZeroDB data:** candidate_positions

#### 6.3 Evidence blend · **5 pts** · Should · Iteration 5

As a voter, I want sponsorships and stated positions included alongside votes, so that the picture is complete.

- **Acceptance:** Default blend is 70% votes, 15% sponsorships, 15% statements; weights come from the methodology.
- **ZeroDB data:** candidate_positions, bills

#### 6.4 Alignment score endpoint · **5 pts** · Must · Iteration 4

As a voter, I want a 0–100 alignment score for each candidate against my profile, so that I can compare quickly.

- **Acceptance:** Implements the PRD formula; computed per request and never persisted; the same input always gives the same output.
- **ZeroDB data:** candidate_positions (read)

#### 6.5 Confidence badges · **2 pts** · Must · Iteration 4

As a voter, I want to know how much evidence a score rests on, so that I don't over-trust thin data.

- **Acceptance:** Badges are High, Medium, Low or Insufficient, with thresholds from the methodology.
- **ZeroDB data:** candidate_positions

#### 6.6 Dealbreaker detection · **3 pts** · Must · Iteration 4

As a voter, I want a warning above the score when a candidate acted against my dealbreaker, so that it isn't averaged away.

- **Acceptance:** Matches the PRD Gherkin scenario "Dealbreaker overrides the average".
- **ZeroDB data:** candidate_positions

#### 6.7 Top reasons with citations · **5 pts** · Must · Iteration 4

As a voter, I want the top 3 reasons behind each score with links to the votes, so that I can see why.

- **Acceptance:** Each reason cites a roll call or bill; no reason lacks a citation.
- **ZeroDB data:** member_votes, roll_calls

#### 6.8 Votes-only mode · **2 pts** · Should · Iteration 5

As a skeptical voter, I want to score on recorded votes only, so that statements don't sway the result.

- **Acceptance:** The toggle changes the evidence blend; the choice persists in my profile.
- **ZeroDB data:** voter_profiles

### E7 — Ballot and compare UI (35 pts)

Ballot view, candidate detail, side-by-side comparison, evidence drawer. Iterations: 4.

#### 7.1 Ballot view · **5 pts** · Must · Iteration 4

As a voter, I want each race with its candidates, alignment scores and confidence, so that I can see my whole ballot at once.

- **Acceptance:** Ballot order by default; incumbents labeled; loads in under 2 seconds on 4G.
- **ZeroDB data:** candidate_cards

#### 7.2 Candidate cards read model · **5 pts** · Must · Iteration 4

As the system, I want a per-candidacy read model rebuilt from events, so that the ballot view needs one query.

- **Acceptance:** Rebuilt on `verdict.published` and `positions.recomputed`; never aggregates at request time.
- **ZeroDB data:** candidate_cards

#### 7.3 Candidate detail · **5 pts** · Must · Iteration 4

As a voter, I want a candidate page that breaks the score down interest by interest, so that I see where we agree and disagree.

- **Acceptance:** One row per interest, with direction, evidence count and links.
- **ZeroDB data:** candidate_positions

#### 7.4 Side-by-side compare · **5 pts** · Must · Iteration 5

As a voter, I want to compare candidates in a race side by side, so that I can see the differences on what matters to me.

- **Acceptance:** Supports 2–4 candidates; same rows across candidates; colors carry text labels too.
- **ZeroDB data:** candidate_positions, candidate_cards

#### 7.5 Evidence drawer · **3 pts** · Must · Iteration 4

As a voter, I want to open any vote or promise behind a score and see the source, so that I can verify it.

- **Acceptance:** Shows roll-call number, date, question, position, bill link and archived source.
- **ZeroDB data:** roll_calls, files

#### 7.6 User-sortable ranking · **2 pts** · Must · Iteration 4

As a voter, I want to sort candidates by score if I choose, so that I control how results are presented.

- **Acceptance:** Default order is ballot order; the sort is a user action; there is no "recommended" label.
- **ZeroDB data:** —

#### 7.7 Installable PWA · **5 pts** · Should · Iteration 6

As a mobile voter, I want to install the app and view my profile offline, so that I can use it at the polling place.

- **Acceptance:** Lighthouse PWA checks pass; the profile is cached client-side, encrypted.
- **ZeroDB data:** —

#### 7.8 Accessibility · **5 pts** · Must · Iteration 7

As a voter using assistive technology, I want the app to meet WCAG 2.2 AA, so that I can use it fully.

- **Acceptance:** Automated axe checks are clean; a manual screen-reader pass covers the main journey.
- **ZeroDB data:** —

### E8 — US Senate ingest and vote typing (18 pts)

Senate LIS feed, vote-type classification, bundled and voice votes. Iterations: 5.

#### 8.1 Senate roll-call ingest · **5 pts** · Must · Iteration 5

As the system, I want Senate roll calls ingested from the senate.gov LIS XML feed, so that senators are scored too.

- **Acceptance:** All Senate roll calls since the 118th Congress, with provenance; hourly sync while in session.
- **ZeroDB data:** roll_calls, member_votes, provenance_log

#### 8.2 LIS-to-bioguide crosswalk · **3 pts** · Must · Iteration 5

As the system, I want Senate LIS member ids mapped to bioguide ids, so that Senate votes join to people.

- **Acceptance:** 100% of sitting senators map; an unmapped id raises an alert.
- **ZeroDB data:** people

#### 8.3 Vote-type classifier · **5 pts** · Must · Iteration 5

As the system, I want each roll call classified as final passage, amendment, cloture, motion to recommit, procedural or other, so that weights apply correctly.

- **Acceptance:** At least 95% accuracy on a labeled sample; matches the PRD "procedural vote" scenario.
- **ZeroDB data:** roll_calls

#### 8.4 Bundled (omnibus) flag · **3 pts** · Must · Iteration 5

As a voter, I want omnibus votes flagged and down-weighted, so that one vote isn't read as support for every clause.

- **Acceptance:** Bills over a configurable size or with omnibus markers get `is_bundled`; the UI explains it.
- **ZeroDB data:** roll_calls, member_votes

#### 8.5 No-recorded-vote handling · **2 pts** · Must · Iteration 5

As a voter, I want voice votes and unanimous consent shown as "no recorded vote", so that silence isn't treated as support.

- **Acceptance:** Matches the PRD Gherkin scenario "No recorded vote".
- **ZeroDB data:** roll_calls

### E9 — Promise collection and extraction (24 pts)

Archive campaign sources and extract atomic, testable commitments. Iterations: 6.

#### 9.1 Archive campaign sites · **5 pts** · Must · Iteration 6

As the system, I want each candidate's campaign site captured once per election cycle and archived, so that promises stay citable after sites change.

- **Acceptance:** Stored as HTML and PDF in Files with sha256; a `promise_sources` row exists per capture.
- **ZeroDB data:** promise_sources, files

#### 9.2 Debates and questionnaires · **5 pts** · Should · Iteration 7

As the system, I want debate transcripts and candidate questionnaires ingested, so that promises made outside websites count.

- **Acceptance:** At least 2 source types beyond websites; each is archived with provenance.
- **ZeroDB data:** promise_sources, files

#### 9.3 Promise extraction agent · **8 pts** · Must · Iteration 6

As the system, I want text turned into atomic commitments (actor, action, object, direction) flagged as testable or not, so that promises can be checked.

- **Acceptance:** At least 90% precision on a 300-promise labeled set; vague rhetoric is tagged not testable; the quote span is kept.
- **ZeroDB data:** promises

#### 9.4 Embed promises · **2 pts** · Must · Iteration 6

As the system, I want each promise embedded, so that it can be matched to bills.

- **Acceptance:** Every promise has a `vector_id`; its metadata includes promise_id and person_id.
- **ZeroDB data:** vectors: promises

#### 9.5 Promise extracted event · **1 pts** · Must · Iteration 6

As the mapping agent, I want a `promise.extracted` event, so that mapping starts automatically.

- **Acceptance:** Emitted once per promise.
- **ZeroDB data:** events

#### 9.6 Challenger promises view · **3 pts** · Must · Iteration 7

As a voter, I want to see a challenger's promises even though they have no voting record, so that I can still compare them.

- **Acceptance:** Promises are grouped by my interests; a prior record in another office is linked if one exists.
- **ZeroDB data:** promises

### E10 — Promise-to-vote mapping and human QA (27 pts)

Match promises to votes and route uncertain mappings to reviewers. Iterations: 6-7.

#### 10.1 Mapping agent · **8 pts** · Must · Iteration 6

As the system, I want each promise matched to its top candidate bills and roll calls, so that it can be tested against votes.

- **Acceptance:** Top 10 above 0.75 similarity; under 0.85 goes to QA; stored in `promises.mappings[]`.
- **ZeroDB data:** promises, vectors: bill_summaries

#### 10.2 Effect classification · **5 pts** · Must · Iteration 7

As the system, I want each mapping labeled as advancing, opposing or neutral to the promise, so that the vote's direction is known.

- **Acceptance:** At least 90% agreement with reviewers on a labeled sample.
- **ZeroDB data:** promises

#### 10.3 Reviewer QA queue · **8 pts** · Must · Iteration 7

As a QA reviewer, I want a queue of low-confidence mappings and all broken verdicts with the evidence side by side, so that I can approve or reject quickly.

- **Acceptance:** Filter by confidence and person; median review under 90 seconds; keyboard shortcuts.
- **ZeroDB data:** promises (qa_pending), verdicts

#### 10.4 Record reviewer decisions · **3 pts** · Must · Iteration 7

As the system, I want each approval or rejection recorded as RLHF feedback and emitted as `mapping.reviewed`, so that verdicts update and models can learn.

- **Acceptance:** One RLHF record and one event per decision.
- **ZeroDB data:** rlhf, events

#### 10.5 Reviewer roles and audit trail · **3 pts** · Must · Iteration 7

As a QA lead, I want reviewer roles and a full audit trail, so that decisions are accountable.

- **Acceptance:** Only reviewers can approve; every decision logs reviewer and timestamp.
- **ZeroDB data:** verdicts

### E11 — Promise vs. Record verdicts (28 pts)

Six verdict types, Say/Do gap, incumbent explanations. Iterations: 7.

#### 11.1 Verdict engine · **8 pts** · Must · Iteration 7

As the system, I want promises scored as kept, partial, broken, no opportunity, absent or not testable by fixed rules, so that verdicts are consistent.

- **Acceptance:** Matches the PRD rules table and the "Kept promise" scenario; append-only with `is_current`.
- **ZeroDB data:** verdicts

#### 11.2 Human gate on broken verdicts · **2 pts** · Must · Iteration 7

As a voter, I want every broken verdict checked by a person before it is published, so that a serious claim is never machine-only.

- **Acceptance:** Broken verdicts stay pending until approved; the UI shows the review status.
- **ZeroDB data:** verdicts

#### 11.3 Promise vs. Record view · **5 pts** · Must · Iteration 7

As a voter, I want an incumbent's promises listed with verdicts and citations, so that I can see whether they did what they said.

- **Acceptance:** Grouped by my interests first; each verdict links the archived promise and the roll calls.
- **ZeroDB data:** promises, verdicts

#### 11.4 Say/Do gap · **5 pts** · Must · Iteration 7

As a voter, I want the gap between alignment from promises and alignment from votes shown as a number, so that I can spot mismatches.

- **Acceptance:** Computed per request against my profile; explained in plain language.
- **ZeroDB data:** promises, candidate_positions

#### 11.5 Incumbent's own explanation · **5 pts** · Should · Iteration 8

As a voter, I want the incumbent's floor speech or statement shown beside a vote when one exists, so that I hear their reasoning.

- **Acceptance:** Linked when found; clearly labeled as the candidate's statement.
- **ZeroDB data:** candidate_statements

#### 11.6 Verdict published event · **3 pts** · Must · Iteration 7

As the system, I want `verdict.published` to rebuild candidate cards and graph edges, so that everything stays in sync.

- **Acceptance:** Cards update within 5 minutes of publish.
- **ZeroDB data:** events, candidate_cards

### E12 — Trust and neutrality (24 pts)

Symmetry audit, public methodology, right of response, corrections. Iterations: 7-8.

#### 12.1 Nightly symmetry audit · **8 pts** · Must · Iteration 7

As a methodology owner, I want mirrored synthetic candidates and profiles scored every night, so that any partisan skew is caught.

- **Acceptance:** Fails if mirrored scores differ by more than 0.5; results are written to `audit_runs`; a failure blocks the next release.
- **ZeroDB data:** audit_runs

#### 12.2 Public methodology page · **3 pts** · Must · Iteration 8

As a voter or journalist, I want the methodology and its changelog published, so that I can check how scores work.

- **Acceptance:** Every verdict and score links to its methodology version.
- **ZeroDB data:** methodology_versions

#### 12.3 Right of response · **5 pts** · Must · Iteration 8

As a verified campaign, I want to attach a statement to any verdict about my candidate, so that our side is heard.

- **Acceptance:** The campaign is verified first; the statement is shown beside the verdict and does not change it.
- **ZeroDB data:** candidate_statements

#### 12.4 Public corrections · **5 pts** · Must · Iteration 8

As anyone, I want to report an error on any claim, so that mistakes get fixed.

- **Acceptance:** Triaged within 72 hours; status is visible to the reporter; contact email is optional and deleted 90 days after resolution.
- **ZeroDB data:** corrections

#### 12.5 Panel gate in release · **3 pts** · Should · Iteration 8

As the advisory panel, we want unapproved taxonomy or stance changes blocked from release, so that neutrality reviews can't be skipped.

- **Acceptance:** The CI check fails on unapproved wording versions.
- **ZeroDB data:** stances

### E13 — Evidence graph and grounded Q&A agent (23 pts)

Knowledge graph and an agent that answers only from cited evidence. Iterations: 8.

#### 13.1 Evidence graph writes · **5 pts** · Must · Iteration 8

As the system, I want ingest and verdict agents to write the evidence graph per the ontology, so that the agent can traverse evidence.

- **Acceptance:** The ontology is registered; `voted` edges exist only where evidence needs them; canonical names are NoSQL ids.
- **ZeroDB data:** graph

#### 13.2 Grounded Q&A agent · **8 pts** · Must · Iteration 8

As a voter, I want to ask why a candidate scored as they did and get an answer with citations, so that I understand the score.

- **Acceptance:** Every answer cites a roll call, bill or promise; an ungrounded answer is replaced by "no evidence found".
- **ZeroDB data:** graph, vectors, memory

#### 13.3 No recommendations · **3 pts** · Must · Iteration 8

As a voter, I want the agent to decline to tell me who to vote for, so that the app stays neutral.

- **Acceptance:** Matches the PRD "Agent refuses to recommend" scenario; red-team suite of 50 prompts passes.
- **ZeroDB data:** —

#### 13.4 Election-period mode · **3 pts** · Should · Iteration 8

As a methodology owner, I want extra human review on new broken verdicts in the 30 days before an election, so that late errors are less likely.

- **Acceptance:** The mode switches on by election date; a second reviewer is required.
- **ZeroDB data:** verdicts, elections

#### 13.5 Short-lived session memory · **2 pts** · Must · Iteration 8

As a voter, I want my Q&A conversation forgotten after a day, so that my questions aren't kept.

- **Acceptance:** Memory retention is 1 day; nothing is written in anonymous mode.
- **ZeroDB data:** memory

#### 13.6 Flag an answer · **2 pts** · Should · Iteration 8

As a voter, I want to flag a wrong or unfair answer, so that it gets reviewed.

- **Acceptance:** The flag creates an RLHF error record and a corrections entry.
- **ZeroDB data:** rlhf, corrections

### E14 — Privacy and security (30 pts)

Encryption, consent, anonymous mode, export/delete, abuse protection. Iterations: 2, 8.

#### 14.1 Encrypted profile storage · **8 pts** · Must · Iteration 2

As a voter, I want my interests encrypted so that even the database operator can't read them, so that my political views stay private.

- **Acceptance:** AES-256-GCM per-user data keys via KMS; HMAC row ids; plaintext never logged.
- **ZeroDB data:** voter_profiles

#### 14.2 Consent at onboarding · **3 pts** · Must · Iteration 2

As a voter, I want to give explicit consent before my political interests are saved, so that I control my data.

- **Acceptance:** GDPR special-category consent recorded; no save without it; anonymous mode is offered as the alternative.
- **ZeroDB data:** voter_profiles

#### 14.3 Anonymous mode · **5 pts** · Must · Iteration 8

As a privacy-conscious voter, I want to use the full app without an account, so that nothing about me is stored.

- **Acceptance:** The profile lives in the browser only; `/v1/alignment` accepts it inline; no server rows are written.
- **ZeroDB data:** —

#### 14.4 Export and delete · **3 pts** · Must · Iteration 8

As a voter, I want to export or permanently delete my profile in one action, so that I stay in control.

- **Acceptance:** Export returns JSON; delete removes the row and shreds the key; completes in under 1 minute.
- **ZeroDB data:** voter_profiles

#### 14.5 Rate limiting and bot protection · **3 pts** · Must · Iteration 8

As an operator, I want public endpoints rate-limited and protected from bots, so that aggregate data isn't scraped.

- **Acceptance:** Per-IP and per-account limits; challenge on abuse; load test shows no degradation for real users.
- **ZeroDB data:** —

#### 14.6 Shareable summary card · **3 pts** · Should · Iteration 8

As a voter, I want to share a candidate summary without revealing my interests, so that I can discuss it safely.

- **Acceptance:** The card contains candidate data only; a test asserts no profile fields leak.
- **ZeroDB data:** candidate_cards

#### 14.7 Privacy-safe analytics · **5 pts** · Should · Iteration 8

As the team, we want usage analytics only above k-anonymity thresholds, so that no individual can be singled out.

- **Acceptance:** Aggregates require k ≥ 50; no third-party trackers.
- **ZeroDB data:** events

### E15 — Follow and alerts (13 pts)

Follow incumbents and get notified on relevant votes and verdicts. Iterations: 8.

#### 15.1 Follow an incumbent · **2 pts** · Should · Iteration 8

As an engaged citizen, I want to follow an incumbent, so that I can track them over their term.

- **Acceptance:** Follows are stored inside the encrypted profile payload.
- **ZeroDB data:** voter_profiles

#### 15.2 Vote alerts on primary interests · **5 pts** · Should · Iteration 8

As a follower, I want an alert when a followed incumbent votes on one of my primary interests, so that I don't miss what matters.

- **Acceptance:** Alert within 24 hours of `rollcall.ingested`; links to the vote and any related promise.
- **ZeroDB data:** events, member_votes

#### 15.3 Verdict change alerts · **3 pts** · Could · Iteration 8

As a follower, I want an alert when a verdict on a followed incumbent changes, so that I stay current.

- **Acceptance:** Triggered by `verdict.published`.
- **ZeroDB data:** events

#### 15.4 Notification preferences · **3 pts** · Should · Iteration 8

As a voter, I want to choose email, push or none, so that alerts suit me.

- **Acceptance:** Preferences are stored in the profile; one-click unsubscribe.
- **ZeroDB data:** voter_profiles

### E16 — Data integrity and observability (18 pts)

Integrity checks, provenance, drift repair, monitoring. Iterations: 1-8.

#### 16.1 Spike: ZeroDB query limits · **2 pts** · Must · Iteration 1

As the team, we want to confirm `zerodb_query_rows` operators and row and pagination limits, so that the data model holds at about 600k member votes per Congress.

- **Acceptance:** A written answer on `$in`, `$gte` and array contains; a load test at 1M rows.
- **ZeroDB data:** member_votes

#### 16.2 Nightly integrity checks · **5 pts** · Must · Iteration 5

As an operator, I want the five integrity checks from the data model run nightly, so that broken references are caught.

- **Acceptance:** Each check has a test; failures page on-call and open a ticket.
- **ZeroDB data:** All tables

#### 16.3 Provenance on every source · **3 pts** · Must · Iteration 3

As a journalist, I want every record with a source URL traceable to when and how it was fetched, so that evidence is auditable.

- **Acceptance:** Every row with a `source_url` has a `provenance_log` entry; a CI test enforces it.
- **ZeroDB data:** provenance_log

#### 16.4 Drift repair · **3 pts** · Must · Iteration 5

As the system, I want denormalized `member_votes` fields repaired when their roll call changes, so that copies never disagree.

- **Acceptance:** A drift check finds mismatches and re-copies them; idempotent.
- **ZeroDB data:** member_votes, roll_calls

#### 16.5 Monitoring and tracing · **5 pts** · Should · Iteration 6

As an operator, I want logs, metrics and traces across API, agents and ZeroDB calls, so that I can find problems fast.

- **Acceptance:** Dashboards for ingest lag, error rate and p95 latency; alerts on ingest lag over 6 hours.
- **ZeroDB data:** events

### E17 — Global expansion adapters (55 pts)

State legislatures, EU Parliament, UK Commons, localization. Iterations: 9+.

#### 17.1 JurisdictionAdapter interface · **5 pts** · Must · Iteration 9+

As a developer, I want a `JurisdictionAdapter` interface that US federal is refactored onto, so that a new country is an adapter, not a rewrite.

- **Acceptance:** The US adapter passes all existing tests through the interface.
- **ZeroDB data:** jurisdictions

#### 17.2 Open States adapter · **13 pts** · Should · Iteration 9+

As a state-level voter, I want my state legislators' votes scored, so that I can judge state races.

- **Acceptance:** 50 states plus DC; votes, bills and legislators ingested with provenance. Split into per-region stories before sprint planning.
- **ZeroDB data:** All record tables

#### 17.3 EU Parliament adapter · **13 pts** · Could · Iteration 9+

As an EU voter, I want my MEPs' roll-call votes scored, so that Informed works in Europe.

- **Acceptance:** HowTheyVote.eu data ingested under ODbL with attribution. Split before sprint planning.
- **ZeroDB data:** All record tables

#### 17.4 UK Commons adapter · **8 pts** · Could · Iteration 9+

As a UK voter, I want my MP's division votes scored, so that Informed works in the UK.

- **Acceptance:** Commons Votes API divisions ingested; constituency lookup works.
- **ZeroDB data:** All record tables

#### 17.5 Taxonomy localization · **8 pts** · Could · Iteration 9+

As a non-US voter, I want issue topics that fit my country, so that my interests map correctly.

- **Acceptance:** Locales en-GB, de and fr with panel review per locale.
- **ZeroDB data:** issue_topics, stances

#### 17.6 UI internationalization · **8 pts** · Could · Iteration 9+

As a non-English speaker, I want the app in my language, so that I can use it fully.

- **Acceptance:** i18n framework in place; 3 languages at launch; right-to-left ready.
- **ZeroDB data:** —

### E18 — Model distillation (Focustem) (29 pts)

Distill extractor and classifier from QA-approved data. Iterations: 9+.

#### 18.1 Export QA-approved dataset · **3 pts** · Could · Iteration 9+

As an ML engineer, I want reviewer-approved mappings and verdicts exported as a training set, so that we can distill smaller models.

- **Acceptance:** Versioned export with labels and source ids; no voter data included.
- **ZeroDB data:** rlhf, promises, verdicts

#### 18.2 Distilled promise extractor · **13 pts** · Could · Iteration 9+

As the team, we want a small domain model for promise extraction, so that per-refresh inference cost drops.

- **Acceptance:** Matches base precision within 2 points at under 30% of the cost. Split before sprint planning.
- **ZeroDB data:** promises

#### 18.3 Distilled vote classifier · **8 pts** · Could · Iteration 9+

As the team, we want a small model for vote-type and bill-stance classification, so that ingest is cheaper.

- **Acceptance:** Matches base accuracy within 2 points.
- **ZeroDB data:** roll_calls, bills

#### 18.4 Shadow evaluation · **5 pts** · Could · Iteration 9+

As the team, we want distilled models run in shadow against the base models, so that we switch only when they're proven.

- **Acceptance:** Two weeks of shadow results with the comparison report stored.
- **ZeroDB data:** files

## Notes

- **Spikes** (2.5, 16.1) are timeboxed and produce a written answer, not shippable code. Both should run in iteration 1 because they unblock 2.6 and the whole NoSQL model.
- **13-point stories** (17.2, 17.3, 18.2) are placeholders for Phase 2–3 and must be split before they enter an iteration.
- **Story 14.1** (encryption) is scheduled in iteration 2 even though its epic lands later, because profiles cannot be saved without it.
