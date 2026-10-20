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
 * Ballot order, never ranked: races render in the order `BallotPage` already
 * passes them (the API's own chamber-then-name order), and within a race,
 * candidates render in the stable name order `/v1/ballot/cards` returns
 * them. Nothing here re-sorts by score — prd.md: the app never recommends
 * who to vote for.
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
import { useId } from 'react';

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
  const electionId = electionIdFor(race);

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

  return (
    <article aria-labelledby={headingId}>
      <h3 id={headingId}>{race.office_name}</h3>
      {query.isPending ? <p>Loading candidates…</p> : null}
      {query.isError ? (
        <p role="alert">We could not load this race&apos;s scores just now. Please try again.</p>
      ) : null}
      {query.isSuccess ? (
        <ul aria-labelledby={headingId}>
          {race.candidates.map((candidate) => (
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
