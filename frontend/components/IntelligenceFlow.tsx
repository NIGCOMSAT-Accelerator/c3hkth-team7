/**
 * The SHELTER story as one animated SVG: satellite → Earth Observation → machine
 * learning → actionable intelligence → the person who acts on it → and back, to check
 * whether we were right.
 *
 * ## "Intelligence", not "warning"
 *
 * The vocabulary here is deliberate and it is positioning, not decoration. "Warning"
 * describes one hazard-driven event and implies the product only speaks when something
 * is wrong — which undersells two of the three tracks and all of the routine use. A
 * cooperative planning irrigation, a lender pricing drought exposure and a health
 * ministry watching standing water are all consuming *intelligence*; only one of them is
 * receiving a warning. The 7-day product is a continuous read on a place, of which the
 * alert is the exceptional case.
 *
 * The one place "early warning" is still correct verbatim is the strapline the consortium
 * uses in the footer and in email — that is a fixed brand line, and a diagram is free to
 * describe the mechanism more precisely than a strapline does.
 *
 * ## Why an SVG animation rather than a video or a Lottie file
 *
 * Three reasons, and the first is the whole point of the product:
 *
 *   1. **Weight.** This is ~6 KB of markup, gzipped to under 2. A short MP4 explaining
 *      the same thing is 2–4 MB. The target user is on a metered connection in rural
 *      Nigeria, and the ZeroRate CDN carries the cost — so a hero animation that costs a
 *      farmer their data balance would contradict the thing we are advertising.
 *   2. **It scales and recolours.** `currentColor` throughout means one asset serves
 *      light theme, dark theme, and any accent change.
 *   3. **It degrades honestly.** With `prefers-reduced-motion` the animation stops and
 *      the diagram still reads as a static explanation — a video would either autoplay
 *      against the user's stated preference or show a black rectangle.
 *
 * ## What is animated, and why those things
 *
 * The motion carries meaning rather than decorating:
 *
 *   * the **satellite** traverses its orbit, because the pass is what starts a cycle;
 *   * the **downlink** pulses along the beam, because data arrives in bursts, not
 *     continuously;
 *   * the **radar sweep** widens through the cloud band, which is the single most
 *     important technical claim on the page — Sentinel-1 sees when optical cannot;
 *   * the **ML nodes** fire in sequence, because the pipeline is a fixed linear
 *     sequence, not a mesh (matching the actual architecture);
 *   * the **alert** travels outward to a household, because the product ends at a
 *     person, not a dashboard;
 *   * the **NIGCOMSAT uplink** rises back to orbit, because the broadcast leg runs the
 *     opposite way to the downlink — the same satellite infrastructure that observed the
 *     hazard also carries the intelligence out when the towers are gone;
 *   * the **Fahis return arc** runs backwards under the pipeline, dashed and slower than
 *     everything else, because verification happens days later and against the flow.
 *
 * ## The two additions, and why each earns its space
 *
 * **Fahis.** A diagram of four forward stages is a diagram of a system that never checks
 * itself, and that is the single most common and most fatal objection to an ML early
 * warning product: *how would you know if it were wrong?* Fahis is the answer, so it
 * belongs in the picture. It is drawn deliberately unlike the other four — off the main
 * rail, dashed, reverse-travelling, on a slower cycle — because in the architecture it is
 * off the main line, writes to `verifications` only, and can never reach an advisory. A
 * fifth node sitting inline after HERALD would draw a system where verification feeds
 * back into scoring, which is exactly the thing the codebase forbids.
 *
 * **NIGCOMSAT.** Previously four words in a channel list, which buried the differentiator.
 * Every competing service stops at a phone with a signal; the claim worth drawing is that
 * a flood which takes out the towers does not take out the delivery. So it gets its own
 * uplink, its own label, and — importantly — the honest caveat that broadcast is one-way,
 * because a `SENT` receipt on that channel means the gateway accepted the burst and not
 * that anyone received it.
 *
 * Everything is CSS `animation` on SVG geometry — no JS, no runtime cost, and it runs
 * on the compositor rather than the main thread, so it cannot make the page janky.
 */

