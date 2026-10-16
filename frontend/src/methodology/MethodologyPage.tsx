/**
 * The public methodology page (story 12.2).
 *
 * prd.md: "Methodology page and changelog; every verdict links to the
 * methodology version that produced it." This is the page half.
 *
 * Who it is for: a voter who wants to know why a candidate matched them, and a
 * journalist who wants to check whether the method is as neutral as the
 * product claims. The second reader is the demanding one — they arrive
 * skeptical, they have no account, and they are looking for the numbers and a
 * way to verify them. So the page does three things in order:
 *
 *   1. names the version in force, which is what a verdict cites;
 *   2. shows every weight actually in force, as a table with the real numbers;
 *   3. says plainly which documented parts are NOT published yet, and links
 *      the changelog so a reader can check what changed and when.
 *
 * Point 3 is the one that is easy to skip and most worth keeping. The backend
 * reports `weights_not_yet_published` because the alignment scorer (epic 6)
 * does not exist, so `evidence_blend` and `vote_types` carry no numbers. A
 * page that quietly rendered only the tier table would read as the complete
 * methodology. Publishing the gap is the honest version, and it is the one a
 * skeptical reader can actually trust.
 *
 * Deliberately plain, like `BallotPage`: ui-prd.md holds the design system and
 * applying it here is a later story. Everything is semantic HTML — a real
 * `<table>` with header cells, so the weights are readable by a screen reader
 * as a table rather than as a run of numbers.
 *
 * Two things this page must not do, both from prd.md's non-negotiables:
 *
 *   * **Recommend anyone.** It explains the method and names no candidate.
 *   * **Need an account.** No sign-in, no profile, no address. `fetchMethodology`
 *     sends no credentials, and nothing here reads voter state.
 */
import { useQuery } from '@tanstack/react-query';

import {
  type PublishedMethodology,
  TIER_DISPLAY_ORDER,
  fetchMethodology,
  weightGroupLabel,
} from '../api/methodology';

export function MethodologyPage() {
  const query = useQuery({
    queryKey: ['methodology'],
    queryFn: ({ signal }) => fetchMethodology(signal),
  });

  return (
    <section aria-label="How Informed scores candidates">
      <h2>How we score</h2>
      <p>
        Informed compares what you told us you care about against what each candidate has
        actually done, and shows the match. The same weights below are applied to every
        candidate and every party — there is one scoring codebase and no party-specific rule
        in it.
      </p>

      {query.isPending ? <p>Loading the published methodology…</p> : null}
      {query.isError ? <p role="alert">{messageFrom(query.error)}</p> : null}
      {query.isSuccess ? <Methodology methodology={query.data} /> : null}
    </section>
  );
}

function Methodology({ methodology }: { methodology: PublishedMethodology }) {
  const tiers = methodology.weights.tiers ?? {};

  return (
    <>
      <h3>Methodology version {methodology.version}</h3>
      <p>
        Published <time dateTime={methodology.published_at}>{formatDate(methodology.published_at)}</time>
        {methodology.is_current ? ' and currently in force.' : ' — since superseded.'}
      </p>
      <p>
        {/* The citation path, shown rather than only linked: this is the URL a
            verdict will cite, and a journalist checking the method wants to be
            able to see and fetch it, not just click it. */}
        Permanent address for this version: <code>{methodology.path}</code>
      </p>

      <h4>Interest tier weights</h4>
      <p>
        When you build a profile you sort issues into three tiers. The tier sets how much that
        issue counts toward a match.
      </p>
      <table aria-label="Interest tier weights">
        <thead>
          <tr>
            <th scope="col">Tier</th>
            <th scope="col">Weight</th>
            <th scope="col">What it means</th>
          </tr>
        </thead>
        <tbody>
          {orderedTiers(tiers).map(([key, weight]) => (
            <tr key={key}>
              <th scope="row">{tierLabel(key)}</th>
              <td>{weight}</td>
              <td>{TIER_EXPLANATIONS[key] ?? ''}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {methodology.weights_not_yet_published.length > 0 ? (
        <NotYetPublished groups={methodology.weights_not_yet_published} />
      ) : null}

      <h4>Changelog</h4>
      <p>
        Every change to this methodology is published as a new version with its own entry. An
        older version stays readable at its own address, so a result worked out under it can
        still show the weights it was actually worked out with — we do not rewrite history.
      </p>
      <p>
        <a href={methodology.changelog_url} rel="noreferrer">
          Read the methodology changelog
        </a>
      </p>
    </>
  );
}

/**
 * The documented weight groups this version does not carry.
 *
 * Rendered as plain prose rather than an empty row in the table above: an
 * empty row invites the reader to think the weight is zero, and zero is a
 * published decision. "Not published yet" is a different statement.
 */
function NotYetPublished({ groups }: { groups: string[] }) {
  return (
    <>
      <h4>What is not published yet</h4>
      <p>
        This version of the methodology does not yet set {groups.length === 1 ? 'one part' : 'these parts'}{' '}
        of the scoring. We are naming the gap rather than filling it with a placeholder number,
        because a published weight would imply something is being worked out with it:
      </p>
      <ul aria-label="Parts of the methodology not published yet">
        {groups.map((group) => (
          <li key={group}>{weightGroupLabel(group)}</li>
        ))}
      </ul>
      <p>
        When they are set, they will appear here as a new version, and this version will stay
        readable at its own address exactly as it is.
      </p>
    </>
  );
}

/**
 * Every published tier, known ones in their declared order and anything else
 * after them.
 *
 * The "anything else" half matters on this page specifically. A naive render
 * walks `TIER_DISPLAY_ORDER` and shows the tiers it knows about — which
 * silently HIDES a tier a future methodology adds, on the one page whose whole
 * claim is that it shows the complete set of weights in force. Dropping a real
 * published weight because this component has not been taught its name yet
 * would be exactly the kind of quiet incompleteness this story exists to
 * prevent, so an unknown key is rendered with its raw name rather than
 * omitted.
 */
function orderedTiers(tiers: Record<string, number>): Array<[string, number]> {
  const known = TIER_DISPLAY_ORDER.map((tier) => tier.key);
  const extra = Object.keys(tiers).filter((key) => !known.includes(key));
  return [...known, ...extra]
    .filter((key) => key in tiers)
    .map((key) => [key, tiers[key]] as [string, number]);
}

/** Display name for a tier key, falling back to the key itself. */
function tierLabel(key: string): string {
  return TIER_DISPLAY_ORDER.find((tier) => tier.key === key)?.label ?? key;
}

/** Plain-words explanation per tier key, for the table's third column. */
const TIER_EXPLANATIONS: Record<string, string> = {
  primary: 'The issues you said matter most. Each counts three times a nice-to-have.',
  secondary: 'Issues you care about, but not decisively. Half the pull of a primary.',
  nice_to_have: 'Issues you would like addressed. The lightest touch on a match.',
};

/** `2026-10-16T21:07:34Z` as `16 October 2026`, falling back to the raw value. */
function formatDate(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.valueOf())) return iso;
  return parsed.toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  });
}

/** A reader-facing message for whatever the query threw. */
function messageFrom(error: unknown): string {
  if (error instanceof Error && error.name === 'MethodologyError') return error.message;
  return 'We could not reach Informed just now. Please check your connection and try again.';
}
