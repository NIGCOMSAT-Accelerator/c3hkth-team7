import type { Metadata, Viewport } from "next";

import AccountMenu from "@/components/AccountMenu";
import ConsortiumMark from "@/components/ConsortiumMark";
import ShelterLogo from "@/components/ShelterLogo";
import ThemeToggle from "@/components/ThemeToggle";
import { DEV_DOCS_URL } from "@/lib/links";
import { getAccount } from "@/lib/session";

import "./globals.css";

const SITE = process.env.NEXT_PUBLIC_SITE_URL ?? "https://shelter.zerorate.io";
const TAGLINE =
  "7-day intelligence gives farmers what satellites never did: time to act.";

// The developer reference is deliberately NOT linked here. It lives behind the gate at
// /auth/login: the API is only usable with a key, and a key requires a commercial
// account, so an ungated link would send an evaluating developer to a reference they
// cannot act on. The partner docs are reachable from the dashboard after signing in.

export const metadata: Metadata = {
  metadataBase: new URL(SITE),
  title: {
    default: "SHELTER — 7-day satellite early warning for African agriculture",
    template: "%s · SHELTER",
  },
  description: TAGLINE,
  openGraph: {
    title: "SHELTER",
    description: TAGLINE,
    url: SITE,
    siteName: "SHELTER",
    type: "website",
  },
  robots: { index: true, follow: true },
  // Sourced from freepass.africa so the portal is visually continuous with the rest
  // of the FreePass estate — a subscriber arriving from there should not feel handed
  // off to an unrelated product. Declared explicitly rather than relying on Next's
  // file convention because the asset is a PNG, and `app/favicon.ico` would have to
  // actually be an ICO.
  icons: {
    icon: [{ url: "/favicon.png", type: "image/png" }],
    apple: [{ url: "/favicon.png" }],
  },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#0b001b" },
  ],
};

/**
 * `async` so the nav can read the session.
 *
 * The topbar previously rendered "Sign in" and "Get started" unconditionally, which
 * invited a signed-in user to create a second account and gave them no way to sign out.
 * Reading the account here means one nav serves both states.
 *
 * `getAccount()` costs one backend call per render. Acceptable because every page in this
 * app is already `force-dynamic` (assessments are live), so there is no cache being
 * invalidated — and it fails soft: a null account renders the marketing nav rather than
 * erroring, so the site still works with IAM unreachable.
 */
export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const account = await getAccount();
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/*
          Applies the stored theme before first paint. Without this the page
          renders in the OS theme and then snaps to the stored one — a visible
          flash on every navigation.
        */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('shelter-theme');if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}})()`,
          }}
        />
      </head>
      <body>
        <header className="topbar">
          <div className="shell topbar__inner">
            <a href="/" className="brand" aria-label="SHELTER home">
              <ShelterLogo size="sm" />
            </a>
            <nav className="nav">
              {/*
                Partner attribution sits *before* the nav links and behind a divider,
                at reduced opacity. Placing it inline with the links would imply it is
                navigation; giving it its own slot reads as provenance, which is what
                it is. Hidden below 1024px — at 360px the lockup and four links cannot
                share a row, and the footer carries the same attribution with room to
                state the relationship in words.
              */}
              <span className="nav__partners">
                <ConsortiumMark variant="row" />
              </span>
              {/*
                Two different navs, because the two audiences want different things. A
                visitor is deciding whether to sign up; a subscriber is trying to reach
                their own alerts. Showing both sets at once is how "Get started" ended up
                in front of people who had already started.
              */}
              {account ? (
                <>
                  <a href="/portal">Portal</a>
                  <a href="/dashboard">Live map</a>
                  {/*
                    Developer docs, for aggregators only.
                    
                    Gated on `kind` rather than shown to everyone with a note, because the
                    reference is only actionable with an API key and only commercial accounts
                    can hold one. An individual clicking it would reach a document describing
                    a credential they cannot obtain — the same reasoning that removed this
                    link from the public footer.

                    Opens in a new tab: it is a reference consulted WHILE integrating, so
                    replacing the portal tab loses the context the developer is working in.
                  */}
                  {account.kind === "commercial" && (
                    <a
                      href={DEV_DOCS_URL}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      Developer docs
                    </a>
                  )}
                  <ThemeToggle />
                  <AccountMenu account={account} />
                </>
              ) : (
                <>
                  <a href="/auth/login">Sign in</a>
                  <a href="/auth/signup" className="nav__cta">
                    Get started
                  </a>
                  <ThemeToggle />
                </>
              )}
            </nav>
          </div>
        </header>

        <main>{children}</main>

        {/*
          The footer carries the consortium claim, because that claim is the product's
          differentiator rather than decoration: NIGCOMSAT is why an alert arrives when
          the flood has taken out the towers, and ZeroRate is why the portal opens on a
          phone with no data balance. Stating each one *with what it does* is what makes
          it read as infrastructure rather than a sponsor logo.
        */}
        <footer className="footer">
          <div className="shell footer__grid">
            <div className="footer__brand">
              {/* The acronym is expanded here and on the landing hero only. A
                  first-time reader needs it once; repeating it in the header would
                  cost phone-width for information already given. */}
              <ShelterLogo size="md" withTagline />
              {/* Scope, then state, then reach — in that order. Naming all three
                  hazards matches the email footer and the tracks column, so the
                  product does not describe itself as narrower here than it does
                  three columns across. "Starting with" is load-bearing: it claims
                  the roadmap without implying flood and health already ship. */}
              <p className="footer__tagline">
                Satellite-enabled &amp; AI-powered early warning for flood, crop
                and health risk across Africa — starting with Agricultural
                Intelligence, delivered on the channels people already have.
              </p>
              <ConsortiumMark
                variant="stacked"
                label="Delivered in partnership with"
              />
            </div>

            <div className="footer__cols">
              {/*
                Three entries, one per thing a visitor can actually do here: identify as
                an individual, identify as an aggregator, or look at live output. The
                separate "Sign in" link is gone — both signup routes and the dashboard
                lead to the gate anyway, so a fourth link only added a decision.

                The intelligence-tracks column that used to sit beside this is also gone.
                Three tracks with five capabilities each made the footer taller than most
                of the pages carrying it, and the landing page and dashboard both present
                the same material with room to do it properly. The tagline above still
                names all three hazards, so scope is not lost.

                No developer column: API docs live behind the gate, because the reference
                is only actionable with a key.
              */}
              <div className="footer__col">
                <h3 className="footer__heading">Product</h3>
                <a href="/auth/signup">For individuals</a>
                <a href="/auth/signup?type=commercial">For aggregators</a>
                <a href="/dashboard">Live dashboard</a>
              </div>
              <div className="footer__col">
                <h3 className="footer__heading">Infrastructure</h3>
                <span className="footer__fact">
                  NIGCOMSAT-1R satellite broadcast — reaches subscribers when
                  terrestrial networks fail
                </span>
                <span className="footer__fact">
                  FreePass ZeroRate CDN — zero-rated delivery, no data cost to
                  the subscriber
                </span>
              </div>
            </div>
          </div>

          <div className="shell footer__legal">
            <p>
              © {new Date().getFullYear()} SHELTER. Operated in partnership by
              FreePass and NIGCOMSAT.
            </p>
            <p>
              Built on open Earth Observation data — Copernicus Sentinel-1 and
              Sentinel-2, CHIRPS, WorldPop, OpenStreetMap. Hazard assessments are
              probabilistic forecasts, not guarantees, and are intended to inform
              local decisions rather than replace official emergency guidance.
            </p>
          </div>
        </footer>
      </body>
    </html>
  );
}
