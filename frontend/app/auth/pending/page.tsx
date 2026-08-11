import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { getAccount } from "@/lib/session";

import PendingPanel from "./PendingPanel";

export const metadata: Metadata = { title: "Confirm your email" };

/**
 * The address SHELTER's mail actually comes from — what the user should allow-list.
 *
 * Read from the environment rather than written into the component, because the hardcoded
 * value was **wrong**: it said `alerts@shelter.zerorate.io`, which does not exist. Anyone
 * who followed that advice would have allow-listed a non-existent address and had their
 * warnings filtered anyway — a failure that looks like the advice worked.
 *
 * Mirrors the backend's `BREVO_SENDER_EMAIL` / `SMTP_FROM`. Deliberately **not**
 * `NEXT_PUBLIC_`: it is rendered server-side into the HTML, so the browser never needs the
 * variable, and keeping it server-only means it cannot drift into a client bundle where a
 * future refactor might treat it as configuration the browser owns.
 *
 * The default is the current production sender, so a deployment that forgets the variable
 * still shows a real address rather than `undefined`.
 */
const SENDER_EMAIL = process.env.SHELTER_SENDER_EMAIL ?? "no-reply@zerorate.io";

/** Live: the verification state changes in another tab, so this must never be cached. */
export const dynamic = "force-dynamic";

/**
 * The waiting room between signup and the dashboard.
 *
 * ## Why this page exists rather than dropping people at the dashboard
 *
 * A mistyped or throwaway address produces an account that cannot be contacted — and for
 * this product that means a flood or crop-stress warning that silently goes nowhere. The
 * address is the single thing the service depends on, so it is confirmed while the user is
 * still present and motivated to fix a typo, not weeks later when an alert fails to arrive.
 *
 * It also raises the cost of scripted signups without taxing a real person: a mailbox
 * round trip is a step an automated signup has to bother with, whereas a CAPTCHA would
 * make a farmer on a low-end phone over a metered connection solve a puzzle to receive a
 * warning.
 *
 * ## Three states, deliberately distinguished
 *
 *   * **no session** — sent to sign-in. Nothing to confirm and nobody to confirm it for.
 *   * **already verified** — sent straight on to the dashboard. Someone who clicked their
 *     link in another tab and then reloaded this one must not be told to keep waiting.
 *   * **unverified** — the panel renders, with resend.
 */
export default async function PendingPage({
  searchParams,
}: {
  searchParams: Promise<{
    welcome?: string;
    sent?: string;
    type?: string;
    reason?: string;
  }>;
}) {
  const params = await searchParams;
  const account = await getAccount();

  if (!account) {
    redirect("/auth/login");
  }

  // Verified in another tab, or arrived here by typing the URL. Do not make them wait for
  // something that has already happened.
  if (account.email_verified) {
    redirect("/dashboard");
  }

  return (
    <PendingPanel
      email={account.email}
      firstName={account.first_name}
      isCommercial={params.type === "commercial" || account.kind === "commercial"}
      justSignedUp={params.welcome === "1"}
      // Set when they arrived by trying to SIGN IN, not by signing up. The two need
      // different first sentences: a returning user already knows they have an account and
      // needs to be told the confirmation is still outstanding.
      cameFromSignIn={params.reason === "signin"}
      sendFailed={params.sent === "0"}
      senderEmail={SENDER_EMAIL}
    />
  );
}
