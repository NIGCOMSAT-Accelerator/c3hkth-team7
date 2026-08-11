"use server";

import { revalidatePath } from "next/cache";

import { api } from "@/lib/api";
import { getSessionToken, setSession } from "@/lib/session";
import { recordPortalEvent } from "@/app/auth/session-actions";

export interface PrefState {
  ok: boolean;
  message: string;
}

/**
 * Update language and preferred channel.
 *
 * Audited via `recordPortalEvent`, because a changed delivery channel is exactly the kind
 * of change someone needs to be able to point at later — "my alerts stopped arriving" is
 * usually "the channel changed", and without a record there is no way to tell whether the
 * subscriber did it or an aggregator did.
 */
export async function updatePreferences(
  _prev: PrefState,
  formData: FormData,
): Promise<PrefState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }

  const language = String(formData.get("language") ?? "").trim();
  const preferred_channel = String(formData.get("preferred_channel") ?? "").trim();

  try {
    await api.updatePreferences(token, {
      language: language || undefined,
      preferred_channel: preferred_channel || undefined,
    });
  } catch (error) {
    return {
      ok: false,
      message:
        error instanceof Error
          ? error.message
          : "Could not save your preferences. Nothing was changed.",
    };
  }

  await recordPortalEvent(
    "preferences.updated",
    `language=${language}, channel=${preferred_channel}`,
  );

  // The layout's nav and this page both read the account, so both must re-render — a
  // stale nav would keep showing the old state after a successful save.
  revalidatePath("/portal", "layout");

  return { ok: true, message: "Saved. Your next advisory uses these settings." };
}

/**
 * State for the two-step in-session password change.
 *
 * One shape for both steps so the client renders notices identically, with the request step's
 * extra fields optional rather than a second interface — a union would make every read of
 * `message` need a narrow first, for no gain.
 */
export interface PasswordChangeState {
  ok: boolean;
  message: string;
  /** Masked address the code went to, e.g. `a****@example.com`. Request step only. */
  sentTo?: string;
  expiresInMinutes?: number;
}

/**
 * Step 1 — email a 6-character confirmation code to the registered address.
 *
 * No inputs. The address is the one on the account, read server-side from the session; accepting
 * one from the form would let the requester redirect the proof to a mailbox they control, which is
 * the entire thing this step exists to prevent.
 */
export async function requestPasswordChangeCode(
  _prev: PasswordChangeState,
  _formData: FormData,
): Promise<PasswordChangeState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }

  try {
    const sent = await api.requestPasswordChangeCode(token);
    // Audited from the portal as well as the API, so "who started a password change" is answerable
    // from either side of the stack.
    await recordPortalEvent("password.change.requested", sent.sent_to);
    return {
      ok: true,
      message: sent.detail,
      sentTo: sent.sent_to,
      expiresInMinutes: sent.expires_in_minutes,
    };
  } catch (error) {
    return {
      ok: false,
      // The backend's own wording is surfaced unchanged — it already distinguishes "email is
      // unavailable" from "too many requests", and both need different actions from the reader.
      message:
        error instanceof Error ? error.message : "Could not send a confirmation code.",
    };
  }
}

/**
 * Step 2 — verify the code and set the new password.
 *
 * The backend returns a fresh session token, which is written to the cookie here. Without that the
 * user would be holding a token minted before the credential changed: still valid, but it would
 * outlive the password it was issued against, and the next request could fail in a way that reads
 * as "my new password does not work".
 */
export async function confirmPasswordChange(
  _prev: PasswordChangeState,
  formData: FormData,
): Promise<PasswordChangeState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }

  const code = String(formData.get("code") ?? "").trim();
  const password = String(formData.get("password") ?? "");
  const confirm = String(formData.get("confirm") ?? "");

  if (!code) return { ok: false, message: "Enter the code from your email." };
  if (!password) return { ok: false, message: "Choose a new password." };
  // Checked here as well as in the field's own live comparison: the client check is a convenience,
  // and a mismatch reaching the API would set a password the user did not intend to type twice.
  if (confirm && password !== confirm) {
    return { ok: false, message: "The two passwords do not match." };
  }

  try {
    const session = await api.confirmPasswordChange(token, { code, password });
    await setSession(session.access_token, session.expires_in);
    await recordPortalEvent("password.changed", "via emailed code");
    revalidatePath("/portal/settings");
    revalidatePath("/portal/security");
    return {
      ok: true,
      message:
        "Your password is changed. Use it the next time you sign in — this session stays active.",
    };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not change your password.",
    };
  }
}

/**
 * State for the alert-delivery editor.
 */
export type ChannelState = {
  ok: boolean;
  message: string;
};

