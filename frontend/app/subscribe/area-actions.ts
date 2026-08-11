"use server";

import { api } from "@/lib/api";
import type { AoiPreview, PlaceResult, ResolvedArea } from "@/lib/types";

/**
 * Server Actions for the area picker.
 *
 * ## Why these go through the server rather than being fetched from the browser
 *
 * `lib/api.ts` is `server-only` and holds `SHELTER_API_KEY`. Routing through actions keeps
 * that key out of the client bundle — but for these three endpoints, which are public, the
 * more important reasons are different:
 *
 *   * **The backend serialises place lookups** to one per second to honour Nominatim's usage
 *     policy, and caches them in Dragonfly. A browser calling Nominatim directly would be
 *     rate-limited per-IP the moment someone types, and would share no cache with anyone.
 *   * **One place to change the provider.** If place search moves to a self-hosted instance,
 *     nothing in the UI changes.
 *   * **Subscriber IPs stay off a third party.** Every query names a district and belongs to
 *     someone registering their own farm.
 *
 * All three return `null` or an empty array rather than throwing. The picker's failure states
 * are part of its design — a subscriber who cannot search can still drop a pin, and one whose
 * map fails to load can still type a place name.
 */

export async function searchPlaces(query: string): Promise<PlaceResult[]> {
  if (query.trim().length < 3) return [];
  try {
    const result = await api.searchPlaces(query);
    return result.results;
  } catch {
    return [];
  }
}

/**
 * Turn a pin and a size in words into a validated, submittable area.
 *
 * The size is passed through untouched — parsing "12 acres" or "medium" is the backend's job
 * (`app/eo/human.py`), and duplicating it here would create two implementations that could
 * disagree about what "1,5 hectares" means. That kind of drift is invisible until someone's
 * field is monitored at ten times its real size.
 */
export async function resolveArea(input: {
  lat?: number;
  lon?: number;
  place?: string;
  size?: string;
  name?: string;
}): Promise<ResolvedArea | null> {
  try {
    return await api.resolveArea(input);
  } catch {
    return null;
  }
}

/**
 * Check a drawn outline: is it monitorable, how big is it, and how much does the outline
 * change the measurement?
 *
 * Called as the shape changes, so the user learns a ring is self-intersecting or too small
 * while they are still drawing rather than after submitting. It runs the *same* validation as
 * the write path, so a preview that passes cannot be refused later.
 */
export async function previewRing(ring: number[][]): Promise<AoiPreview | null> {
  try {
    return await api.previewAoi({ ring });
  } catch {
    return null;
  }
}

export type { AoiPreview, PlaceResult, ResolvedArea };

/**
 * State → LGA → map position, for when a place name finds nothing.
 *
 * ## Why this path exists
 *
 * A subscriber registering "Alspecs Farms in Kobape, Ogun State" got zero search results.
 * Verified against Nominatim directly: OSM has no entry for Kobape at all, though its LGA
 * (Obafemi Owode) resolves and GRID3 places the coordinates correctly. Browser geolocation
 * then filled the gap and the farm was registered in **England**.
 *
 * So "no results" is not an error state to apologise for — it is the common rural case, and it
 * needs a working alternative rather than an empty list. GRID3 ships all 774 LGAs, so browsing
 * to one is always possible even when the village is unfindable by name.
 *
 * All three return empty/null on failure. The picker degrades to pin-drop, which still works.
 */
export async function adminStates(): Promise<string[]> {
  try {
    return (await api.adminStates()).names;
  } catch {
    return [];
  }
}

export async function adminLgas(state: string): Promise<string[]> {
  if (!state.trim()) return [];
  try {
    return (await api.adminLgas(state)).names;
  } catch {
    return [];
  }
}

/**
 * The LGA's centre, for positioning the map.
 *
 * Returns only the centroid, not the extent — deliberately. The caller's next step is to place
 * a pin, and handing back a 58×63 km bbox invites treating it as the area to monitor, which
 * would average a whole district into one reading.
 */
export async function adminWards(state: string, lga: string): Promise<string[]> {
  if (!state.trim() || !lga.trim()) return [];
  try {
    return (await api.adminWards(state, lga)).names;
  } catch {
    return [];
  }
}

export async function adminCentre(
  state: string,
  lga: string,
  ward?: string,
): Promise<{ lat: number; lon: number; note: string } | null> {
  if (!state.trim() || !lga.trim()) return null;
  try {
    const result = await api.adminExtent(state, lga, ward);
    if (result.centroid_lat == null || result.centroid_lon == null) return null;
    return { lat: result.centroid_lat, lon: result.centroid_lon, note: result.note };
  } catch {
    return null;
  }
}
