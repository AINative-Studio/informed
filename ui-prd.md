# Informed — UI & Landing Page PRD

Oct 15, 2026 · Companion to `prd.md`, `datamodel.md` and `backlog.md` · **Hand-off target: Claude Design**

## Purpose and how to use this document

This is the design brief for Informed's public landing page and core app UI. It translates the product PRD's requirements into what a designer needs to start: screen inventory, content and states for each screen, the journeys that connect them, and the non-negotiable constraints (neutrality, evidence, privacy) that must show up in the interface, not just the backend.

This document does **not** prescribe visual style, layout, or a component library — that is Claude Design's call to make. It specifies *what must be true of the design*, not what it should look like. Where a decision has already been fixed by the product PRD (e.g. randomized stance order, no "recommended" label), it is called out explicitly as a constraint, not a suggestion.

Read `prd.md` in full before starting — this document assumes its definitions (tiers, verdicts, confidence badges, Say/Do gap) and does not redefine them.

## Design principles, inherited from the product

These five rules constrain every screen in this document. Flag any design direction that conflicts with one back to product rather than resolving it unilaterally.

1. **Voter-defined, not app-defined.** No screen may suggest what a user should care about, or who to vote for. Candidate ordering defaults to ballot order, never a "recommended" rank.
2. **Nonpartisan by construction.** Identical visual treatment for every party and candidate. No party-color-coding by default (red/blue). If party must be shown, use a neutral, consistent indicator — not color that implies judgment.
3. **Evidence over opinion.** Every score, verdict, or claim must be visually paired with a path to its citation. A number or verdict with no visible "why" link is a bug, not a simplification.
4. **Show uncertainty.** Confidence badges, "no recorded vote," and "not testable" are first-class UI states, not footnotes. Thin evidence must look visibly different from strong evidence.
5. **Privacy first.** Anonymous mode must be a real, equally-functional path, not a degraded one. No screen may display a user's stated interests to anyone but that user.

## Audience and tone

Three personas from `prd.md`, one design center:

| Persona | What the UI must do for them |
| --- | --- |
| **Busy voter** (design center) | Get from landing to a ballot-ready report in under 5 minutes, on a phone, without reading documentation. |
| **Engaged citizen** | Support deep exploration — evidence drawers, follow/alerts, Say/Do gap — without cluttering the busy voter's path. |
| **Journalist / civic educator** | Every claim must be exportable and independently verifiable — citations need to be copy/link-able, not just visually present. |

**Tone:** calm, factual, unexcitable. This is not a campaign tool, a news app, or a social feed — avoid design patterns borrowed from any of those (no "breaking," no infinite scroll of outrage, no partisan red/blue visual language, no gamified scoring that feels like a quiz result). The emotional register is closer to a tax-prep tool or a court-records lookup: trustworthy because it's plain, not because it's reassuring.

## Information architecture

```
Landing page (public, unauthenticated)
  └─ Start → Address entry → Ballot resolved
        └─ Interest Profile onboarding (quick / guided / advanced)
              └─ Ballot view (primary hub after onboarding)
                    ├─ Candidate detail
                    │     ├─ Promise vs. Record
                    │     ├─ Evidence drawer (modal/panel, reachable from many screens)
                    │     └─ Ask the agent (grounded Q&A)
                    ├─ Side-by-side compare (2–4 candidates)
                    └─ Share summary card (candidate-only, non-identifying)
  Account-level screens (reachable from any authenticated screen)
        ├─ Profile editor (tiers, re-tiering, versioning)
        ├─ Followed incumbents + alert preferences
        ├─ Privacy center (export, delete, anonymous-mode toggle, consent record)
        └─ Methodology & corrections (public, also linked from every score)
```

Anonymous mode runs the identical tree with no account-level screens persisted server-side; the profile editor still exists but writes to client storage only.

---

## Part 1 — Landing page

### Job of the page

Get a visitor who has never heard of Informed to understand, in one screen's worth of scrolling, what makes this different from "another voter guide" — and get them into address entry. The landing page's single conversion goal is **starting onboarding**, not sign-up (sign-up can come later, inside the flow).

