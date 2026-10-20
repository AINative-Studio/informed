# Informed methodology changelog

This is the plain-language record of how Informed scores candidates, and of
every change to it. Each entry is one **methodology version**. The machine-
readable form of the same information is served publicly, with no account
needed, at:

- `GET /v1/methodology` — the version in force right now
- `GET /v1/methodology/versions` — every version ever published, newest first
- `GET /v1/methodology/{version}` — one version, including superseded ones

A superseded version stays readable at its own URL forever. That is the point:
a verdict or score computed under `2026.1` can show you the weights it was
*actually* computed with, even years after a newer methodology has replaced
them. We do not rewrite history in place.

## How to read a version

Each version publishes its weights in named groups:

| Group | What it weights |
| --- | --- |
| `tiers` | How much each of your interest tiers counts toward a match |
| `evidence_blend` | How a candidate's voting record is balanced against their stated promises |
| `vote_types` | How much a final-passage vote counts versus a procedural one |
| `badge_thresholds` | How much evidence a score needs before it is badged High, Medium or Low |

A group a version does not carry is listed in `weights_not_yet_published`.
We name those explicitly rather than omitting them or filling them with
zeroes, because a page that quietly left out half the methodology would read
as though it were the whole thing.

---

<a id="20263"></a>

## 2026.3 — confidence badge thresholds

