"use server";

import { revalidatePath } from "next/cache";

import { ApiError, api, safeApi } from "@/lib/api";
import { getAccount, getSessionToken } from "@/lib/session";
import { recordPortalEvent } from "@/app/auth/session-actions";
import type { Severity } from "@/lib/types";

/**
 * How long to wait for a synchronous assessment before giving up on it.
 *
 * `POST /risk/assess` runs 10-40s in the normal case, so this is deliberately generous — the
 * failure it guards is a *hung* catalogue, not a slow one, and aborting a scan that would have
 * answered at 50s wastes the COG reads already paid for.
 *
 * 75s sits above the observed range and below the ~10s-to-minutes ceiling a serverless renderer
 * will impose anyway. When the platform's own limit is lower it wins, which is fine: both paths
 * end at the same honest message, and this one at least produces it deliberately.
 */
const ASSESS_TIMEOUT_MS = 75_000;

/**
 * Plain-language severity for the confirmation sentence.
 *
 * Deliberately NOT imported from `SEVERITY_META`/`INTELLIGENCE`: those are client-module
 * vocabularies for badges and legends, and this is one word inside a server action's string.
 * The set is the five members of the backend's `Severity` enum, keyed so a future member is a
 * type error here rather than a silently missing word.
 */
const SEVERITY_WORD: Record<Severity, string> = {
  info: "nothing needing attention",
  advisory: "an advisory",
  watch: "a watch",
  warning: "a warning",
  emergency: "an emergency",
};

export interface AreaState {
  ok: boolean;
  message: string;
  /**
   * What was actually activated, for the confirmation panel.
   *
   * A one-line "it worked" leaves the subscriber to trust that the right piece of ground was
   * registered — and the commonest real error is a correct-looking success over the wrong plot.
   * Echoing back the name, size, location and what happens next lets them catch that in the one
   * moment they are still looking at the screen.
   */
  activated?: {
    name: string;
    aoiId: string;
    hectares: number | null;
    centre: string | null;
    crop: string | null;
  };
  /**
   * Which plot a successful rename changed.
   *
   * One `useActionState` serves every row on the page, so `ok` alone cannot tell a row whether the
   * save that just landed was its own — and a row keyed on `ok` would close its editor when a
   * *different* plot saved. This is what makes "exit edit mode once saved" specific to the plot
   * that was actually saved.
   */
  savedAoiId?: string;
}

/**
 * Area management for the signed-in subscriber.
 *
 * ## What is offered, and what is deliberately not
 *
 * **Add** and **rename/re-crop** only. Both are safe: adding creates a new plot with its own
 * clean history from its first satellite pass, and renaming is an in-place edit that keeps the
 * `aoi_id`, so every past assessment stays attached *and* stays meaningful.
 *
 * **No geometry editing.** Moving or resizing an area leaves one timeline mixing measurements of
 * two different footprints under a single name — a "65% under standing water" reading from last
 * week would describe land the subscriber may no longer monitor. The API supports it for
 * correcting a mis-dropped pin, but the portal steers to "add a new area" instead, because that
 * is the right answer for a genuinely different plot and there is no limit on how many you hold.
 */

async function requireSubscriber(): Promise<
  { token: string; subscriberId: string } | AreaState
> {
  const token = await getSessionToken();
  if (!token) {
    return { ok: false, message: "Your session expired. Please sign in again." };
  }
  const account = await getAccount();
  if (!account?.subscriber_id) {
    return {
      ok: false,
      message:
        "No monitoring is set up on this account yet. Add your first area to get started.",
    };
  }
  return { token, subscriberId: account.subscriber_id };
}

