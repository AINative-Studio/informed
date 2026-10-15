import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { App } from './App';

describe('App', () => {
  it('shows the Informed name as the page heading', () => {
    render(<App />);
    expect(screen.getByRole('heading', { level: 1, name: 'Informed' })).toBeInTheDocument();
  });

  it('states that Informed is nonpartisan', () => {
    render(<App />);
    expect(screen.getByText(/nonpartisan/i)).toBeInTheDocument();
  });

  it('does not tell the voter who to vote for', () => {
    render(<App />);
    expect(document.body.textContent ?? '').not.toMatch(/vote for/i);
  });
});
