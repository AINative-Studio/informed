/**
 * The Informed PWA's root component (stories 1.5, 4.1, 4.2-4.4).
 *
 * Story 1.1's placeholder landing page is the address-entry screen: the
 * walking skeleton's UI. Story 4.1 adds the quick-onboarding flow as a second
 * screen; stories 4.2-4.4 add the fuller tier-editing profile screen as a
 * third. All three are reached by a plain toggle rather than a router —
 * there is still no routing library in this app, and three screens do not
 * need one yet. A real router is a later story's concern once there are
 * enough screens (the ballot view, Promise vs. Record) to justify one.
 *
 * It carries no voter data and makes no recommendation, per prd.md's
 * non-negotiables. In particular, switching to onboarding never shows a
 * score: onboarding's own completion screen is explicitly score-free (see
 * `onboarding/QuickOnboarding.tsx`), and the full profile editor's own
 * summary is score-free for the same reason (see `profile/ProfileEditor.tsx`).
 */
import { useState } from 'react';

import { BallotPage } from './BallotPage';
import { QuickOnboarding } from './onboarding/QuickOnboarding';
import { ProfileEditor } from './profile/ProfileEditor';

type Screen = 'ballot' | 'onboarding' | 'profile';

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
        <button
          type="button"
          onClick={() => setScreen('profile')}
          aria-current={screen === 'profile'}
        >
          Edit my full profile
        </button>
      </nav>
      {screen === 'ballot' ? <BallotPage /> : null}
      {screen === 'onboarding' ? <QuickOnboarding /> : null}
      {screen === 'profile' ? <ProfileEditor /> : null}
    </>
  );
}