export async function addArea(
  _prev: AreaState,
  formData: FormData,
): Promise<AreaState> {
  const gate = await requireSubscriber();
  if ("ok" in gate) return gate;

  const name = String(formData.get("area_name") ?? "").trim();
  // The picker submits the fully resolved area as JSON — bbox, ring, hectares and admin fields
  // already validated server-side by `POST /places/resolve`. Re-deriving any of it here would
  // risk a bbox that disagrees with its ring, which produces an all-NaN mask and a silent 0%
  // reading the Oracle reads as "no hazard".
  const raw = String(formData.get("resolved_area") ?? "");

  if (!name) return { ok: false, message: "Give the plot a name you will recognise." };
  if (!raw) {
    return {
      ok: false,
      message: "Choose where the plot is on the map, or search for the place by name.",
    };
  }

  let resolved: Record<string, unknown>;
  try {
    resolved = JSON.parse(raw);
  } catch {
    return { ok: false, message: "Could not read the area you selected. Please pick it again." };
  }

  try {
    const created = await api.addMyArea(gate.token, gate.subscriberId, {
      ...resolved,
      name,
      crop: String(formData.get("crop") ?? "") || null,
    });
    await recordPortalEvent("area.added", `${created.name} · ${created.id}`);
    revalidatePath("/portal/areas");
    revalidatePath("/portal");

    // The centre point, not the corners: one lat/long can be checked against a phone's map app,
    // and that check is the whole reason for echoing it back.
    const centre = created.bbox
      ? `${((created.bbox.south + created.bbox.north) / 2).toFixed(4)}, ${(
          (created.bbox.west + created.bbox.east) /
          2
        ).toFixed(4)}`
      : null;

    return {
      ok: true,
      message: `${created.name} is now being monitored.`,
      activated: {
        name: created.name,
        aoiId: created.id,
        hectares: created.hectares ?? null,
        centre,
        crop: created.crop ?? null,
      },
    };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not add the area.",
    };
  }
}

export async function renameArea(
  _prev: AreaState,
  formData: FormData,
): Promise<AreaState> {
  const gate = await requireSubscriber();
  if ("ok" in gate) return gate;

  const aoiId = String(formData.get("aoi_id") ?? "").trim();
  const name = String(formData.get("name") ?? "").trim();
  const crop = String(formData.get("crop") ?? "").trim();

  if (!aoiId) return { ok: false, message: "Missing area." };
  if (!name) return { ok: false, message: "The plot needs a name." };

  try {
    const updated = await api.renameMyArea(gate.token, gate.subscriberId, aoiId, {
      name,
      // Empty string means "clear it", which `undefined` would not — a subscriber removing a
      // crop they no longer plant must be able to.
      crop: crop || undefined,
    });
    await recordPortalEvent("area.renamed", `${aoiId} -> ${updated.name}`);
    revalidatePath("/portal/areas");
    return {
      ok: true,
      // Said explicitly, because it is the reassurance that matters: an edit that silently reset
      // their history would be indistinguishable from this one at the moment of saving.
      message: `${updated.name} saved. Your monitoring history for this plot is unchanged.`,
      // Returned so the row that saved can close its own editor and no other row closes with it.
      savedAoiId: aoiId,
    };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "Could not save the change.",
    };
  }
}

/**
 * Assess one plot right now, against live open data.
 *
 * ## Why this exists
 *
 * Every other path to a reading is on SHELTER's clock: the watch loop every ~6 hours, or the
 * scan queued when a plot is added or its geometry edited. Those are correct for the product —
 * a farmer should not have to remember to ask — but they left the subscriber with no way to
 * answer "what does the satellite say about my field *now*", and no way to see that the
 * service queries live data at all rather than replaying something stored.
 *
 * ## The `aoi_id` is a lookup key, never the thing assessed
 *
 * The form supplies an id; this action does NOT pass it onward. It re-reads the subscriber
 * through `getSubscriber` — scoped server-side, 404 for anyone else's record — and assesses
 * the area found *in that record*. So a forged id assesses nothing: it is absent from the
 * caller's own areas and the action stops.
 *
 * That ordering is the whole guard. `POST /risk/assess` takes a geometry rather than an id and
 * spends real catalogue quota on whatever it is given, so an action that forwarded a
 * client-supplied bbox would let anyone with a session bill arbitrary reads against someone
 * else's coordinates — and get the reading back. The id is the only thing accepted from the
 * client, and it is used to *find* trusted geometry, never as geometry.
 *
 * ## Nothing is dispatched
 *
 * `assess` runs Scout → Analyst → Oracle and stops. The Herald never sees it, so no advisory
 * is generated, no channel fires, and the 18-hour dedupe window is untouched. A subscriber
 * pressing this repeatedly cannot page themselves, and cannot suppress a real alert later.
 * That is what makes it safe to put in front of an end user at all.
 */