### Required sections

| Section | Must communicate | Content notes |
| --- | --- | --- |
| **Hero** | The core premise: state your priorities first, see who matches, with evidence. One primary CTA: "See my ballot" or equivalent, leading straight to address entry. | Do not lead with a logo/brand story. Lead with the mechanism. No stock photos of ballot boxes or flags — avoid generic civic-tech visual clichés. |
| **How it works** | The 3-step mechanism: (1) tell us what you care about, (2) we show your ballot scored against *your* priorities, (3) for incumbents, see if their votes matched their promises. | This is the product's actual novelty (voter-first, not candidate-first) — it must be legible without reading paragraphs. |
| **Evidence proof point** | One concrete, real example of the Promise vs. Record mechanic (e.g. a real but anonymized/illustrative promise → vote → verdict chain) to make "cited evidence" tangible rather than abstract. | Must visually show a citation link, not just claim one exists. |
| **Trust section** | Nonpartisan-by-construction, methodology is public, right of response for campaigns, symmetry audit exists. | This is load-bearing, not boilerplate — skepticism about bias is the biggest adoption risk per `prd.md`'s risk table. Link out to the public methodology page. |
| **Privacy proof point** | Anonymous mode exists; profile is encrypted; never sold, never used for targeting. | State plainly, don't bury in a footer link. Voters are being asked to share political opinions — this needs to be visible before they're asked for an address. |
| **Final CTA** | Repeat the entry point. | Same CTA as hero, not a different action. |
| **Footer** | Methodology link, corrections/report-an-error link, privacy policy, about/who's behind this, contact. | Per `prd.md` open question, the advisory panel composition should be linkable here once decided — leave a placeholder slot. |

### States

- **First-time visitor** (default, described above).
- **Returning visitor with a saved/local profile** — landing page should detect this (cookie/local storage check) and offer "Continue where you left off" above the primary CTA, without forcing it.
- **Visitor arriving via a shared candidate summary card** — a distinct landing variant: show the shared candidate's report preview with a clear "this is what your own ballot could look like" CTA, since they didn't arrive by choice. The shared card itself never reveals the sharer's interests (see Part 2, Share summary card).

### Explicit non-goals for this page

- No candidate photos, names, or party logos on the public landing page. Keeping it fully abstract until a voter enters their own address reinforces "nonpartisan by construction" from the first screen.
- No donation, membership, or fundraising CTA of any kind (the product itself does not do campaign finance for the candidate, and must not be confused with one that takes political money).
- No countdown clocks or urgency language ("election in X days!") — calm tone per above.

---

## Part 2 — Core app screens

Each screen below lists: job, required content/data (sourced from `prd.md` / `datamodel.md`), states, and constraints. Wireframe-level detail (exact layout, component choice) is intentionally left to design.

### 2.1 Address entry

**Job:** Resolve a voter to their districts and ballot with the lowest possible friction.

**Content:** Single address/postcode input. On submit, calls `GET /v1/ballot?address=`.

**States:**
- Empty / default.
- Loading (resolving address → district → ballot; target p95 under 800ms per `prd.md`, but design a loading state regardless).
- Success → routes to onboarding (first-time) or ballot view (returning, profile exists).
- Invalid address → inline, specific error ("We couldn't find that address" / "Try including your ZIP code"), never a generic failure.
- No upcoming election / no candidates found for this address → explain why (e.g. off-cycle), don't show an empty ballot silently.

**Constraint:** The address itself is never stored (`prd.md` data rules) — the UI must not imply it will be remembered beyond resolving the district (e.g. no "save this address" option).

### 2.2 Interest Profile onboarding

**Job:** Get a tiered, stance-specific profile with minimal friction, respecting that this is sensitive political-opinion data.

**Three entry paths** (voter chooses):

