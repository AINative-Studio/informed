# Informed PRD — Interest-Aligned Voter Intelligence

Oct 15, 2026 · @Toby

## Overview

Informed starts with what the voter cares about, then shows how closely each candidate matches it — and, for incumbents, whether their votes matched their campaign promises. It is built agent-native on AINative: ZeroDB holds the voter's interest profile and the evidence graph, and an agent swarm ingests, classifies and cites legislative records.

**Problem.** Voters judge candidates mostly on campaign messaging, ads and party label. The record that matters for an incumbent — how they actually voted — is public but scattered across roll-call databases, bill texts and procedural votes few people can read. No mainstream tool lets a voter state their own priorities first and then measure candidates against them with cited evidence.

**Vision.** Every voter, in any democracy with public legislative records, can answer in five minutes: "On the things I care most about, who will act in my interest — and has this incumbent done what they said?"

**Product principles**

1. **Voter-defined, not app-defined.** The app never tells anyone who to vote for or what their interests should be. It measures alignment against the voter's own stated priorities.
2. **Nonpartisan by construction.** Same methodology, same data sources, same scoring code for every candidate and party. Methodology is public and versioned.
3. **Evidence over opinion.** Every score drills down to the primary source: the promise (with its URL and date) and the vote (roll-call number, chamber, date, bill text).
4. **Show uncertainty.** Procedural votes, amendments, absences and ambiguous promises are labelled, not silently scored.
5. **Privacy first.** A person's political interests are sensitive data. Profiles are private by default, encrypted, never sold, never used for ad targeting or campaign outreach.

## Goals, non-goals and success metrics

The MVP succeeds if a US voter can build a tiered interest profile and see a cited Promise vs. Record report for their own members of Congress before the next federal election.

**Goals**

- Capture a voter's interests in three tiers: primary (must-have), secondary (important), nice-to-have.
- Score each candidate's alignment to that profile, weighted by tier.
- For incumbents, compare campaign commitments to actual roll-call votes and sponsorships, issue by issue.
- Expand country by country through a pluggable jurisdiction adapter.

**Non-goals (MVP)**

- Recommending a candidate, party or ballot choice.
- Fact-checking statements unrelated to legislative action.
- Campaign tools, donations, voter registration or get-out-the-vote features.
- Local offices (city council, school board) — later phase.

| Metric | Target (6 months post-launch) |
| --- | --- |
| Profile completion (≥3 primary interests) | 70% of sign-ups |
| Time to first candidate report | Under 5 minutes |
| Claims with a primary-source citation | 100% |
| Promise-to-vote mappings passing human QA sample | ≥95% precision |
| Cross-party scoring audit (same rules applied) | Zero unresolved findings |
| Users who say they understand a candidate better | ≥60% (post-report survey) |

## Personas and core journeys

Three personas cover the MVP; the busy voter is the design center.

| Persona | Need | Key journey |
| --- | --- | --- |
| Busy voter (design center) | Decide in minutes, trust the result | Onboard → set tiered interests → see ballot candidates ranked by alignment → open one incumbent's Promise vs. Record |
| Engaged citizen | Dig into evidence, track over a term | Follow an incumbent → get alerts when they vote on a primary interest → compare to their promise |
| Journalist / civic educator | Verifiable, exportable evidence | Search a bill or promise → export cited report → flag a disputed mapping |

**Primary journey (busy voter)**

1. Enter location (address or postcode) → app resolves districts and the candidates on the ballot.
2. Build interest profile (next section) — pick topics, set stance, sort into tiers.
3. See the ballot view: each race lists candidates with an alignment score, a confidence badge and the top three reasons.
4. Open an incumbent → Promise vs. Record: kept, broken, partial, no-action, per promise, each with citations.
5. Optionally ask the Informed agent questions ("Why is she 40% on healthcare?") — answers cite sources only.
6. Save, share a non-identifying summary card, or set alerts.

## Feature 1 — Interest Profile

The profile is the product's starting point: every score downstream is computed against it, so it must be quick to build and precise enough to map to legislation.

**Tiers**

