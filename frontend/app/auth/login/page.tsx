import type { Metadata } from "next";

import { takeFlash } from "@/lib/session";

import SignInPanel from "./SignInPanel";

export const metadata: Metadata = {
  title: "Sign in",
  description:
    "Sign in to SHELTER — satellite-enabled and AI-powered early warning for flood, crop and health risk.",
  // A sign-in page has no value in search results and appearing there invites
  // credential-phishing lookalikes to rank beside it.
  robots: { index: false, follow: false },
};

/**
 * The "just verified" notice arrives in a one-shot httpOnly cookie (see `setFlash`), not a
 * query parameter — nothing about the session belongs in browser history, Referer headers
 * or CDN logs. It is set when someone confirmed their address in a browser with no session
 * — the normal case when the link is opened from a phone's mail app, which is often a
 * different browser from the one they signed up in. Without an acknowledgement here, a
 * successful confirmation would look like it had failed: they clicked a link and got a
 * login form.
 *
 * `?reason=suspended` comes from the dashboard gate.
 */
export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string; next?: string; ended?: string }>;
}) {
  const params = await searchParams;
  const flash = await takeFlash();

  return (
    <SignInPanel
      // The real configured value, not a hardcoded 15. `IAM_IDLE_TIMEOUT_MINUTES` is a
      // setting, so a deployment that lengthens it would otherwise have this page state a
      // number that is simply untrue — and the whole point of naming the reason is to be
      // believed.
      idleMinutes={Number(process.env.IAM_IDLE_TIMEOUT_MINUTES ?? 15) || 15}
      justVerified={flash === "verified"}
      idleEnded={flash === "idle" || params.ended === "idle"}
      suspended={params.reason === "suspended"}
    />
  );
}
