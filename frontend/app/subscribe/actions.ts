"use server";

import { api, ApiError } from "@/lib/api";
import { getSessionToken } from "@/lib/session";
import type { Channel, Severity } from "@/lib/types";

export interface SubscribeState {
  ok: boolean;
  message: string;
  subscriberId?: string;
}

const CHANNELS: Channel[] = [
  "whatsapp",
  "telegram",
  "signal",
  "email",
  "slack",
  "webhook",
  "nigcomsat_broadcast",
];

/**
 * Activates monitoring for the SIGNED-IN account. Runs on the server so no credential
 * reaches the browser.
 *
 * ## Why `/iam/activate` and not `POST /subscribers`
 *
 * This action used `api.createSubscriber`, the **platform** endpoint an aggregator uses to
 * onboard somebody else. It writes the Postgres subscriber row and never touches the IAM
 * account — so `account.subscriber_id` stayed null while the subscription ran perfectly.
 *
 * Every portal page gates on that field, so the visible result was: an area monitored on every
 * satellite pass, ten assessments recorded, real Sentinel-1 measurements — and both
 * `/portal` and `/portal/areas` saying "nothing is being monitored". Two stores, no link.
 *
 * `/iam/activate` persists the subscriber *and* binds it to the account, and it takes the
 * session so the plot belongs to whoever is signed in rather than to an id the caller supplies.
 *
 * The backend queues a first scan on activation, so someone signing up during a developing
 * flood is assessed immediately rather than at the next 6-hour cycle.
 */
export async function subscribe(
  _prev: SubscribeState,
  formData: FormData,
): Promise<SubscribeState> {
  const name = String(formData.get("name") ?? "").trim();
  const areaName = String(formData.get("area_name") ?? "").trim();

  if (!name) return { ok: false, message: "Please give your name." };
  if (!areaName) return { ok: false, message: "Please name the area to watch." };

  // The area comes from the picker, already resolved and validated by
  // `POST /places/resolve`. This action no longer computes a bounding box.
  //
  // It used to: latitude, longitude and a radius converted to degrees with a cosine
  // correction, here in the browser. Two problems with that, and the second is the reason
  // it is gone. A farmer cannot supply decimal coordinates — the form asked a question its
  // user could not answer. And the same maths existed in `app/eo/human.py`, so a fix to one
  // would silently not reach the other; a client and server disagreeing about where a field
  // is has no visible symptom.
  const rawArea = String(formData.get("resolved_area") ?? "").trim();
  if (!rawArea) {
    return {
      ok: false,
      message:
        "Please choose the area to watch — use your location, search for your village, or " +
        "draw the outline.",
    };
  }

  let resolvedArea: Record<string, unknown>;
  try {
    resolvedArea = JSON.parse(rawArea);
  } catch {
    // Only reachable if the hidden field was tampered with or truncated. The backend would
    // reject it anyway; failing here gives a message the user can act on.
    return {
      ok: false,
      message: "Something went wrong reading the area. Please choose it again.",
    };
  }

  const channels = CHANNELS.flatMap((channel) => {
    const address = String(formData.get(`addr_${channel}`) ?? "").trim();
    if (!address) return [];
    return [
      {
        channel,
        address,
        enabled: true,
        min_severity: (formData.get(`min_${channel}`) as Severity) || "advisory",
      },
    ];
  });

  if (channels.length === 0) {
    return {
      ok: false,
      message: "Add at least one way to reach you.",
    };
  }

  // Session-scoped, so the subscription binds to the signed-in account. A missing session
  // here means the gate on /subscribe was bypassed, which should be impossible — reported
  // rather than silently falling back to the platform path that caused the original bug.
  const session = await getSessionToken();
  if (!session) {
    return {
      ok: false,
      message: "Your session has expired. Please sign in again and we will keep your details.",
    };
  }

  try {
    const subscriber = await api.activateSubscription(session, {
      area: {
        // Spread first so the resolved bbox, geometry, hectares and admin fields all
        // survive; `name` and `crop` are the two things this form owns.
        ...resolvedArea,
        name: areaName,
        crop: String(formData.get("crop") ?? "") || null,
      },
      channels,
    });

    return {
      ok: true,
      subscriberId: subscriber.id,
      message:
        "You're registered. A first scan of your area has been queued — you'll hear from us only when something needs action.",
    };
  } catch (error) {
    if (error instanceof ApiError) {
      return {
        ok: false,
        message:
          error.status === 503
            ? // Shown mid-signup, so it has to protect the conversion as well as
              // inform: say nothing was charged or half-created, and give a next
              // step that is not "debug our infrastructure".
              // `hi@freepass.africa` is the consortium's real reply-to (the backend's
              // BREVO_REPLY_TO_EMAIL). The previous text named
              // `support@shelter.zerorate.io`, which does not exist — so a subscriber
              // hitting a genuine outage was sent to a mailbox nobody reads, and their
              // report vanished at exactly the moment we most needed it.
              "We could not complete your registration just now — a temporary " +
              "service interruption. Nothing was saved, so please try again in a " +
              "moment. If it persists, contact hi@freepass.africa."
            : `We could not complete your registration: ${error.message}`,
      };
    }
    return {
      ok: false,
      message:
        "Something went wrong on our side. Nothing was saved — please try again.",
    };
  }
}
