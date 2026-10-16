import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { App } from './App';

/**
 * The root component's own contract (stories 1.1, 1.5).
 *
 * Story 1.5 made `App` render the address-entry screen, which uses TanStack
 * Query, so these now wrap it in a provider. The three assertions themselves
 * are story 1.1's and still hold: the name, the nonpartisan statement, and the
 * absence of any recommendation. `BallotPage.test.tsx` covers the screen's own
 * behaviour.
 */
function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
}

describe('App', () => {
  it('shows the Informed name as the page heading', () => {
    renderApp();
    expect(screen.getByRole('heading', { level: 1, name: 'Informed' })).toBeInTheDocument();
  });

  it('states that Informed is nonpartisan', () => {
    renderApp();
    expect(screen.getByText(/nonpartisan/i)).toBeInTheDocument();
  });

  it('does not tell the voter who to vote for', () => {
    renderApp();
    expect(document.body.textContent ?? '').not.toMatch(/vote for/i);
  });

  it('asks the voter for an address', () => {
    renderApp();
    expect(screen.getByLabelText(/address/i)).toBeInTheDocument();
  });
});
