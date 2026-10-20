/**
 * The whole-ballot view: every race with its candidates, scores and
 * confidence (story 7.1).
 *
 * "As a voter, I want each race with its candidates, alignment scores and
 * confidence, so that I can see my whole ballot at once." Reads
 * `/v1/ballot/cards` for each race `BallotPage`'s own `/v1/ballot` lookup
 * already resolved, through `app.ballot.cards_service`'s single-table read
 * over story 7.2's `candidate_cards` — see `src/api/ballotCards.ts` for the
 * full contract.
 *
 * Deliberately NOT the personalized alignment score (`GET
 * /v1/candidates/{id}/alignment?profile_id=`, story 6.4): this view takes no
 * voter profile, so `say_do_gap` — the one alignment-shaped number
 * `candidate_cards` carries — is the same for every voter who opens it. A
 * confidence badge accompanies it exactly as prd.md's ballot view describes:
 * "each race lists candidates with ... a confidence badge". When nothing has
 * been computed for a candidate yet, this renders an honest "not available
 * yet" rather than a fabricated score — real live data has no verdicts at
 * all yet (epic 11 is unbuilt; see `app.ballot.cards`'s module docstring),
 * so an "insufficient"/none state is the common case today, and that is a
 * true reflection of the data, not a bug in this view.
 *
 * Ballot order by default, and only by a voter's own choice otherwise
 * (story 7.6): races render in the order `BallotPage` already passes them
 * (the API's own chamber-then-name order), and within a race, candidates
 * render in the stable name order `/v1/ballot/cards` returns them, unless
 * the voter has turned on "Sort by alignment score" for that race — a
 * per-race checkbox, off by default, see `SortToggle` below.
 *
 * "As a voter, I want to sort candidates by score if I choose, so that I
 * control how results are presented." This is deliberately NOT this view
 * re-sorting on its own judgment: prd.md's non-negotiable is that the app
 * never recommends who to vote for, and a screen that silently reordered
 * candidates by score would be doing exactly that, quietly, by default. The
 * difference that keeps this story on the right side of that line is that
 * nothing is reordered until the voter asks — `sortCardsByScore` runs only
 * inside `RaceView`, gated on `sortByScore` state nothing sets but a click
 * on this race's own checkbox — and the control is never captioned
 * "recommended", "best match", "top pick" or any other endorsement language;
 * see `SortToggle`'s own label.
 *
 * "View details" (story 7.3): each candidate with a `candidacy_id` gets a
 * button to this candidate's detail page
 * (`src/candidate/CandidateDetailPage.tsx`), which breaks their score down
 * interest by interest. A callback prop rather than a link, because there is
 * still no routing library in this app (see `App.tsx`'s own docstring) — the
 * screen that owns navigation state (`BallotPage`) decides what happens when
 * a voter asks to see one candidate's detail, the same posture `ShareButton`
 * already takes for a different per-candidate action on this same row.
 */
import { useQuery } from '@tanstack/react-query';
import { useId, useState } from 'react';

import type { Race } from '../api/ballot';
import {
  type CandidateCardView,
  type ConfidenceBadge,
  electionIdFor,
  fetchBallotCards,
} from '../api/ballotCards';

/** Human-readable label for each confidence badge, matching prd.md Feature 3. */
const BADGE_LABEL: Record<ConfidenceBadge, string> = {
  high: 'High confidence',
  medium: 'Medium confidence',
  low: 'Low confidence',
  insufficient: 'Insufficient confidence',
};

/**
 * Every race on this voter's ballot, each with its candidates' cards.
 *
 * One race a component, so a slow or failed card fetch for one race never
 * blocks another — a voter with a House race loaded and a stalled Senate
 * fetch still sees the House race.
 */
export function BallotView({
  races,
  onViewDetails,
}: {
  races: Race[];
  /**
   * Called with a candidacy id when a voter asks to see that candidate's
   * detail page. Optional: a caller that has not wired up a detail screen
   * yet (or a BDD fixture proving only the card list) simply omits it, and
   * no button is rendered — no per-candidate affordance has to exist before
   * the view it points at does.
   */
  onViewDetails?: (candidacyId: string) => void;
}) {
  if (races.length === 0) return null;

  return (
    <section aria-label="Your ballot">
      <h2>Your ballot</h2>
      {races.map((race) => (
        <RaceView key={race.id} race={race} onViewDetails={onViewDetails} />
      ))}
    </section>
  );
}