| Path | Target time | Mechanism |
| --- | --- | --- |
| **Quick** | Under 2 minutes | Pick 3 primary topics + stance each. |
| **Guided** | Variable | Agent interview asking questions only, proposing interests for confirmation (never silently added) — story 4.8, later iteration. |
| **Advanced** | Variable | Full taxonomy browser (~20 topics, ~60 subtopics), searchable, for users who want granular control — story 4.7. |

**Per-interest content, every path:**
- **Topic** (from shared taxonomy, e.g. Healthcare → Prescription drug pricing).
- **Stance**: plain-language outcome the voter wants, chosen from **balanced stance cards** — multiple options spanning the political spectrum, **presented in randomized order every session**. This randomization is a hard constraint, not a nice-to-have: any design that implies a "default" or "most common" stance by position or styling violates nonpartisanship.
- **Tier**: primary / secondary / nice-to-have, with the limits enforced in the UI (1–5 primary, up to 8 secondary, unlimited nice-to-have).
- **Dealbreaker flag** — primary interests only; must be visually distinct from a normal primary interest (this is not a ranking, it's an override behavior shown later in the ballot view).
- **Intensity** (1–3, for tie-breaking) — minor UI element, doesn't need primary visual weight.
- **Optional free-text note** — agent maps this to taxonomy topics and shows the mapping back for the voter to confirm. Never silently stored or scored as-is; the raw text itself is not persisted (`datamodel.md`: "Voter free-text notes are embedded in memory only and never stored").

**Consent gate (blocking):** Before any interest is saved to an account, show the GDPR special-category consent language and the anonymous-mode alternative, side by side, as an actual choice — not a pre-checked box. This blocks profile save, per story 14.2.

**States:**
- Fresh onboarding, no interests yet. No candidate score may render anywhere until at least one primary interest exists — if the user navigates away mid-onboarding, the ballot view must reflect "no primary interest yet," not a blank or zero score.
- Mid-build (some tiers filled, limits approaching — show remaining slots, e.g. "2 of 5 primary picked").
- Limit reached (6th primary attempted) — inline block with explanation, not a silent failure.
- Returning to edit an existing profile (drag-and-drop re-tiering, instant rescore — story 4.5, later iteration).

**Constraint:** Stance cards must never pre-select an option, and the agent (free-text mapping, guided interview) must only ask clarifying questions — any UI copy that reads as a suggestion of what to prioritize is out of bounds.

### 2.3 Ballot view

**Job:** The primary hub. One screen showing every race on the voter's ballot, every candidate scored against their profile, at a glance.

**Content, per race:**
- Race/office name, district.
- Each candidate: name, incumbent flag (if applicable), **alignment score (0–100)**, **confidence badge** (High / Medium / Low / Insufficient data), and **top 3 reasons** behind the score, each reason linking to its evidence.
- **Dealbreaker warning**, shown *above* the score (not blended into it) when a candidate has acted against a voter's dealbreaker interest — this must be impossible to miss, since the whole point is that it overrides the average.

**Default ordering:** Ballot order (the order the race/candidates would appear on an actual ballot). Sorting by score is a **user-initiated action**, never the default, and the UI must not label any sort as "recommended."

**States:**
- Normal (full data, confident scores).
- Partial data (some candidates have Low/Insufficient confidence — don't visually suppress these, show the badge honestly).
- Challenger with no voting record — show their promises-only view indicator inline (clicking through leads to a Promises view rather than a Promise vs. Record view, since there's no record yet).
- Multiple races (federal + state, once Phase 2 lands) — must scale without the page becoming a wall of cards; grouping/collapsing is a design decision but every race must remain reachable without a search.

**Constraint:** No candidate card may use color alone to convey direction/alignment — color must carry a text label too (`prd.md` compare-view rule generalizes to the whole app for accessibility).

### 2.4 Candidate detail

**Job:** Break a single candidate's score down into why, interest by interest.

**Content:**
- Candidate identity (name, party, district, office, incumbent/challenger status, photo if available — see the Source Map artifact for where challenger photos realistically come from, since Ballotpedia is paid and FEC filings don't include one).
- One row per voter interest: the candidate's position/direction on that stance, evidence count, and a link to the underlying votes/promises.
- Entry points to: Promise vs. Record (2.5), Evidence drawer (2.7), Ask the agent (2.8), and (if following is live) a follow toggle.

**States:**
- Incumbent with full voting record.
- Challenger with promises-only data (no votes to show — the per-interest rows show promise-based positions only, clearly labeled as such, not silently blended with vote-based positions the way an incumbent's would be).
- Low-confidence/thin-evidence candidate — same per-interest row format, but confidence must read as visually thinner (not just a smaller badge buried in a corner).

### 2.5 Promise vs. Record

**Job:** For incumbents, show whether they did what they said, promise by promise, with a verdict and citation for each.

**Content, per promise:**
- The promise's quote/text, with its source (campaign site, debate, questionnaire) and date — this is itself a citation and must link to the archived snapshot.
- **Verdict**: Kept / Partial / Broken / No opportunity / Absent / Not testable — each needs a distinct, legible visual treatment; this is a six-state system, not a pass/fail, and the UI must not compress it into fewer visual states.
- Linked roll-call vote(s) that tested the promise (roll-call number, date, question, the candidate's recorded position).
- **Procedural/bundled context notes** where applicable — e.g. "procedural vote" or "part of a 1,000-page omnibus bill" shown as a plain-language explainer beside the relevant vote, not hidden in a tooltip. These notes exist specifically so a "No" on a cloture motion isn't misread as breaking a promise.
- **Incumbent's own explanation** (floor speech/statement), when one exists, linked beside the relevant vote and clearly labeled as the candidate's own statement, not Informed's.
- **QA/review status** — every verdict shows whether it's been human-reviewed; *Broken* verdicts are always human-gated before publish (story 11.2) and the UI must show this status honestly (e.g. a pending-review state should be visible, not hidden until approved).

**Content, product-level, on this screen:**
- **Say/Do gap**: the numeric gap between alignment-from-promises and alignment-from-votes, shown as its own distinct figure with a plain-language explanation of what it means — not folded into the main score.

**States:**
- Grouped by the voter's own interests first (their primary interests' promises surface before others).
- A promise with no mapped vote yet (no opportunity) — must read as categorically different from "broken," both verdicts being visually distinct is the whole point of the six-state system above.
- Challenger view variant: no verdicts exist; show promises list only, plus a link to any prior-office record if one exists (story 9.6), clearly separated from the current race.

### 2.6 Side-by-side compare

**Job:** Let a voter compare 2–4 candidates in one race, row-aligned by interest.

**Content:** Same per-interest rows as candidate detail, but columns per candidate, same interest rows across all candidates so differences are scannable. "Why" links persist per cell.

**Constraint:** Direction must be color **and** label (again, not color alone). No candidate column may be visually emphasized as "winning" — this is a comparison, not a leaderboard.

### 2.7 Evidence drawer

**Job:** A reusable panel/modal, reachable from ballot view, candidate detail, compare, and Promise vs. Record — opens any vote or promise behind a score and shows its primary source.

**Content:** Roll-call number, date, full question text, the candidate's recorded position, bill link, and a link to the archived source document (served via presigned URL per `datamodel.md`). For a promise: the quote span, source type, capture date, and a link to the archived snapshot.

**Constraint:** This component must be designed once and reused everywhere a citation appears — it is the single mechanism that makes "evidence over opinion" real rather than aspirational, so consistency across every entry point matters more than any individual screen's layout.

### 2.8 Ask the agent (grounded Q&A)

**Job:** Let a voter ask free-text questions about a score and get a cited answer, or an honest "no evidence found."

**Content:** Chat-style input; responses must visually distinguish an answer that cites evidence (with inline/footnote-style citations linking into the Evidence drawer) from a refusal (e.g. "Who should I vote for?" → the agent explains it doesn't recommend, and offers the alignment comparison instead — this refusal needs its own calm, non-apologetic visual treatment, not an error state).

**States:**
- Grounded answer with citation(s).
- "No evidence found" — must look categorically different from a grounded answer, not just shorter.
- Refusal to recommend — a designed state, not a fallback.
- Session memory notice — since Q&A memory is retained only 1 day (and not at all in anonymous mode), the UI should make this legible (e.g. "this conversation isn't saved" in anonymous mode) rather than silent.
- Flag an answer — a lightweight affordance on every agent response (story 13.6).

### 2.9 Share summary card

**Job:** Let a voter share a candidate's report without exposing their own interests.

**Content:** Candidate-only data — score context must be generic enough not to leak the sharer's profile (e.g. show the candidate's verdict counts/record, not "scored 82% for you"). Test case: a viewer of the shared card should learn nothing about the sharer's political priorities.

**Constraint:** This is a hard privacy boundary (story 14.6 acceptance criteria: "a test asserts no profile fields leak") — treat any design that includes personalized score language as a bug to flag back to product, not a style choice.

### 2.10 Account-level: Profile editor, Follow/alerts, Privacy center, Methodology & corrections

Lighter-weight utility screens; each needs:

- **Profile editor**: drag-and-drop re-tiering with instant rescore (under 300ms target), profile versioning (see how scores changed when priorities changed), export and one-action delete (must be genuinely one action, not buried in nested settings).
- **Follow/alerts**: follow an incumbent, notification preference (email/push/none), one-click unsubscribe.
- **Privacy center**: anonymous-mode toggle/explanation, consent record, export, delete — this is where "privacy first" has to be operable, not just stated on the landing page.
- **Methodology & corrections**: public methodology page with version/changelog, and a correction-submission form reachable from *every* claim in the app (not just from this hub) — design the correction entry point once, surface it everywhere a verdict or score appears.

---

## Cross-cutting UI requirements

**Accessibility:** WCAG 2.2 AA (story 7.8) — this is a Must, not aspirational. Specifically: no information conveyed by color alone anywhere in the app (recurring theme above), full keyboard operability including drag-and-drop re-tiering, screen-reader-tested primary journey.

**Responsive / PWA:** Installable PWA (story 7.7), mobile-first given the busy-voter design center — the ballot view and onboarding in particular must work well on a phone screen, not just scale down from desktop.

**Confidence as a visual system, not a label:** Because every score, position, and verdict carries a confidence/QA dimension, design a **consistent visual language for evidence strength** once (e.g. a treatment that reads as "thin" vs. "solid" at a glance) and apply it everywhere a score, badge, or verdict appears — ballot view, candidate detail, compare, Promise vs. Record.

**No party-color system by default.** If a visual system for "direction" or "party" is needed, it must be designed to not read as red/blue American political shorthand, and must apply identically regardless of party (symmetry audit's spirit extends to visual design, not just scoring math).

## Open questions for design to flag back to product

- Exact visual treatment for the six-verdict system (2.5) — product has defined the states, not their visual hierarchy.
- Whether party affiliation should be shown at all by default, or be an opt-in reveal (tension between "useful context" and "avoid priming").
- Final product name and domain (`prd.md` open question) — landing page copy in this doc uses generic placeholders ("Informed") pending that decision.
- Advisory panel composition/bios for the landing page's trust section — placeholder until decided.

## Appendix — screen-to-API map

| Screen | Primary API(s) |
| --- | --- |
| Address entry | `GET /v1/ballot?address=` |
| Interest Profile onboarding | `POST /v1/profiles` |
| Ballot view | `GET /v1/ballot?address=`, `GET /v1/candidates/{id}/alignment?profile_id=` |
| Candidate detail | `GET /v1/candidates/{id}/alignment?profile_id=` |
| Promise vs. Record | `GET /v1/candidates/{id}/promise-record` |
| Side-by-side compare | `GET /v1/candidates/{id}/alignment?profile_id=` (per candidate) |
| Ask the agent | `POST /v1/agent/ask` |
| Methodology & corrections | `POST /v1/corrections` |

See `prd.md` → "Core API" and `datamodel.md` → "ZeroDB data model" for full field-level detail behind each endpoint.
