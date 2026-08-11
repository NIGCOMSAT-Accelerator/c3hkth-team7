import IntelligenceFlow from "@/components/IntelligenceFlow";
import IntelligenceTracks from "@/components/IntelligenceTracks";
import { safeApi } from "@/lib/api";
import { CHANNEL_LABEL } from "@/lib/types";

export const dynamic = "force-dynamic";

const PROBLEM = [
  {
    title: "Optical satellites go blind exactly when it matters",
    body: "Sentinel-2 cannot see through a rainstorm. The cloud that hides the ground is the same cloud that is causing the flood, so optical-only systems report nothing during the event they exist to detect.",
  },
  {
    title: "The damage does not stop at the water line",
    body: "Standing water becomes waterlogged roots in days, a lost harvest in weeks, and a malaria surge in roughly six. Each stage is predictable from the one before, and each is warned about separately or not at all.",
  },
  {
    title: "Internet alerts arrive after the network fails",
    body: "A flood takes out power and backhaul. The channel most warning systems depend on is the channel the disaster removes first.",
  },
];

/**
 * The five agents, described by what they DELIVER rather than how they are built.
 *
 * ## Why the implementation detail was removed
 *
 * This previously named the specific catalogues queried, the file format read, the transport used
 * to read it, and the ML framework behind the models. All true, and all wrong for a public page:
 *
 *   * **It is a build specification.** A competitor reading it learns which data sources to
 *     integrate, how to fetch them economically, and roughly what the models do — the part that
 *     took the longest to get right.
 *   * **It is not what anyone is buying.** A farmer decides on "will I know in time?", a bank on
 *     "can I rely on this?". Neither question is answered by a file format.
 *   * **It dates badly.** Naming a framework means the page is wrong the day it changes, and the
 *     claim that mattered — monitoring continues through cloud — was never about the framework.
 *
 * What is kept is every claim a customer can hold us to: cloud-piercing radar, a 7-day outlook,
 * exposure-aware severity, delivery over satellite when the ground network fails, and independent
 * verification afterwards. Those are commitments, not internals.
 *
 * Sentinel-1 and Sentinel-2 stay named in the footer's provenance line, because attribution of
 * open data is a licence obligation and a credibility asset — "we use Copernicus" is a different
 * statement from "here is how we read it".
 */
const ANSWER = [
  {
    step: "01",
    name: "Scout",
    body: "Watches for new satellite coverage of every area under monitoring, and knows when it arrives. When cloud hides the ground it says so and switches to radar rather than reporting nothing — the moment most services go quiet is the moment ours has to keep working.",
  },
  {
    step: "02",
    name: "Analyst",
    body: "Measures what changed on that specific piece of land: how much of it is holding water, and how much of the crop is under stress. Measurements only — no interpretation, no judgement, just the numbers the decision will rest on.",
  },
  {
    step: "03",
    name: "Oracle",
    body: "Decides whether those measurements amount to a hazard, how severe it is, and how confident we are. Weighs the 7-day rainfall outlook and how many people are actually exposed, then names what the hazard is likely to trigger next — flooding leads to waterlogged crops, which lead to standing water and the health risk that follows.",
  },
  {
    step: "04",
    name: "Herald",
    body: "Turns the decision into an advisory in the subscriber's own language, grounded strictly in what was measured, and delivers it on every channel they chose. When the ground network is down — which is when a flood warning matters most — it escalates to NIGCOMSAT-1R satellite broadcast.",
  },
  {
    // Fahis belongs on the public page precisely because it is the accountability claim: it is why
    // the other four can be trusted, and no competitor gains anything from knowing we check.
    step: "05",
    name: "Fahis",
    body: "Runs backward, days later, and asks the one question nothing else asks: were we right? It looks for independent confirmation of the hazard we warned about and records a verdict. Where a remote area has no coverage either way, that is recorded as unverified — never quietly counted as a false alarm. No verdict can ever change an advisory, so accountability stays separate from the warning it judges.",
  },
];

