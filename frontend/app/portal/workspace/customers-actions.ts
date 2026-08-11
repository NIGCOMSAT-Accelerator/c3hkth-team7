"use server";

import { revalidatePath } from "next/cache";

import { api } from "@/lib/api";
import { getSessionToken } from "@/lib/session";
import { recordPortalEvent } from "@/app/auth/session-actions";

export interface CustomerState {
  ok: boolean;
  message: string;
}

/**
 * Workspace → Customers → Areas, for the aggregator portal.
 *
 * ## Why the portal has this at all when the Partner API exists
 *
 * An integrator should not have to debug their first integration against an API they have never
 * seen succeed. Onboarding one customer by hand here, watching the first assessment arrive, and
 * only then automating it turns a blind integration into a verification — the portal call and the
 * Partner API call produce an identical record, so what works here works there.
 *
 * These go through a **session**, never an API key. An operator pasting their own key into a
 * browser form would put a credential into browser history, screenshots and support chats.
 */

export async function addCustomer(
  _prev: CustomerState,
  formData: FormData,
): Promise<CustomerState> {
  const token = await getSessionToken();
  if (!token) return { ok: false, message: "Your session expired. Please sign in again." };

  const workspaceId = String(formData.get("workspace_id") ?? "").trim();
  const email = String(formData.get("email") ?? "").trim();
  const first = String(formData.get("first_name") ?? "").trim();
  const last = String(formData.get("last_name") ?? "").trim();
  const ref = String(formData.get("external_ref") ?? "").trim();
  const areaName = String(formData.get("area_name") ?? "").trim();
  const raw = String(formData.get("resolved_area") ?? "");

  if (!workspaceId) return { ok: false, message: "Missing workspace." };
  if (!email) return { ok: false, message: "Enter the farmer's email address." };
  if (!first || !last) return { ok: false, message: "Enter the farmer's first and last name." };

  // The area is optional: an aggregator may register the person now and bind their plot later.
  let area: Record<string, unknown> | null = null;
  if (raw) {
    if (!areaName) {
      return { ok: false, message: "Give the plot a name, or clear the map selection." };
    }
    try {
      area = { ...JSON.parse(raw), name: areaName };
    } catch {
      return { ok: false, message: "Could not read the selected area. Please pick it again." };
    }
  }

  try {
    const created = await api.addWorkspaceCustomer(token, workspaceId, {
      email,
      first_name: first,
      last_name: last,
      external_ref: ref || null,
      area,
    });
    await recordPortalEvent("workspace.customer.added", `${email} · ${workspaceId}`);
    revalidatePath(`/portal/workspace/${workspaceId}/customers`);
    return {
      ok: true,
      message: area
        ? `${created.full_name} is onboarded and their plot is queued for its first scan. ` +
          `They will receive an email to claim their own account.`
        : `${created.full_name} is onboarded. Add their farm area to start monitoring.`,
    };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not onboard the customer.",
    };
  }
}

export async function addCustomerArea(
  _prev: CustomerState,
  formData: FormData,
): Promise<CustomerState> {
  const token = await getSessionToken();
  if (!token) return { ok: false, message: "Your session expired. Please sign in again." };

  const workspaceId = String(formData.get("workspace_id") ?? "").trim();
  const accountId = String(formData.get("account_id") ?? "").trim();
  const name = String(formData.get("area_name") ?? "").trim();
  const raw = String(formData.get("resolved_area") ?? "");

  if (!workspaceId || !accountId) return { ok: false, message: "Missing customer." };
  if (!name) return { ok: false, message: "Give the plot a name." };
  if (!raw) return { ok: false, message: "Choose where the plot is on the map." };

  let area: Record<string, unknown>;
  try {
    area = { ...JSON.parse(raw), name };
  } catch {
    return { ok: false, message: "Could not read the selected area. Please pick it again." };
  }

  try {
    const created = await api.addWorkspaceCustomerArea(token, workspaceId, accountId, area);
    await recordPortalEvent("workspace.customer.area_added", `${accountId} · ${created.id}`);
    revalidatePath(`/portal/workspace/${workspaceId}/customers`);
    return { ok: true, message: `${created.name} is queued for its first scan.` };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not add the area.",
    };
  }
}

export async function renameCustomerArea(
  _prev: CustomerState,
  formData: FormData,
): Promise<CustomerState> {
  const token = await getSessionToken();
  if (!token) return { ok: false, message: "Your session expired. Please sign in again." };

  const workspaceId = String(formData.get("workspace_id") ?? "").trim();
  const accountId = String(formData.get("account_id") ?? "").trim();
  const aoiId = String(formData.get("aoi_id") ?? "").trim();
  const name = String(formData.get("name") ?? "").trim();
  const crop = String(formData.get("crop") ?? "").trim();

  if (!workspaceId || !accountId || !aoiId) return { ok: false, message: "Missing area." };
  if (!name) return { ok: false, message: "The plot needs a name." };

  try {
    await api.renameWorkspaceCustomerArea(token, workspaceId, accountId, aoiId, {
      name,
      crop: crop || undefined,
    });
    await recordPortalEvent("workspace.customer.area_renamed", `${aoiId} -> ${name}`);
    revalidatePath(`/portal/workspace/${workspaceId}/customers`);
    // Said explicitly: an edit that silently reset the history would look identical at save time.
    return { ok: true, message: "Saved. The plot's monitoring history is unchanged." };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not save the change.",
    };
  }
}

export async function removeCustomerArea(
  _prev: CustomerState,
  formData: FormData,
): Promise<CustomerState> {
  const token = await getSessionToken();
  if (!token) return { ok: false, message: "Your session expired. Please sign in again." };

  const workspaceId = String(formData.get("workspace_id") ?? "").trim();
  const accountId = String(formData.get("account_id") ?? "").trim();
  const aoiId = String(formData.get("aoi_id") ?? "").trim();
  const name = String(formData.get("name") ?? "").trim();

  if (!workspaceId || !accountId || !aoiId) return { ok: false, message: "Missing area." };

  try {
    await api.removeWorkspaceCustomerArea(token, workspaceId, accountId, aoiId);
    await recordPortalEvent("workspace.customer.area_removed", name || aoiId);
    revalidatePath(`/portal/workspace/${workspaceId}/customers`);
    return {
      ok: true,
      message: `${name || "That plot"} is no longer monitored. Past assessments are kept, and billing for it stops.`,
    };
  } catch (error) {
    return {
      ok: false,
      // A 409 on the customer's last area already explains what to do instead.
      message: error instanceof Error ? error.message : "Could not remove the area.",
    };
  }
}
