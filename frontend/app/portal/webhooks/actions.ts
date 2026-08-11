"use server";

import { revalidatePath } from "next/cache";

import { recordPortalEvent } from "@/app/auth/session-actions";
import { api } from "@/lib/api";
import { getSessionToken } from "@/lib/session";

/**
 * Creating, testing and removing webhook endpoints.
 *
 * ## Why this did not exist before
 *
 * The page's "+ Create" button linked to `/portal/webhooks/new`, a route that was never built — so
 * the one call-to-action on the page produced a **404**. Reported during MVP review.
 *
 * The route was missing for a reason rather than by oversight: every webhook endpoint required
 * `platform:operate`, a scope only the operations team's key holds, so a portal session could not
 * have created a subscription even with a form in front of it. Fixing the form without fixing the
 * scope would have replaced a 404 with a 403.
 *
 * The API side is now scoped per aggregator (`webhook_caller` resolves either a platform key or an
 * aggregator key with `webhooks:manage`, and every per-subscription route proves ownership), so the
 * form can exist.
 *
 * ## The signing secret appears exactly once
 *
 * `POST /webhook/subscriptions` returns it in the creation response and stores only a hash. There is
 * no reveal endpoint — a leaked secret can forge flood alerts into a payout engine, which is why
 * rotation is a hard cutover with no grace period. So this hands the plaintext back for a single
 * render and never persists it: not in a cookie, not in the audit detail, not in the cache.
 */

export type WebhookState = {
  ok: boolean;
  message: string;
  /** The signing secret, for one render only. Never persisted. */
  secret?: string;
  endpointName?: string;
};

/** Events the form offers. Anything else is rejected before it reaches the API. */
const OFFERED_EVENTS = new Set(["alert.created", "assessment.completed"]);

export async function createWebhook(
  _prev: WebhookState,
  formData: FormData,
): Promise<WebhookState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session has expired. Sign in again." };
  }

  const name = String(formData.get("name") ?? "").trim();
  const url = String(formData.get("url") ?? "").trim();
  const minSeverity = String(formData.get("min_severity") ?? "").trim();
  const workspaceId = String(formData.get("workspace_id") ?? "").trim();

  // A checkbox name is user input. The API would refuse an unknown event, but the form must not be
  // the thing relying on that.
  const events = formData
    .getAll("events")
    .map(String)
    .filter((e) => OFFERED_EVENTS.has(e));

  if (!name) {
    return { ok: false, message: "Give the endpoint a name you will recognise later." };
  }
  if (!url) {
    return { ok: false, message: "Enter the URL that should receive events." };
  }
  // Checked here as well as server-side: an http:// endpoint would carry a farmer's plot location
  // and contact address in clear text across the public internet.
  if (!url.startsWith("https://")) {
    return {
      ok: false,
      message:
        "The URL must start with https:// — deliveries carry plot locations and contact " +
        "addresses, so an unencrypted endpoint is refused.",
    };
  }
  if (events.length === 0) {
    return {
      ok: false,
      message: "Choose at least one event, or the endpoint would never receive anything.",
    };
  }

  try {
    const created = await api.createWebhook(token, {
      name,
      url,
      events,
      // Blank means every severity — the same convention as a channel binding with no floor.
      min_severity: minSeverity || null,
      // Blank means every workspace this account owns. Not validated here: the API checks
      // membership and returns 404 for one the caller does not own, and duplicating that list
      // client-side would be a second copy of the tenancy rule.
      workspace_id: workspaceId || null,
    });

    // Name and id only. The secret must never reach the audit log.
    await recordPortalEvent("webhook.created", `${created.name} · ${created.id}`);
    revalidatePath("/portal/webhooks");

    return {
      ok: true,
      message:
        "Endpoint created. Copy the signing secret now — this is the only time it is shown, " +
        "and we cannot recover it. Verify every delivery with it.",
      secret: created.secret,
      endpointName: created.name,
    };
  } catch (error) {
    // The API's own message is surfaced unchanged: it explains a scope refusal in terms the
    // aggregator can act on, which a generic string cannot.
    return {
      ok: false,
      message:
        error instanceof Error
          ? error.message
          : "Could not create the endpoint. Please try again.",
    };
  }
}

export async function sendTestDelivery(
  _prev: WebhookState,
  formData: FormData,
): Promise<WebhookState> {
  const token = await getSessionToken();
  if (!token) return { ok: false, message: "Your session has expired. Sign in again." };

  const id = String(formData.get("subscription_id") ?? "").trim();
  if (!id) return { ok: false, message: "No endpoint selected." };

  try {
    await api.testWebhook(token, id);
    revalidatePath("/portal/webhooks");
    return {
      ok: true,
      message:
        "Test payload sent. It is deliberately obvious as synthetic so it cannot be mistaken " +
        "for a real warning — check your handler received and verified it.",
    };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not send the test.",
    };
  }
}

export async function deleteWebhook(
  _prev: WebhookState,
  formData: FormData,
): Promise<WebhookState> {
  const token = await getSessionToken();
  if (!token) return { ok: false, message: "Your session has expired. Sign in again." };

  const id = String(formData.get("subscription_id") ?? "").trim();
  if (!id) return { ok: false, message: "No endpoint selected." };

  try {
    await api.deleteWebhook(token, id);
    await recordPortalEvent("webhook.deleted", id);
    revalidatePath("/portal/webhooks");
    return { ok: true, message: "Endpoint removed. It will receive nothing further." };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not remove the endpoint.",
    };
  }
}
