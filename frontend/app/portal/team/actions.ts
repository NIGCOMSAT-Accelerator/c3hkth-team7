"use server";

import { revalidatePath } from "next/cache";

import { api } from "@/lib/api";
import { getSessionToken } from "@/lib/session";
import { recordPortalEvent } from "@/app/auth/session-actions";
import type { WorkspaceGrant } from "@/lib/types";

export interface TeamState {
  ok: boolean;
  message: string;
}

/**
 * Team actions — invite, re-scope, remove.
 *
 * ## Grants are read as `role-<workspace_id>` fields
 *
 * The form renders one role selector per workspace, so the submitted set is naturally
 * (workspace, role) pairs. A single role plus a list of workspaces would be a smaller payload
 * and the wrong model: the whole reason membership is per workspace is that a colleague may
 * need Engineering on the pilot and View-Only on the live season.
 *
 * An empty selector means "no access to this workspace", and is omitted rather than sent — the
 * API treats an omitted workspace as revoked, which is the intended reading of an edit form.
 */
function readGrants(formData: FormData): WorkspaceGrant[] {
  const grants: WorkspaceGrant[] = [];

  for (const [field, value] of formData.entries()) {
    if (!field.startsWith("role-")) continue;
    const role = String(value);
    if (!role) continue;
    grants.push({ workspace_id: field.slice("role-".length), role });
  }
  return grants;
}

export async function inviteMember(
  _prev: TeamState,
  formData: FormData,
): Promise<TeamState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }

  const email = String(formData.get("email") ?? "").trim();
  const grants = readGrants(formData);

  if (!email) {
    return { ok: false, message: "Enter the colleague's email address." };
  }
  if (grants.length === 0) {
    return {
      ok: false,
      message:
        "Give them a role on at least one workspace. An invitation with no access would " +
        "let them sign in and see nothing.",
    };
  }

  try {
    const result = await api.inviteMember(token, { email, grants });
    await recordPortalEvent(
      "team.invited",
      `${email} · ${grants.map((g) => `${g.workspace_id}:${g.role}`).join(", ")}`,
    );
    revalidatePath("/portal/team");

    // The email outcome is reported honestly. If delivery failed the invitation still
    // exists — but saying "invited" while nothing arrived would leave the administrator
    // waiting on a colleague who was never contacted.
    return {
      ok: true,
      message: result.email_sent
        ? `Invited ${email}. The invitation expires ${new Date(result.expires_at).toLocaleDateString()}.`
        : `Invitation created for ${email}, but the email could not be sent. Check the ` +
          `mail configuration — they cannot accept until they receive the link.`,
    };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not send the invitation.",
    };
  }
}

export async function resendInvitation(
  _prev: TeamState,
  formData: FormData,
): Promise<TeamState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }

  const email = String(formData.get("email") ?? "").trim();
  if (!email) return { ok: false, message: "Missing email address." };

  try {
    const result = await api.resendInvitation(token, email);
    await recordPortalEvent("team.invite_resent", email);
    revalidatePath("/portal/team");

    // The delivery outcome is reported honestly. Saying "resent" while the email failed would
    // leave an administrator waiting on a colleague who was never contacted a second time.
    return {
      ok: true,
      message: result.email_sent
        ? `A new link is on its way to ${email}, valid until ` +
          `${new Date(result.expires_at).toLocaleDateString()}. The earlier invitation no ` +
          `longer works.`
        : `Invitation reissued for ${email}, but the email could not be sent. Check the mail ` +
          `configuration — they cannot join until they receive the link.`,
    };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not resend the invitation.",
    };
  }
}

export async function updateGrants(
  _prev: TeamState,
  formData: FormData,
): Promise<TeamState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }

  const accountId = String(formData.get("account_id") ?? "").trim();
  if (!accountId) return { ok: false, message: "Missing team member." };

  const grants = readGrants(formData);
  if (grants.length === 0) {
    return {
      ok: false,
      message:
        "This would remove all their access. Use “Remove from team” if that is what you " +
        "mean — it is the same outcome, said plainly.",
    };
  }

  try {
    await api.setMemberGrants(token, accountId, grants);
    await recordPortalEvent(
      "team.role_changed",
      `${accountId} · ${grants.map((g) => `${g.workspace_id}:${g.role}`).join(", ")}`,
    );
    revalidatePath("/portal/team");
    return { ok: true, message: "Access updated." };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not update access.",
    };
  }
}

export async function removeMember(
  _prev: TeamState,
  formData: FormData,
): Promise<TeamState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }

  const accountId = String(formData.get("account_id") ?? "").trim();
  const name = String(formData.get("name") ?? "").trim();
  if (!accountId) return { ok: false, message: "Missing team member." };

  try {
    await api.removeMember(token, accountId);
    await recordPortalEvent("team.removed", name || accountId);
    revalidatePath("/portal/team");
    return {
      ok: true,
      // Said explicitly, because "remove" is ambiguous and the difference matters: their
      // own areas and alert history are untouched.
      message: `${name || accountId} no longer has access. Their account still exists.`,
    };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not remove the member.",
    };
  }
}
