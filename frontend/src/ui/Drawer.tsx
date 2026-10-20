/**
 * A focus-trapped, labeled modal drawer (stories 7.5/7.6 prep).
 *
 * No modal/drawer primitive existed anywhere in this codebase before this
 * story — `ProfileEditor.tsx`'s delete confirmation is a plain `<div
 * role="group">` panel inline in the page flow, not a dialog. This is the
 * first one, built so `CandidateDetailPage`'s evidence drawer (story 7.5)
 * does not reinvent focus-trap/Escape/return-focus handling, and so any
 * later screen needing an overlay panel can reuse it instead of writing a
 * second one from scratch.
 *
 * `role="dialog"` with `aria-modal="true"` and a required accessible name
 * (`label`, via `aria-label`): a dialog with no name is one a screen reader
 * announces as "dialog" and nothing else, which fails the same kind of
 * accessible-name expectation `CandidateDetailPage`'s own `aria-label`
 * choices already hold elsewhere in this app.
 *
 * Behaviour, each covered by `Drawer.test.tsx`:
 *  - Renders nothing at all when closed — not hidden, absent — so a closed
 *    drawer can never be found by role and can never trap focus by mistake.
 *  - Moves focus into itself the moment it opens (to its own close button,
 *    the one control guaranteed to exist), so a keyboard or screen-reader
 *    user lands inside the drawer rather than wherever focus happened to be.
 *  - Tab and Shift+Tab cycle focus only among the drawer's own focusable
 *    elements — a real focus trap, not merely "close on Escape" — because a
 *    voter tabbing past the last control inside a drawer should not land back
 *    on page content that is, while the drawer is open, inert to them.
 *  - Escape closes it.
 *  - Closing (by Escape, the close button, or a caller-driven `isOpen`
 *    flip) returns focus to `returnFocusTo` when given, so the voter's
 *    keyboard position after closing is exactly where they were before
 *    opening it, never lost to the top of the page.
 */
import { useEffect, useRef } from 'react';
import type { ReactNode, RefObject } from 'react';

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Drawer({
  isOpen,
  onClose,
  label,
  returnFocusTo,
  children,
}: {
  isOpen: boolean;
  onClose: () => void;
  /** The dialog's accessible name (`aria-label`). Required: see module docstring. */
  label: string;
  /** Focus moves here when the drawer closes, if the ref still points at an element. */
  returnFocusTo?: RefObject<HTMLElement | null>;
  children: ReactNode;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  // Captured once per open, not read fresh on close: by the time Escape or
  // the close button fires, `returnFocusTo.current` may already have
  // changed (e.g. the opener unmounted and a new one mounted in its place),
  // and the voter's expectation is "put focus back where MY keyboard was",
  // which is the element present at open time.
  const openerAtOpen = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    openerAtOpen.current = returnFocusTo?.current ?? (document.activeElement as HTMLElement | null);
    closeButtonRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentionally re-runs only on open/close
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== 'Tab') return;

      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) return;

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;

      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      } else if (!dialog.contains(active)) {
        // Focus escaped the trap some other way (e.g. a prior render tore
        // down the element that held it) — pull it back in rather than
        // leaving it on page content the drawer is covering.
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [isOpen]);

  function close() {
    // Focus moves back to the opener BEFORE `onClose` triggers the caller's
    // state update that unmounts this drawer. React 18 batches that update
    // rather than applying it synchronously inside this handler, so the
    // drawer (and the element currently focused inside it) is still in the
    // DOM at this point — calling `.focus()` here works in every browser,
    // where deferring it a tick (e.g. with `requestAnimationFrame`) does
    // not reliably re-run after jsdom's own event loop in tests, and is one
    // more place for the return-focus step to silently never fire.
    openerAtOpen.current?.focus();
    onClose();
  }

  if (!isOpen) return null;

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={label}
      // Clicking the dimmed backdrop is an established, expected way to
      // dismiss an overlay; the panel itself stops the click from
      // bubbling back to this handler so a click *inside* the drawer never
      // closes it.
      onClick={close}
    >
      <div onClick={(event) => event.stopPropagation()}>
        <button type="button" ref={closeButtonRef} onClick={close}>
          Close
        </button>
        {children}
      </div>
    </div>
  );
}