**Status:** in force.
**Published:** 2026-10-19.
**Replaces:** [2026.2](#20262), which stays readable at its own URL showing
exactly the weights it covered.

This version adds one group, `badge_thresholds`, and changes nothing else. The
tier weights, the evidence blend and the vote-type weights are all carried
forward from `2026.2` unaltered.

### What a confidence badge is for

Beside every alignment score, Informed shows how much evidence that score
rests on. The score answers "how closely does this candidate's record match
what you said you care about". The badge answers a different and equally
important question — **should you trust that number at all?**

A candidate whose record on your priorities comes down to a single recorded
vote can still score 100. That is arithmetically correct and would be
misleading on its own, so the badge says so.

| Badge | Relevant records behind the score | In plain words |
| --- | --- | --- |
| **High** | 10 or more | A real record. Several occasions, not one episode. |
| **Medium** | 4 to 9 | Some record. More than one occasion, well short of a pattern. |
| **Low** | 1 to 3 | Thin. Treat the number as a hint, not a finding. |
| **Insufficient data** | 0 | No recorded evidence on your priorities. No score is shown. |

"Relevant records" means recorded votes — and, once the evidence blend is in
use, sponsorships and stated positions — on the stances **you** picked. A
candidate's long record on an issue you did not choose does not raise your
badge, because it is not evidence about your priorities.

### Where these numbers come from

The **High** threshold of 10 is published in the product requirements and is
not a judgment made here.

**Medium at 4** and **Low at 1** are judgment calls, and we would rather say so
than present them as derived. The reasoning:

- **Medium at 4.** A member's votes on one stance within a single legislative
  episode commonly run to three or four — a final passage, an amendment, a
  cloture motion, a procedural vote. Four is roughly the point at which a
  reader could plausibly be looking at more than one occasion rather than one
  bill's worth of procedure.
- **Low at 1.** Any recorded evidence at all. It starts at one rather than zero
  because with zero evidence there is no score to badge — that case is
  *Insufficient data*, and the two must not overlap.

If these prove wrong in use, the fix is a new version on this page, not a quiet
edit. A score badged High under `2026.3` keeps citing `2026.3`.

### What the badge is *not*

- It is **not** part of the score. Changing a threshold cannot move a single
  alignment number; the two are computed separately and deliberately so.
- It is **not** a judgment about the candidate. A Low badge means Informed has
  thin evidence, not that the candidate has a thin record — most often it means
  the issue you chose has not come to a recorded vote.

---

<a id="20262"></a>

## 2026.2 — evidence blend and vote types

**Status:** superseded by [2026.3](#20263) on 2026-10-19. Still readable at its
own URL, and anything scored under it still cites it.
**Published:** 2026-10-17.
**Replaces:** [2026.1](#20261), which stays readable at its own URL showing
exactly the weights it covered.

`2026.1` published one weight group of the three and said so. This version
fills in the other two. Nothing about the interest tier weights changed.

### Interest tier weights (`tiers`) — unchanged

| Tier | Weight |
| --- | --- |
| `primary` | **3.0** |
| `secondary` | **1.5** |
| `nice_to_have` | **0.5** |

### Evidence blend (`evidence_blend`) — new

For an incumbent, we have three kinds of evidence about where they stand on an
issue. This is how much each counts:

| Evidence | Weight | Why |
| --- | --- | --- |
| `votes` | **70%** | What they actually did on the floor. The strongest evidence there is. |
| `sponsorships` | **15%** | What they chose to put their name on. Real, but it is not a vote. |
| `statements` | **15%** | What they have said their position is. Counted, but it cannot outweigh the record. |

The three add up to 100%. You can switch your own view to "votes only" at any
time, which sets the other two to zero for your score and nobody else's.

A challenger has no voting record, so their position comes from their promises
alone. That is a difference in the evidence available, not a different rule:
there is one scoring codebase and it does not know which party anyone is in.

### Vote type weights (`vote_types`) — new

Not every recorded vote says the same amount about where someone stands. A
vote on whether to *finish debating* a bill is not a vote on the bill. So each
vote is classified by type and weighted accordingly, relative to a
final-passage vote counting **1.0**:

| Vote type | Weight | What it is |
| --- | --- | --- |
| `final_passage` | **1.0** | The vote on whether the bill becomes law. The clearest statement of a position, and the benchmark everything else is measured against. |
| `amendment` | **0.7** | A vote on specific text in the bill. Often the most revealing vote on one particular promise — this is how a "Partial" verdict happens: supported the bill, voted for an amendment that weakened it. |
| `motion_to_recommit` | **0.5** | The minority's last attempt to change a bill before passage. Real policy content, but voted close to party lines often enough that it says less about an individual. |
| `cloture` | **0.4** | Whether to end debate and proceed. About process, not policy. Someone can vote against cloture and for the bill, and that is not a broken promise. |
| `procedural` | **0.2** | Rules, previous question, adjournment. Floor management. |
| `other` | **0.2** | A vote our classifier could not confidently type. Weighted low precisely because we are not sure what it was. |

Two things worth stating plainly about these numbers:

- **Nothing is weighted zero.** A procedural vote counts for little, but it
  counts. We show you every recorded vote, and showing a vote we did not count
  at all would be misleading.
- **The ordering is the substance; the exact figures are a judgment call.**
  That procedural and cloture votes weigh less than final passage is a
  commitment. That cloture is specifically 0.4 rather than 0.35 is our
  considered choice, published here so you can disagree with it. If we change
  it, it will be a new version on this page, and this one will stay exactly as
  it is.

**Bundled bills** are handled separately from this table. A vote on a
1,000-page omnibus is flagged as *bundled* and discounted on top of its vote
type weight, because a yes on such a bill is not a yes to every clause in it.

### What this version still does not publish

Nothing. All three documented weight groups are now in force, so
`weights_not_yet_published` is empty for the first time.

---

<a id="20261"></a>

## 2026.1 — the first published methodology

**Status:** superseded by [2026.2](#20262) on 2026-10-17. Still readable at
`/v1/methodology/2026.1`, and anything scored under it still cites it.
**Published:** 2026-10-16.

> This heading also keeps its original GitHub-generated anchor
> (`#20261--the-first-published-methodology`), because story 4.2's live
> `2026.1` row stores that longer URL and a stored citation must not rot. New
> versions use the short `#{version without the dot}` form, which story 6.1's
> publisher can generate from the version id alone — a link built from a
> heading's prose cannot be, which is how the mismatch arose.

### Interest tier weights (`tiers`)

When you build an interest profile, you sort what you care about into three
tiers. Those tiers are the only thing in this version that carries a weight:

| Tier | Weight | In plain words |
| --- | --- | --- |
| `primary` | **3.0** | The issues you told us matter most. Each counts three times as much as a nice-to-have. |
| `secondary` | **1.5** | Issues you care about, but not decisively. Half the pull of a primary. |
| `nice_to_have` | **0.5** | Issues you would like to see addressed. The lightest touch on a match. |

These three numbers are the same for every candidate and every party. There is
one scoring codebase and no party-specific rule anywhere in it.

### What this version does *not* yet publish

Every weight group except `tiers` is deliberately absent from `2026.1`, and the
API reports them in `weights_not_yet_published`:

- **`evidence_blend`** — how a voting record is weighed against a stated
  promise. The component that would use it (the alignment scorer) is not
  built yet.
- **`vote_types`** — how a final-passage vote is weighed against a cloture
  vote, a motion to recommit or another procedural vote. Same reason.
- **`badge_thresholds`** — how much evidence a score needs before it is badged
  High, Medium or Low. Added as a group only in [2026.3](#20263); when `2026.1`
  was published the concept had no stored home at all.

We are stating this plainly rather than publishing placeholder numbers,
because a published weight implies something is being computed with it, and
nothing is. When those groups go into force, they will appear in a new
version here with its own entry, and `2026.1` will remain readable at its own
URL showing exactly what it did and did not cover.

> That is what happened: `evidence_blend` and `vote_types` went into force in
> [2026.2](#20262) on 2026-10-17, and `badge_thresholds` in
> [2026.3](#20263) on 2026-10-19. The paragraph
> above is left exactly as it was written, because it describes what `2026.1`
> covered and that has not changed — a verdict or score citing `2026.1` is
> citing a methodology that weighted tiers and nothing else.

### What is deliberately *not* weighted, in this or any version

- **Party.** No version of this methodology will ever carry a party weight.
- **Who we think you should vote for.** Informed does not make a
  recommendation. It reports how closely each candidate's record matches the
  priorities *you* stated, with a link to the primary source behind every
  claim.
- **Your profile, anywhere outside your own score.** Your stated priorities
  are political-opinion data. They are encrypted per user, never used for
  targeting, never shared with a campaign or a party, and never published
  here or anywhere else.

---

## Changing this page

A methodology change is not a code change that happens to alter a number. It
is a new row in `methodology_versions` with its own id, its own
`published_at`, and its own entry on this page — and the version it replaces
stays exactly as it was. Anything already scored or verdicted under the old
version keeps citing the old version.
