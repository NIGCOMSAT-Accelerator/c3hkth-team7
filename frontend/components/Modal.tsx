"use client";

import { useEffect, useRef } from "react";

/**
 * The one modal in the portal.
 *
 * ## Why a native `<dialog>` rather than a positioned div
 *
 * A hand-rolled overlay has to reimplement four things, and portal code got all four wrong at least
 * once before this existed:
 *
 *   * **Focus trapping.** Tab must not escape to the page behind. `showModal()` makes everything
 *     outside inert for free; a div requires a focus-cycle handler that breaks on the first
 *     conditionally-rendered input.
 *   * **Escape to close.** Free with `<dialog>`, and it fires `cancel` so there is one close path
 *     rather than two.
 *   * **The top layer.** `showModal()` promotes the element above every stacking context, so no
 *     z-index arms race with the sticky nav.
 *   * **`aria-modal` semantics.** A screen reader announces a dialog and stops reading the page
 *     underneath. A div with `role="dialog"` needs `aria-modal` plus manual `aria-hidden` on the
 *     rest of the document, which is easy to forget and impossible to notice without a reader.
 *
 * Supported everywhere this ships (Chrome 37+, Safari 15.4+, Firefox 98+).
 *
 * ## Open state lives in the parent, deliberately
 *
 * `open` is a prop, not internal state. A create form's visibility is usually derived from
 * something the page already knows — a `useActionState` result that just succeeded, a row selected
 * for editing — and duplicating that into the modal produces the bug where the form closes on
 * success but the parent still thinks it is open.
 *
 * ## Body scroll is locked while open
 *
 * `<dialog>` does not do this, and without it a phone user scrolling a long form reaches the end
 * and keeps scrolling the page behind — which on the Areas page moves the map they were about to
 * use. Restored on close, including when the component unmounts mid-open.
 */
export default function Modal({
  open,
  onClose,
  title,
  description,
  children,
  wide = false,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  /** One sentence under the title. Optional — omit rather than pad. */
  description?: string;
  children: React.ReactNode;
  /** Wider column, for a form with a map or a multi-column layout. */
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;

    // `open` as an attribute renders a NON-modal dialog: no focus trap, no top layer, no Escape.
    // The imperative call is what makes it a real modal, so it must be driven by the effect.
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

  return (
    <dialog
      ref={ref}
      className={`modal${wide ? " modal--wide" : ""}`}
      // Escape and the browser's own dismissal both route here, so the parent's state cannot
      // drift out of sync with what the user sees.
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      // Clicking the backdrop closes. The check is `target === dialog` because the backdrop is
      // the dialog element itself — a click anywhere inside the card has a descendant as its
      // target, so this cannot fire while someone is selecting text in the form.
      onClick={(event) => {
        if (event.target === ref.current) onClose();
      }}
      aria-labelledby="modal-title"
    >
      <div className="modal__card">
        <div className="modal__head">
          <div>
            <h2 className="modal__title" id="modal-title">
              {title}
            </h2>
            {description && <p className="modal__sub">{description}</p>}
          </div>
          <button
            type="button"
            className="modal__close"
            onClick={onClose}
            aria-label="Close"
          >
            &times;
          </button>
        </div>

        <div className="modal__body">{children}</div>
      </div>
    </dialog>
  );
}
