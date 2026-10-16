/**
 * The Informed PWA's root component (stories 1.5, 4.1, 4.2-4.4, 12.2).
 *
 * Story 1.1's placeholder landing page is the address-entry screen: the
 * walking skeleton's UI. Story 4.1 adds the quick-onboarding flow as a second
 * screen; stories 4.2-4.4 add the fuller tier-editing profile screen as a
 * third; story 12.2 adds the public methodology page as a fourth. All four are
 * reached by a plain toggle rather than a router — there is still no routing
 * library in this app. A real router is a later story's concern, and story
 * 12.2 noted one reason to bring it forward: the methodology page is the first
 * screen that genuinely wants its own shareable URL, because prd.md has
 * verdicts linking to a methodology version and a link needs an address. The
 * backend already serves that address (`/v1/methodology/{version}`); giving
 * the UI a matching route is the router story's job, not this one's.
 *
 * It carries no voter data and makes no recommendation, per prd.md's
 * non-negotiables. In particular, switching to onboarding never shows a
 * score: onboarding's own completion screen is explicitly score-free (see
 * `onboarding/QuickOnboarding.tsx`), and the full profile editor's own
 * summary is score-free for the same reason (see `profile/ProfileEditor.tsx`).
 * The methodology page shows how scoring works and names no candidate at all.
 */
import { useState } from 'react';

import { BallotPage } from './BallotPage';
import { MethodologyPage } from './methodology/MethodologyPage';
import { QuickOnboarding } from './onboarding/QuickOnboarding';
import { ProfileEditor } from './profile/ProfileEditor';

type Screen = 'ballot' | 'onboarding' | 'profile' | 'methodology';

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
        {/* Story 12.2. Last in the nav but reachable from every screen and
            needing no account: prd.md publishes the methodology precisely so
            that a reader who does not use the product can check it. */}
        <button
          type="button"
          onClick={() => setScreen('methodology')}
          aria-current={screen === 'methodology'}
        >
          How we score
        </button>
      </nav>
      {screen === 'ballot' ? <BallotPage /> : null}
      {screen === 'onboarding' ? <QuickOnboarding /> : null}
      {screen === 'profile' ? <ProfileEditor /> : null}
      {screen === 'methodology' ? <MethodologyPage /> : null}
    </>
  );
}