export default function IntelligenceFlow({
  className,
}: {
  className?: string;
}) {
  return (
    <div className={`flowviz${className ? ` ${className}` : ""}`}>
      <svg
        viewBox="0 0 720 392"
        role="img"
        aria-labelledby="flowviz-title flowviz-desc"
        className="flowviz__svg"
      >
        <title id="flowviz-title">
          How SHELTER turns satellite data into actionable intelligence
        </title>
        {/* The full narrative in text, for screen readers and for anyone who has
            motion disabled. The animation is an illustration of this, not a
            replacement for it. */}
        <desc id="flowviz-desc">
          A satellite passes overhead and downlinks Earth Observation data. Radar
          penetrates cloud that blocks optical sensors. Machine-learning models measure
          standing water and crop stress, a risk model fuses them with rainfall and
          population, and a plain-language advisory is delivered to the household — over
          email, WhatsApp and SMS where there is a network, and by one-way NIGCOMSAT-1R
          satellite broadcast where there is not. Days later a fifth agent, Fahis, works
          backwards: it searches independent reporting to judge whether the intelligence
          was right, and records the verdict. Fahis can never alter an advisory.
        </desc>

        <defs>
          <linearGradient id="flowBeam" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.55" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
          </linearGradient>
          <linearGradient id="flowGround" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.14" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0.02" />
          </linearGradient>
          {/* The orbit path is referenced by the satellite's motion, so the two can
              never disagree about where it travels. */}
          <path id="flowOrbit" d="M40 62 Q360 8 680 62" />
        </defs>

        {/* ---------------- 1 · Orbit and the satellite pass ---------------- */}
        <use href="#flowOrbit" fill="none" stroke="currentColor" strokeWidth="1"
             strokeDasharray="3 5" opacity="0.28" />

        <g className="flowviz__sat">
          <animateMotion dur="14s" repeatCount="indefinite" rotate="auto">
            <mpath href="#flowOrbit" />
          </animateMotion>
          {/* Body plus two solar panels — enough to read as a satellite at 40px. */}
          <rect x="-5" y="-3.5" width="10" height="7" rx="1.5" fill="currentColor" />
          <rect x="-12" y="-2" width="6" height="4" rx="1" fill="currentColor" opacity="0.6" />
          <rect x="6" y="-2" width="6" height="4" rx="1" fill="currentColor" opacity="0.6" />
        </g>

        <text x="40" y="30" className="flowviz__stage">SENTINEL-1 · SENTINEL-2</text>
        <text x="40" y="45" className="flowviz__note">
          Radar and optical, every few days
        </text>

        {/* ---------------- 2 · Downlink through cloud ---------------- */}
        {/* The beam is a cone from the satellite's mid-pass position to the ground. */}
        <path d="M355 66 L300 196 L410 196 Z" fill="url(#flowBeam)" />

        {/* Cloud band. Sits between satellite and ground because that is the physical
            situation the product is built around: the cloud causing the flood is the
            cloud hiding it. */}
        <g opacity="0.5">
          <ellipse cx="318" cy="118" rx="30" ry="11" fill="currentColor" opacity="0.16" />
          <ellipse cx="352" cy="112" rx="38" ry="13" fill="currentColor" opacity="0.2" />
          <ellipse cx="392" cy="120" rx="26" ry="10" fill="currentColor" opacity="0.14" />
        </g>

        {/* The radar sweep: expands from the satellite through the cloud, which is the
            claim optical-only systems cannot make. */}
        <circle className="flowviz__sweep" cx="355" cy="70" r="14"
                fill="none" stroke="currentColor" strokeWidth="1.4" />
        <circle className="flowviz__sweep flowviz__sweep--b" cx="355" cy="70" r="14"
                fill="none" stroke="currentColor" strokeWidth="1.4" />

        {/* Data packets travelling down the beam — bursts, not a continuous stream. */}
        <circle className="flowviz__packet" cx="355" cy="70" r="2.6" fill="currentColor" />
        <circle className="flowviz__packet flowviz__packet--b" cx="355" cy="70" r="2.2"
                fill="currentColor" />

        <text x="430" y="112" className="flowviz__stage">CLOUD-PENETRATING RADAR</text>
        <text x="430" y="127" className="flowviz__note">
          Monitoring continues through the storm
        </text>

        {/* ---------------- 3 · Ground: the monitored plot ---------------- */}
        <path d="M0 196 H720 V320 H0 Z" fill="url(#flowGround)" />
        <path d="M0 196 H720" stroke="currentColor" strokeWidth="1" opacity="0.25" />

        {/* Field rows, to read as cultivated land rather than abstract ground. */}
        <g opacity="0.3" stroke="currentColor" strokeWidth="1">
          <path d="M292 214 H418" /><path d="M286 226 H424" /><path d="M280 238 H430" />
        </g>
        {/* The plot marker: the specific place being watched. */}
        <circle cx="355" cy="226" r="4" fill="currentColor" />
        <circle className="flowviz__ping" cx="355" cy="226" r="4"
                fill="none" stroke="currentColor" strokeWidth="1.5" />

        {/* ---------------- 4 · The ML pipeline ---------------- */}
        {/* Four nodes on a line, firing in sequence — the architecture is a fixed linear
            pipeline, so a mesh or a cycle here would misdescribe it. */}
        <g transform="translate(96 276)">
          <path d="M0 0 H528" stroke="currentColor" strokeWidth="1" opacity="0.25" />
          {[
            { x: 0, label: "SCOUT", sub: "discover" },
            { x: 176, label: "ANALYST", sub: "measure" },
            { x: 352, label: "ORACLE", sub: "decide" },
            { x: 528, label: "HERALD", sub: "deliver" },
          ].map((n, i) => (
            <g key={n.label} transform={`translate(${n.x} 0)`}>
              <circle
                className="flowviz__node"
                style={{ animationDelay: `${i * 0.9}s` }}
                r="6"
                fill="currentColor"
              />
              <text y="22" className="flowviz__stage" textAnchor="middle">
                {n.label}
              </text>
              <text y="35" className="flowviz__note" textAnchor="middle">
                {n.sub}
              </text>
            </g>
          ))}
          {/* The travelling pulse: data moving along the pipeline. */}
          <circle className="flowviz__pulse" r="3.5" fill="currentColor" cy="0" />

          {/* ------- 4b · Fahis: the accountability agent, off the main line -------

              Drawn as a return arc BELOW the rail rather than a fifth node on it. That
              is not a layout preference — in the architecture Fahis runs days later,
              writes only to `verifications`, and structurally cannot enqueue anything
              that reaches an advisory. An inline fifth node would draw a feedback loop
              into scoring, which is the exact thing the codebase forbids and tests for.

              The arrowhead points LEFT, back towards the start, because the question it
              asks is retrospective. */}
          <path
            id="flowFahis"
            d="M528 0 C528 54 176 54 176 0"
            fill="none"
            stroke="currentColor"
            strokeWidth="1"
            strokeDasharray="4 4"
            opacity="0.42"
          />
          {/* Reverse-travelling verdict, on a slower cycle than the forward pulse so the
              eye reads it as a separate, later process rather than part of the flow. */}
          <circle className="flowviz__verdict" r="3" fill="currentColor" />
          <path d="M176 0 l5 -5 M176 0 l5 5" fill="none" stroke="currentColor"
                strokeWidth="1.2" opacity="0.5" transform="translate(0 1)" />

          <g transform="translate(352 62)">
            <text className="flowviz__stage flowviz__stage--verify" textAnchor="middle">
              FAHIS · WERE WE RIGHT?
            </text>
            <text y="13" className="flowviz__note" textAnchor="middle">
              Independent reporting checked days later · verdict recorded, never fed back
            </text>
          </g>
        </g>

        {/* ---------------- 5 · Delivery to a household ----------------

            Two legs, deliberately drawn as different things:

              * terrestrial — email, WhatsApp, SMS. Works when there is a network.
              * NIGCOMSAT-1R broadcast — works when there is not. This is the leg that
                distinguishes the product, so it is drawn rather than listed: the beam
                goes UP from the ground station and comes back DOWN over the household,
                because a flood that takes out the towers does not take out the satellite.

            The broadcast beam is dashed and one-way-arrowed. One-way is a real property,
            not a stylistic choice — there is no delivery confirmation on that channel, so
            drawing it as a round trip would overstate what the receipt means. */}

        {/* Ground station uplink, rising back to orbit. */}
        <g transform="translate(510 196)">
          <path d="M0 22 L10 22 L5 4 Z" fill="currentColor" opacity="0.6" />
          <path d="M-6 22 H16" stroke="currentColor" strokeWidth="1.2" opacity="0.5" />
        </g>
        <path
          id="flowUplink"
          d="M515 194 Q560 120 614 186"
          fill="none"
          stroke="currentColor"
          strokeWidth="1"
          strokeDasharray="3 4"
          opacity="0.38"
        />
        {/* The burst travelling up and over: the broadcast leg in motion. */}
        <circle className="flowviz__burst" r="2.4" fill="currentColor">
          <animateMotion dur="4.2s" repeatCount="indefinite" begin="1.2s">
            <mpath href="#flowUplink" />
          </animateMotion>
        </circle>

        <g transform="translate(596 196)">
          {/* A house: the product ends at a person, not a dashboard. */}
          <path d="M0 34 L18 18 L36 34 V52 H0 Z" fill="currentColor" opacity="0.85" />
          <path d="M14 40 H22 V52 H14 Z" fill="url(#flowGround)" opacity="0.9" />
          {/* Alert rings arriving. */}
          <circle className="flowviz__alert" cx="18" cy="10" r="6"
                  fill="none" stroke="currentColor" strokeWidth="1.6" />
          <circle className="flowviz__alert flowviz__alert--b" cx="18" cy="10" r="6"
                  fill="none" stroke="currentColor" strokeWidth="1.6" />
        </g>

        {/* "Intelligence", not "warning" — see the module docstring. The 7-day product is
            a continuous read on a place; the alert is its exceptional case. */}
        <text x="470" y="158" className="flowviz__stage">7-DAY INTELLIGENCE</text>
        <text x="470" y="172" className="flowviz__note">
          Email · WhatsApp · SMS where there is a network
        </text>
        <text x="470" y="184" className="flowviz__note">
          NIGCOMSAT-1R broadcast where there is not — one-way, no signal required
        </text>
      </svg>
    </div>
  );
}
