"use client";

import { useState } from "react";

import SeverityBadge from "@/components/SeverityBadge";
import { HAZARD_LABEL, type Alert } from "@/lib/types";

/**
 * One alert in the queue, collapsed to a scannable row until opened.
 *
 * ## The problem this solves
 *
 * Each alert renders around 120 lines of markup — card rows, actions, five track modules, the
 * explanation surfaces, the evidence list, delivery receipts and a verdict. Correct, and at fifty
 * alerts it is a wall: finding the WARNING from Tuesday means scrolling past everything quiet since.
 *
 * ## What stays visible when collapsed, and why exactly this
 *
 * The collapsed row has to support triage without opening, so it carries the four things that
 * decide whether to open:
 *
 *   * **Severity badge** — colour *and* an icon *and* a text label. Never colour alone; that rule
 *     holds here as everywhere in the portal.
 *   * **The headline** — the plain-language answer, which is the whole point of the advisory.
 *   * **Plot, hazard, and when** — which field, and is this current or last week's.
 *   * **A delivery marker** — an alert that reached nobody is the one an operator must open first,
 *     and it would otherwise be invisible until expanded.
 *
 * ## The first one starts open
 *
 * The queue is newest-first, so the leading alert is the one someone came to read. Opening it by
 * default means the common case needs no interaction, while the rest stay collapsed. Anything more
 * clever — auto-opening every EMERGENCY, say — would reintroduce the wall on exactly the day it
 * matters most, when several fire at once.
 *
 * ## Why not `<details>`
 *
 * `<details>` cannot be controlled from React state without fighting the browser's own toggle, and
 * `::marker` styling is inconsistent across Safari versions. A button with `aria-expanded` and
 * `aria-controls` gives the same semantics to a screen reader with none of that.
 *
 * The body is unmounted rather than hidden with CSS: it holds the TrackModules subtree per alert,
 * and keeping fifty of those mounted is real memory and real reconciliation cost for markup nobody
 * is looking at.
 */
export default function AlertCard({
  alert,
  defaultOpen = false,
  when,
  children,
}: {
  alert: Alert;
  defaultOpen?: boolean;
  /**
   * The already-formatted timestamp.
   *
   * A **string, not a formatter function.** The page is a server component and this is a client
   * one, so a function prop cannot cross that boundary — it is not serialisable, and passing one
   * fails at render rather than at compile. Formatting stays on the server, where the page's own
   * `formatWhen` already lives, so the collapsed row and the expanded body cannot format dates
   * differently.
   */
  when: string;
  /** The full alert body. Rendered only while open. */
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);

  // An alert whose every receipt FAILED or was SKIPPED reached nobody. Surfaced in the collapsed
  // row because it is the one state where the alert existing is not the same as the alert working.
  //
  // `pending` is deliberately excluded from both counts: a receipt still in flight is neither a
  // success nor a failure, and treating it as the latter would label every just-dispatched alert
  // as broken for as long as the queue takes.
  const settled = alert.receipts.filter((r) => r.status !== "pending");
  const undelivered =
    settled.length > 0 && !settled.some((r) => r.status === "sent");

  return (
    <article className="pcard alertrow" data-open={open}>
      <button
        type="button"
        className="alertrow__head"
        aria-expanded={open}
        aria-controls={`alert-body-${alert.id}`}
        onClick={() => setOpen((v) => !v)}
      >
        <SeverityBadge severity={alert.assessment.severity} />

        <span className="alertrow__main">
          <span className="alertrow__title">{alert.advisory.headline}</span>
          <span className="alertrow__meta">
            {HAZARD_LABEL[alert.assessment.hazard]} · {alert.assessment.aoi_name} ·{" "}
            {when}
            {undelivered && (
              <>
                {" · "}
                <span className="alertrow__undelivered">reached nobody</span>
              </>
            )}
          </span>
        </span>

        <span className="alertrow__chevron" aria-hidden="true">
          {open ? "−" : "+"}
        </span>
      </button>

      {open && (
        <div className="alertrow__body" id={`alert-body-${alert.id}`}>
          {children}
        </div>
      )}
    </article>
  );
}
