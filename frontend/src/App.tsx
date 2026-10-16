/**
 * The Informed PWA's root component (stories 1.5, 4.1).
 *
 * Story 1.1's placeholder landing page is the address-entry screen: the
 * walking skeleton's UI. Story 4.1 adds the quick-onboarding flow as a second
 * screen, reached by a plain toggle rather than a router — there is still no
 * routing library in this app, and two screens do not need one yet. A real
 * router is a later story's concern once there are enough screens (the
 * ballot view, Promise vs. Record) to justify one.
 *
 * It carries no voter data and makes no recommendation, per prd.md's
 * non-negotiables. In particular, switching to onboarding never shows a
 * score: onboarding's own completion screen is explicitly score-free (see
 * `onboarding/QuickOnboarding.tsx`).
 */
import { useState } from 'react';

import { BallotPage } from './BallotPage';
import { QuickOnboarding } from './onboarding/QuickOnboarding';

type Screen = 'ballot' | 'onboarding';

export function App() {
  const [screen, setScreen] = useState<Screen>('ballot');

  return (
    <>
      <nav aria-label="Informed sections">
        <button type="button" onClick={() => setScreen('ballot')} aria-current={screen === 'ballot'}>
          Address lookup
        </button>
        <button
          type="button"
          onClick={() => setScreen('onboarding')}
          aria-current={screen === 'onboarding'}
        >
          Set up my interests
        </button>
      </nav>
      {screen === 'ballot' ? <BallotPage /> : <QuickOnboarding />}
    </>
  );
}
