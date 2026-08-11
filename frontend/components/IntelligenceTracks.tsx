/**
 * The three intelligence tracks SHELTER is built for.
 *
 * ## Why the roadmap is stated rather than implied
 *
 * Agricultural Intelligence is live; Environmental and Public Health are the next phase.
 * Showing all three without marking which is which would be a false promise — someone
 * evaluating the platform would sign up expecting flood vulnerability mapping today. But
 * hiding the other two would understate the design, which genuinely accommodates all
 * three: the same SAR flood engine underpins Track B, and the malaria cascade the Oracle
 * already computes is Track C's core signal.
 *
 * So each card carries an explicit state badge. "Next phase" is honest in a way that
 * "coming soon" is not — it says there is a plan without implying a date.
 *
 * ## Shared, not duplicated
 *
 * Rendered on both the landing page and the dashboard. Same component, so the roadmap
 * cannot say one thing to a prospect and another to a subscriber — which is exactly the
 * drift that happens when marketing copy and product copy are maintained separately.
 */

type TrackState = "live" | "next";

interface Track {
  id: string;
  name: string;
  state: TrackState;
  badge: string;
  lede: string;
  capabilities: string[];
  /** A distinct glyph per track, so the three are distinguishable without reading. */
  icon: React.ReactNode;
}

const TRACKS: Track[] = [
  {
    id: "agricultural",
    name: "Agricultural Intelligence",
    state: "live",
    badge: "Live now",
    lede:
      "Watch a plot through the season and know before a harvest is lost, not after.",
    capabilities: [
      "Crop health monitoring",
      "Soil moisture analysis",
      "Crop stress scoring",
      "Irrigation recommendations",
      "Farm monitoring dashboards",
    ],
    icon: (
      // A sprout over a furrow — legible at 20px, which a detailed plant would not be.
      <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" fill="none">
        <path d="M12 21V11" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        <path
          d="M12 11C12 11 8 10 7 6c4 0 5 3 5 5Zm0 0c0 0 4-1 5-5-4 0-5 3-5 5Z"
          fill="currentColor"
          opacity="0.85"
        />
        <path d="M4 21h16" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" opacity="0.45" />
      </svg>
    ),
  },
  {
    id: "environmental",
    name: "Environmental Intelligence",
    state: "next",
    badge: "Next phase",
    lede:
      // Was: "The SAR flood engine already running underneath Track A" — which named the internal
      // architecture and the internal track lettering. States the capability and the readiness
      // instead, which is what a responder or planner is assessing.
      "The same cloud-piercing flood detection that already protects farms, surfaced for responders and planners.",
    capabilities: [
      "Flood prediction",
      "Flood vulnerability mapping",
      "Community risk alerts",
      "Emergency response dashboards",
      "Infrastructure monitoring",
    ],
    icon: (
      // Water lines under a rising level — reads as flood rather than generic weather.
      <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" fill="none">
        <path
          d="M2 16c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2 2.5 2 5 2"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
        <path
          d="M2 21c2.5 0 2.5-2 5-2s2.5 2 5 2 2.5-2 5-2 2.5 2 5 2"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          opacity="0.5"
        />
        <path d="M12 3v7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        <path d="M9 6l3-3 3 3" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    ),
  },
  {
    id: "public-health",
    name: "Public Health Intelligence",
    state: "next",
    badge: "Next phase",
    lede:
      // Was: "the cascade the Oracle already names" — an internal agent name. The claim that
      // matters is the lead time, which is the whole value of predicting a health risk from water.
      "Standing water today is a malaria surge in roughly six weeks — and we already see the water.",
    capabilities: [
      "Malaria environmental risk index",
      "Standing water detection",
      "Community health alerts",
      "Climate-health monitoring",
      "Disease surveillance support",
    ],
    icon: (
      // A shield with a pulse line: health protection rather than a medical cross,
      // which would read as clinical care.
      <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" fill="none">
        <path
          d="M12 2 4 5v6c0 5 3.4 9.3 8 11 4.6-1.7 8-6 8-11V5Z"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinejoin="round"
        />
        <path
          d="M7.5 12h2l1.5-3 2 6 1.5-3h2"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
];

/**
 * How many capabilities show before the rest are folded away.
 *
 * Three keeps a dashboard card scannable. The remainder is one tap away rather than absent — see
 * the `<details>` below for why that distinction matters.
 */
const COMPACT_VISIBLE = 3;

export default function IntelligenceTracks({
  /** Folds the capability lists on the dashboard, where space is scarcer. */
  compact = false,
}: {
  compact?: boolean;
}) {
  return (
    <div className="tracks">
      {TRACKS.map((track) => (
        <article key={track.id} className="track" data-state={track.state}>
          <div className="track__head">
            <span className="track__icon">{track.icon}</span>
            <h3 className="track__name">{track.name}</h3>
            {/* State as text, never as colour alone — the same rule SeverityBadge
                follows, and it survives a greyscale print or colour blindness. */}
            <span className="track__badge">{track.badge}</span>
          </div>
          <p className="track__lede">{track.lede}</p>
          <ul className="track__list">
            {(compact
              ? track.capabilities.slice(0, COMPACT_VISIBLE)
              : track.capabilities
            ).map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>

          {/*
            The remainder, expandable.
            
            This was a dead "+2 more" — it told the reader something existed and gave them no way
            to see it, which is worse than either showing everything or showing nothing. Someone
            evaluating whether a track covers their need was told the answer was withheld.
            
            `<details>` rather than a state hook: this is a server component, and folding content
            is exactly what the element is for. It works with no JavaScript, it is keyboard
            reachable and announced correctly by screen readers for free, and it costs no bundle —
            which matters on the metered connections this product is designed around.
          */}
          {compact && track.capabilities.length > COMPACT_VISIBLE && (
            <details className="track__more">
              <summary>
                {track.capabilities.length - COMPACT_VISIBLE} more{" "}
                {track.capabilities.length - COMPACT_VISIBLE === 1
                  ? "capability"
                  : "capabilities"}
              </summary>
              <ul className="track__list">
                {track.capabilities.slice(COMPACT_VISIBLE).map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            </details>
          )}
        </article>
      ))}
    </div>
  );
}
