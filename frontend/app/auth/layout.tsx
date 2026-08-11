import ConsortiumMark from "@/components/ConsortiumMark";
import IntelligenceFlow from "@/components/IntelligenceFlow";

/**
 * Split-screen auth shell: the story on the left, the form on the right.
 *
 * ## Why the left panel exists at all
 *
 * An auth screen is where an evaluating user decides whether to continue. A bare form
 * gives them nothing to decide with — so the left panel carries the proposition and the
 * animation that explains it, and the three intelligence tracks so a cooperative can see
 * the roadmap before committing.
 *
 * ## Why it collapses rather than stacks on mobile
 *
 * Below 900px the panel is **hidden**, not moved above the form. Stacking would push the
 * email field below the fold on a 360px screen, which is the one thing that must never
 * happen on a sign-in page. A shortened version of the proposition is kept above the form
 * instead, so a phone user still knows what they are signing into.
 */
export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="authshell">
      {/* aria-hidden: this is a marketing panel, and a screen-reader user reaching a
          sign-in page wants the form, not a re-read of the landing copy they have
          already passed. The same content is reachable from the site nav. */}
      <aside className="authshell__story" aria-hidden="true">
        {/*
          No logo here. The topbar carries it on every route and the footer repeats it —
          a third instance in the left panel made four marks on one page, which reads as
          uncertainty about the brand rather than confidence in it. The panel now opens on
          the headline, which is the thing a visitor actually needs to read.
        */}
        <div className="authshell__story-inner">
          {/* "Intelligence", not "warning" — the same positioning the animation below
              carries. A warning is one hazard-driven event; intelligence is the
              continuous read on a place that a cooperative planning irrigation or a
              lender pricing drought exposure is actually buying. */}
          <h2 className="authshell__headline">
            Raw satellite Earth Observation, turned into intelligence someone can act
            on.
          </h2>

          <IntelligenceFlow className="authshell__flow" />

          <div className="authshell__tracks">
            <div className="authshell__track" data-state="live">
              <span className="authshell__track-name">Agricultural Intelligence</span>
              <span className="authshell__track-state">Live now</span>
            </div>
            <div className="authshell__track" data-state="next">
              <span className="authshell__track-name">Environmental Intelligence</span>
              <span className="authshell__track-state">Next phase</span>
            </div>
            <div className="authshell__track" data-state="next">
              <span className="authshell__track-name">Public Health Intelligence</span>
              <span className="authshell__track-state">Next phase</span>
            </div>
          </div>

          <div className="authshell__partners">
            <ConsortiumMark variant="row" />
          </div>
        </div>
      </aside>

      <main className="authshell__panel">
        <div className="authshell__panel-inner">
          {/*
            The mobile brand block that sat here is gone too. It existed to show the logo
            where the story panel is hidden — but the TOPBAR is visible at every width, so
            it was duplicating a mark 40px above itself and pushing the form down on
            exactly the screens with least room for it.

            The descriptor line it carried is not lost: each auth page states its own
            purpose in its lede ("Sign in to SHELTER", "Get started"), which is more
            useful than a generic strapline.
          */}
          {children}
        </div>
      </main>
    </div>
  );
}
