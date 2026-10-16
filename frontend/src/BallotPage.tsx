/**
 * The address-entry screen (story 1.5).
 *
 * The UI half of the walking skeleton: a voter types an address, the app calls
 * `/v1/ballot` through TanStack Query, and the House member and two senators
 * that came back out of ZeroDB are rendered.
 *
 * Deliberately plain. ui-prd.md holds the design system, and epic 2's ballot
 * view is where it gets applied; everything here is semantic HTML whose job is
 * to prove the data flows and to be accessible while it does — the two lists
 * carry accessible names so a screen reader (and the BDD suite) can tell the
 * chambers apart.
 *
 * Two things this screen must not do, both from prd.md's non-negotiables:
 *
 *   * **Recommend anyone.** No ordering by preference, no "best match", no
 *     endorsement. Officeholders arrive in the stable chamber-then-name order
 *     the API chose and are rendered in it.
 *   * **Keep the address.** It is submitted as a query parameter and lives only
 *     in the input the voter can edit. Nothing writes it to storage, and the
 *     results region never repeats it back.
 */
import { useQuery } from '@tanstack/react-query';
import { type FormEvent, useId, useState } from 'react';

import {
  type Ballot,
  HOUSE_BODY_ID,
  type Officeholder,
  SENATE_BODY_ID,
  fetchBallot,
} from './api/ballot';
import { ProfileSaveError, followPerson, unfollowPerson } from './api/profiles';

export function BallotPage() {
  const inputId = useId();
  /** What the voter is typing. */
  const [address, setAddress] = useState('');
  /**
   * The address that was actually submitted, and the query key. Separate from
   * `address` so a keystroke does not fire a request — the voter submits when
   * they are ready.
   */
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ['ballot', submitted],
    // `enabled` below guarantees `submitted` is non-null when this runs.
    queryFn: ({ signal }) => fetchBallot(submitted as string, signal),
    enabled: submitted !== null,
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = address.trim();
    if (!trimmed) {
      // Caught here rather than by the API: an empty submit is not worth a
      // round trip, and the voter gets the answer immediately.
      setValidationError('Please enter your address.');
      setSubmitted(null);
      return;
    }
    setValidationError(null);
    setSubmitted(trimmed);
  }

  return (
    <main>
      <h1>Informed</h1>
      <p>
        Informed is nonpartisan by construction: one scoring codebase for every candidate and
        every party.
      </p>

      <form onSubmit={onSubmit} noValidate>
        <label htmlFor={inputId}>Your address</label>
        <input
          id={inputId}
          name="address"
          type="text"
          autoComplete="street-address"
          value={address}
          onChange={(event) => setAddress(event.target.value)}
          aria-describedby={validationError ? `${inputId}-error` : undefined}
          aria-invalid={validationError ? true : undefined}
        />
        <button type="submit">Find my representatives</button>
      </form>

      {validationError ? (
        <p id={`${inputId}-error`} role="alert">
          {validationError}
        </p>
      ) : null}

      {query.isPending && submitted !== null ? <p>Looking up your representatives…</p> : null}

      {query.isError ? <p role="alert">{messageFrom(query.error)}</p> : null}

      {query.isSuccess ? <Delegation ballot={query.data} /> : null}
    </main>
  );
}

/** The district and its two chambers. */
function Delegation({ ballot }: { ballot: Ballot }) {
  const house = ballot.officeholders.filter((person) => person.body_id === HOUSE_BODY_ID);
  const senate = ballot.officeholders.filter((person) => person.body_id === SENATE_BODY_ID);

  return (
    <section aria-label="Your representatives">
      <h2>{ballot.district.name}</h2>
      <Chamber heading="House" members={house} emptyMessage="We don't have this district's representative yet." />
      <Chamber heading="Senate" members={senate} emptyMessage="We don't have this state's senators yet." />
    </section>
  );
}

/**
 * One chamber's members.
 *
 * The `<ul>` carries the chamber's name via `aria-labelledby`, so the list has
 * an accessible name rather than being an anonymous list of people — which is
 * also what lets a reader (and the tests) say "the House list" at all.
 */
function Chamber({
  heading,
  members,
  emptyMessage,
}: {
  heading: string;
  members: Officeholder[];
  emptyMessage: string;
}) {
  const headingId = useId();

  return (
    <>
      <h3 id={headingId}>{heading}</h3>
      {members.length === 0 ? (
        <p>{emptyMessage}</p>
      ) : (
        <ul aria-labelledby={headingId}>
          {members.map((person) => (
            <li key={person.person_id}>
              {person.name}
              {person.party ? <span> ({person.party})</span> : null}
              {' '}
              <FollowButton person={person} />
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/**
 * Follow/unfollow toggle for one officeholder (story 15.1).
 *
 * Minimal and functional, not polished — ui-prd.md's full design spec is a
 * later story's job. Deliberately just a button with a readable label
 * ("Follow Rep. X" / "Unfollow Rep. X"), not an icon-only control, so the
 * accessible name and the visible text are the same thing.
 *
 * Follow state is local to this button (not re-derived from a loaded
 * profile): story 15.1's scope is "follow an incumbent", and reading back
 * who is already followed to seed this screen's initial state is
 * `loadProfile`'s job for a future story to wire in here, not invented now.
 */
function FollowButton({ person }: { person: Officeholder }) {
  const userId = ensureDemoUserId();
  const [isFollowing, setIsFollowing] = useState(false);
  const [isPending, setIsPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onToggle() {
    setError(null);
    setIsPending(true);
    try {
      if (isFollowing) {
        await unfollowPerson(userId, person.person_id);
        setIsFollowing(false);
      } else {
        await followPerson(userId, person.person_id);
        setIsFollowing(true);
      }
    } catch (caught) {
      setError(
        caught instanceof ProfileSaveError
          ? caught.message
          : 'We could not update who you follow just now. Please try again.',
      );
    } finally {
      setIsPending(false);
    }
  }

  return (
    <>
      <button type="button" onClick={onToggle} disabled={isPending}>
        {isFollowing ? `Unfollow ${person.name}` : `Follow ${person.name}`}
      </button>
      {error ? <span role="alert">{error}</span> : null}
    </>
  );
}

/**
 * A per-browser id for the pre-auth demo path. Not a real account.
 *
 * Same key (and therefore the same id) `QuickOnboarding` and `ProfileEditor`
 * already each define their own copy of — see those components' own
 * `ensureDemoUserId` — so a follow made from the ballot view attaches to the
 * same profile a voter saves their interests to, rather than a fourth,
 * disconnected identity.
 */
function ensureDemoUserId(): string {
  const key = 'informed:demo-user-id';
  const existing = window.localStorage.getItem(key);
  if (existing) return existing;
  const generated = `demo-${Math.random().toString(36).slice(2)}-${Date.now()}`;
  window.localStorage.setItem(key, generated);
  return generated;
}

/** A voter-facing message for whatever the query threw. */
function messageFrom(error: unknown): string {
  // `BallotError`'s message is already written for a voter; anything else
  // (a network failure, an aborted fetch) is not, so it is not shown.
  if (error instanceof Error && error.name === 'BallotError') return error.message;
  return 'We could not reach Informed just now. Please check your connection and try again.';
}
