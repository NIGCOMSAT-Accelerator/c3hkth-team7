/**
 * The SHELTER logo — mark, wordmark, and the combined lockup.
 *
 * Inlined SVG rather than `<img src="/shelter-mark.svg">`, for three reasons that all
 * matter here:
 *
 *   1. **`currentColor` works.** The mark inherits the surrounding text colour, so one
 *      component serves the light theme, the dark theme and a white-on-brand reverse
 *      with no second asset to drift out of sync. An `<img>` cannot be recoloured by CSS.
 *   2. **No extra request, no flash.** The header is above the fold on every page; a
 *      separately-fetched logo appears after first paint.
 *   3. **It is accessible as one unit.** The mark is `aria-hidden` and the wordmark
 *      carries the accessible name, so a screen reader announces "SHELTER" once
 *      instead of reading a decorative graphic and then the text.
 *
 * The standalone `/shelter-mark.svg` is kept for the favicon, OG image and app icon,
 * where a file is required.
 */

type LogoSize = "sm" | "md" | "lg";

const MARK_PX: Record<LogoSize, number> = { sm: 26, md: 34, lg: 48 };
const WORD_PX: Record<LogoSize, number> = { sm: 17, md: 22, lg: 30 };

/** The glyph alone. Used where the wordmark would be redundant — favicons, tight nav. */
export function ShelterMark({
  size = "md",
  className,
}: {
  size?: LogoSize;
  className?: string;
}) {
  const px = MARK_PX[size];

  return (
    <svg
      width={px}
      height={px}
      viewBox="0 0 40 40"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {/*
        A satellite keeping watch, above coverage arcs, above the shelter it protects.
        Read top to bottom that is the whole product in one glyph: an orbiting sensor, the
        area it sees, and the household the warning reaches.

        ## Why the satellite is a body plus two panels and nothing more

        The topbar renders this at 26px. At that size a dish, a truss or a solar-cell grid
        collapses into a smudge — so the satellite is three rounded rectangles, which is the
        minimum that still reads as a spacecraft rather than a dash. Detail that survives at
        48px but muddies at 26px would make the smallest, most-used instance the worst one.

        ## Why the panels are lighter than the body

        Opacity, not a second colour, so the mark still works printed in one ink and still
        inherits `currentColor` for dark mode and white-on-brand reverses. The contrast also
        does the silhouette work a stroke outline would otherwise have to.

        ## Why two arcs rather than one

        One arc reads as a rainbow over a house. Two nested arcs at different opacities read
        as signal spreading downward from the satellite — the same visual language the
        landing-page animation uses for the radar sweep, so the logo and the diagram are
        recognisably one system.
      */}

      {/* Satellite body */}
      <rect x="17.7" y="2.6" width="4.6" height="4.1" rx="1.1" fill="currentColor" />
      {/* Solar panels */}
      <rect
        x="11.9"
        y="3.7"
        width="4.9"
        height="1.9"
        rx="0.65"
        fill="currentColor"
        opacity="0.55"
      />
      <rect
        x="23.2"
        y="3.7"
        width="4.9"
        height="1.9"
        rx="0.65"
        fill="currentColor"
        opacity="0.55"
      />

      {/* Coverage arcs, spreading down from the satellite toward the shelter. */}
      <path
        d="M13.2 12.9a9.2 9.2 0 0 1 13.6 0"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
        opacity="0.62"
      />
      <path
        d="M8.8 16.4a14.6 14.6 0 0 1 22.4 0"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
        opacity="0.26"
      />

      {/* The shelter: roof over an open base. The opening is sized to CONTAIN the plot dot
          — an earlier revision clipped it against the base, which read as a rendering
          fault rather than a mark. */}
      <path d="M20 18 8.6 27.6v8.2h5.6v-6.6h11.6v6.6h5.6v-8.2Z" fill="currentColor" />
      {/* The monitored plot — what stops the roof reading as a plain chevron. */}
      <circle cx="20" cy="32" r="2.5" fill="currentColor" />
    </svg>
  );
}

/**
 * Mark plus wordmark. The default brand presentation.
 *
 * `withTagline` adds the descriptor, which the brief spells out in full
 * ("Satellite Hazard & Early-warning Local Tactical Emergency Response"). That expansion
 * belongs in the footer and on the landing hero, where a first-time reader needs to know
 * what the acronym means — and nowhere else, because repeating it in the header would
 * cost horizontal space on a phone for information already given.
 */
export default function ShelterLogo({
  size = "md",
  withTagline = false,
  className,
}: {
  size?: LogoSize;
  withTagline?: boolean;
  className?: string;
}) {
  return (
    <span className={`logo logo--${size}${className ? ` ${className}` : ""}`}>
      <ShelterMark size={size} className="logo__mark" />
      <span className="logo__text">
        <span className="logo__word" style={{ fontSize: WORD_PX[size] }}>
          SHELTER
        </span>
        {withTagline && (
          <span className="logo__tagline">
            Satellite Hazard &amp; Early-warning Local Tactical Emergency Response
          </span>
        )}
      </span>
    </span>
  );
}