/**
 * Replace where alerts are delivered.
 *
 * ## Why the whole set travels, not a diff
 *
 * The API takes the full desired list, and so does this. It is what makes "stop using WhatsApp"
 * expressible — a diff-based form needs a separate delete verb, and a partial update cannot say
 * "remove this channel".
 *
 * The form encodes one row per channel as `ch_{i}_*` fields. Indices rather than channel names
 * because the same channel may appear twice with different areas: email for all plots, and email
 * to a different address for one specific plot.
 *
 * ## What is deliberately NOT validated here
 *
 * Address format, and whether an `aoi_id` belongs to this subscriber. Both are the backend's job —
 * it owns the subscriber's area list and rejects an unknown id with a readable 422. Re-checking
 * here would be a second implementation that can disagree, and the one that matters is the one
 * guarding the write.
 */
export async function replaceChannels(
  _prev: ChannelState,
  formData: FormData,
): Promise<ChannelState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }

  const subscriberId = String(formData.get("subscriber_id") ?? "").trim();
  if (!subscriberId) {
    return { ok: false, message: "No subscription on this account yet." };
  }

  const channels: {
    channel: string;
    address: string;
    min_severity: string;
    enabled: boolean;
    aoi_id: string | null;
    min_score: number | null;
  }[] = [];

  // Bounded loop rather than iterating formData keys: a hostile or malformed submission cannot
  // make this allocate unboundedly, and 24 rows is far more than any real subscriber configures.
  for (let i = 0; i < 24; i += 1) {
    const channel = String(formData.get(`ch_${i}_channel`) ?? "").trim();
    if (!channel) continue;
    const address = String(formData.get(`ch_${i}_address`) ?? "").trim();
    // A row with no address is a row the subscriber cleared. Dropping it is how "remove this
    // channel" works without a delete button — and the backend refuses an empty final list, so
    // clearing everything fails loudly rather than silently muting them.
    if (!address) continue;

    const area = String(formData.get(`ch_${i}_aoi`) ?? "").trim();

    // The sensitivity dial. Empty means "no score filter", which must reach the API as **null**
    // rather than 0 — the two are deliberately different there: null lets the severity ladder
    // govern alone, whereas 0 is an explicit floor a future default change must not overwrite.
    //
    // Parsed and range-checked here rather than trusted. This is a select today, but a Server
    // Action accepts whatever is posted to it, and a `min_score` above 1 would be refused by the
    // backend's own validator — better a clean fall back to "no filter" than a 422 on a form the
    // subscriber filled in correctly.
    const rawScore = String(formData.get(`ch_${i}_score`) ?? "").trim();
    const parsedScore = rawScore === "" ? null : Number(rawScore);
    const minScore =
      parsedScore !== null &&
      Number.isFinite(parsedScore) &&
      parsedScore >= 0 &&
      parsedScore <= 1
        ? parsedScore
        : null;

    channels.push({
      channel,
      address,
      min_severity: String(formData.get(`ch_${i}_min`) ?? "advisory"),
      enabled: true,
      aoi_id: area || null,
      min_score: minScore,
    });
  }

  if (channels.length === 0) {
    return {
      ok: false,
      message:
        "Keep at least one way to reach you. With none, your plots are watched and you are never told.",
    };
  }

  try {
    await api.replaceChannels(token, subscriberId, channels as never);
  } catch (error) {
    return {
      ok: false,
      message:
        error instanceof Error
          ? error.message
          : "Could not save. Check your connection and try again.",
    };
  }

  await recordPortalEvent("channels.updated", `${channels.length} channels`);
  revalidatePath("/portal/settings");
  return {
    ok: true,
    message: "Saved. Your next advisory will use these settings.",
  };
}

/**
 * Change who contacts the subscriber about one plot.
 *
 * `webhook` means SHELTER sends nothing directly and the aggregator relays — the backend refuses
 * it for a plot with no aggregator behind it, because there would be nobody to relay and the
 * setting would silently be an off switch.
 */
export async function setAreaDelivery(
  _prev: ChannelState,
  formData: FormData,
): Promise<ChannelState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }

  const subscriberId = String(formData.get("subscriber_id") ?? "").trim();
  const aoiId = String(formData.get("aoi_id") ?? "").trim();
  const mode = String(formData.get("mode") ?? "").trim();
  if (!subscriberId || !aoiId || !mode) {
    return { ok: false, message: "Choose a plot and a delivery option." };
  }

  try {
    await api.setAreaDelivery(token, subscriberId, aoiId, mode as never);
  } catch (error) {
    return {
      ok: false,
      message:
        error instanceof Error
          ? error.message
          : "Could not save that delivery option.",
    };
  }

  await recordPortalEvent("area.delivery_changed", `${aoiId} -> ${mode}`);
  revalidatePath("/portal/settings");
  revalidatePath("/portal/areas");
  return { ok: true, message: "Saved." };
}
