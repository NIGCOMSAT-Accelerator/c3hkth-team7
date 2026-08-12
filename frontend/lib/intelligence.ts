/**
 * The intelligence vocabulary — what each alert category MEANS, and which track it belongs to.
 *
 * ## Why this exists separately from `SEVERITY_META`
 *
 * That table has labels, icons and a rank — enough to sort and colour a badge, and nothing that
 * tells a subscriber what to *do*. "Watch" and "Advisory" are not self-explanatory words, and a
 * farmer deciding whether to move stored grain tonight cannot act on a purple triangle.
 *
 * SHELTER's product is not alerts, it is **decisions someone can act on**. So each category
 * states its own meaning, its urgency in plain language, and what response it warrants — which is
 * also what lets a subscriber set their own threshold per channel and know what they are
 * choosing.
 *
 * ## Tracks
 *
 * The platform turns Earth Observation into intelligence across three tracks. Agricultural is
 * live in this MVP; the other two follow. `TRACK_STATUS` is honest about which is which, because a
 * track presented as available and delivering nothing is worse than one marked "next phase" —
 * `backend/app/iam/tracks.py` holds the authoritative version, and `deliverable` there is
 * computed from what `OracleAgent._classify` can actually return.
 */

import type { HazardType, Severity, VerdictSummary } from "./types";

/** What an alert category means and how to treat it. */
export interface IntelligenceCategory {
  label: string;
  /** One line: what this category IS. */
  meaning: string;
  /** What the subscriber should do about it. The actionable half. */
  response: string;
  /** How soon. Separate from `response` because "when" and "what" are different decisions. */
  urgency: string;
  /** Drives the badge and the left border. A reserved status palette — never brand purple, and
   *  always paired with an icon and a text label so colour is never the only signal. */
  tone: "info" | "advisory" | "watch" | "warning" | "emergency";
}

export const INTELLIGENCE: Record<Severity, IntelligenceCategory> = {
  info: {
    label: "Info",
    meaning:
      "A routine reading. Conditions were measured and nothing needs your attention.",
    response: "No action needed. Worth a glance if you are curious how the season is tracking.",
    urgency: "No time pressure",
    tone: "info",
  },
  advisory: {
    label: "Advisory",
    meaning:
      "Something has changed that is worth knowing, but it does not threaten the crop yet.",
    response: "Read when convenient. Plan around it rather than reacting to it.",
    urgency: "Within a few days",
    tone: "advisory",
  },
  watch: {
    label: "Watch",
    meaning:
      "Conditions that could develop into a problem are present now. Not yet a threat, but the direction matters.",
    response:
      "Check the plot yourself if you can, and prepare what would be slow to arrange later — drainage, labour, somewhere dry for stored produce.",
    urgency: "Next day or two",
    tone: "watch",
  },
  warning: {
    label: "Warning",
    meaning:
      "A hazard is likely to affect this area. The measurement and the outlook agree.",
    response:
      "Act now. Move what can be moved, clear drainage, and tell anyone else farming nearby.",
    urgency: "Today",
    tone: "warning",
  },
  emergency: {
    label: "Emergency",
    meaning:
      "A severe hazard is happening or imminent, with high confidence.",
    response:
      "Act immediately and follow official emergency guidance. SHELTER informs that decision; it does not replace it.",
    urgency: "Immediately",
    tone: "emergency",
  },
};

/** Which intelligence track a hazard belongs to. Mirrors `backend/app/iam/tracks.py`. */
export type Track =
  | "agricultural"
  | "environmental"
  | "public_health"
  | "financial";

export const HAZARD_TRACK: Record<HazardType, Track> = {
  crop_waterlogging: "agricultural",
  crop_drought_stress: "agricultural",
  crop_vegetation_anomaly: "agricultural",
  flood_inundation: "environmental",
  flood_forecast: "environmental",
  // Cascade-only today: `_classify` never returns it as a primary hazard, so it appears as a
  // consequence of a flood or waterlogging alert rather than on its own.
  malaria_risk: "public_health",
  // NOTE: `financial` appears in `Track` but in no entry here, and that is correct rather than an
  // omission. `HAZARD_TRACK` maps a hazard to its track, and the Financial track has no hazards —
  // a credit signal is not an event to warn someone about. `TRACK_HAZARDS[FINANCIAL]` is likewise
  // empty on the backend. If a hazard is ever added for it, add the mapping here too.
};

