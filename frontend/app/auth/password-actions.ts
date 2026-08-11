"use server";

import { api } from "@/lib/api";

/**
 * Breached-password screening for the live validator.
 *
 * ## Why a Server Action rather than a fetch from the browser
 *
 * The browser could call `api.pwnedpasswords.com` directly — the k-anonymity protocol is
 * designed for it. Going through the server instead means no subscriber's device ever makes
 * a request to a third party during signup, so their IP never appears in HIBP's logs
 * alongside a password-prefix query. It also shares one cache across all subscribers.
 *
 * ## Returns a shape that cannot be mistaken for "safe"
 *
 * `checked: false` means the screening did not happen — disabled, or HIBP unreachable. The
 * caller must not render a reassuring state in that case: claiming a password is clean on
 * the strength of a check that never ran is worse than saying nothing.
 */
export interface BreachResult {
  breached: boolean;
  timesSeen: number;
  checked: boolean;
}

export async function screenPassword(password: string): Promise<BreachResult> {
  // Nothing below 8 characters is worth a round trip: it already fails the 12-character
  // minimum, so the requirement list is telling the user the actionable thing.
  if (!password || password.length < 8) {
    return { breached: false, timesSeen: 0, checked: false };
  }

  try {
    const result = await api.checkPassword(password);
    return {
      breached: result.breached,
      timesSeen: result.times_seen,
      checked: result.checked,
    };
  } catch {
    // Fails open, matching the backend. A network blip must not block a signup or, worse,
    // tell someone their perfectly good password is compromised.
    return { breached: false, timesSeen: 0, checked: false };
  }
}