| Tier | Meaning | Default weight | Rules |
| --- | --- | --- | --- |
| Primary | Must-have; drives the decision | 3.0 | 1–5 interests; each can be flagged **dealbreaker** |
| Secondary | Important, but tradeable | 1.5 | Up to 8 |
| Nice-to-have | Good if present, not required | 0.5 | Unlimited; never lowers a score below the primary/secondary result by more than 10% |

**Each interest captures**

- **Topic** from a shared issue taxonomy (e.g. Healthcare → Prescription drug pricing), localizable per country.
- **Stance**: the outcome the voter wants, in plain words ("lower out-of-pocket drug costs"), chosen from balanced stance cards written to be neutral — each topic offers positions from across the spectrum, presented in randomized order.
- **Intensity** inside its tier (1–3) for tie-breaking.
- **Optional free-text note**, which an agent maps to taxonomy topics and shows back for confirmation (never silently).

**Requirements**

- Onboarding paths: quick (pick 3 primary topics, 2 minutes), guided (agent interview), or advanced (full taxonomy).
- Drag-and-drop re-tiering; changes re-score instantly.
- Profile versioning — users see how scores change if they change priorities.
- Profile stored in ZeroDB under the user's tenant, encrypted; exportable and deletable in one action.
- The app may *ask* clarifying questions but never suggests what a user's interests should be.

## Feature 2 — Promise vs. Record (incumbents)

For every incumbent legislator, Informed extracts what they campaigned on, links each promise to the bills and votes that tested it, and gives a cited verdict per promise. This is the product's core differentiator.

**Pipeline**

1. **Collect promises** — campaign websites (archived snapshots by election cycle), official platform documents, debate and town-hall transcripts, candidate questionnaires, press releases.
2. **Extract commitments** — an agent turns text into atomic, testable commitments: actor, action, object, direction ("vote to cap insulin at $35/month"). Vague rhetoric is tagged *non-testable* and shown but not scored.
3. **Map to legislation** — commitments are matched to bills, amendments and roll-call votes by topic embedding plus bill subject codes; each mapping gets a confidence score.
4. **Read the vote** — classify each vote by type (final passage, amendment, cloture, motion to recommit, procedural) and whether a yes/no advanced or opposed the commitment.
5. **Verdict** — aggregate the votes and sponsorships per commitment into a verdict.
6. **Human QA** — low-confidence mappings and all *broken* verdicts go to a reviewer queue before publishing; every verdict shows reviewer status.

**Verdicts**

| Verdict | Rule (simplified) |
| --- | --- |
| Kept | Voted/sponsored consistently with the commitment on final-passage votes |
| Partial | Mixed: e.g. supported the bill but voted for an amendment that weakened it |
| Broken | Voted against the commitment on a final-passage vote with no stated rationale |
| No opportunity | No vote occurred on the matter during the term |
| Absent | Missed the relevant votes |
| Not testable | Promise too vague to tie to any legislative action |

**Context rules (to stay fair)**

- Procedural and cloture votes are shown but weighted lower than final passage, with a plain-language explainer.
- Omnibus bills: a vote on a 1,000-page bill is not a vote for every clause — flagged as *bundled*, low weight.
- The incumbent's own public explanation for a vote (floor speech, statement) is linked beside it when found.
- Also tracked: bill sponsorship and co-sponsorship, and for executives or ministers (later phase) official actions.
- Challengers have no voting record; they get a Promises-only view, plus their prior record if they held another office.

## Feature 3 — Alignment scoring and comparison

Each candidate gets one alignment score (0–100) against the voter's profile, plus a confidence level, and any dealbreaker violation is shown above the score rather than averaged away.

Per interest *i*, the candidate's position *s\_i* runs from −1 (acts against the voter's stance) to +1 (acts for it). For incumbents, *s\_i* comes mostly from votes; for challengers, from promises. Tier weight *w\_i* and intensity *k\_i* combine as:

```latex
\text{Alignment} = 50 \left(1 + \frac{\sum_i w_i k_i c_i s_i}{\sum_i w_i k_i c_i}\right)
```

*c\_i* is evidence confidence (0–1), so thin evidence moves the score less. Nice-to-have interests are capped as described in Feature 1.

