"use server";

import { revalidatePath } from "next/cache";

import { api } from "@/lib/api";
import { recordPortalEvent } from "@/app/auth/session-actions";
import { getSessionToken } from "@/lib/session";

/**
 * Minting and revoking Partner API keys.
 *
 * ## Why a Server Action rather than a client fetch
 *
 * The session token authorises key creation, and a key is the credential that reaches every one of
 * an aggregator's customers. Handling it in a Server Action keeps the token server-side — the same
 * reason `subscribe/actions.ts` and `lib/api.ts` are `server-only`.
 *
 * ## The plaintext appears exactly once
 *
 * The API returns the secret in the creation response and stores only a hash. There is no reveal
 * endpoint and deliberately so. That means this action must hand the plaintext back to the page for
 * a single render, and the page must say plainly that it will not be shown again — a key the
 * aggregator failed to copy is a support request, and a key they *think* they can retrieve later is
 * worse, because they will not copy it at all.
 *
 * It is returned in the action result rather than stashed anywhere: not a cookie, not the audit
 * detail, not `revalidatePath`'s cache. It lives in the response and then in the aggregator's
 * clipboard.
 */

export type KeyState = {
  ok: boolean;
  message: string;
  /** The plaintext, for one render only. Never persisted. */
  secret?: string;
  keyName?: string;
};

/** Scopes offered in the UI, in the order they are granted in practice. */
const GRANTABLE = new Set([
  "customers:read",
  "customers:write",
  "scan:trigger",
  "webhooks:manage",
]);

export async function createKey(
  _prev: KeyState,
  formData: FormData,
): Promise<KeyState> {
  const token = await getSessionToken();
  if (!token) return { ok: false, message: "Your session has expired. Sign in again." };

  const name = String(formData.get("name") ?? "").trim();
  const workspaceId = String(formData.get("workspace_id") ?? "").trim();
  const expiresRaw = String(formData.get("expires_in_days") ?? "").trim();

  // Only scopes this form offers. A checkbox name is user-controlled input, and forwarding an
  // arbitrary string would let a crafted POST request a scope the picker never showed — the API
  // would still refuse anything above the caller's role, but the UI should not be the thing relying
  // on that.
  const scopes = formData
    .getAll("scopes")
    .map(String)
    .filter((s) => GRANTABLE.has(s));

  if (!name) {
    return { ok: false, message: "Give the key a name you will recognise in six months." };
  }
  if (!workspaceId) {
    return {
      ok: false,
      message: "Choose which workspace this key is for. A key reaches one workspace only.",
    };
  }
  if (scopes.length === 0) {
    return {
      ok: false,
      message:
        "Grant at least one permission, or the key can authenticate and do nothing.",
    };
  }

  // Blank means no expiry, which is a legitimate choice for a long-running integration.
  const expiresInDays = expiresRaw ? Number(expiresRaw) : null;
  if (expiresRaw && (!Number.isFinite(expiresInDays) || (expiresInDays ?? 0) < 1)) {
    return { ok: false, message: "Expiry must be a number of days, or left blank for none." };
  }

  try {
    const created = await api.createApiKey(token, {
      name,
      workspace_id: workspaceId,
      scopes,
      expires_in_days: expiresInDays,
    });

    // The audit detail records the key's NAME and prefix, never the secret.
    await recordPortalEvent("apikey.created", `${created.name} · ${created.prefix}`);
    revalidatePath("/portal/api-keys");

    return {
      ok: true,
      message:
        "Key created. Copy it now — this is the only time it will be shown, and we cannot " +
        "recover it. If you lose it, revoke it and mint another.",
      secret: created.key,
      keyName: created.name,
    };
  } catch (error) {
    // The API's own message is surfaced unchanged: it explains scope refusals ("your role on this
    // workspace is View-Only") in terms the aggregator can act on, which a generic string cannot.
    return {
      ok: false,
      message:
        error instanceof Error
          ? error.message
          : "Could not create the key. Please try again.",
    };
  }
}

export async function revokeKey(
  _prev: KeyState,
  formData: FormData,
): Promise<KeyState> {
  const token = await getSessionToken();
  if (!token) return { ok: false, message: "Your session has expired. Sign in again." };

  const keyId = String(formData.get("key_id") ?? "").trim();
  if (!keyId) return { ok: false, message: "No key selected." };

  try {
    await api.revokeApiKey(token, keyId);
    await recordPortalEvent("apikey.revoked", keyId);
    revalidatePath("/portal/api-keys");
    return {
      ok: true,
      message: "Key revoked. Any integration using it is now refused.",
    };
  } catch (error) {
    return {
      ok: false,
      message:
        error instanceof Error ? error.message : "Could not revoke the key.",
    };
  }
}