export const TRACK_META: Record<
  Track,
  { label: string; short: string; scope: string; status: "live" | "next" }
> = {
  agricultural: {
    label: "Agricultural Intelligence",
    short: "Agriculture",
    scope:
      "Crop health monitoring, soil-moisture analysis, crop-stress scoring and irrigation guidance.",
    status: "live",
  },
  environmental: {
    label: "Environmental Intelligence",
    short: "Environment",
    scope:
      "Flood prediction, vulnerability mapping, community risk alerts and infrastructure monitoring.",
    // Flood alerting runs on the same SAR water mask that is live today, but the distinct
    // product surface — vulnerability maps, response dashboards — is the next phase.
    status: "next",
  },
  public_health: {
    label: "Public Health Intelligence",
    short: "Public health",
    scope:
      "Malaria environmental risk, standing-water detection, community health alerts and climate-health monitoring.",
    status: "next",
  },
  financial: {
    label: "Financial & Credit Risk Intelligence",
    short: "Financial",
    scope:
      "Geospatial KYC/KYB address verification, neighbourhood commercial and demographic risk scoring, asset and activity validation, and lender portfolio exposure.",
    // `next`, and the per-capability detail comes from the SERVER (`Track.capabilities` on
    // `GET /iam/tracks`) rather than being restated here. Seven capabilities at four different
    // stages cannot be reduced to one label, and a TypeScript copy of that breakdown would drift
    // from the backend the moment one of them ships — which is the same reason `Track` itself is a
    // server-driven interface in `lib/types.ts` rather than a hardcoded table.
    status: "next",
  },
};

/**
 * How much to trust a reading, in words.
 *
 * A bare "41%" invites the wrong question ("41% chance of what?"). Confidence here is how sure
 * SHELTER is of its own measurement, not the probability of the hazard — and the difference
 * matters enough to name.
 *
 * The bands are not arbitrary: below 0.65 the Oracle caps severity at Watch
 * (`CONFIDENCE_ESCALATION_FLOOR`), so "cannot raise an emergency" is a real property of a
 * low-confidence reading and worth saying.
 */
export function confidenceBand(confidence: number): {
  label: string;
  detail: string;
} {
  if (confidence >= 0.85) {
    return {
      label: "High confidence",
      detail: "Clear imagery and agreeing data sources.",
    };
  }
  if (confidence >= 0.65) {
    return {
      label: "Good confidence",
      detail: "Enough agreement between sources to escalate if conditions worsen.",
    };
  }
  if (confidence >= 0.4) {
    return {
      label: "Limited confidence",
      detail:
        "Cloud, missing rainfall data or untrained models. Severity is capped at Watch until confidence improves.",
    };
  }
  return {
    label: "Low confidence",
    detail:
      "Too little usable data this cycle. Treated as information only, never as a warning.",
  };
}


/**
 * Who wrote an advisory, and what it was measured from — for display.
 *
 * ## Why the model name is not shown
 *
 * `generated_by` carries the resolved model, e.g. `gemini-2.5-flash`. Rendering it published our
 * LLM vendor and version to every subscriber and anyone they showed their screen to. That is a
 * supplier relationship, a cost structure and a switching risk given away for nothing: a farmer
 * cannot act on it, and a competitor can.
 *
 * It is also unstable in a way the reader would misread. Swapping model would silently change the
 * byline on every alert, making it look like something about the advisory had changed when only our
 * procurement had.
 *
 * So two names, both of which describe a capability the subscriber can reason about:
 *
 *   * **Herald-AI** — written for this specific situation, in their language.
 *   * **System ML** — the deterministic fallback, generated from the measurements without a
 *     language model. Not a lesser advisory: it is the guaranteed path, and it is what runs when no
 *     provider is configured or a model declines.
 *
 * The exact model stays on `/health`, which is where an operator looks.
 *
 * ## Why the data sources are named
 *
 * Provenance is the product's own argument. "Herald-AI · from Sentinel-1, WorldCover" tells a
 * subscriber the advisory rests on measured satellite data rather than a projection — and for
 * Copernicus and OpenStreetMap it is also an attribution obligation, not a courtesy.
 */