export async function reassessArea(
  _prev: AreaState,
  formData: FormData,
): Promise<AreaState> {
  const gate = await requireSubscriber();
  if ("ok" in gate) return gate;

  const aoiId = String(formData.get("aoi_id") ?? "").trim();
  if (!aoiId) return { ok: false, message: "Missing area." };

  // Read the plot from the account's own record rather than trusting the form. See above:
  // this is what makes a forged or another tenant's id assess nothing at all.
  const mine = await safeApi.getSubscriber(gate.subscriberId);
  const area = mine?.areas.find((a) => a.id === aoiId);

  if (!area) {
    // Deliberately does not distinguish "not yours" from "does not exist" from "the record
    // could not be read" — the same reasoning as the backend's 404-not-403 on cross-tenant
    // reads, since an id that errors differently is an id an attacker can confirm.
    return {
      ok: false,
      message: "That plot could not be found on your account.",
      savedAoiId: aoiId,
    };
  }

  try {
    const assessment = await api.assess(area, ASSESS_TIMEOUT_MS);
    await recordPortalEvent("area.reassessed", `${area.name} · ${aoiId}`);

    // Revalidate so the MonitoringPanel re-renders with the reading that just landed.
    //
    // The action's own response carries the re-rendered route (see the Next.js server-actions
    // guide: revalidatePath re-renders the current route and includes the new RSC payload in
    // the same roundtrip), so the panel updates without a manual reload. Without this the
    // subscriber would be told the scan succeeded while still looking at the old severity —
    // the one outcome that would make the button read as broken.
    //
    // `/portal` too: its highest-severity tile is derived from the same assessments, so
    // leaving it stale would have two pages of the portal disagreeing about the same plot.
    revalidatePath("/portal/areas");
    revalidatePath("/portal");

    return {
      ok: true,
      message:
        `${area.name} reassessed just now — ${SEVERITY_WORD[assessment.severity]}` +
        ` at ${Math.round(assessment.confidence * 100)}% confidence.` +
        " The reading below is from this scan.",
      savedAoiId: aoiId,
    };
  } catch (error) {
    const status = error instanceof ApiError ? error.status : 0;

    // A timeout is reported as what it is: the scan was accepted and is very likely still
    // running server-side, so the honest instruction is to wait rather than to retry. Telling
    // someone to press it again would spend a second set of COG reads on the same plot.
    if (status === 504) {
      return {
        ok: false,
        message:
          `${area.name} is taking longer than usual to assess — a satellite catalogue is` +
          " probably slow. The scan is still running; reload this page in a minute and the" +
          " new reading should be here. Your scheduled monitoring is unaffected.",
        savedAoiId: aoiId,
      };
    }

    return {
      ok: false,
      // Prefixed with the plot name because one action serves every row: an unprefixed
      // failure sentence under a page of plots does not say which one it is about.
      message:
        error instanceof Error
          ? `${area.name}: ${error.message}`
          : `${area.name} could not be reassessed just now. Your scheduled monitoring is unaffected.`,
      savedAoiId: aoiId,
    };
  }
}

export async function removeArea(
  _prev: AreaState,
  formData: FormData,
): Promise<AreaState> {
  const gate = await requireSubscriber();
  if ("ok" in gate) return gate;

  const aoiId = String(formData.get("aoi_id") ?? "").trim();
  const name = String(formData.get("name") ?? "").trim();
  if (!aoiId) return { ok: false, message: "Missing area." };

  try {
    await api.removeMyArea(gate.token, gate.subscriberId, aoiId);
    await recordPortalEvent("area.removed", name || aoiId);
    revalidatePath("/portal/areas");
    revalidatePath("/portal");
    return {
      ok: true,
      message: `${name || "That plot"} is no longer monitored. Past alerts for it are kept in your history.`,
    };
  } catch (error) {
    return {
      ok: false,
      // The backend refuses removing the last area with a 409 and an explanation, which is
      // surfaced unchanged — it already says what to do instead.
      message: error instanceof Error ? error.message : "Could not remove the area.",
    };
  }
}
