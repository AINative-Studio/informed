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

## 2026.1 — the first published methodology

**Status:** in force.
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
