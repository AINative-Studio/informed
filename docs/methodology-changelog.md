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

A group a version does not carry is listed in `weights_not_yet_published`.
We name those explicitly rather than omitting them or filling them with
zeroes, because a page that quietly left out half the methodology would read
as though it were the whole thing.

---

## 2026.2 — evidence blend and vote types

**Status:** in force.
**Published:** 2026-10-17.
**Replaces:** [2026.1](#20261--the-first-published-methodology), which stays
readable at its own URL showing exactly the weights it covered.

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

## 2026.1 — the first published methodology

**Status:** superseded by [2026.2](#20262--evidence-blend-and-vote-types) on
2026-10-17. Still readable at `/v1/methodology/2026.1`, and anything scored
under it still cites it.
**Published:** 2026-10-16.

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

Two of the three documented weight groups are deliberately absent from
`2026.1`, and the API reports them in `weights_not_yet_published`:

- **`evidence_blend`** — how a voting record is weighed against a stated
  promise. The component that would use it (the alignment scorer) is not
  built yet.
- **`vote_types`** — how a final-passage vote is weighed against a cloture
  vote, a motion to recommit or another procedural vote. Same reason.

We are stating this plainly rather than publishing placeholder numbers,
because a published weight implies something is being computed with it, and
nothing is. When those groups go into force, they will appear in a new
version here with its own entry, and `2026.1` will remain readable at its own
URL showing exactly what it did and did not cover.

> That is what happened: both groups went into force in
> [2026.2](#20262--evidence-blend-and-vote-types) on 2026-10-17. The paragraph
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