function RaceView({
  race,
  onViewDetails,
}: {
  race: Race;
  onViewDetails?: (candidacyId: string) => void;
}) {
  const headingId = useId();
  const sortToggleId = useId();
  const electionId = electionIdFor(race);

  // Off by default (story 7.6's "default order is ballot order"), and
  // local to this one race: a voter turning on sorting for a House race
  // they're studying closely has no bearing on how a Senate race they
  // haven't looked at yet is presented.
  const [sortByScore, setSortByScore] = useState(false);

  const query = useQuery({
    queryKey: ['ballot-cards', electionId, race.district_id],
    queryFn: ({ signal }) =>
      fetchBallotCards({ electionId, districtId: race.district_id }, signal),
  });

  // A card keyed by candidacy id, so a candidate who has no card yet (no
  // `positions.recomputed`/`verdict.published` event has touched them) still
  // renders — from `race.candidates`, the data this view already has — with
  // an honest "not available yet" rather than being silently dropped from
  // the ballot.
  const cardsByCandidacy = new Map<string, CandidateCardView>(
    (query.data?.cards ?? []).map((card) => [card.candidacy_id, card]),
  );

  // Ballot order unless the voter has turned sorting on for this race (see
  // `sortByScore` above and this module's docstring on why that gate is
  // what keeps this from being a silent recommendation).
  const orderedCandidates = sortByScore
    ? sortCardsByScore(
        race.candidates,
        (candidate) =>
          (candidate.candidacy_id ? cardsByCandidacy.get(candidate.candidacy_id) : undefined)?.say_do_gap ??
          null,
      )
    : race.candidates;

  return (
    <article aria-labelledby={headingId}>
      <h3 id={headingId}>{race.office_name}</h3>
      {query.isSuccess ? (
        <p>
          <label htmlFor={sortToggleId}>
            <input
              id={sortToggleId}
              type="checkbox"
              checked={sortByScore}
              onChange={(event) => setSortByScore(event.target.checked)}
            />
            {' '}Sort by alignment score
          </label>
        </p>
      ) : null}
      {query.isPending ? <p>Loading candidates…</p> : null}
      {query.isError ? (
        <p role="alert">We could not load this race&apos;s scores just now. Please try again.</p>
      ) : null}
      {query.isSuccess ? (
        <ul aria-labelledby={headingId}>
          {orderedCandidates.map((candidate) => (
            <li key={candidate.person_id}>
              <CandidateRow
                candidate={candidate}
                card={candidate.candidacy_id ? cardsByCandidacy.get(candidate.candidacy_id) : undefined}
                onViewDetails={onViewDetails}
              />
            </li>
          ))}
        </ul>
      ) : null}
    </article>
  );
}

function CandidateRow({
  candidate,
  card,
  onViewDetails,
}: {
  candidate: Race['candidates'][number];
  card: CandidateCardView | undefined;
  onViewDetails?: (candidacyId: string) => void;
}) {
  return (
    <>
      <span>{candidate.name}</span>
      {candidate.party ? <span> ({candidate.party})</span> : null}
      {' — '}
      <IncumbencyLabel isIncumbent={candidate.is_incumbent} />
      {card ? <CardScore card={card} /> : <span>Scores not available yet.</span>}
      {onViewDetails && candidate.candidacy_id ? (
        <>
          {' '}
          {/* Visible text stays generic ("View details"), with the candidate's
              name only in the accessible name (`aria-label`) rather than in
              both: `ballot_view.steps.ts`'s existing `getByText('Rep.
              Synthetic Example')` locator (story 7.1, already merged) would
              otherwise ambiguously match this button too, since Playwright's
              text matcher is substring by default. */}
          <button
            type="button"
            aria-label={`View details for ${candidate.name}`}
            onClick={() => onViewDetails(candidate.candidacy_id as string)}
          >
            View details
          </button>
        </>
      ) : null}
    </>
  );
}

function IncumbencyLabel({ isIncumbent }: { isIncumbent: boolean }) {
  return <strong>{isIncumbent ? 'Incumbent' : 'Challenger'}</strong>;
}

/**
 * A card's say/do gap and confidence badge, or an honest "not available yet"
 * when nothing has been computed. Never invents a number: `say_do_gap` and
 * `positions_summary` are both `null` until a real rebuild has filled them
 * (`app.ballot.cards`), which today — on real live data — is every
 * candidate, because epic 11's verdict engine has not shipped.
 */
function CardScore({ card }: { card: CandidateCardView }) {
  const hasEvidence = (card.positions_summary?.length ?? 0) > 0;
  return (
    <>
      {' '}
      <span>{BADGE_LABEL[card.confidence_badge]}</span>
      {hasEvidence && card.say_do_gap !== null ? (
        <span>, say/do gap {Math.round(card.say_do_gap * 100)}%</span>
      ) : (
        <span>. Scores not available yet.</span>
      )}
    </>
  );
}

/**
 * Stable sort by descending score, with every `null`-scored item pushed
 * after every scored one — never dropped, and never silently treated as a
 * score of zero (story 7.6's own honesty requirement: a candidate with
 * `say_do_gap: null` has not been measured, which is not the same claim as
 * "measured and found worst").
 *
 * Generic over the item and its score accessor rather than hard-coded to
 * `Race['candidates'][number]`, so this one function is directly unit
 * testable against a plain fixture (`BallotView.test.tsx`) without having to
 * construct a full `CandidateCardView`.
 *
 * `Array#sort` is stable per the ECMAScript spec (guaranteed since ES2019,
 * which every browser and Node version this app targets implements), so two
 * items with the same score — or two `null`-scored items — keep their
 * original relative order rather than being shuffled.
 */
export function sortCardsByScore<T>(items: T[], scoreOf: (item: T) => number | null): T[] {
  const scored: T[] = [];
  const unscored: T[] = [];
  for (const item of items) {
    (scoreOf(item) === null ? unscored : scored).push(item);
  }
  scored.sort((a, b) => (scoreOf(b) as number) - (scoreOf(a) as number));
  return [...scored, ...unscored];
}
