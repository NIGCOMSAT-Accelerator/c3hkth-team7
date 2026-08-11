import Image from "next/image";

import FreePassLogo from "./FreePassLogo";

/**
 * Partner attribution — FreePass ZeroRate and NIGCOMSAT, in that order.
 *
 * ## Why the first attempt looked wrong
 *
 * It matched both logos on *height* and put a `×` between them. That reads badly
 * because the two marks have opposite aspect ratios: FreePass is 251×50 (5:1, a
 * wordmark) and NIGCOMSAT is 108×100 (near-square, an emblem). Equal heights make the
 * wordmark five times wider, so the pair looks lopsided rather than paired — and a `×`
 * between two differently-shaped objects reads as clutter, not partnership.
 *
 * The fix is standard lockup practice: balance on **optical weight**, not measurement.
 * A dense square emblem holds its own at less height than a long wordmark needs, so the
 * emblem is sized down and the wordmark up until neither dominates. A vertical hairline
 * replaces the `×` — a divider separates without implying arithmetic.
 *
 * ## Light and dark
 *
 * FreePass is **inlined** (`FreePassLogo`), so `currentColor` handles both themes from
 * one file — brand purple where it should lead, muted grey where it should recede, and
 * it inherits correctly on a dark surface. That is why the supplied grayscale variant
 * does not need to ship as a second asset.
 *
 * NIGCOMSAT is only available as a raster, so it is inverted in CSS for dark mode. A
 * filter rather than a second PNG: one source of truth, and a hand-inverted duplicate
 * would drift when the brand updates.
 */

type Variant = "row" | "stacked";

/**
 * Heights tuned for equal optical weight, not equal measurement. At these values the
 * wordmark renders ~90px wide against a ~24px emblem, and the pair reads balanced.
 */
const SIZING = {
  row: { freepass: 17, nigcomsat: 21 },
  stacked: { freepass: 21, nigcomsat: 27 },
} as const;

const NIGCOMSAT_RATIO = 108 / 100;

export default function ConsortiumMark({
  variant = "row",
  label,
}: {
  variant?: Variant;
  /** Eyebrow text above the logos. Omit for the bare lockup. */
  label?: string;
}) {
  const h = SIZING[variant];

  return (
    <div className="partners" data-variant={variant}>
      {label && <span className="partners__label">{label}</span>}
      <div className="partners__row">
        {/*
          FreePass first, then NIGCOMSAT — matching `iam/email_layout._footer()`.

          SHELTER is a FreePass product; NIGCOMSAT is the satellite and broadcast partner.
          The order used to be reversed here and in the email footer, and in the email it
          read as "Powered by NIGCOMSAT" with the FreePass mark looking like an
          afterthought — reported from an aggregator activation email. The web and the
          email are one brand system, so they carry the same order.

          Inherits `color` from .partners__logo — that is the whole point of inlining it,
          and what makes one asset serve both themes.
        */}
        <FreePassLogo height={h.freepass} className="partners__logo partners__logo--word" />
        <span className="partners__divider" aria-hidden="true" />
        <Image
          src="/nigcomsat-logo.png"
          alt="NIGCOMSAT"
          width={Math.round(NIGCOMSAT_RATIO * h.nigcomsat)}
          height={h.nigcomsat}
          // Above the fold on every page and ~8 KB, so eager loading avoids a flash of
          // missing brand for no meaningful cost.
          priority
          className="partners__logo partners__logo--emblem"
        />
      </div>
    </div>
  );
}
