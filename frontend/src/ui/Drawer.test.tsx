import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useRef, useState } from 'react';
import { describe, expect, it } from 'vitest';

import { Drawer } from './Drawer';

/**
 * Unit tests for the shared `Drawer` primitive (stories 7.5/7.6 prep).
 *
 * No modal/drawer primitive existed anywhere in `src/` before this story
 * (checked: the only other `role="group"` confirmation panel in
 * `ProfileEditor.tsx` is an inline section, not a dialog) — this is the
 * first one, built here so `CandidateDetailPage`'s evidence drawer (story
 * 7.5) does not reinvent focus-trap/escape/return-focus handling, and so a
 * later screen can reuse it instead of writing a second one.
 *
 * Exercised through a tiny host component rather than mounting `Drawer`
 * directly with a fixed `isOpen`, because the behaviours under test —
 * "Escape closes it", "closing returns focus to the opener" — are properties
 * of a real open/close lifecycle, not of a single static render.
 */
function Host() {
  const [isOpen, setIsOpen] = useState(false);
  const openerRef = useRef<HTMLButtonElement>(null);

  return (
    <>
      <button type="button" ref={openerRef} onClick={() => setIsOpen(true)}>
        Open evidence
      </button>
      <Drawer isOpen={isOpen} onClose={() => setIsOpen(false)} label="Evidence" returnFocusTo={openerRef}>
        <p>Drawer content</p>
        <button type="button">Something inside</button>
      </Drawer>
    </>
  );
}

describe('Drawer', () => {
  it('renders nothing when closed', () => {
    render(<Host />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('renders as a labeled dialog when open', async () => {
    const user = userEvent.setup();
    render(<Host />);
    await user.click(screen.getByRole('button', { name: 'Open evidence' }));

    const dialog = screen.getByRole('dialog', { name: 'Evidence' });
    expect(dialog).toBeInTheDocument();
    expect(screen.getByText('Drawer content')).toBeInTheDocument();
  });

  it('moves focus into the drawer when it opens', async () => {
    const user = userEvent.setup();
    render(<Host />);
    await user.click(screen.getByRole('button', { name: 'Open evidence' }));

    const dialog = screen.getByRole('dialog');
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it('closes on Escape', async () => {
    const user = userEvent.setup();
    render(<Host />);
    await user.click(screen.getByRole('button', { name: 'Open evidence' }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('closes on clicking its close button and returns focus to the opener', async () => {
    const user = userEvent.setup();
    render(<Host />);
    const opener = screen.getByRole('button', { name: 'Open evidence' });
    await user.click(opener);

    await user.click(screen.getByRole('button', { name: /close/i }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('traps Tab focus inside the drawer', async () => {
    const user = userEvent.setup();
    render(<Host />);
    await user.click(screen.getByRole('button', { name: 'Open evidence' }));

    const dialog = screen.getByRole('dialog');
    const focusable = dialog.querySelectorAll('button');
    const last = focusable[focusable.length - 1] as HTMLElement;
    last.focus();

    await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});