**Evidence blend for incumbents** — votes 70%, sponsorships 15%, stated positions 15% by default; the voter can switch to "votes only".

**Comparison view**

- Side-by-side candidates per race, one row per interest, colored by direction, with "why" links to evidence.
- **Say/Do gap** per incumbent: alignment computed from promises vs. from votes — the gap is shown as its own number.
- Confidence badge: High (≥10 relevant votes or clear commitments), Medium, Low, Insufficient data.
- Never a single "vote for X" output; ranking is sortable by the user, default order is ballot order.

## Data sources and coverage

The US MVP can run entirely on free, official or open-licensed sources; global expansion is gated by each country's machine-readable vote data. Two once-standard sources are gone: ProPublica's Congress API is no longer available ([ProPublica](https://projects.propublica.org/represent/)), and Google turned down its Civic Information Representatives API in April 2025 ([Google Civic API group](https://groups.google.com/g/google-civicinfo-api/c/9fwFn-dhktA/m/ftUoL83mAwAJ)), so district lookup must be rebuilt from Census geocoding plus open legislator data.

| Jurisdiction | Source | What it gives | Access | Phase |
| --- | --- | --- | --- | --- |
| US House | [Congress.gov API house-vote](https://blogs.loc.gov/law/2025/05/?gca=14) | Roll calls and per-member positions, from the 118th Congress (2023) | Free API key | MVP |
| US Senate | senate.gov LIS roll-call XML feed ([note](https://glama.ai/mcp/servers/cyanheads/congressgov-mcp-server)) | Senate roll calls; Congress.gov API exposes no Senate votes | Public XML | MVP |
| US bills, sponsors, subjects | Congress.gov API v3 | Bill text, actions, cosponsors, subject terms | Free API key | MVP |
| US legislators | [unitedstates/congress-legislators](https://web-archive.nli.org.il/National_Library/20161108065307mp_/http://govtrack.us/developers) | All members since 1789, IDs crosswalk | Open (GitHub) | MVP |
| US address → district | US Census Geocoder + legislator data ([pattern](https://glama.ai/mcp/servers/oyfh93qav2)) | Congressional and state districts | Free, keyless | MVP |
| US states | [Open States API v3](https://docs.openstates.org/api-v3/) | State bills, votes, legislators, geo lookup | Free API key | Phase 2 |
| European Parliament | [HowTheyVote.eu](https://howtheyvote.eu/about) | Roll-call votes from the 9th term, ODbL-licensed API and CSV | Open | Phase 3 |
| UK House of Commons | [Commons Votes API](https://jentic.com/apis/parliament.uk/parliament-uk-commons-votes) | Divisions and how each MP voted | Public, no key | Phase 3 |
| Campaign promises (all) | Candidate sites (archived per cycle), debate transcripts, questionnaires | Raw promise text | Crawl + archive | MVP |

**Data rules**

- Every ingested record keeps source URL, retrieval timestamp, and a content hash in ZeroDB, so any score can be reproduced.
- Only recorded (roll-call) votes are scored. Voice votes and unanimous consent are shown as "no recorded vote", never as support.
- Each jurisdiction plugs in through a `JurisdictionAdapter` interface (districts, members, votes, bills, promise sources) — adding a country is an adapter plus a taxonomy localization, not a rewrite.

## AINative architecture

Informed is a thin FastAPI + React app over AINative primitives: agents do ingest and reasoning, ZeroDB is the single store of evidence, and every score can be recomputed from it.

&#91;embedded content: Informed architecture · 4 layers on AINative\]

Sources and human reviewers feed the agent swarm, which writes to ZeroDB; FastAPI reads profiles and evidence directly from ZeroDB (right-hand path) and calls agents only for extraction and Q&A.

**Stack**

- **Backend:** Python, FastAPI, Pydantic v2, pytest + pytest-bdd, httpx. All database operations go through the AINative ZeroDB MCP server client — no direct SQL.
- **Frontend:** React + TypeScript, TanStack Query, installable PWA for mobile; Vitest + React Testing Library for units, Playwright + Cucumber for BDD.
- **Agents:** AINative Agent Swarm; one ingest agent per `JurisdictionAdapter`, scheduled hourly while a chamber is in session.
- **Knowledge model:** ZeroDB ontology with RDF triples and SHACL shapes enforcing evidence rules (e.g. every *Kept/Broken/Partial* verdict must link ≥1 recorded vote).
- **Focustem:** distill the promise extractor and vote classifier into small domain models from QA-approved data, cutting inference cost per refresh.
- **CI:** every push runs unit + BDD suites and the symmetry audit; red build blocks merge.

**ZeroDB data model**

| Collection / graph | Contents | Rules |
| --- | --- | --- |
| voter\_profiles | Interests, tiers, stances, weights, versions | Encrypted per user; one-action delete |
| candidates | Person, office, district, jurisdiction IDs | Crosswalk to Bioguide / OCD-IDs |
| promises | Atomic commitment, source URL, archive snapshot, cycle, testable flag | Vector-embedded |
| bills\_votes | Roll call, vote type, member position, bill subjects | Bill summaries vector-embedded |
| evidence\_graph | Triples: promise → tested\_by → vote; vote → supports/opposes → stance | SHACL-validated |
| verdicts\_scores | Verdict, confidence, methodology version, QA status | Immutable history; recomputable |
| provenance\_log | Source URL, fetched\_at, SHA-256 | Append-only |

**Core API (FastAPI)**

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/profiles` | Create or update a tiered interest profile |
| `GET /v1/ballot?address=` | Districts, races and candidates for an address |
| `GET /v1/candidates/{id}/alignment?profile_id=` | Score, confidence, dealbreakers, top reasons |
| `GET /v1/candidates/{id}/promise-record` | Per-promise verdicts with citations |
| `POST /v1/agent/ask` | Grounded Q&A over the evidence graph |
| `POST /v1/corrections` | Public correction submission |

## Trust, neutrality, privacy and safety

The product only works if voters across the spectrum believe it treats every candidate the same, and if their political profile can never leak or be weaponized.

**Neutrality**

- One scoring codebase for all candidates; no party-specific rules. A nightly **symmetry audit** runs the same synthetic profiles against all candidates and flags skew.
- Stance cards and issue taxonomy reviewed by a cross-partisan advisory panel before each release; wording changes are versioned and public.
- Methodology page and changelog; every verdict links to the methodology version that produced it.
- Candidates get a **right of response**: a verified campaign can attach a statement to any verdict, shown beside it, without changing the verdict.
- Public correction form on every claim, with an SLA (72 hours to triage).

**Agent guardrails**

- The Q&A agent answers only from the ZeroDB evidence graph and must cite; ungrounded answers are blocked.
- It never says who to vote for, never predicts outcomes, never generates persuasive content for or against a candidate.
- Election-period mode: extra human review on new *broken* verdicts in the 30 days before an election.

**Privacy**

- Political opinions are special-category data under GDPR; explicit consent at onboarding, purpose-limited to scoring.
- Profiles encrypted per user in ZeroDB; no third-party trackers; no sale, no ad targeting, no sharing with campaigns or parties.
- Anonymous mode: full use without an account, profile held client-side only.
- One-tap export and permanent deletion; aggregate analytics only above k-anonymity thresholds (k ≥ 50).

**Integrity and abuse**

- Rate limits and bot protection on public endpoints to stop scraping of aggregate sentiment.
- Shared summary cards contain the candidate report, never the user's interest profile.

## BDD acceptance criteria

These Gherkin scenarios are the executable definition of done for the MVP; each becomes a `pytest-bdd` feature (backend) or a Playwright + Cucumber feature (React), written before the code.

```gherkin
Feature: Tiered interest profile
  Scenario: Voter sets primary, secondary and nice-to-have interests
    Given a new voter on the onboarding screen
    When they choose "Prescription drug pricing" as a primary interest with stance "lower out-of-pocket costs"
    And they choose "Broadband access" as a secondary interest
    And they choose "Public lands" as a nice-to-have interest
    Then their profile is saved with weights 3.0, 1.5 and 0.5
    And no candidate score is shown until at least one primary interest exists

  Scenario: Dealbreaker overrides the average
    Given a voter marks "Prescription drug pricing" as a dealbreaker
    And candidate A scores 82 overall but acted against that stance on 3 final-passage votes
    When the voter views the ballot
    Then candidate A shows a dealbreaker warning above the score

Feature: Promise vs. Record
  Scenario: Kept promise with cited evidence
    Given incumbent B's 2024 campaign site committed to "vote to cap insulin costs"
    And B voted Yea on the final-passage roll call of a bill capping insulin costs
    When the voter opens B's Promise vs. Record
    Then the promise shows verdict "Kept"
    And it links the archived campaign page and the roll-call record

  Scenario: Procedural vote is not scored as broken
    Given B voted Nay on a cloture motion but Yea on final passage of the same bill
    Then the verdict is "Kept" with a "procedural vote" note on the cloture vote

  Scenario: No recorded vote
    Given a promise maps only to a bill passed by voice vote
    Then the verdict is "No opportunity" and the UI says "no recorded vote"

Feature: Neutrality
  Scenario: Symmetry audit
    Given two synthetic candidates with mirrored voting records
    And two synthetic voters with mirrored profiles
    When scores are computed
    Then each voter's score for their aligned candidate is identical within 0.5 points

Feature: Agent answers are grounded
  Scenario: Agent refuses to recommend
    When a voter asks "Who should I vote for?"
    Then the agent explains it doesn't recommend candidates
    And offers the voter's alignment comparison instead
```

## XP release plan, risks and open questions

Ship in one-week XP iterations, each ending in a releasable increment; the walking skeleton lands in iteration 1 and the US House/Senate Promise vs. Record MVP by iteration 8.

| Iteration | User stories (thin vertical slices) | Release |
| --- | --- | --- |
| 1 | Walking skeleton: FastAPI + React + ZeroDB; address → district → member list | Internal |
| 2 | Interest profile (quick path), tier weights, profile in ZeroDB | Internal |
| 3 | Ingest House roll calls + bills; vote-to-topic classifier v0 | Internal |
| 4 | Alignment score for incumbents, votes only, with citations | Alpha |
| 5 | Senate LIS ingest; procedural/omnibus vote typing | Alpha |
| 6 | Promise extraction from archived campaign sites; human QA queue | Alpha |
| 7 | Promise vs. Record verdicts + Say/Do gap; correction form | Beta |
| 8 | Q&A agent (grounded), symmetry audit, privacy controls, anonymous mode | Public beta |
| 9+ | Open States (state legislatures), then EU Parliament and UK Commons adapters | Phase 2–3 |

**XP practices** — planning game with the advisory panel as on-site customer for neutrality stories; pair (human + agent) programming; Gherkin-first for every story; red-green-refactor with ≥90% coverage on scoring and verdict logic; continuous integration on every push; collective code ownership; small releases behind feature flags.

**Risks**

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Perceived partisan bias | Fatal to trust | Symmetry audits, cross-partisan panel, public methodology, right of response |
| Wrong promise-to-vote mapping | Misleads voters, legal exposure | Confidence threshold, human QA on all *broken* verdicts, correction SLA |
| Data source changes or shutdowns | Gaps in coverage | Adapter pattern, archive raw data with hashes, multiple sources per jurisdiction |
| Political-opinion data breach | Severe harm, GDPR penalties | Per-user encryption, anonymous mode, minimal retention |
| Countries without machine-readable votes | Limits "global" claim | Launch per country only when an adapter meets the coverage bar; say so in-app |

**Open questions**

- [ ] Final product name and domain.
- [ ] Business model: nonprofit/foundation-funded, freemium civic tool, or B2B (newsrooms, civic educators) API — must not depend on campaign money.
- [ ] Who sits on the cross-partisan advisory panel, and how members are chosen.
- [ ] Which election is the MVP launch target (US 2028 cycle, or a pilot on the current Congress)?
- [ ] How far back to score: current term only, or full career?
- [ ] Should challengers' prior non-legislative records (e.g. as governor or mayor) be in scope for Phase 2?
