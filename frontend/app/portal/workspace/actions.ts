"use server";

import { revalidatePath } from "next/cache";

import { api } from "@/lib/api";
import { getSessionToken } from "@/lib/session";
import { recordPortalEvent } from "@/app/auth/session-actions";

export interface WorkspaceState {
  ok: boolean;
  message: string;
}

/**
 * Workspace CRUD, as Server Actions.
 *
 * ## Why actions rather than client fetches
 *
 * `lib/api.ts` is `server-only` and the session lives in an httpOnly cookie, so a client
 * component cannot call the API at all — which is the point. It also means the session token
 * never enters the browser bundle, and the API key that authorises the platform never leaves
 * the server.
 *
 * Every action returns `{ok, message}` rather than throwing: a failed track activation must
 * render a sentence next to the form, not an error page that loses what the user typed.
 */

/** Tracks arrive as repeated `tracks` checkbox values. */
function readTracks(formData: FormData): string[] {
  return formData.getAll("tracks").map(String).filter(Boolean);
}

export async function createWorkspace(
  _prev: WorkspaceState,
  formData: FormData,
): Promise<WorkspaceState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }

  const name = String(formData.get("name") ?? "").trim();
  const tracks = readTracks(formData);

  if (!name) {
    return { ok: false, message: "Give the project a name you will recognise later." };
  }
  // Checked here as well as in the API, so the user gets the sentence without a round trip.
  // The API check is the one that counts — this is only to save them the wait.
  if (tracks.length === 0) {
    return {
      ok: false,
      message: "Activate at least one intelligence track. A workspace with none receives nothing.",
    };
  }

  try {
    const created = await api.createWorkspace(token, { name, tracks });
    await recordPortalEvent(
      "workspace.created",
      `${created.name} · ${created.tracks.join(", ")}`,
    );
    revalidatePath("/portal/workspace");
    return { ok: true, message: `Created ${created.name}.` };
  } catch (error) {
    return {
      ok: false,
      message:
        error instanceof Error ? error.message : "Could not create the workspace.",
    };
  }
}

export async function updateWorkspace(
  _prev: WorkspaceState,
  formData: FormData,
): Promise<WorkspaceState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }

  const id = String(formData.get("workspace_id") ?? "").trim();
  const name = String(formData.get("name") ?? "").trim();
  const tracks = readTracks(formData);

  if (!id) return { ok: false, message: "Missing workspace." };
  if (tracks.length === 0) {
    return {
      ok: false,
      message:
        "Activate at least one intelligence track. Deactivating the last one would stop " +
        "everything this project receives.",
    };
  }

  try {
    const updated = await api.updateWorkspace(token, id, { name, tracks });
    await recordPortalEvent(
      "workspace.updated",
      `${updated.name} · ${updated.tracks.join(", ")}`,
    );
    revalidatePath("/portal/workspace");
    return { ok: true, message: `Saved ${updated.name}.` };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not save the workspace.",
    };
  }
}

export async function deleteWorkspace(
  _prev: WorkspaceState,
  formData: FormData,
): Promise<WorkspaceState> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }

  const id = String(formData.get("workspace_id") ?? "").trim();
  const name = String(formData.get("name") ?? "").trim();
  if (!id) return { ok: false, message: "Missing workspace." };

  try {
    await api.deleteWorkspace(token, id);
    await recordPortalEvent("workspace.deleted", name || id);
    revalidatePath("/portal/workspace");
    return { ok: true, message: `Deleted ${name || id}.` };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not delete the workspace.",
    };
  }
}