export default async function HomePage() {
  const health = await safeApi.health();
  const channels = health?.channels_configured ?? [];

  return (
    <>
      <section className="shell" style={{ padding: "88px 24px 64px" }}>
        <p
          style={{
            margin: "0 0 18px",
            fontSize: 12,
            letterSpacing: "0.14em",
            textTransform: "uppercase",
            color: "var(--accent)",
            fontWeight: 650,
          }}
        >
          Africa&rsquo;s sovereign amber alert network
        </p>

        <h1
          style={{
            fontSize: "clamp(34px, 5.4vw, 60px)",
            fontWeight: 800,
            maxWidth: "17ch",
            margin: "0 0 22px",
          }}
        >
          7-day intelligence gives farmers what satellites never did:{" "}
          <span style={{ color: "var(--accent)" }}>time to act.</span>
        </h1>

        <p
          style={{
            fontSize: 19,
            color: "var(--text-secondary)",
            maxWidth: "62ch",
            margin: "0 0 32px",
          }}
        >
          When floods strike Sub-Saharan Africa, communities don&rsquo;t just
          lose homes — they lose harvests to waterlogging and lives to malaria
          weeks later. SHELTER watches with cloud-piercing radar, forecasts the
          whole cascade, and delivers the warning even when the network is
          down.
        </p>

        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <a className="btn btn--primary" href="/subscribe">
            Activate alerts for my area
          </a>
          <a className="btn btn--ghost" href="/dashboard">
            See the live dashboard
          </a>
        </div>

        {/*
          Placed AFTER the headline and CTAs, not above them. The animation explains a
          claim the reader has just been given — it is evidence, not a hero image, and
          putting it first would push the actual proposition below the fold on a phone.
        */}
        <IntelligenceFlow className="hero__flow" />

        {health && (
          <p className="muted" style={{ fontSize: 13, marginTop: 26 }}>
            <span
              aria-hidden="true"
              style={{
                display: "inline-block",
                width: 8,
                height: 8,
                borderRadius: 999,
                marginRight: 8,
                background:
                  health.status === "ok"
                    ? "var(--viz-rain)"
                    : "var(--sev-warning)",
              }}
            />
            {/*
              The MODE, not the model.
              
              This rendered the resolved model name — so every visitor to the public landing page
              learned which vendor and which version write our advisories. That is a supplier
              relationship, a cost structure and a switching risk, published for no benefit: a
              subscriber cannot act on it, and a competitor can.
              
              "Generated" vs "template" is the part that means something to a reader: whether they
              are getting language written for their situation or the deterministic fallback. The
              exact model stays on `/health`, which is where an operator looks.
            */}
            Pipeline {health.status} · advisories{" "}
            <span className="mono">
              {health.advisory_generator?.provider &&
              health.advisory_generator.provider !== "template"
                ? "generated"
                : "from templates"}
            </span>
            {channels.length > 0 && (
              <>
                {" "}
                · delivering on{" "}
                {channels.map((c) => CHANNEL_LABEL[c] ?? c).join(", ")}
              </>
            )}
          </p>
        )}
      </section>

      <section
        className="shell"
        style={{ paddingBottom: 64 }}
        aria-labelledby="problem"
      >
        <h2 id="tracks"
          style={{
            fontSize: 13,
            letterSpacing: "0.12em",
            textTransform: "uppercase",
            color: "var(--text-muted)",
            marginBottom: 8,
          }}
        >
          Three intelligence tracks
        </h2>
        <p
          style={{
            fontSize: 16,
            color: "var(--text-secondary)",
            maxWidth: "70ch",
            margin: "0 0 24px",
          }}
        >
          One platform, one satellite pipeline, three audiences. The MVP ships{" "}
          <strong style={{ color: "var(--text-primary)" }}>
            Agricultural Intelligence
          </strong>{" "}
          first — Environmental and Public Health Intelligence follow as the next
          phase, on the same engine.
        </p>
        <div style={{ marginBottom: 64 }}>
          <IntelligenceTracks />
        </div>

        <h2 id="problem" style={{ fontSize: 13, letterSpacing: "0.12em", textTransform: "uppercase", color: "var(--text-muted)", marginBottom: 20 }}>
          Why existing warnings miss
        </h2>
        <div className="grid grid--tiles">
          {PROBLEM.map((p) => (
            <article key={p.title} className="card">
              <h3 style={{ fontSize: 16, marginBottom: 8 }}>{p.title}</h3>
              <p style={{ margin: 0, fontSize: 14, color: "var(--text-secondary)" }}>
                {p.body}
              </p>
            </article>
          ))}
        </div>
      </section>

      <section className="shell" style={{ paddingBottom: 72 }} aria-labelledby="pipeline">
        <h2 id="pipeline" style={{ fontSize: 13, letterSpacing: "0.12em", textTransform: "uppercase", color: "var(--text-muted)", marginBottom: 20 }}>
          The five-agent pipeline
        </h2>
        <div className="grid grid--tiles">
          {ANSWER.map((a) => (
            <article key={a.step} className="card">
              <div
                className="mono"
                style={{ color: "var(--accent)", marginBottom: 10, fontWeight: 700 }}
              >
                {a.step}
              </div>
              <h3 style={{ fontSize: 17, marginBottom: 8 }}>{a.name}</h3>
              <p style={{ margin: 0, fontSize: 14, color: "var(--text-secondary)" }}>
                {a.body}
              </p>
            </article>
          ))}
        </div>
      </section>

      <section className="shell" style={{ paddingBottom: 88 }}>
        <div className="card" style={{ background: "var(--surface-sunken)" }}>
          <h2 style={{ fontSize: 20, marginBottom: 10 }}>
            The last mile is a satellite, not a signal bar
          </h2>
          <p
            style={{
              margin: 0,
              maxWidth: "70ch",
              color: "var(--text-secondary)",
              fontSize: 15,
            }}
          >
            WhatsApp, Telegram, Signal, email and Slack all assume the recipient
            has working internet — the assumption a flood breaks first. At
            warning level and above, or when every terrestrial channel has
            failed, SHELTER escalates to a one-way NIGCOMSAT-1R broadcast that
            reaches the whole Ku-band footprint with no ground network at the
            receiving end.
          </p>
        </div>
      </section>
    </>
  );
}
