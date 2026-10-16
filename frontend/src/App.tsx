/**
 * The Informed PWA's root component (story 1.5).
 *
 * Story 1.1's placeholder landing page is now the address-entry screen: the
 * walking skeleton's UI. Routing arrives when there is a second screen to route
 * to (the ballot view, epic 2, and Promise vs. Record, epic 7).
 *
 * It carries no voter data and makes no recommendation, per prd.md's
 * non-negotiables.
 */
import { BallotPage } from './BallotPage';

export function App() {
  return <BallotPage />;
}