export function advisoryByline(
  generatedBy: string,
  dataSources: string[] = [],
): { author: string; sources: string } {
  const author =
    !generatedBy || generatedBy === "template" ? "System ML" : "Herald-AI";

  // Presentation names, so a reader sees "Sentinel-1" rather than the internal slug. Unknown
  // sources fall through unchanged rather than being dropped — omitting a source we actually used
  // would understate the provenance, which is the opposite of the point.
  const NAMES: Record<string, string> = {
    "sentinel-1": "Sentinel-1 radar",
    "sentinel-2": "Sentinel-2 optical",
    worldcover: "ESA WorldCover",
    openstreetmap: "OpenStreetMap",
    chirps: "CHIRPS rainfall",
    imerg: "IMERG rainfall",
    era5: "ERA5 reanalysis",
    climateserv: "ClimateSERV forecast",
    soilgrids: "SoilGrids",
    worldpop: "WorldPop",
    "malaria-atlas": "Malaria Atlas",
    "jrc-gsw": "JRC surface water",
    "cop-dem": "Copernicus DEM",
  };

  const sources = dataSources.map((s) => NAMES[s] ?? s).join(", ");
  return { author, sources };
}


/**
 * How to present a Fahis verdict to a subscriber.
 *
 * ## Why `unverified` is not a failure
 *
 * This is the part that must not be got wrong. A flood in a remote local government area may never
 * be reported by anything indexable, so "no independent reporting found" is the COMMON rural case —
 * not evidence the warning was wrong. Rendering it as a cross, or in a warning colour, would tell a
 * subscriber their correct warning was a false alarm and quietly destroy the trust the whole
 * accountability agent exists to build.
 *
 * So `unverified` is neutral and its copy says so explicitly. Only `refuted` — where a source
 * affirmatively says the hazard did not happen — reads as a miss, and that verdict is rare and
 * guarded: Fahis downgrades `refuted` to `unverified` without a credible source.
 *
 * ## Why `not_attempted` is separate
 *
 * An outage is not a finding. Collapsing it into `unverified` would count our own downtime as a
 * lack of corroboration and drag the precision figure down for a reason that has nothing to do with
 * the model.
 */
export const VERDICT_META: Record<
  VerdictSummary["verdict"],
  { label: string; icon: string; note: string; tone: "good" | "neutral" | "bad" }
> = {
  confirmed: {
    // Attributed to the agent by name. "Independently confirmed" alone reads as a passive claim
    // the platform makes about itself; naming Fahis-AI says WHO checked and that it is a distinct
    // step from the advisory — which is the whole point of an agent that runs backward and asks
    // whether we were right.
    label: "Independently confirmed by Fahis-AI",
    icon: "✓",
    note: "Outside reporting described this hazard, in this area, at this time.",
    tone: "good",
  },
  partial: {
    label: "Partly confirmed",
    icon: "≈",
    note: "Reporting describes the right area but a different hazard or severity.",
    tone: "neutral",
  },
  refuted: {
    label: "Not corroborated",
    icon: "✕",
    note: "A credible source indicates this hazard did not occur. We record this against ourselves.",
    tone: "bad",
  },
  unverified: {
    label: "No independent reporting found",
    icon: "○",
    // The honest, load-bearing sentence. Never present this as a false alarm.
    note: "Common for rural areas — many real events are never reported publicly. This is not counted as a false alarm.",
    tone: "neutral",
  },
  not_attempted: {
    label: "Not yet checked",
    icon: "…",
    note: "Verification was unavailable for this cycle. Recorded as an outage, not as a finding.",
    tone: "neutral",
  },
};
